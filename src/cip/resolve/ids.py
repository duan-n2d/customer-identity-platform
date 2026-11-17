"""Stable person IDs.

Clustering is recomputed every day, but downstream systems (CRM, marketing automation, the data
warehouse) store ``person_id`` and must not see it change just because the clusters were
recomputed. The rules below make an ID follow its people:

* **inherit**  a new cluster that mostly continues one previous ID keeps that ID
* **merge**    when two previous IDs end up in one cluster, the *oldest* ID survives and the other
               is recorded as ``merged_into`` it, so any system holding the old ID can still
               resolve it (see :func:`canonical`)
* **split**    when one previous ID's records divide across clusters, the largest part keeps the
               ID and each smaller part gets a *new* ID, with the parent recorded
* **new**      a cluster with no previous history gets a fresh ID

Every change is written to ``person_id_events``, so ID churn is auditable and measurable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

ID_PREFIX = "P"


@dataclass
class IdState:
    person_map: pd.DataFrame = field(
        default_factory=lambda: pd.DataFrame(
            {"record_key": pd.Series(dtype=str), "person_id": pd.Series(dtype=str)}
        )
    )
    registry: pd.DataFrame = field(
        default_factory=lambda: pd.DataFrame(
            {
                "person_id": pd.Series(dtype=str),
                "created_day": pd.Series(dtype=int),
                "status": pd.Series(dtype=str),
                "merged_into": pd.Series(dtype=object),
            }
        )
    )


def _format(n: int) -> str:
    return f"{ID_PREFIX}{n:08d}"


def _next_number(registry: pd.DataFrame) -> int:
    if registry.empty:
        return 1
    return int(registry["person_id"].str[len(ID_PREFIX) :].astype(int).max()) + 1


def assign_ids(clusters: pd.DataFrame, state: IdState, day: int) -> tuple[IdState, pd.DataFrame]:
    """Map today's clusters to stable person IDs.

    ``clusters`` has columns ``record_key`` and ``cluster_label``. Returns the new state and a
    frame of ID events (day, event, person_id, related_person_id, n_records).
    """
    c = clusters[["record_key", "cluster_label"]].copy()
    prev = state.person_map.set_index("record_key")["person_id"]
    c["prev_id"] = c["record_key"].map(prev)
    registry = state.registry.set_index("person_id")
    events: list[dict] = []

    overlap = (
        c.dropna(subset=["prev_id"])
        .groupby(["prev_id", "cluster_label"])
        .size()
        .rename("n")
        .reset_index()
        .sort_values(["prev_id", "n", "cluster_label"], ascending=[True, False, True])
    )
    heir = overlap.drop_duplicates("prev_id")  # for each previous ID, the cluster that continues it
    claims: dict[str, list[str]] = heir.groupby("cluster_label")["prev_id"].apply(list).to_dict()

    cluster_ids: dict[str, str] = {}
    registry_rows: dict[str, dict] = {pid: row.to_dict() for pid, row in registry.iterrows()}
    n_per_cluster = c.groupby("cluster_label").size().to_dict()

    for label, candidates in claims.items():
        survivor = min(candidates, key=lambda p: (int(registry_rows[p]["created_day"]), p))
        cluster_ids[label] = survivor
        for loser in candidates:
            if loser != survivor:
                registry_rows[loser]["status"] = "merged"
                registry_rows[loser]["merged_into"] = survivor
                events.append(
                    {
                        "day": day,
                        "event": "merge",
                        "person_id": survivor,
                        "related_person_id": loser,
                        "n_records": n_per_cluster[label],
                    }
                )

    counter = _next_number(state.registry)
    parent_of = (
        overlap.sort_values(["cluster_label", "n", "prev_id"], ascending=[True, False, True])
        .drop_duplicates("cluster_label")
        .set_index("cluster_label")["prev_id"]
        .to_dict()
    )
    for label in sorted(set(c["cluster_label"]) - set(cluster_ids)):
        new_id = _format(counter)
        counter += 1
        cluster_ids[label] = new_id
        registry_rows[new_id] = {"created_day": day, "status": "active", "merged_into": None}
        parent = parent_of.get(label)
        events.append(
            {
                "day": day,
                "event": "split" if parent else "new",
                "person_id": new_id,
                "related_person_id": parent,
                "n_records": n_per_cluster[label],
            }
        )

    new_map = pd.DataFrame(
        {"record_key": c["record_key"], "person_id": c["cluster_label"].map(cluster_ids)}
    )
    new_registry = pd.DataFrame(
        [{"person_id": pid, **row} for pid, row in registry_rows.items()],
        columns=["person_id", "created_day", "status", "merged_into"],
    ).sort_values("person_id", ignore_index=True)
    new_registry["created_day"] = new_registry["created_day"].astype(int)
    events_df = pd.DataFrame(
        events, columns=["day", "event", "person_id", "related_person_id", "n_records"]
    ).astype({"related_person_id": "string", "event": "string", "person_id": "string"})
    # explicit string dtype: an all-empty column would otherwise reach the warehouse typed NULL
    new_registry["merged_into"] = new_registry["merged_into"].astype("string")
    return IdState(new_map.reset_index(drop=True), new_registry), events_df


def canonical(registry: pd.DataFrame, person_id: str) -> str:
    """Follow ``merged_into`` links to the ID that is active today."""
    merged = registry.set_index("person_id")["merged_into"].to_dict()
    seen: set[str] = set()
    # pandas turns a missing value into NaN, so test with isna rather than ``is None``
    while person_id not in seen and not pd.isna(merged.get(person_id)):
        seen.add(person_id)
        person_id = merged[person_id]
    return person_id
