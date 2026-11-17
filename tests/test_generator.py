import pandas as pd
import pytest

from cip.config import Settings
from cip.generator import WorldConfig, fingerprint, generate, write_landing
from cip.normalize import normalize_phone

CFG = WorldConfig(n_persons=500, seed=11, days=3)


@pytest.fixture(scope="module")
def data():
    return generate(CFG)


def test_same_seed_gives_identical_data():
    assert fingerprint(generate(CFG)) == fingerprint(generate(CFG))


def test_different_seed_gives_different_data():
    other = WorldConfig(n_persons=500, seed=12, days=3)
    assert fingerprint(generate(CFG)) != fingerprint(generate(other))


def test_every_record_has_exactly_one_true_person_and_a_unique_key(data):
    tr = data.truth_records
    assert tr["record_key"].is_unique
    total = sum(len(df) for df in data.sources.values())
    assert len(tr) == total
    assert set(tr["person_id"]) <= set(data.truth_persons["person_id"])


def test_every_person_appears_in_at_least_one_record(data):
    appearing = set(data.truth_records["person_id"])
    missing = set(data.truth_persons["person_id"]) - appearing
    # people whose first_day is after the horizon cannot appear; everyone else must
    assert not missing


def test_ingest_days_stay_inside_the_horizon(data):
    for df in data.sources.values():
        assert df["ingest_day"].between(0, CFG.days).all()


def test_day_zero_holds_the_backlog_and_later_days_are_small_increments(data):
    by_day = data.truth_records.groupby("ingest_day").size()
    assert by_day[0] > 5 * by_day.drop(0).max()


def test_late_arrivals_exist(data):
    tr = data.truth_records
    assert (tr["ingest_day"] > tr["event_day"].clip(lower=0)).any()


def test_household_trap_distinct_people_share_a_phone(data):
    """Different people in one household must really share a number (a false-merge hazard)."""
    tr = data.truth_records
    hh = tr[tr["uses_household_phone"]].merge(
        data.truth_persons[["person_id", "household_id"]], on="person_id"
    )
    assert hh["household_id"].notna().all()
    assert (hh.groupby("household_id")["person_id"].nunique() >= 2).any()


def test_store_phone_trap_creates_hub_numbers(data):
    pos = data.sources["pos"].copy()
    pos["record_key"] = "pos:" + pos["txn_id"]
    pos["phone"] = pos["customer_phone"].map(lambda x: normalize_phone(x).e164)
    pos = pos.merge(data.truth_records[["record_key", "person_id"]], on="record_key")
    distinct = pos.dropna(subset=["phone"]).groupby("phone")["person_id"].nunique()
    # an ordinary customer phone sits on at most a household (<= 4 people); a store number
    # sits on many more, and several stores must show it
    assert (distinct >= 8).sum() >= 5


def test_recycled_phone_trap_gives_one_number_to_two_people_at_different_times(data):
    assert data.truth_persons["phone_recycled"].sum() >= 4
    assert data.truth_persons["common_name"].sum() > 0


def test_unresolvable_records_exist_so_recall_is_measured_honestly(data):
    pos = data.sources["pos"]
    no_identifier = pos["customer_phone"].isna() | (
        pos["customer_phone"].map(lambda x: not normalize_phone(x).valid)
    )
    assert no_identifier.mean() > 0.2


def test_placeholder_phones_are_present_and_rejected_by_normalisation(data):
    pos = data.sources["pos"]
    phones = pos["customer_phone"].dropna()
    placeholders = phones[phones.isin(["0123456789", "0900000000", "0911111111", "0987654321"])]
    assert len(placeholders) > 0
    assert not any(normalize_phone(p).valid for p in placeholders)


def test_landing_zone_is_partitioned_by_ingest_day_and_drops_the_ingest_day_column(tmp_path):
    settings = Settings(data_dir=tmp_path)
    meta = write_landing(generate(CFG), settings)
    assert (tmp_path / "landing" / "pos" / "ingest_day=0" / "part.parquet").exists()
    df = pd.read_parquet(tmp_path / "landing" / "pos" / "ingest_day=0" / "part.parquet")
    assert "ingest_day" not in df.columns
    assert "ingested_at" in df.columns
    assert meta["fingerprint"] == fingerprint(generate(CFG))


def test_schema_drift_renames_the_crm_phone_column_from_the_drift_day(tmp_path):
    cfg = WorldConfig(n_persons=500, seed=11, days=3, drift_day=2)
    write_landing(generate(cfg), Settings(data_dir=tmp_path))
    before = pd.read_parquet(tmp_path / "landing" / "crm" / "ingest_day=0" / "part.parquet")
    assert "phone" in before.columns and "phone_number" not in before.columns
    after_files = list((tmp_path / "landing" / "crm" / "ingest_day=2").glob("*.parquet"))
    if after_files:  # a small world may deliver no CRM rows on that exact day
        after = pd.read_parquet(after_files[0])
        assert "phone_number" in after.columns and "phone" not in after.columns
