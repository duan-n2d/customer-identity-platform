"""Evaluation of an identity resolution against the hidden ground truth.

Why several metrics
-------------------
* **Pairwise precision/recall** count record pairs. Precision answers "of the pairs we put
  together, how many are really the same person?" (false merges are the costly error: two
  customers share a profile, so one sees the other's data). Recall answers "of all pairs that
  should be together, how many did we find?".
* **B-cubed** averages per record, so one giant wrong cluster cannot hide behind many small
  correct ones.
* **Linkable pairs.** Two records of the same person can only be linked if they share *some*
  evidence (a phone, an email, a date of birth). In real customer data most true pairs share
  nothing (the person used a different phone at the till and online), so no algorithm can find
  them. Recall over *all* true pairs mostly measures the data. We therefore report recall over
  the **linkable pairs**, and separately report what share of true pairs are linkable at all.
* **Error breakdown** by the trap that caused it (household, recycled number, common name,
  phone change) tells you what to fix next, which a single F1 never does.
"""

from __future__ import annotations

from itertools import combinations

import pandas as pd


def _pairs(n: pd.Series) -> int:
    return int((n * (n - 1) // 2).sum())


def pairwise(labels: pd.Series, truth: pd.Series) -> dict:
    """Pairwise precision, recall and F1 of a clustering (both series indexed by record_key)."""
    df = pd.DataFrame({"c": labels, "p": truth}).dropna()
    tp = _pairs(df.groupby(["c", "p"]).size())
    predicted = _pairs(df.groupby("c").size())
    actual = _pairs(df.groupby("p").size())
    precision = tp / predicted if predicted else 1.0
    recall = tp / actual if actual else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "true_pairs": actual,
        "predicted_pairs": predicted,
        "correct_pairs": tp,
    }


def bcubed(labels: pd.Series, truth: pd.Series) -> dict:
    df = pd.DataFrame({"c": labels, "p": truth}).dropna()
    both = df.groupby(["c", "p"]).size().rename("n_both").reset_index()
    both["n_c"] = both["c"].map(df.groupby("c").size())
    both["n_p"] = both["p"].map(df.groupby("p").size())
    # every record in a (cluster, person) cell contributes n_both/n_c (precision) and n_both/n_p
    weight = both["n_both"]
    precision = float((weight * both["n_both"] / both["n_c"]).sum() / weight.sum())
    recall = float((weight * both["n_both"] / both["n_p"]).sum() / weight.sum())
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"bcubed_precision": precision, "bcubed_recall": recall, "bcubed_f1": f1}


def cluster_errors(labels: pd.Series, truth: pd.Series) -> dict:
    df = pd.DataFrame({"c": labels, "p": truth}).dropna()
    persons_per_cluster = df.groupby("c")["p"].nunique()
    clusters_per_person = df.groupby("p")["c"].nunique()
    return {
        "clusters": int(df["c"].nunique()),
        "true_persons": int(df["p"].nunique()),
        "clusters_with_multiple_people": int((persons_per_cluster > 1).sum()),
        "people_split_across_clusters": int((clusters_per_person > 1).sum()),
        "largest_cluster": int(df.groupby("c").size().max()),
    }


def false_merge_breakdown(
    labels: pd.Series, truth: pd.Series, persons: pd.DataFrame
) -> dict[str, int]:
    """Count record pairs wrongly put together, by the trap involved (priority order)."""
    df = pd.DataFrame({"c": labels, "p": truth}).dropna()
    info = persons.set_index("person_id")
    out = {"household": 0, "recycled_phone": 0, "common_name": 0, "other": 0}
    for _, group in df.groupby("c"):
        counts = group["p"].value_counts()
        if len(counts) < 2:
            continue
        wrong = int(len(group) * (len(group) - 1) // 2 - (counts * (counts - 1) // 2).sum())
        members = info.loc[counts.index]
        if (
            members["household_id"].notna().sum() >= 2
            and members["household_id"].dropna().duplicated().any()
        ):
            out["household"] += wrong
        elif members["phone_recycled"].any():
            out["recycled_phone"] += wrong
        elif members["common_name"].any():
            out["common_name"] += wrong
        else:
            out["other"] += wrong
    return out


def missed_link_breakdown(
    labels: pd.Series, truth: pd.Series, persons: pd.DataFrame
) -> dict[str, int]:
    """Count record pairs that belong together but were not linked, by the person's trap."""
    df = pd.DataFrame({"c": labels, "p": truth}).dropna()
    info = persons.set_index("person_id")
    out = {"phone_changed": 0, "recycled_phone": 0, "household": 0, "other": 0}
    for person, group in df.groupby("p"):
        sizes = group.groupby("c").size()
        if len(sizes) < 2:
            continue
        missing = int(len(group) * (len(group) - 1) // 2 - (sizes * (sizes - 1) // 2).sum())
        row = info.loc[person]
        if row["phone_changed"]:
            out["phone_changed"] += missing
        elif row["phone_recycled"]:
            out["recycled_phone"] += missing
        elif row["in_household"]:
            out["household"] += missing
        else:
            out["other"] += missing
    return out


def linkable_pairs(records: pd.DataFrame, truth: pd.Series) -> set[tuple[str, str]]:
    """True pairs sharing a usable phone, email or date of birth (so a linker *could* find them)."""
    t = records[["record_key", "phone_match", "email_match", "dob_norm"]].copy()
    t["person_id"] = t["record_key"].map(truth)
    pairs: set[tuple[str, str]] = set()
    for col in ("phone_match", "email_match", "dob_norm"):
        for _, group in t.dropna(subset=[col, "person_id"]).groupby([col, "person_id"]):
            if len(group) > 1:
                pairs.update(combinations(sorted(group["record_key"]), 2))
    return pairs


def evaluate_clustering(
    labels: pd.Series, truth_records: pd.DataFrame, persons: pd.DataFrame, records: pd.DataFrame
) -> dict:
    """Full metric set for a clustering of ``records`` (indexed by record_key via ``labels``)."""
    truth = truth_records.set_index("record_key")["person_id"].reindex(labels.index)
    allv = {**pairwise(labels, truth), **bcubed(labels, truth)}
    linkable = linkable_pairs(records, truth)
    found = sum(1 for a, b in linkable if labels[a] == labels[b])
    linkable_recall = found / len(linkable) if linkable else 1.0
    precision = allv["precision"]
    f1 = (
        2 * precision * linkable_recall / (precision + linkable_recall)
        if precision + linkable_recall
        else 0.0
    )
    return {
        "records": int(len(labels)),
        "precision": precision,
        "linkable_recall": linkable_recall,
        "linkable_f1": f1,
        "linkable_share_of_true_pairs": len(linkable) / allv["true_pairs"]
        if allv["true_pairs"]
        else 1.0,
        "all_pairs": allv,
        "errors": cluster_errors(labels, truth),
        "false_merge_pairs_by_trap": false_merge_breakdown(labels, truth, persons),
        "missed_pairs_by_trap": missed_link_breakdown(labels, truth, persons),
    }


def id_stability(previous: pd.DataFrame, current: pd.DataFrame, registry: pd.DataFrame) -> dict:
    """Share of records that kept their person ID between two days.

    ``previous``/``current`` have ``record_key, person_id``. ``alias_resolved`` counts a changed
    ID as fine when the old ID is merely an alias of the new one (a recorded merge), because the
    old ID can still be resolved.
    """
    from cip.resolve.ids import canonical

    both = previous.merge(current, on="record_key", suffixes=("_prev", "_now"))
    if both.empty:
        return {"records_compared": 0, "unchanged": 1.0, "alias_resolved": 1.0}
    unchanged = both["person_id_prev"] == both["person_id_now"]
    resolved = both.apply(
        lambda r: (
            r["person_id_prev"] == r["person_id_now"]
            or canonical(registry, r["person_id_prev"]) == r["person_id_now"]
        ),
        axis=1,
    )
    return {
        "records_compared": int(len(both)),
        "unchanged": float(unchanged.mean()),
        "alias_resolved": float(resolved.mean()),
    }
