"""Export the gold layer to Delta Lake tables, ready to register in Unity Catalog on Databricks.

Privacy by default: the **analytics** export contains only tables with no direct identifiers.
The tables that hold phone, email, name and date of birth (``dim_customer``, the lineage bridge)
are exported only when the caller asks for them with ``include_pii=True``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
from deltalake import DeltaTable, write_deltalake

from cip.config import Settings
from cip.warehouse import Warehouse

ANALYTICS_TABLES = {
    "dim_customer_public": ("gold", None),
    "fct_identity_coverage": ("gold", None),
    "person_id_events": ("resolve", None),
    "person_registry": ("resolve", None),
}
PII_TABLES = {
    "dim_customer": ("gold", None),
    "bridge_person_record": ("gold", ["source"]),
}


def export(settings: Settings, *, include_pii: bool = False) -> dict[str, int]:
    """Write each table as a Delta table under ``data/delta``; returns {table: rows}."""
    tables = {**ANALYTICS_TABLES, **(PII_TABLES if include_pii else {})}
    out = settings.delta_dir
    out.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    with Warehouse(settings, read_only=True) as wh:
        for name, (schema, partition_by) in tables.items():
            if not wh.table_exists(schema, name):
                continue
            frame = wh.df(f"SELECT * FROM {schema}.{name}")
            write_deltalake(
                out / name,
                pa.Table.from_pandas(frame, preserve_index=False),
                mode="overwrite",
                partition_by=partition_by,
            )
            counts[name] = len(frame)
    (out / "_manifest.json").write_text(
        json.dumps({"include_pii": include_pii, "rows": counts}, indent=2), encoding="utf-8"
    )
    return counts


def verify(settings: Settings) -> dict[str, tuple[int, int]]:
    """Read every exported table back and compare its row count with the warehouse."""
    manifest = json.loads((settings.delta_dir / "_manifest.json").read_text(encoding="utf-8"))
    result: dict[str, tuple[int, int]] = {}
    for name, rows in manifest["rows"].items():
        table = DeltaTable(str(Path(settings.delta_dir) / name))
        result[name] = (rows, table.to_pyarrow_table().num_rows)
    return result
