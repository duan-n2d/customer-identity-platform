"""DuckDB warehouse: schemas, contract-checked landing, and quarantine.

Schemas
-------
``raw``       what the sources delivered, append-only, one table per source
``stg``       normalised records ready for matching (rebuilt from ``raw``)
``resolve``   matching outputs: clusters, person map/registry/events, hubs
``quality``   contract findings, quarantined deliveries, monitor results
``gold``      built by dbt (customer_360 and friends)
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import duckdb
import pandas as pd

from cip.config import SOURCES, Settings
from cip.generator.landing import landing_files
from cip.quality import Contract, Finding, check_delivery, has_errors, load_contracts, to_frame

SCHEMAS = ("raw", "stg", "resolve", "quality", "privacy", "gold")


class Warehouse:
    def __init__(self, settings: Settings, *, read_only: bool = False):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(settings.db_path), read_only=read_only)
        if not read_only:
            for schema in SCHEMAS:
                self.con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> Warehouse:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ helpers
    def table_exists(self, schema: str, table: str) -> bool:
        # fetchall, not fetchone: DuckDB 2.x keeps the connection busy until a result is drained
        rows = self.con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema=? AND table_name=?",
            [schema, table],
        ).fetchall()
        return rows[0][0] > 0

    def df(self, sql: str, params: list | None = None) -> pd.DataFrame:
        return self.con.execute(sql, params or []).df()

    def append_frame(self, schema: str, table: str, frame: pd.DataFrame) -> None:
        if frame.empty:
            return
        self.con.register("_incoming", frame)
        if self.table_exists(schema, table):
            self.con.execute(f"INSERT INTO {schema}.{table} SELECT * FROM _incoming")
        else:
            self.con.execute(f"CREATE TABLE {schema}.{table} AS SELECT * FROM _incoming")
        self.con.unregister("_incoming")

    # ------------------------------------------------------------------ landing
    def land_day(
        self,
        day: int,
        contracts: dict[str, Contract] | None = None,
        delivery_filter: Callable[[str, pd.DataFrame], tuple[pd.DataFrame, int]] | None = None,
    ) -> list[Finding]:
        """Load one ingest day, enforcing each source's contract. Returns every finding.

        ``delivery_filter(source, frame)`` may drop rows before they are stored (used for the
        erasure suppression list, so an erased person's PII never re-enters ``raw``).
        """
        contracts = contracts or load_contracts(Path("contracts"))
        findings: list[Finding] = []
        for source in SOURCES:
            contract = contracts[source]
            files = landing_files(self.settings, source, day)
            if not files:
                findings.append(
                    Finding(
                        day,
                        source,
                        "delivery.present",
                        "warn",
                        day != 0,
                        "no file",
                        "a file",
                        "" if day != 0 else "the initial load must contain every source",
                    )
                )
                continue
            delivery = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
            history = self._increment_counts(source) if self.table_exists("raw", source) else []
            loaded = (
                self._loaded_keys(source, contract) if self.table_exists("raw", source) else None
            )
            found = check_delivery(
                contract, delivery, day=day, history_counts=history, loaded_keys=loaded
            )
            findings.extend(found)
            if has_errors(found):
                self._quarantine(source, day, files, delivery, found)
                continue
            if delivery_filter is not None:
                delivery, dropped = delivery_filter(source, delivery)
                if dropped:
                    findings.append(
                        Finding(
                            day,
                            source,
                            "privacy.suppressed_rows",
                            "warn",
                            False,
                            str(dropped),
                            "0",
                            "rows matching the erasure suppression list were not stored",
                        )
                    )
            self._load(source, contract, delivery, day)
        self.append_frame("quality", "dq_results", to_frame(findings))
        return findings

    def _increment_counts(self, source: str) -> list[int]:
        rows = self.con.execute(
            f"SELECT _ingest_day, count(*) FROM raw.{source} WHERE _ingest_day >= 1 GROUP BY 1"
        ).fetchall()
        return [int(n) for _, n in rows]

    def _loaded_keys(self, source: str, contract: Contract) -> set[str]:
        pk = contract.primary_key
        return {r[0] for r in self.con.execute(f"SELECT {pk} FROM raw.{source}").fetchall()}

    def _load(self, source: str, contract: Contract, delivery: pd.DataFrame, day: int) -> None:
        cols = list(contract.columns)
        frame = delivery[cols].copy()
        frame["_ingest_day"] = day
        self.con.register("_delivery", frame)
        pk = contract.primary_key
        if not self.table_exists("raw", source):
            self.con.execute(f"CREATE TABLE raw.{source} AS SELECT * FROM _delivery")
        else:  # idempotent: a key delivered twice is stored once
            self.con.execute(
                f"INSERT INTO raw.{source} SELECT * FROM _delivery "
                f"WHERE {pk} NOT IN (SELECT {pk} FROM raw.{source})"
            )
        self.con.unregister("_delivery")

    def _quarantine(
        self, source: str, day: int, files: list[Path], delivery: pd.DataFrame, found: list[Finding]
    ) -> None:
        target = self.settings.data_dir / "quarantine" / source / f"ingest_day={day}"
        target.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copy(f, target / f.name)
        reasons = "; ".join(
            f"{x.check}: {x.observed}" for x in found if x.severity == "error" and not x.passed
        )
        self.append_frame(
            "quality",
            "quarantine",
            pd.DataFrame(
                [{"run_day": day, "source": source, "rows": len(delivery), "reasons": reasons}]
            ),
        )
