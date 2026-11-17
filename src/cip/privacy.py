"""Right-to-erasure and PII protection.

Erasing a person in an identity graph is harder than deleting a row, for three reasons this module
addresses explicitly:

1. **Their records are spread across every source and every layer** (raw, staging, matching state,
   the gold tables). All of them must go, in one operation.
2. **The sources keep sending.** The same customer buys again tomorrow and a new record arrives
   with the same phone and name. Without a *suppression list* the person is resurrected on the
   next run. The list stores only keyed HMAC tokens, never the identifiers themselves, so it is
   not a new place where the PII lives.
3. **Proof.** :func:`verify_erasure` checks every layer for residue, because "we ran the delete"
   is not the same as "it is gone".

Suppression is keyed on ``email`` or on ``phone + name``. Not on phone alone: a household shares a
number, and erasing one member must not silently drop the others' future records.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from cip.config import Settings
from cip.normalize import token
from cip.resolve.features import _PROJECTION, normalise, project_source
from cip.warehouse import Warehouse

DEV_KEY = "dev-only-key-set-CIP_PII_KEY-in-production"
GOLD_TABLES = ("dim_customer", "bridge_person_record")


def _key(settings: Settings) -> str:
    return settings.pii_key or DEV_KEY


def record_tokens(records: pd.DataFrame, key: str) -> pd.DataFrame:
    """Suppression tokens for staged records: ``email`` and ``phone_name`` kinds."""
    rows = []
    for r in records[["record_key", "email_norm", "phone_e164", "name_key"]].itertuples(
        index=False
    ):
        if isinstance(r.email_norm, str):
            rows.append((r.record_key, "email", token(r.email_norm, key)))
        if isinstance(r.phone_e164, str) and isinstance(r.name_key, str):
            rows.append((r.record_key, "phone_name", token(f"{r.phone_e164}|{r.name_key}", key)))
    return pd.DataFrame(rows, columns=["record_key", "kind", "token"])


def apply_suppression(wh: Warehouse, staged: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    """Drop staged records that match the suppression list (people who asked to be erased)."""
    if not wh.table_exists("privacy", "suppression") or staged.empty:
        return staged
    tokens = record_tokens(staged, _key(settings))
    blocked = set(wh.df("SELECT token FROM privacy.suppression")["token"])
    drop = set(tokens.loc[tokens["token"].isin(blocked), "record_key"])
    return staged[~staged["record_key"].isin(drop)].reset_index(drop=True) if drop else staged


def filter_delivery(
    source: str, delivery: pd.DataFrame, wh: Warehouse, settings: Settings
) -> tuple[pd.DataFrame, int]:
    """Drop rows of a delivery that match the suppression list *before* they are stored."""
    if delivery.empty or not wh.table_exists("privacy", "suppression"):
        return delivery, 0
    staged = normalise(project_source(source, delivery.assign(_ingest_day=0)))
    tokens = record_tokens(staged, _key(settings))
    blocked = set(wh.df("SELECT token FROM privacy.suppression")["token"])
    drop = set(tokens.loc[tokens["token"].isin(blocked), "record_key"])
    if not drop:
        return delivery, 0
    keep = ~staged["record_key"].isin(drop).to_numpy()
    return delivery.loc[keep].reset_index(drop=True), int((~keep).sum())


def _ids_for(wh: Warehouse, person_id: str) -> list[str]:
    """The person's ID plus every ID that was merged into it (aliases)."""
    reg = wh.df("SELECT person_id, merged_into FROM resolve.person_registry")
    ids, frontier = {person_id}, {person_id}
    while frontier:
        frontier = set(reg.loc[reg["merged_into"].isin(frontier), "person_id"]) - ids
        ids |= frontier
    return sorted(ids)


def erase_person(settings: Settings, person_id: str) -> dict:
    """Erase one person from every layer and add them to the suppression list."""
    with Warehouse(settings) as wh:
        ids = _ids_for(wh, person_id)
        placeholders = ",".join("?" for _ in ids)
        keys = wh.df(
            f"SELECT record_key FROM resolve.person_map WHERE person_id IN ({placeholders})", ids
        )["record_key"].tolist()
        if not keys:
            return {"person_id": person_id, "erased_records": 0, "found": False}
        marks = ",".join("?" for _ in keys)
        staged = wh.df(f"SELECT * FROM stg.records WHERE record_key IN ({marks})", keys)

        tokens = record_tokens(staged, _key(settings)).drop(columns="record_key").drop_duplicates()
        tokens["created_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        wh.append_frame("privacy", "suppression", tokens)

        for source, group in staged.groupby("source"):
            pk = _PROJECTION[source][0]
            src_ids = group["source_record_id"].tolist()
            m = ",".join("?" for _ in src_ids)
            wh.con.execute(f"DELETE FROM raw.{source} WHERE {pk} IN ({m})", src_ids)
        wh.con.execute(f"DELETE FROM stg.records WHERE record_key IN ({marks})", keys)
        wh.con.execute(f"DELETE FROM resolve.person_map WHERE record_key IN ({marks})", keys)
        wh.con.execute(f"DELETE FROM resolve.record_clusters WHERE record_key IN ({marks})", keys)
        wh.con.execute(
            f"UPDATE resolve.person_registry SET status = 'erased', merged_into = NULL "
            f"WHERE person_id IN ({placeholders})",
            ids,
        )
        for table in GOLD_TABLES:
            if wh.table_exists("gold", table):
                wh.con.execute(f"DELETE FROM gold.{table} WHERE person_id IN ({placeholders})", ids)
        wh.append_frame(
            "privacy",
            "erasure_log",
            pd.DataFrame(
                [
                    {
                        "subject_token": token(person_id, _key(settings)),
                        "erased_records": len(keys),
                        "erased_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    }
                ]
            ),
        )
        return {
            "person_id": person_id,
            "erased_records": len(keys),
            "found": True,
            "record_keys": keys,
            "suppression_tokens": len(tokens),
        }


def verify_erasure(settings: Settings, person_id: str, record_keys: list[str]) -> dict[str, int]:
    """Count residue of an erased person in every layer. Everything must be zero."""
    with Warehouse(settings, read_only=True) as wh:
        ids = _ids_for(wh, person_id)
        residue: dict[str, int] = {}
        for source in _PROJECTION:
            pk = _PROJECTION[source][0]
            plain = [k.split(":", 1)[1] for k in record_keys if k.startswith(f"{source}:")]
            if plain and wh.table_exists("raw", source):
                m = ",".join("?" for _ in plain)
                residue[f"raw.{source}"] = wh.con.execute(
                    f"SELECT count(*) FROM raw.{source} WHERE {pk} IN ({m})", plain
                ).fetchall()[0][0]
        marks = ",".join("?" for _ in record_keys)
        for table in ("stg.records", "resolve.person_map", "resolve.record_clusters"):
            schema, name = table.split(".")
            if wh.table_exists(schema, name):
                residue[table] = wh.con.execute(
                    f"SELECT count(*) FROM {table} WHERE record_key IN ({marks})", record_keys
                ).fetchall()[0][0]
        placeholders = ",".join("?" for _ in ids)
        for table in GOLD_TABLES:
            if wh.table_exists("gold", table):
                residue[f"gold.{table}"] = wh.con.execute(
                    f"SELECT count(*) FROM gold.{table} WHERE person_id IN ({placeholders})", ids
                ).fetchall()[0][0]
        return residue
