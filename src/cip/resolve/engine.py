"""The matching engine: staged records to clusters, with every component switchable.

Every guard in the engine exists because an experiment showed it was needed (see
``docs/evaluation.md``). Keeping them switchable is what makes that claim checkable: the ablation
runs the same data with one component off at a time.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from cip.config import Settings
from cip.resolve import model
from cip.resolve.cluster import constrained_clusters
from cip.resolve.hubs import apply_hubs, detect_hubs
from cip.resolve.rules import deterministic_edges, shared_identifiers

EDGE_COLUMNS = ["unique_id_l", "unique_id_r", "match_probability"]


@dataclass(frozen=True)
class MatchConfig:
    hub_filter: bool = True  # drop store-style numbers before matching
    deterministic_pass: bool = True  # link personal phones/emails outright
    shared_detection: bool = True  # do not treat household/recycled numbers as personal
    shared_min_group_records: int = 1  # a name group needs this many records to count as a person
    name_constraint: bool = False  # refuse merges where every name pair conflicts (measured: hurts)
    dob_constraint: bool = True  # refuse merges across clearly different dates of birth
    cannot_link_below: float | None = None  # model negative evidence (off: no measurable benefit)
    threshold: float = 0.5  # probabilistic edges below this never merge records
    max_cluster_size: int = 25

    def label(self) -> str:
        return ", ".join(f"{k}={v}" for k, v in asdict(self).items())

    @classmethod
    def precision_first(cls) -> MatchConfig:
        """Default. A false merge shows one customer another customer's data, so it costs more
        than a missed link. A name group needs a single record to count as a second person."""
        return cls(shared_min_group_records=1)

    @classmethod
    def balanced(cls) -> MatchConfig:
        """Higher F1, lower precision: a typo no longer makes a phone look shared, so more
        household numbers slip through as personal."""
        return cls(shared_min_group_records=2)


@dataclass
class MatchResult:
    labels: pd.Series  # record_key -> cluster label
    records: pd.DataFrame  # staged records plus hub flags and match columns
    edges: pd.DataFrame  # candidate edges offered to the clusterer
    rejected: pd.DataFrame  # edges refused by a constraint, with the reason
    hubs: pd.DataFrame
    shared_phones: set[str] = field(default_factory=set)
    shared_emails: set[str] = field(default_factory=set)
    timings: dict[str, float] = field(default_factory=dict)


def run_matching(
    staged: pd.DataFrame,
    settings: Settings,
    mc: MatchConfig,
    model_path: Path,
    *,
    retrain: bool,
) -> MatchResult:
    timings: dict[str, float] = {}
    t = time.perf_counter()

    def lap(name: str) -> None:
        nonlocal t
        now = time.perf_counter()
        timings[name] = round(now - t, 3)
        t = now

    if mc.hub_filter:
        hubs = detect_hubs(
            staged,
            min_records=settings.hub_min_records,
            min_distinct_names=settings.hub_min_distinct_names,
        )
    else:
        hubs = pd.DataFrame(
            columns=["identifier_type", "identifier", "n_records", "n_distinct_names"]
        )
    records = apply_hubs(staged, hubs)
    lap("hubs")

    if retrain or not model_path.exists():
        model.train(records, settings, model_path)
        lap("train")
    pairs = model.score(
        records, model_path, 0.0 if mc.cannot_link_below is not None else mc.threshold
    )
    lap("score")

    shared_phones: set[str] = set()
    shared_emails: set[str] = set()
    parts = [pairs[pairs["match_probability"] >= mc.threshold][EDGE_COLUMNS]]
    if mc.deterministic_pass:
        if mc.shared_detection:
            shared_phones = shared_identifiers(records, "phone_match", mc.shared_min_group_records)
            shared_emails = shared_identifiers(records, "email_match", mc.shared_min_group_records)
        parts.append(deterministic_edges(records, shared_phones, shared_emails)[EDGE_COLUMNS])
    edges = pd.concat(parts, ignore_index=True)
    lap("rules")

    cannot = None
    if mc.cannot_link_below is not None:
        cannot = pairs[pairs["match_probability"] < mc.cannot_link_below]
    keys = list(records["record_key"])
    result = constrained_clusters(
        keys,
        dict(zip(records["record_key"], records["dob_norm"], strict=True))
        if mc.dob_constraint
        else {},
        edges,
        threshold=mc.threshold,
        max_cluster_size=mc.max_cluster_size,
        cannot_link=cannot,
        name_by_key=dict(zip(records["record_key"], records["name_key"], strict=True))
        if mc.name_constraint
        else None,
    )
    lap("cluster")
    return MatchResult(
        labels=pd.Series(result.labels),
        records=records,
        edges=edges,
        rejected=result.rejected,
        hubs=hubs,
        shared_phones=shared_phones,
        shared_emails=shared_emails,
        timings=timings,
    )
