"""Deterministic pass: link records that share a *personal* phone or email.

Why a deterministic pass next to a probabilistic model
------------------------------------------------------
A phone number that belongs to one person is the strongest evidence there is, and a model
should not have to rediscover that. But a phone is only personal if one person uses it. The
tell-tale of a *shared* identifier (a household phone, a number re-issued to someone else) is
that the records carrying it hold two or more names that cannot be the same person.

* personal identifier -> link every record that carries it (these edges are certain)
* shared identifier   -> create no edge from the identifier alone; the probabilistic model may
                         still link records whose *names* agree, and keeps the rest apart

Hub identifiers (a store's own phone) were already removed upstream and never reach this step.
"""

from __future__ import annotations

from itertools import combinations

import pandas as pd

from cip.normalize import dob_compatible, names_conflict

RULE_PROBABILITY = 0.99


def _name_groups(counts: dict[str, int]) -> list[int]:
    """Record counts of the mutually compatible name groups (names that do not conflict join up)."""
    names = sorted(counts)
    parent = list(range(len(names)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            if not names_conflict(names[i], names[j]):
                parent[find(i)] = find(j)
    totals: dict[int, int] = {}
    for i, name in enumerate(names):
        totals[find(i)] = totals.get(find(i), 0) + counts[name]
    return list(totals.values())


def shared_identifiers(records: pd.DataFrame, column: str, min_group_records: int = 2) -> set[str]:
    """Identifiers used by more than one person.

    Two signals, either is enough: the records carrying the identifier hold names that cannot be
    one person, or dates of birth that clearly differ. The second catches a household phone whose
    nameless POS records would otherwise look like one person's number.

    ``min_group_records`` makes the name signal robust to typos: a single mistyped record forms a
    "group" of one, which is far more likely a typo than a second person, so a name group must
    be backed by at least this many records to count.
    """
    shared: set[str] = set()
    for value, group in records.dropna(subset=[column]).groupby(column):
        counts = group["name_key"].dropna().value_counts().to_dict()
        if len(counts) >= 2:
            sizes = _name_groups(counts)
            if sum(1 for n in sizes if n >= min_group_records) >= 2:
                shared.add(value)
                continue
        dobs = sorted(set(group["dob_norm"].dropna()))
        if len(dobs) >= 2 and any(not dob_compatible(a, b) for a, b in combinations(dobs, 2)):
            shared.add(value)
    return shared


def deterministic_edges(
    records: pd.DataFrame, shared_phones: set[str], shared_emails: set[str]
) -> pd.DataFrame:
    """Star edges from each personal identifier's first record to every other record carrying it."""
    frames = []
    for column, shared, rule in (
        ("phone_match", shared_phones, "personal_phone"),
        ("email_match", shared_emails, "personal_email"),
    ):
        valid = records.dropna(subset=[column])
        valid = valid[~valid[column].isin(shared)]
        valid = valid.sort_values("record_key")
        first = valid.groupby(column)["record_key"].transform("first")
        pairs = pd.DataFrame(
            {"unique_id_l": first, "unique_id_r": valid["record_key"], "rule": rule}
        )
        frames.append(pairs[pairs["unique_id_l"] != pairs["unique_id_r"]])
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    out["match_probability"] = RULE_PROBABILITY
    return out[["unique_id_l", "unique_id_r", "match_probability", "rule"]]
