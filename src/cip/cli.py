"""Command line: one entry point for the whole lifecycle.

cip demo                      generate -> run every day -> dbt build -> evaluate -> export
cip generate / run / dbt / evaluate / ablate / bench / export-delta / erase / serve / report
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from cip.config import Settings
from cip.evaluate import evaluate_clustering
from cip.generator import WorldConfig, generate, write_landing
from cip.resolve.engine import MatchConfig


def _settings(args: argparse.Namespace) -> Settings:
    return Settings(data_dir=Path(args.data_dir))


def _profile(name: str) -> MatchConfig:
    return MatchConfig.balanced() if name == "balanced" else MatchConfig.precision_first()


def _naive_unchanged(prev: pd.Series | None, cur: pd.Series) -> float:
    """Share of records whose ID is unchanged if the ID is the cluster's smallest record key."""
    if prev is None:
        return 1.0
    common = prev.index.intersection(cur.index)
    return float((prev[common] == cur[common]).mean()) if len(common) else 1.0


def cmd_generate(args: argparse.Namespace) -> None:
    cfg = WorldConfig(
        n_persons=args.persons, seed=args.seed, days=args.days, drift_day=args.drift_day
    )
    meta = write_landing(generate(cfg), _settings(args))
    print(f"generated {sum(meta['records'].values()):,} records, fingerprint {meta['fingerprint']}")


def _run(settings: Settings, days: int, mc: MatchConfig, world: dict) -> dict:
    from cip.pipeline import run_day

    truth_path = settings.truth_dir / "records.parquet"
    truth = pd.read_parquet(truth_path) if truth_path.exists() else None
    persons = pd.read_parquet(settings.truth_dir / "persons.parquet") if truth is not None else None
    rows, prev_labels, last_eval = [], None, None
    for day in range(days + 1):
        r = run_day(settings, day, mc)
        row = {
            "day": day,
            "records": r.records,
            "clusters": r.clusters,
            "active_ids": r.active_ids,
            "seconds": r.seconds,
            "stability": r.stability,
            "id_events": r.id_events,
            "quarantined": r.quarantined_sources,
            "alerts": [f"{m.metric}:{m.status}" for m in r.monitors if m.status != "ok"],
            "timings": r.match.timings,
            "naive_min_key_unchanged": _naive_unchanged(prev_labels, r.match.labels),
        }
        if truth is not None:
            ev = evaluate_clustering(r.match.labels, truth, persons, r.match.records)
            row.update(
                precision=ev["precision"],
                linkable_recall=ev["linkable_recall"],
                linkable_f1=ev["linkable_f1"],
                linkable_share_of_true_pairs=ev["linkable_share_of_true_pairs"],
            )
            last_eval = ev
        prev_labels = r.match.labels
        rows.append(row)
        print(
            f"day {day}: {r.records:,} records -> {r.active_ids:,} people | "
            + (
                f"P={row['precision']:.4f} linkR={row['linkable_recall']:.4f} | "
                if truth is not None
                else ""
            )
            + f"ID unchanged {r.stability['unchanged']:.4f} | {r.seconds:.0f}s"
        )
    return {
        "config": {"world": world, "match": asdict(mc), "settings": settings.as_dict()},
        "days": rows,
        "final": last_eval,
    }


def cmd_run(args: argparse.Namespace) -> None:
    settings = _settings(args)
    meta_path = settings.truth_dir / "meta.json"
    world = json.loads(meta_path.read_text())["config"] if meta_path.exists() else {}
    result = _run(settings, args.days, _profile(args.profile), world)
    out = settings.results_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "run.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")


def cmd_dbt(args: argparse.Namespace) -> None:
    from cip import dbt_build

    sys.exit(0 if dbt_build.build(_settings(args)) else 1)


def cmd_ablate(args: argparse.Namespace) -> None:
    from cip.experiments import ablation, aggregate_seeds

    frames = []
    for seed in args.seeds:
        print(f"ablation, seed {seed} ...", flush=True)
        frames.append(ablation(args.persons, seed).assign(seed=seed))
    df = aggregate_seeds(frames)
    out = _settings(args).results_dir
    out.mkdir(parents=True, exist_ok=True)
    df.to_csv(out / args.out, index=False)
    pd.concat(frames).to_csv(out / args.out.replace(".csv", "_by_seed.csv"), index=False)
    print(df.to_string(index=False, float_format=lambda v: f"{v:.4f}"))


def cmd_bench(args: argparse.Namespace) -> None:
    from cip.pipeline import run_day

    rows = []
    for n in args.sizes:
        with tempfile.TemporaryDirectory(prefix="cip_bench_") as work:
            settings = Settings(data_dir=Path(work))
            write_landing(generate(WorldConfig(n_persons=n, seed=1, days=0)), settings)
            t = time.perf_counter()
            r = run_day(settings, 0, MatchConfig.precision_first())
            sec = time.perf_counter() - t
        rows.append(
            {
                "persons": n,
                "records": r.records,
                "seconds": sec,
                "records_per_second": r.records / sec,
            }
        )
        print(f"{n:,} persons / {r.records:,} records: {sec:.1f}s")
    out = _settings(args).results_dir
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out / "bench.csv", index=False)


def cmd_export(args: argparse.Namespace) -> None:
    from cip import export_delta

    s = _settings(args)
    counts = export_delta.export(s, include_pii=args.include_pii)
    check = export_delta.verify(s)
    for name, (expected, got) in check.items():
        print(f"delta {name}: {got:,} rows ({'ok' if expected == got else 'MISMATCH'})")
    sys.exit(0 if all(a == b for a, b in check.values()) and counts else 1)


def cmd_erase(args: argparse.Namespace) -> None:
    from cip import privacy

    s = _settings(args)
    summary = privacy.erase_person(s, args.person_id)
    if not summary["found"]:
        print("no such person")
        sys.exit(1)
    residue = privacy.verify_erasure(s, args.person_id, summary["record_keys"])
    print(f"erased {summary['erased_records']} records; residue: {residue}")
    sys.exit(0 if all(v == 0 for v in residue.values()) else 2)


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from cip.api.app import create_app

    uvicorn.run(create_app(_settings(args)), host=args.host, port=args.port)


def cmd_report(args: argparse.Namespace) -> None:
    from cip import report

    out = _settings(args).results_dir
    run = json.loads((out / "run.json").read_text(encoding="utf-8"))
    ablation = pd.read_csv(out / "ablation.csv") if (out / "ablation.csv").exists() else None
    bench = pd.read_csv(out / "bench.csv") if (out / "bench.csv").exists() else None
    Path("docs").mkdir(exist_ok=True)
    Path("docs/evaluation.md").write_text(
        report.evaluation_md(run, ablation, bench), encoding="utf-8"
    )
    readme = Path("README.md")
    if readme.exists() and report.START in readme.read_text(encoding="utf-8"):
        report.update_readme(readme, report.headline(run))
    print("wrote docs/evaluation.md")


def cmd_demo(args: argparse.Namespace) -> None:
    from cip import dbt_build, export_delta

    settings = _settings(args)
    if settings.data_dir.exists() and not args.keep:
        import shutil

        shutil.rmtree(settings.data_dir)
    world = WorldConfig(n_persons=args.persons, seed=args.seed, days=args.days)
    write_landing(generate(world), settings)
    # hub thresholds scale with volume: ~40 records per store number at 6,000 people
    scaled = Settings(
        data_dir=settings.data_dir,
        hub_min_records=max(8, int(40 * args.persons / 6000)),
        hub_min_distinct_names=max(5, int(12 * args.persons / 6000)),
    )
    result = _run(scaled, args.days, _profile(args.profile), asdict(world))
    out = scaled.results_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "run.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    ok = dbt_build.build(scaled)
    counts = export_delta.export(scaled)
    print(f"dbt build: {'passed' if ok else 'FAILED'} | delta tables: {counts}")
    sys.exit(0 if ok else 1)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="cip", description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    p.add_argument("--data-dir", default=os.environ.get("CIP_DATA_DIR", "data"))
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate", help="generate a synthetic world into the landing zone")
    g.add_argument("--persons", type=int, default=6000)
    g.add_argument("--seed", type=int, default=7)
    g.add_argument("--days", type=int, default=4)
    g.add_argument("--drift-day", type=int, default=None, help="CRM renames `phone` from this day")
    g.set_defaults(fn=cmd_generate)

    r = sub.add_parser("run", help="run the daily pipeline for days 0..N")
    r.add_argument("--days", type=int, default=4)
    r.add_argument("--profile", choices=["precision_first", "balanced"], default="precision_first")
    r.set_defaults(fn=cmd_run)

    sub.add_parser("dbt", help="build the gold layer with dbt").set_defaults(fn=cmd_dbt)

    a = sub.add_parser("ablate", help="measure each component's contribution")
    a.add_argument("--persons", type=int, default=3000)
    a.add_argument("--seeds", type=int, nargs="+", default=[7, 21, 22])
    a.add_argument("--out", default="ablation.csv", help="file name inside results/")
    a.set_defaults(fn=cmd_ablate)

    b = sub.add_parser("bench", help="time the day-0 pipeline at several sizes")
    b.add_argument("--sizes", type=int, nargs="+", default=[2000, 6000, 12000])
    b.set_defaults(fn=cmd_bench)

    e = sub.add_parser("export-delta", help="export the gold layer as Delta tables")
    e.add_argument("--include-pii", action="store_true")
    e.set_defaults(fn=cmd_export)

    x = sub.add_parser("erase", help="erase one person from every layer")
    x.add_argument("person_id")
    x.set_defaults(fn=cmd_erase)

    s = sub.add_parser("serve", help="serve the lookup API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)

    sub.add_parser("report", help="render docs/evaluation.md from results/").set_defaults(
        fn=cmd_report
    )

    d = sub.add_parser("demo", help="the whole pipeline end to end")
    d.add_argument("--persons", type=int, default=6000)
    d.add_argument("--seed", type=int, default=7)
    d.add_argument("--days", type=int, default=4)
    d.add_argument("--profile", choices=["precision_first", "balanced"], default="precision_first")
    d.add_argument("--keep", action="store_true", help="do not wipe the data directory first")
    d.set_defaults(fn=cmd_demo)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
