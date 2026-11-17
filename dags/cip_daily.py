"""Daily customer-identity run: land + resolve -> quality gate -> dbt -> Delta export.

Design choices
--------------
* ``max_active_runs=1``: the warehouse is a single DuckDB file, which allows one writer. Runs
  (and backfills) are therefore serialised.
* The **quality gate** sits between matching and publishing. If a monitor is at ``fail`` (for
  example a giant cluster from a new store phone), nothing is built or exported for that day,
  so a bad identity graph never reaches consumers. The run fails loudly instead.
* Heavy imports happen inside tasks, so parsing the DAG file stays fast.
* ``day`` is derived from the data interval, so reruns and backfills of a date are idempotent
  (landing is idempotent per source and day; matching is a deterministic full recompute).

Config (environment): ``CIP_DATA_DIR`` (default ``data``), ``CIP_DAY_ZERO`` (ISO date of day 0
of the landing zone, default ``2026-06-01``).
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta

try:  # Airflow 3
    from airflow.sdk import dag, task
except ImportError:  # Airflow 2.x
    from airflow.decorators import dag, task

from airflow.exceptions import AirflowFailException

DAY_ZERO = os.environ.get("CIP_DAY_ZERO", "2026-06-01")


def _settings():
    from pathlib import Path

    from cip.config import Settings

    return Settings(data_dir=Path(os.environ.get("CIP_DATA_DIR", "data")))


@dag(
    dag_id="cip_daily",
    schedule="@daily",
    start_date=datetime(2026, 6, 1),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 2, "retry_delay": timedelta(minutes=5)},
    tags=["identity", "customer-360"],
    doc_md=__doc__,
)
def cip_daily():
    @task
    def land_and_resolve(data_interval_start=None) -> dict:
        """Land the day's files under their contracts, match, assign stable IDs, run monitors."""
        from cip.pipeline import run_day
        from cip.resolve.engine import MatchConfig

        day = (data_interval_start.date() - date.fromisoformat(DAY_ZERO)).days
        if day < 0:
            raise AirflowFailException(f"interval {data_interval_start} is before day zero")
        r = run_day(_settings(), day, MatchConfig.precision_first())
        return {
            "day": day,
            "records": r.records,
            "people": r.active_ids,
            "quarantined_sources": r.quarantined_sources,
            "monitors": {m.metric: m.status for m in r.monitors},
        }

    @task
    def quality_gate(result: dict) -> dict:
        """Stop before publishing if any monitor is at ``fail``."""
        failed = [k for k, v in result["monitors"].items() if v == "fail"]
        if failed:
            raise AirflowFailException(f"monitors at fail, not publishing: {failed}")
        return result

    @task
    def dbt_build(result: dict) -> None:
        """Survivorship, marts and the 32 dbt checks."""
        from cip import dbt_build as dbt

        if not dbt.build(_settings()):
            raise AirflowFailException("dbt build failed (models or tests)")

    @task
    def export_delta() -> dict:
        """Publish the PII-free gold tables as Delta and verify the row counts."""
        from cip import export_delta as ex

        settings = _settings()
        counts = ex.export(settings)
        mismatched = {k: v for k, v in ex.verify(settings).items() if v[0] != v[1]}
        if mismatched:
            raise AirflowFailException(f"delta row counts differ from the warehouse: {mismatched}")
        return counts

    gated = quality_gate(land_and_resolve())
    dbt_build(gated) >> export_delta()


cip_daily()
