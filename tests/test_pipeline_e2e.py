"""End-to-end tests: generate -> land -> match -> stable IDs -> erase. Slow (a few seconds each)."""

import pytest

from cip import privacy
from cip.config import Settings
from cip.evaluate import evaluate_clustering
from cip.generator import WorldConfig, generate, write_landing
from cip.pipeline import load_state, run_all, run_day
from cip.warehouse import Warehouse

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    # hub thresholds scale with data volume: a 700-person world has ~12 records per store number
    cfg = Settings(
        data_dir=tmp_path_factory.mktemp("cip_e2e"), hub_min_records=8, hub_min_distinct_names=5
    )
    data = generate(WorldConfig(n_persons=700, seed=5, days=2))
    write_landing(data, cfg)
    runs = run_all(cfg, 2)
    return cfg, data, runs


def test_every_record_gets_exactly_one_person_id(world):
    cfg, _, runs = world
    with Warehouse(cfg, read_only=True) as wh:
        stg = wh.df("SELECT record_key FROM stg.records")
        pm = wh.df("SELECT record_key, person_id FROM resolve.person_map")
        reg = wh.df("SELECT * FROM resolve.person_registry")
    assert len(stg) == len(pm) == pm["record_key"].nunique()
    assert set(pm["person_id"]) <= set(reg.loc[reg["status"] == "active", "person_id"])
    assert runs[-1].active_ids == pm["person_id"].nunique()


def test_matching_quality_on_a_small_world(world):
    _, data, runs = world
    last = runs[-1]
    m = evaluate_clustering(
        last.match.labels, data.truth_records, data.truth_persons, last.match.records
    )
    assert m["precision"] >= 0.95
    assert m["linkable_recall"] >= 0.85


def test_person_ids_are_stable_across_days(world):
    _, _, runs = world
    for r in runs[1:]:
        assert r.stability["unchanged"] >= 0.99, r.stability
        assert r.stability["alias_resolved"] >= r.stability["unchanged"]


def test_rerunning_a_day_is_idempotent(world):
    cfg, _, runs = world
    with Warehouse(cfg, read_only=True) as wh:
        before = wh.df("SELECT * FROM resolve.person_map ORDER BY record_key")
        n_events = wh.df("SELECT count(*) AS n FROM resolve.person_id_events")["n"][0]
    again = run_day(cfg, 2)
    with Warehouse(cfg, read_only=True) as wh:
        after = wh.df("SELECT * FROM resolve.person_map ORDER BY record_key")
        n_events_after = wh.df("SELECT count(*) AS n FROM resolve.person_id_events")["n"][0]
    assert before.equals(after)
    assert n_events == n_events_after  # no new/merge/split events
    assert again.id_events == {}


def test_the_hubs_and_shared_numbers_are_recorded(world):
    cfg, _, _ = world
    with Warehouse(cfg, read_only=True) as wh:
        assert wh.df("SELECT count(*) AS n FROM resolve.hub_identifiers")["n"][0] > 0
        assert wh.df("SELECT count(*) AS n FROM resolve.shared_identifiers")["n"][0] > 0


def test_a_schema_drift_quarantines_the_feed_and_the_pipeline_continues(tmp_path):
    cfg = Settings(data_dir=tmp_path)
    write_landing(generate(WorldConfig(n_persons=500, seed=9, days=2, drift_day=1)), cfg)
    runs = run_all(cfg, 2)
    drifted = [r for r in runs if "crm" in r.quarantined_sources]
    assert drifted, "the renamed CRM column should be caught on or after the drift day"
    assert all(r.day >= 1 for r in drifted)
    assert all(r.active_ids > 0 for r in runs)  # matching still ran on the trustworthy data
    with Warehouse(cfg, read_only=True) as wh:
        q = wh.df("SELECT * FROM quality.quarantine")
        assert "crm" in set(q["source"])
        assert "schema.required_columns" in " ".join(q["reasons"])
        assert (
            wh.df("SELECT max(_ingest_day) AS d FROM raw.crm")["d"][0] == 0
        )  # nothing loaded after drift
    assert (tmp_path / "quarantine" / "crm").exists()


def test_erasure_removes_a_person_everywhere_and_they_stay_erased(tmp_path):
    cfg = Settings(data_dir=tmp_path)
    data = generate(WorldConfig(n_persons=500, seed=3, days=1))
    write_landing(data, cfg)
    run_all(cfg, 1)

    with Warehouse(cfg, read_only=True) as wh:
        pm = wh.df("SELECT record_key, person_id FROM resolve.person_map")
        sizes = pm.groupby("person_id").size()
        victim = sizes[sizes >= 3].index[0]
        bystanders_before = (
            pm[pm["person_id"] != victim].sort_values("record_key").reset_index(drop=True)
        )

    summary = privacy.erase_person(cfg, victim)
    assert summary["found"] and summary["erased_records"] >= 3
    assert all(v == 0 for v in privacy.verify_erasure(cfg, victim, summary["record_keys"]).values())
    with Warehouse(cfg, read_only=True) as wh:
        assert wh.df("SELECT count(*) AS n FROM privacy.suppression")["n"][0] > 0
        log = wh.df("SELECT * FROM privacy.erasure_log")
        assert victim not in " ".join(
            log.astype(str).to_numpy().ravel()
        )  # the log holds a token, not the ID

    # the sources deliver the same people again (a replay): the erased person must not come back
    with Warehouse(cfg) as wh:
        for source in ("pos", "ecom", "crm", "loyalty"):
            wh.con.execute(f"DELETE FROM raw.{source} WHERE _ingest_day = 1")
    run_day(cfg, 1)
    with Warehouse(cfg, read_only=True) as wh:
        pm_after = wh.df("SELECT record_key, person_id FROM resolve.person_map")
    assert not set(summary["record_keys"]) & set(pm_after["record_key"])
    # nobody else's ID changed because of the erasure
    now = pm_after[pm_after["record_key"].isin(bystanders_before["record_key"])]
    merged = now.merge(bystanders_before, on="record_key", suffixes=("_now", "_before"))
    assert (merged["person_id_now"] == merged["person_id_before"]).mean() >= 0.995
    state = load_state(Warehouse(cfg, read_only=True))
    assert state.registry.set_index("person_id").loc[victim, "status"] == "erased"


def test_a_subject_not_in_the_graph_is_reported_not_found(world):
    cfg, _, _ = world
    assert privacy.erase_person(cfg, "P99999999")["found"] is False
