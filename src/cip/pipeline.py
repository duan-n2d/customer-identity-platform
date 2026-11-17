"""The daily pipeline: land -> check contracts -> stage -> match -> assign stable IDs -> monitor.

Design notes
------------
* **Full recompute, stable IDs.** Each run re-matches every record. That is simple and correct,
  and DuckDB handles millions of records. What must stay stable is the *person ID*, not the
  cluster, so IDs are assigned by :mod:`cip.resolve.ids` after clustering.
* **Train weekly, score daily.** The Splink model is trained on day 0 and reused; retraining
  daily would let the model drift with the data it is supposed to judge.
* **A bad delivery never reaches matching.** A source that breaks its contract is quarantined,
  and the run continues with the data that is trustworthy.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from cip import privacy
from cip.config import Settings
from cip.quality import load_contracts
from cip.quality.monitors import MonitorResult, run_monitors, write_alerts
from cip.resolve.engine import MatchConfig, MatchResult, run_matching
from cip.resolve.features import build_staging
from cip.resolve.ids import IdState, assign_ids
from cip.warehouse import Warehouse


@dataclass
class DayResult:
    day: int
    records: int
    clusters: int
    active_ids: int
    findings_failed: list[str]
    quarantined_sources: list[str]
    id_events: dict[str, int]
    stability: dict
    monitors: list[MonitorResult]
    match: MatchResult
    seconds: float


def load_state(wh: Warehouse) -> IdState:
    if not wh.table_exists("resolve", "person_map"):
        return IdState()
    registry = wh.df("SELECT * FROM resolve.person_registry")
    registry["merged_into"] = (
        registry["merged_into"].astype(object).where(registry["merged_into"].notna(), None)
    )
    return IdState(wh.df("SELECT record_key, person_id FROM resolve.person_map"), registry)


def _replace(wh: Warehouse, schema: str, table: str, frame: pd.DataFrame) -> None:
    wh.con.execute(f"DROP TABLE IF EXISTS {schema}.{table}")
    wh.con.register("_f", frame)
    wh.con.execute(f"CREATE TABLE {schema}.{table} AS SELECT * FROM _f")
    wh.con.unregister("_f")


def run_day(
    settings: Settings,
    day: int,
    mc: MatchConfig | None = None,
    contracts_dir: Path = Path("contracts"),
) -> DayResult:
    from cip.evaluate import id_stability

    mc = mc or MatchConfig.precision_first()
    started = time.perf_counter()
    model_path = settings.data_dir / "model" / "splink_model.json"
    with Warehouse(settings) as wh:
        findings = wh.land_day(
            day,
            load_contracts(contracts_dir),
            delivery_filter=lambda src, df: privacy.filter_delivery(src, df, wh, settings),
        )
        staged = privacy.apply_suppression(wh, build_staging(wh), settings)
        match = run_matching(staged, settings, mc, model_path, retrain=not model_path.exists())

        prev = load_state(wh)
        clusters = pd.DataFrame(
            {"record_key": match.labels.index, "cluster_label": match.labels.to_numpy()}
        )
        state, events = assign_ids(clusters, prev, day)
        stability = id_stability(prev.person_map, state.person_map, state.registry)

        prev_hubs = (
            int(wh.con.execute("SELECT count(*) FROM resolve.hub_identifiers").fetchall()[0][0])
            if wh.table_exists("resolve", "hub_identifiers")
            else None
        )
        _replace(wh, "resolve", "person_map", state.person_map)
        _replace(wh, "resolve", "person_registry", state.registry)
        _replace(wh, "resolve", "hub_identifiers", match.hubs)
        _replace(
            wh,
            "resolve",
            "shared_identifiers",
            pd.DataFrame(
                [("phone", p) for p in sorted(match.shared_phones)]
                + [("email", e) for e in sorted(match.shared_emails)],
                columns=["identifier_type", "identifier"],
            ),
        )
        _replace(wh, "resolve", "record_clusters", clusters)
        wh.append_frame("resolve", "person_id_events", events)
        if len(match.rejected):
            wh.append_frame("resolve", "rejected_edges", match.rejected.assign(run_day=day))

        active_ids = int((state.registry["status"] == "active").sum())
        monitors = run_monitors(
            day,
            labels=match.labels,
            max_cluster_size=mc.max_cluster_size,
            n_hubs=len(match.hubs),
            prev_n_hubs=prev_hubs,
            events=events,
            n_active_ids=active_ids,
            stability=stability if day > 0 else None,
            rejected=match.rejected,
        )
        wh.append_frame("quality", "monitor_results", pd.DataFrame([m.__dict__ for m in monitors]))
        write_alerts(monitors, settings.data_dir / "alerts.jsonl")

    return DayResult(
        day=day,
        records=len(staged),
        clusters=match.labels.nunique(),
        active_ids=active_ids,
        findings_failed=[
            f"{f.source}:{f.check}" for f in findings if f.severity == "error" and not f.passed
        ],
        quarantined_sources=sorted(
            {f.source for f in findings if f.severity == "error" and not f.passed}
        ),
        id_events=events["event"].value_counts().to_dict() if len(events) else {},
        stability=stability,
        monitors=monitors,
        match=match,
        seconds=round(time.perf_counter() - started, 2),
    )


def run_all(settings: Settings, days: int, mc: MatchConfig | None = None) -> list[DayResult]:
    return [run_day(settings, d, mc) for d in range(days + 1)]
