"""Hub identifiers: values shared by so many unrelated records that they stop being evidence.

A store's own phone number, typed by a cashier for customers who did not give one, can sit on
thousands of different people. If it were used for matching, one store number would glue those
thousands of people into a single "customer". We detect such values from the data itself
(many records AND many different names) and exclude them from matching.

Household phones are deliberately *not* hubs: a handful of records and a handful of names. They
are real shared identifiers that matching must handle with the name evidence instead.
"""

from __future__ import annotations

import pandas as pd


def detect_hubs(
    records: pd.DataFrame, *, min_records: int, min_distinct_names: int
) -> pd.DataFrame:
    """One row per hub: identifier_type, identifier, n_records, n_distinct_names."""
    frames = []
    for kind, col in (("phone", "phone_e164"), ("email", "email_norm")):
        valid = records.dropna(subset=[col])
        if valid.empty:
            continue
        grouped = valid.groupby(col).agg(
            n_records=("record_key", "count"), n_distinct_names=("name_key", "nunique")
        )
        hubs = grouped[
            (grouped["n_records"] >= min_records)
            & (grouped["n_distinct_names"] >= min_distinct_names)
        ].reset_index()
        hubs.insert(0, "identifier_type", kind)
        frames.append(hubs.rename(columns={col: "identifier"}))
    if not frames:
        return pd.DataFrame(
            columns=["identifier_type", "identifier", "n_records", "n_distinct_names"]
        )
    return pd.concat(frames, ignore_index=True)


def apply_hubs(records: pd.DataFrame, hubs: pd.DataFrame) -> pd.DataFrame:
    """Add ``phone_match`` and ``email_match``: the identifiers matching may use (hubs removed)."""
    r = records.copy()
    hub_phones = set(hubs.loc[hubs["identifier_type"] == "phone", "identifier"])
    hub_emails = set(hubs.loc[hubs["identifier_type"] == "email", "identifier"])
    r["phone_is_hub"] = r["phone_e164"].isin(hub_phones)
    r["email_is_hub"] = r["email_norm"].isin(hub_emails)
    r["phone_match"] = r["phone_e164"].where(~r["phone_is_hub"])
    r["email_match"] = r["email_norm"].where(~r["email_is_hub"])
    return r
