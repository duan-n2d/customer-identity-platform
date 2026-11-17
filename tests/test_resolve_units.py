import pandas as pd
from hypothesis import given, settings
from hypothesis import strategies as st

from cip.resolve.cluster import connected_components, constrained_clusters
from cip.resolve.hubs import apply_hubs, detect_hubs
from cip.resolve.ids import IdState, assign_ids, canonical


def edges(*triples):
    return pd.DataFrame(triples, columns=["unique_id_l", "unique_id_r", "match_probability"])


# ------------------------------------------------------------------------------ clustering


def test_plain_components_merge_through_a_chain_but_constraints_do_not():
    keys = ["a", "b", "c"]
    e = edges(("a", "b", 0.99), ("b", "c", 0.95))
    plain = connected_components(keys, e, 0.9)
    assert len(set(plain.values())) == 1  # A and C are glued together through B

    res = constrained_clusters(
        keys,
        {"a": "1990-01-01", "b": None, "c": "1985-05-05"},
        e,
        threshold=0.9,
        max_cluster_size=10,
    )
    assert res.labels["a"] == res.labels["b"] != res.labels["c"]
    assert list(res.rejected["reason"]) == ["dob_conflict"]


def test_the_strongest_evidence_is_applied_first():
    keys = ["a", "b", "c"]
    dobs = {"a": None, "b": "1990-01-01", "c": "1985-05-05"}
    # a-c is the stronger edge, so b must be the one that is refused
    res = constrained_clusters(
        keys, dobs, edges(("a", "b", 0.92), ("a", "c", 0.99)), threshold=0.9, max_cluster_size=10
    )
    assert res.labels["a"] == res.labels["c"] != res.labels["b"]


def test_a_day_month_swap_is_not_a_conflict():
    res = constrained_clusters(
        ["a", "b"],
        {"a": "1990-05-07", "b": "1990-07-05"},
        edges(("a", "b", 0.99)),
        threshold=0.9,
        max_cluster_size=10,
    )
    assert res.labels["a"] == res.labels["b"]


def test_max_cluster_size_is_enforced_and_reported():
    keys = [f"r{i}" for i in range(6)]
    e = edges(*[(keys[i], keys[i + 1], 0.99 - i * 0.001) for i in range(5)])
    res = constrained_clusters(keys, {}, e, threshold=0.9, max_cluster_size=3)
    sizes = pd.Series(res.labels).value_counts()
    assert sizes.max() <= 3
    assert (res.rejected["reason"] == "max_size").any()


def test_missing_dobs_arriving_as_nan_from_pandas_do_not_break_or_block_merges():
    res = constrained_clusters(
        ["a", "b"],
        {"a": float("nan"), "b": "1990-01-01"},
        edges(("a", "b", 0.99)),
        threshold=0.9,
        max_cluster_size=10,
    )
    assert res.labels["a"] == res.labels["b"]


def test_edges_below_the_threshold_never_merge():
    res = constrained_clusters(
        ["a", "b"], {}, edges(("a", "b", 0.5)), threshold=0.9, max_cluster_size=9
    )
    assert res.labels["a"] != res.labels["b"]


def test_cluster_label_is_the_smallest_member_so_results_are_deterministic():
    res = constrained_clusters(
        ["z", "m", "a"],
        {},
        edges(("z", "m", 0.99), ("m", "a", 0.98)),
        threshold=0.9,
        max_cluster_size=9,
    )
    assert set(res.labels.values()) == {"a"}


@settings(max_examples=60, deadline=None)
@given(
    st.lists(st.tuples(st.integers(0, 9), st.integers(0, 9), st.floats(0.5, 1.0)), max_size=30),
    st.lists(
        st.sampled_from([None, "1990-01-01", "1985-05-05", "2000-12-12"]), min_size=10, max_size=10
    ),
)
def test_no_cluster_ever_contains_two_clearly_different_dobs(raw_edges, dobs):
    from cip.normalize import dob_compatible

    keys = [f"r{i}" for i in range(10)]
    dob = dict(zip(keys, dobs, strict=True))
    e = edges(*[(f"r{a}", f"r{b}", p) for a, b, p in raw_edges if a != b])
    res = constrained_clusters(keys, dob, e, threshold=0.6, max_cluster_size=10)
    members: dict[str, list[str]] = {}
    for k, label in res.labels.items():
        members.setdefault(label, []).append(k)
    for group in members.values():
        known = [dob[k] for k in group if dob[k]]
        assert all(dob_compatible(x, y) for x in known for y in known)


# ------------------------------------------------------------------------------ hubs


def _records():
    rows = []
    for i in range(60):  # a "store phone": many records, many different names
        rows.append(
            {
                "record_key": f"s{i}",
                "phone_e164": "+84900000001",
                "email_norm": None,
                "name_key": f"name{i}",
            }
        )
    for i in range(6):  # a household phone: a few records, a few names
        rows.append(
            {
                "record_key": f"h{i}",
                "phone_e164": "+84900000002",
                "email_norm": None,
                "name_key": f"member{i % 3}",
            }
        )
    for i in range(50):  # a busy but legitimate customer: many records, ONE name
        rows.append(
            {
                "record_key": f"c{i}",
                "phone_e164": "+84900000003",
                "email_norm": None,
                "name_key": "same name",
            }
        )
    return pd.DataFrame(rows)


def test_hubs_require_many_records_and_many_names():
    hubs = detect_hubs(_records(), min_records=40, min_distinct_names=12)
    assert list(hubs["identifier"]) == ["+84900000001"]  # not the household, not the loyal customer


def test_apply_hubs_removes_the_identifier_from_matching_but_keeps_the_raw_value():
    records = _records()
    hubs = detect_hubs(records, min_records=40, min_distinct_names=12)
    out = apply_hubs(records, hubs)
    store = out[out["phone_e164"] == "+84900000001"]
    assert store["phone_match"].isna().all() and store["phone_is_hub"].all()
    assert out[out["phone_e164"] == "+84900000002"]["phone_match"].notna().all()


# ------------------------------------------------------------------------------ stable ids


def clusters(mapping: dict[str, str]) -> pd.DataFrame:
    return pd.DataFrame({"record_key": list(mapping), "cluster_label": list(mapping.values())})


def test_first_run_creates_sequential_deterministic_ids():
    state, events = assign_ids(clusters({"a": "a", "b": "a", "c": "c"}), IdState(), day=0)
    ids = dict(zip(state.person_map["record_key"], state.person_map["person_id"], strict=True))
    assert ids["a"] == ids["b"] != ids["c"]
    assert sorted(set(ids.values())) == ["P00000001", "P00000002"]
    assert set(events["event"]) == {"new"}


def test_rerunning_the_same_clusters_changes_nothing():
    c = clusters({"a": "a", "b": "a", "c": "c"})
    s1, _ = assign_ids(c, IdState(), 0)
    s2, ev = assign_ids(c, s1, 1)
    assert s1.person_map.equals(s2.person_map)
    assert ev.empty


def test_a_new_record_joining_an_existing_cluster_inherits_its_id():
    s1, _ = assign_ids(clusters({"a": "a", "b": "a"}), IdState(), 0)
    s2, ev = assign_ids(clusters({"a": "a", "b": "a", "z": "a"}), s1, 1)
    assert s2.person_map["person_id"].nunique() == 1
    assert ev.empty


def test_cluster_label_changes_do_not_change_the_id():
    s1, _ = assign_ids(clusters({"b": "b", "c": "b"}), IdState(), 0)
    s2, ev = assign_ids(clusters({"a": "a", "b": "a", "c": "a"}), s1, 1)  # label moved to "a"
    assert set(s2.person_map["person_id"]) == set(s1.person_map["person_id"])
    assert ev.empty


def test_merge_keeps_the_oldest_id_and_leaves_an_alias_trail():
    s1, _ = assign_ids(clusters({"a": "a", "b": "b"}), IdState(), 0)
    old = s1.person_map.set_index("record_key")["person_id"]
    s2, ev = assign_ids(clusters({"a": "a", "b": "a"}), s1, 1)
    survivor = min(old["a"], old["b"])
    loser = max(old["a"], old["b"])
    assert set(s2.person_map["person_id"]) == {survivor}
    assert list(ev["event"]) == ["merge"]
    assert canonical(s2.registry, loser) == survivor
    assert s2.registry.set_index("person_id").loc[loser, "status"] == "merged"


def test_split_keeps_the_id_with_the_larger_part_and_records_the_parent():
    s1, _ = assign_ids(clusters({k: "a" for k in "abcd"}), IdState(), 0)
    parent = s1.person_map["person_id"].iloc[0]
    s2, ev = assign_ids(clusters({"a": "a", "b": "a", "c": "a", "d": "d"}), s1, 1)
    ids = s2.person_map.set_index("record_key")["person_id"]
    assert ids["a"] == ids["b"] == ids["c"] == parent
    assert ids["d"] != parent
    assert list(ev["event"]) == ["split"]
    assert ev.iloc[0]["related_person_id"] == parent


def test_an_id_is_never_reused_after_a_merge():
    s1, _ = assign_ids(clusters({"a": "a", "b": "b"}), IdState(), 0)
    s2, _ = assign_ids(clusters({"a": "a", "b": "a", "c": "c"}), s1, 1)
    assert "P00000003" in set(s2.person_map["person_id"])  # not P00000002, which was merged away


@settings(max_examples=40, deadline=None)
@given(st.lists(st.lists(st.integers(0, 5), min_size=8, max_size=8), min_size=2, max_size=5))
def test_ids_are_a_function_of_clusters_and_every_cluster_has_exactly_one(partitions):
    """Whatever sequence of clusterings arrives, each cluster maps to exactly one active ID."""
    state = IdState()
    keys = [f"r{i}" for i in range(8)]
    for day, labels in enumerate(partitions):
        mapping = {k: f"c{labels[i]}" for i, k in enumerate(keys)}
        state, _ = assign_ids(clusters(mapping), state, day)
        m = state.person_map.set_index("record_key")["person_id"]
        for label in set(mapping.values()):
            members = [k for k, v in mapping.items() if v == label]
            assert m[members].nunique() == 1  # one ID per cluster
        assert m.nunique() == len(set(mapping.values()))  # one cluster per ID
        active = state.registry[state.registry["status"] == "active"]["person_id"]
        assert set(m) <= set(active)


# ------------------------------------------------------------- name conflict and cannot-link


def test_names_conflict_tolerates_typos_and_dropped_middle_names_but_not_relatives():
    from cip.normalize import names_conflict

    assert names_conflict("an nguyen van", "lan nguyen thi")  # two family members
    assert names_conflict("hong nguyen nhung", "cong nguyen phuc")  # short tokens: exact only
    assert not names_conflict("an nguyen van", "an nguyen")  # middle name dropped
    assert not names_conflict("nguyen nhung thi", "nguyen nhugn thi")  # typo in a long token
    assert not names_conflict(None, "nguyen van an")  # missing evidence is not a conflict


def test_name_constraint_refuses_a_merge_of_different_names():
    keys = ["a", "b"]
    kwargs = dict(threshold=0.9, max_cluster_size=10)
    e = edges(("a", "b", 0.99))
    names = {"a": "an nguyen van", "b": "lan nguyen thi"}
    free = constrained_clusters(keys, {}, e, **kwargs)
    assert free.labels["a"] == free.labels["b"]
    held = constrained_clusters(keys, {}, e, name_by_key=names, **kwargs)
    assert held.labels["a"] != held.labels["b"]
    assert list(held.rejected["reason"]) == ["name_conflict"]


def test_cannot_link_blocks_a_pair_even_through_a_chain():
    keys = ["a", "b", "c"]
    e = edges(("a", "b", 0.99), ("b", "c", 0.95))
    cannot = pd.DataFrame([("a", "c")], columns=["unique_id_l", "unique_id_r"])
    res = constrained_clusters(keys, {}, e, threshold=0.9, max_cluster_size=10, cannot_link=cannot)
    assert res.labels["a"] == res.labels["b"] != res.labels["c"]
