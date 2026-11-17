"""Runtime monitors on the *outputs* of matching.

Data contracts guard what comes in. These guard what we produce, because an identity graph can
degrade silently: a new source with a shared number, a bad deploy of the rules, or a feed that
starts duplicating records all show up as a shift in a handful of numbers before any customer
complains.

Each monitor returns a :class:`MonitorResult`; ``warn`` and ``fail`` also land in ``alerts.jsonl``
(a stand-in for a Slack/PagerDuty webhook).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class MonitorResult:
    run_day: int
    metric: str
    value: float
    warn_at: float
    fail_at: float
    direction: str  # "above" or "below": which side is bad
    status: str  # ok | warn | fail
    detail: str = ""


def _status(value: float, warn: float, fail: float, direction: str) -> str:
    bad = (lambda x, t: x >= t) if direction == "above" else (lambda x, t: x <= t)
    if bad(value, fail):
        return "fail"
    return "warn" if bad(value, warn) else "ok"


def run_monitors(
    day: int,
    *,
    labels: pd.Series,
    max_cluster_size: int,
    n_hubs: int,
    prev_n_hubs: int | None,
    events: pd.DataFrame,
    n_active_ids: int,
    stability: dict | None,
    rejected: pd.DataFrame,
) -> list[MonitorResult]:
    sizes = labels.value_counts()
    out: list[MonitorResult] = []

    def add(
        metric: str, value: float, warn: float, fail: float, direction: str, detail: str = ""
    ) -> None:
        out.append(
            MonitorResult(
                day,
                metric,
                float(value),
                warn,
                fail,
                direction,
                _status(float(value), warn, fail, direction),
                detail,
            )
        )

    add(
        "largest_cluster",
        sizes.max(),
        0.8 * max_cluster_size,
        max_cluster_size,
        "above",
        "a customer with this many records is usually two or more people glued together",
    )
    add(
        "singleton_share",
        float((sizes == 1).sum() / len(sizes)),
        0.75,
        0.90,
        "above",
        "share of clusters with one record; a jump means matching stopped linking",
    )
    if prev_n_hubs is not None and prev_n_hubs > 0:
        add(
            "hub_growth",
            n_hubs / prev_n_hubs - 1,
            0.25,
            1.0,
            "above",
            "new hub identifiers: a new store/feed is typing a shared number",
        )
    n_merge = int((events["event"] == "merge").sum()) if len(events) else 0
    n_split = int((events["event"] == "split").sum()) if len(events) else 0
    if day > 0 and n_active_ids:
        add(
            "merge_rate",
            n_merge / n_active_ids,
            0.005,
            0.02,
            "above",
            "share of IDs merged away today; a spike means the rules loosened",
        )
        add(
            "split_rate",
            n_split / n_active_ids,
            0.005,
            0.02,
            "above",
            "share of IDs split today; a spike means the rules tightened or data was removed",
        )
    if stability and stability.get("records_compared"):
        add(
            "id_unchanged_share",
            stability["unchanged"],
            0.995,
            0.98,
            "below",
            "records that kept their person ID since yesterday",
        )
    if len(rejected):
        top = rejected["reason"].value_counts()
        add(
            "constraint_rejections",
            len(rejected),
            500,
            5000,
            "above",
            "edges the constraints refused: " + ", ".join(f"{k}={v}" for k, v in top.items()),
        )
    return out


def write_alerts(results: list[MonitorResult], path: Path) -> int:
    """Append warn/fail monitor results to a JSON-lines file. Returns the number written."""
    bad = [r for r in results if r.status != "ok"]
    if bad:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for r in bad:
                f.write(json.dumps(asdict(r)) + "\n")
    return len(bad)
