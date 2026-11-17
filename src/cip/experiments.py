"""Controlled experiments: ablation of each guard, and threshold selection on a held-out dev set.

Method
------
* Two synthetic worlds with different seeds: **dev** (used to choose settings) and **test**
  (used only to report). Choosing and reporting on the same data would flatter the result.
* Ablation changes one component at a time against the full configuration, on the same data and
  the same trained model, so each row isolates one component's contribution.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

import pandas as pd

from cip.config import Settings
from cip.evaluate import evaluate_clustering
from cip.generator import WorldConfig, generate, write_landing
from cip.quality import load_contracts
from cip.resolve.engine import MatchConfig, run_matching
from cip.resolve.features import build_staging
from cip.warehouse import Warehouse


def build_world(
    n_persons: int, seed: int, workdir: Path
) -> tuple[Settings, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Generate a world, load day 0; return (settings, staged, truth records, truth persons)."""
    cfg = Settings(data_dir=workdir)
    data = generate(WorldConfig(n_persons=n_persons, seed=seed, days=0))
    write_landing(data, cfg)
    with Warehouse(cfg) as wh:
        wh.land_day(0, load_contracts(Path("contracts")))
        staged = build_staging(wh)
    return cfg, staged, data.truth_records, data.truth_persons


def _row(name: str, m: dict) -> dict:
    return {
        "variant": name,
        "precision": m["precision"],
        "linkable_recall": m["linkable_recall"],
        "linkable_f1": m["linkable_f1"],
        "false_merge_pairs": sum(m["false_merge_pairs_by_trap"].values()),
        "households": m["false_merge_pairs_by_trap"]["household"],
        "recycled": m["false_merge_pairs_by_trap"]["recycled_phone"],
        "common_name": m["false_merge_pairs_by_trap"]["common_name"],
    }


def ablation(n_persons: int, seed: int, base: MatchConfig | None = None) -> pd.DataFrame:
    """Full configuration versus one component removed at a time, plus the baselines."""
    base = base or MatchConfig()
    work = Path(tempfile.mkdtemp(prefix="cip_ablate_"))
    try:
        cfg, staged, truth, persons = build_world(n_persons, seed, work)
        variants: list[tuple[str, MatchConfig]] = [
            (
                "Splink only (no guards)",
                MatchConfig(
                    hub_filter=False,
                    deterministic_pass=False,
                    shared_detection=False,
                    name_constraint=False,
                    dob_constraint=False,
                    threshold=base.threshold,
                ),
            ),
            (
                "+ hub filter",
                MatchConfig(
                    deterministic_pass=False,
                    shared_detection=False,
                    name_constraint=False,
                    dob_constraint=False,
                    threshold=base.threshold,
                ),
            ),
            (
                "+ deterministic pass",
                MatchConfig(
                    shared_detection=False,
                    name_constraint=False,
                    dob_constraint=False,
                    threshold=base.threshold,
                ),
            ),
            (
                "+ shared-identifier detection",
                MatchConfig(name_constraint=False, dob_constraint=False, threshold=base.threshold),
            ),
            ("+ DOB constraint", MatchConfig(name_constraint=False, threshold=base.threshold)),
            ("+ name constraint", replace(base, name_constraint=True)),
            ("default profile (name constraint off)", replace(base, name_constraint=False)),
        ]
        rows = []
        models: dict[bool, Path] = {}
        for name, mc in variants:
            key = mc.hub_filter  # the model only depends on whether hubs were removed
            path = work / f"model_{int(key)}.json"
            result = run_matching(staged, cfg, mc, path, retrain=key not in models)
            models[key] = path
            rows.append(
                _row(name, evaluate_clustering(result.labels, truth, persons, result.records))
            )
        # leave-one-out on the full configuration
        for name, mc in [
            (
                "default - DOB constraint",
                replace(base, name_constraint=False, dob_constraint=False),
            ),
            ("default - shared detection", replace(base, shared_detection=False)),
            (
                "default - deterministic pass",
                replace(base, deterministic_pass=False, shared_detection=False),
            ),
            ("default + model cannot-link (<0.02)", replace(base, cannot_link_below=0.02)),
        ]:
            result = run_matching(staged, cfg, mc, models[mc.hub_filter], retrain=False)
            rows.append(
                _row(name, evaluate_clustering(result.labels, truth, persons, result.records))
            )
        return pd.DataFrame(rows)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def threshold_sweep(
    n_persons: int, seed: int, thresholds=(0.3, 0.5, 0.7, 0.9, 0.95)
) -> pd.DataFrame:
    """Precision/recall at each probabilistic-edge threshold on a given world."""
    work = Path(tempfile.mkdtemp(prefix="cip_sweep_"))
    try:
        cfg, staged, truth, persons = build_world(n_persons, seed, work)
        rows = []
        path = work / "model.json"
        for i, th in enumerate(thresholds):
            mc = MatchConfig(threshold=th)
            result = run_matching(staged, cfg, mc, path, retrain=i == 0)
            m = evaluate_clustering(result.labels, truth, persons, result.records)
            rows.append({"threshold": th, **_row("", m)})
        return pd.DataFrame(rows).drop(columns="variant")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def aggregate_seeds(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Mean over seeds per variant (original order), plus the standard deviation of the rates."""
    allf = pd.concat(frames)
    order = list(dict.fromkeys(allf["variant"]))
    by = allf.drop(columns="seed").groupby("variant", sort=False)
    mean = by.mean(numeric_only=True)
    out = mean.copy()
    if len(frames) > 1:
        sd = by.std(numeric_only=True)
        for c in ("precision", "linkable_recall", "linkable_f1"):
            out[f"{c}_sd"] = sd[c]
    out.insert(0, "seeds", len(frames))
    return out.loc[order].reset_index()
