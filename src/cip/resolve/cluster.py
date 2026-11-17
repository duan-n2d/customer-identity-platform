"""Clustering of scored record pairs, with hard constraints.

Splink gives a probability for each candidate pair. Turning pairs into people is a separate
problem. Plain connected components has a well-known failure: one bad edge between two big
groups merges two different people, and the damage compounds (a chain A-B-C joins A and C even
if A and C are obviously different people).

This module processes edges from most to least confident and refuses a merge that would put
two *incompatible* records in one cluster:

* ``dob_conflict``  both sides have known dates of birth that clearly differ
* ``name_conflict`` every name on one side conflicts with every name on the other (two family
                    members, or an old and a new owner of a recycled phone number). Requiring
                    *all* pairs to conflict keeps one mistyped record from blocking the rest
* ``cannot_link``   the model scored a pair across the two sides as very unlikely to be the same
                    person even though they share an identifier (e.g. two members of a household
                    on one phone). Such negative evidence stops a weak, nameless record from
                    acting as a bridge that fuses the two people
* ``max_size``      the merged cluster would exceed the configured size cap

Because the strongest evidence is applied first, a weak edge arriving later cannot override it.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from cip.normalize import dob_compatible, names_conflict


@dataclass
class ClusterResult:
    labels: dict[str, str]  # record_key -> cluster label (the smallest record_key in the cluster)
    rejected: pd.DataFrame  # edges refused: id_l, id_r, match_probability, reason
    n_edges_considered: int


class _UnionFind:
    def __init__(
        self,
        keys: list[str],
        dobs: dict[str, str | None],
        cannot_link: dict[str, set[str]] | None = None,
        names: dict[str, str | None] | None = None,
    ):
        self.parent = {k: k for k in keys}
        self.size = dict.fromkeys(keys, 1)
        # pandas hands us NaN for a missing value, which is truthy, so accept only real strings
        self.dobs = {
            k: ({dobs[k]} if isinstance(dobs.get(k), str) and dobs[k] else set()) for k in keys
        }
        self.cannot_link = cannot_link or {}
        self.names = {
            k: ({names[k]} if names and isinstance(names.get(k), str) and names[k] else set())
            for k in keys
        }
        self.members: dict[str, set[str]] = {}  # only for clusters with more than one record

    def find(self, x: str) -> str:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def _members(self, root: str) -> set[str]:
        return self.members.get(root) or {root}

    def names_all_conflict(self, a: str, b: str) -> bool:
        na, nb = self.names[a], self.names[b]
        return bool(na and nb and all(names_conflict(x, y) for x in na for y in nb))

    def blocked_by_cannot_link(self, a: str, b: str) -> bool:
        """True if any record of cluster ``a`` has a cannot-link to a record of cluster ``b``."""
        small, large = (a, b) if self.size[a] <= self.size[b] else (b, a)
        big = self._members(large)
        return any(self.cannot_link.get(x, set()) & big for x in self._members(small))

    def union(self, a: str, b: str) -> None:
        if self.size[a] < self.size[b]:
            a, b = b, a
        merged = self._members(a) | self._members(b)
        self.parent[b] = a
        self.size[a] += self.size[b]
        self.dobs[a] |= self.dobs[b]
        self.names[a] |= self.names[b]
        self.members[a] = merged
        self.members.pop(b, None)


def constrained_clusters(
    record_keys: list[str],
    dob_by_key: dict[str, str | None],
    edges: pd.DataFrame,
    *,
    threshold: float,
    max_cluster_size: int,
    cannot_link: pd.DataFrame | None = None,
    name_by_key: dict[str, str | None] | None = None,
) -> ClusterResult:
    """Cluster records. ``cannot_link`` has columns ``unique_id_l``/``unique_id_r``."""
    links: dict[str, set[str]] = {}
    if cannot_link is not None and len(cannot_link):
        for a, b in cannot_link[["unique_id_l", "unique_id_r"]].itertuples(index=False):
            links.setdefault(a, set()).add(b)
            links.setdefault(b, set()).add(a)
    uf = _UnionFind(record_keys, dob_by_key, links, name_by_key)
    cols = ["unique_id_l", "unique_id_r", "match_probability"]
    kept = edges.loc[edges["match_probability"] >= threshold, cols] if len(edges) else edges
    kept = kept.sort_values(
        ["match_probability", "unique_id_l", "unique_id_r"], ascending=[False, True, True]
    )
    rejected: list[tuple[str, str, float, str]] = []
    for a, b, p in kept.itertuples(index=False):
        ra, rb = uf.find(a), uf.find(b)
        if ra == rb:
            continue
        if uf.size[ra] + uf.size[rb] > max_cluster_size:
            rejected.append((a, b, float(p), "max_size"))
            continue
        if not all(dob_compatible(x, y) for x in uf.dobs[ra] for y in uf.dobs[rb]):
            rejected.append((a, b, float(p), "dob_conflict"))
            continue
        if uf.names_all_conflict(ra, rb):
            rejected.append((a, b, float(p), "name_conflict"))
            continue
        if uf.blocked_by_cannot_link(ra, rb):
            rejected.append((a, b, float(p), "cannot_link"))
            continue
        uf.union(ra, rb)

    groups: dict[str, list[str]] = {}
    for k in record_keys:
        groups.setdefault(uf.find(k), []).append(k)
    labels = {k: min(members) for members in groups.values() for k in members}
    return ClusterResult(
        labels,
        pd.DataFrame(rejected, columns=["id_l", "id_r", "match_probability", "reason"]),
        len(kept),
    )


def connected_components(
    record_keys: list[str], edges: pd.DataFrame, threshold: float
) -> dict[str, str]:
    """Unconstrained baseline: plain connected components over edges at or above the threshold."""
    result = constrained_clusters(
        record_keys, {}, edges, threshold=threshold, max_cluster_size=len(record_keys) + 1
    )
    return result.labels
