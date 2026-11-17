"""API, Delta export and the CLI, against a small real pipeline + dbt build."""

import pytest
from fastapi.testclient import TestClient

from cip import dbt_build, export_delta
from cip.api.app import create_app
from cip.cli import main
from cip.config import Settings
from cip.generator import WorldConfig, generate, write_landing
from cip.pipeline import run_all
from cip.warehouse import Warehouse

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    cfg = Settings(
        data_dir=tmp_path_factory.mktemp("cip_api"), hub_min_records=8, hub_min_distinct_names=5
    )
    write_landing(generate(WorldConfig(n_persons=600, seed=4, days=1)), cfg)
    run_all(cfg, 1)
    assert dbt_build.build(cfg), "dbt build (models and tests) must pass"
    return cfg, TestClient(create_app(cfg))


def _one(cfg, sql):
    with Warehouse(cfg, read_only=True) as wh:
        return wh.df(sql)


def test_health_counts_the_golden_records(served):
    _, client = served
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["persons"] > 100


def test_a_person_is_returned_without_nan_and_with_their_sources(served):
    cfg, client = served
    pid = _one(cfg, "SELECT person_id FROM gold.dim_customer WHERE n_sources >= 2 LIMIT 1")[
        "person_id"
    ][0]
    body = client.get(f"/v1/persons/{pid}").json()
    assert body["person_id"] == pid and len(body["sources"]) >= 2
    assert all(v == v for v in body.values() if isinstance(v, float))  # no NaN leaks into JSON


def test_unknown_person_is_404_without_echoing_input(served):
    _, client = served
    r = client.get("/v1/persons/P_secret_value")
    assert r.status_code == 404 and "P_secret_value" not in r.text


def test_a_personal_phone_resolves_to_exactly_one_person(served):
    cfg, client = served
    row = _one(
        cfg,
        """
        SELECT r.phone_e164 AS phone, min(pm.person_id) AS person_id
        FROM stg.records r JOIN resolve.person_map pm USING (record_key)
        WHERE r.phone_e164 IS NOT NULL
          AND r.phone_e164 NOT IN (SELECT identifier FROM resolve.hub_identifiers)
          AND r.phone_e164 NOT IN (SELECT identifier FROM resolve.shared_identifiers)
        GROUP BY 1 HAVING count(DISTINCT pm.person_id) = 1 LIMIT 1
        """,
    )
    national = "0" + row["phone"][0][3:]  # the caller may send any common format
    body = client.post("/v1/resolve", json={"phone": national}).json()
    assert body["person_id"] == row["person_id"][0] and body["basis"] == "phone"


def test_a_hub_phone_is_not_resolved_to_anyone(served):
    cfg, client = served
    hub = _one(cfg, "SELECT identifier FROM resolve.hub_identifiers LIMIT 1")["identifier"][0]
    body = client.post("/v1/resolve", json={"phone": hub}).json()
    assert body["person_id"] is None and body["reason"] == "hub_identifier"


def test_a_shared_phone_reports_ambiguity_instead_of_guessing(served):
    cfg, client = served
    shared = _one(
        cfg,
        "SELECT identifier FROM resolve.shared_identifiers WHERE identifier_type='phone' LIMIT 1",
    )
    body = client.post("/v1/resolve", json={"phone": shared["identifier"][0]}).json()
    assert body["person_id"] is None and body["reason"] == "shared_identifier"


def test_resolve_input_validation(served):
    _, client = served
    assert client.post("/v1/resolve", json={}).status_code == 422
    assert client.post("/v1/resolve", json={"phone": "12345"}).status_code == 422
    assert client.post("/v1/resolve", json={"phone": "0903854127"}).json()["reason"] == "not_found"


def test_a_merged_id_redirects_to_the_canonical_one(served):
    cfg, client = served
    ids = _one(cfg, "SELECT person_id FROM gold.dim_customer LIMIT 2")["person_id"].tolist()
    with Warehouse(cfg) as wh:
        wh.con.execute(
            "INSERT INTO resolve.person_registry VALUES ('P_old', 0, 'merged', ?)", [ids[0]]
        )
    r = client.get("/v1/persons/P_old", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == f"/v1/persons/{ids[0]}"


def test_erasure_needs_an_admin_key_and_then_leaves_nothing(served, monkeypatch):
    cfg, client = served
    pid = _one(cfg, "SELECT person_id FROM gold.dim_customer WHERE n_records >= 3 LIMIT 1")[
        "person_id"
    ][0]

    monkeypatch.delenv("CIP_ADMIN_KEY", raising=False)
    assert client.delete(f"/v1/subjects/{pid}").status_code == 503  # disabled until configured
    monkeypatch.setenv("CIP_ADMIN_KEY", "s3cret")
    assert client.delete(f"/v1/subjects/{pid}").status_code == 403
    assert client.delete(f"/v1/subjects/{pid}", headers={"X-Admin-Key": "wrong"}).status_code == 403

    r = client.delete(f"/v1/subjects/{pid}", headers={"X-Admin-Key": "s3cret"})
    assert r.status_code == 200 and r.json()["verified"] is True
    assert client.get(f"/v1/persons/{pid}").status_code == 410
    assert (
        _one(cfg, f"SELECT count(*) AS n FROM gold.dim_customer WHERE person_id = '{pid}'")["n"][0]
        == 0
    )
    assert (
        client.delete("/v1/subjects/P99999999", headers={"X-Admin-Key": "s3cret"}).status_code
        == 404
    )


def test_the_analytics_export_contains_no_direct_identifiers_by_default(served):
    cfg, _ = served
    counts = export_delta.export(cfg)
    assert "dim_customer" not in counts and "bridge_person_record" not in counts
    assert counts["dim_customer_public"] > 0
    assert all(a == b for a, b in export_delta.verify(cfg).values())
    import json

    assert json.loads((cfg.delta_dir / "_manifest.json").read_text())["include_pii"] is False


def test_the_pii_export_is_opt_in_and_partitioned(served):
    cfg, _ = served
    counts = export_delta.export(cfg, include_pii=True)
    assert counts["dim_customer"] > 0
    assert (cfg.delta_dir / "bridge_person_record" / "source=pos").exists()
    assert all(a == b for a, b in export_delta.verify(cfg).values())


def test_the_cli_demo_runs_every_stage_and_exits_zero(tmp_path):
    with pytest.raises(SystemExit) as stop:
        main(["--data-dir", str(tmp_path), "demo", "--persons", "400", "--days", "1"])
    assert stop.value.code == 0
    assert (tmp_path / "delta" / "dim_customer_public").exists()
