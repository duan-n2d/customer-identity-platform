from pathlib import Path

import pandas as pd
import pytest

from cip.quality import check_delivery, has_errors, load_contract

CRM = load_contract(Path("contracts/crm.yml"))


def good_crm(n: int = 40, day: int = 0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "contact_id": [f"CR{i:04d}" for i in range(n)],
            "full_name": [f"Nguyen Van {i}" for i in range(n)],
            "email": [f"a{i}@x.com" if i % 3 == 0 else None for i in range(n)],
            "phone": ["0903854127" for _ in range(n)],
            "dob": ["1990-01-01" for _ in range(n)],
            "updated_date": ["2026-06-01" for _ in range(n)],
            "ingested_at": [f"2026-06-{1 + day:02d}T02:00:00" for _ in range(n)],
        }
    )


def failed(findings, check):
    return [f for f in findings if f.check == check and not f.passed]


def test_a_clean_delivery_has_no_errors():
    assert not has_errors(check_delivery(CRM, good_crm(), day=0))


def test_a_missing_column_is_an_error_and_a_rename_is_called_out():
    df = good_crm().rename(columns={"phone": "phone_number"})
    findings = check_delivery(CRM, df, day=1)
    assert has_errors(findings)
    assert failed(findings, "schema.required_columns")
    unexpected = failed(findings, "schema.unexpected_columns")
    assert "possible rename" in unexpected[0].detail
    assert "phone" in unexpected[0].detail and "phone_number" in unexpected[0].detail


def test_an_extra_column_alone_only_warns():
    df = good_crm().assign(loyalty_tier="gold")
    findings = check_delivery(CRM, df, day=0)
    assert failed(findings, "schema.unexpected_columns")
    assert not has_errors(findings)


def test_duplicate_or_null_primary_keys_are_errors():
    dup = good_crm()
    dup.loc[1, "contact_id"] = dup.loc[0, "contact_id"]
    assert failed(check_delivery(CRM, dup, day=0), "primary_key.unique_in_delivery")
    null = good_crm()
    null.loc[2, "contact_id"] = None
    assert failed(check_delivery(CRM, null, day=0), "primary_key.not_null")


def test_a_non_nullable_column_with_a_null_is_an_error():
    df = good_crm()
    df.loc[0, "full_name"] = None
    assert has_errors(check_delivery(CRM, df, day=0))


def test_a_null_rate_over_the_contract_only_warns():
    df = good_crm()
    df["phone"] = None  # contract allows 20% nulls on phone
    findings = check_delivery(CRM, df, day=0)
    assert failed(findings, "nulls.phone")
    assert not has_errors(findings)


def test_unparseable_dates_are_errors():
    df = good_crm()
    df.loc[:5, "updated_date"] = "not-a-date"
    assert failed(check_delivery(CRM, df, day=0), "types.updated_date")


def test_a_stale_delivery_fails_freshness():
    df = good_crm(day=0)  # ingested_at says June 1st but the batch is June 6th
    assert failed(check_delivery(CRM, df, day=5), "freshness")


def test_a_volume_collapse_warns_on_incremental_days_only():
    findings = check_delivery(CRM, good_crm(3, day=3), day=3, history_counts=[100, 120, 90])
    assert failed(findings, "volume")
    assert not has_errors(findings)
    # day 0/1 is the backlog or the first increment: nothing to compare with
    assert not failed(check_delivery(CRM, good_crm(3), day=0, history_counts=[100]), "volume")


def test_redelivered_keys_are_reported_so_the_load_can_stay_idempotent():
    df = good_crm(10)
    findings = check_delivery(CRM, df, day=0, loaded_keys=set(df["contact_id"][:4]))
    redelivered = failed(findings, "primary_key.redelivery")
    assert redelivered and redelivered[0].observed == "4"


@pytest.mark.parametrize("source", ["pos", "ecom", "crm", "loyalty"])
def test_every_shipped_contract_loads_and_declares_its_key(source):
    contract = load_contract(Path(f"contracts/{source}.yml"))
    assert contract.primary_key in contract.columns
    assert not contract.columns[contract.primary_key].nullable
