"""Build ``stg.records``: one normalised row per source record, ready for matching.

All sources are projected onto one shape. Normalisation never invents evidence: an identifier
that fails validation becomes NULL (see :mod:`cip.normalize`).
"""

from __future__ import annotations

from functools import lru_cache

import pandas as pd

from cip.normalize import name_key, name_tokens, normalize_dob, normalize_email, normalize_phone
from cip.warehouse import Warehouse

# source -> (primary key, name, email, phone, dob, event date) column names; None = not collected
_PROJECTION: dict[str, tuple[str, str, str | None, str | None, str | None, str]] = {
    "pos": ("txn_id", "customer_name", None, "customer_phone", None, "txn_date"),
    "ecom": ("account_id", "full_name", "email", "phone", None, "created_date"),
    "crm": ("contact_id", "full_name", "email", "phone", "dob", "updated_date"),
    "loyalty": ("member_no", "full_name", None, "phone", "dob", "enrolled_date"),
}

_phone = lru_cache(maxsize=None)(lambda raw: normalize_phone(raw))  # noqa: PLW0108


def _tok_min_max(raw: str | None) -> tuple[str | None, str | None]:
    tokens = name_tokens(raw)
    if len(tokens) < 2:
        return None, None
    return min(tokens), max(tokens)


def project_source(source: str, raw: pd.DataFrame) -> pd.DataFrame:
    pk, name, email, phone, dob, event = _PROJECTION[source]

    def col(c: str | None) -> pd.Series:
        return (
            raw[c]
            if c is not None and c in raw.columns
            else pd.Series([None] * len(raw), index=raw.index)
        )

    out = pd.DataFrame(
        {
            "record_key": source + ":" + raw[pk].astype(str),
            "source": source,
            "source_record_id": raw[pk].astype(str),
            "event_date": pd.to_datetime(raw[event]).dt.date,
            "ingest_day": raw["_ingest_day"].astype(int),
            "name_raw": col(name),
            "email_raw": col(email),
            "phone_raw": col(phone),
            "dob_raw": col(dob),
        }
    )
    return out


def normalise(records: pd.DataFrame) -> pd.DataFrame:
    r = records.copy()
    phones = r["phone_raw"].map(lambda x: _phone(x) if isinstance(x, str) else _phone(None))
    r["phone_e164"] = phones.map(lambda p: p.e164)
    r["phone_reason"] = phones.map(lambda p: p.reason)
    r["email_norm"] = r["email_raw"].map(
        lambda x: normalize_email(x) if isinstance(x, str) else None
    )
    r["dob_norm"] = r["dob_raw"].map(lambda x: normalize_dob(x) if isinstance(x, str) else None)
    r["name_key"] = r["name_raw"].map(lambda x: name_key(x) if isinstance(x, str) else None)
    toks = r["name_raw"].map(lambda x: _tok_min_max(x) if isinstance(x, str) else (None, None))
    r["tok_a"] = toks.map(lambda t: t[0])
    r["tok_z"] = toks.map(lambda t: t[1])
    return r


def build_staging(wh: Warehouse) -> pd.DataFrame:
    """Rebuild ``stg.records`` from every raw table and return it."""
    parts = []
    for source in _PROJECTION:
        if wh.table_exists("raw", source):
            parts.append(project_source(source, wh.df(f"SELECT * FROM raw.{source}")))
    records = normalise(pd.concat(parts, ignore_index=True)) if parts else pd.DataFrame()
    wh.con.execute("DROP TABLE IF EXISTS stg.records")
    wh.con.register("_stg", records)
    wh.con.execute("CREATE TABLE stg.records AS SELECT * FROM _stg")
    wh.con.unregister("_stg")
    return records
