"""Data contracts: a YAML description of what each source promises, and the checks that enforce it.

A delivery that breaks the contract in a way that would corrupt matching (a missing column, a
non-unique key, a primary key that is null, unparseable dates) is *quarantined* instead of loaded.
Softer problems (null rates drifting, an unexpected column, a volume swing) raise a warning but
the data still loads, because dropping data on a soft signal does more harm than the signal.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yaml

from cip.generator.world import BASE_DATE


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    type: str  # string | date | timestamp
    nullable: bool
    max_null_rate: float | None


@dataclass(frozen=True)
class Contract:
    source: str
    description: str
    owner: str
    primary_key: str
    columns: dict[str, ColumnSpec]
    max_lag_days: int
    min_ratio_vs_median: float
    max_ratio_vs_median: float


@dataclass(frozen=True)
class Finding:
    run_day: int
    source: str
    check: str
    severity: str  # "error" quarantines the delivery, "warn" only alerts
    passed: bool
    observed: str
    threshold: str
    detail: str = ""


def load_contract(path: Path) -> Contract:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    cols = {
        name: ColumnSpec(
            name=name,
            type=spec["type"],
            nullable=bool(spec.get("nullable", True)),
            max_null_rate=spec.get("max_null_rate"),
        )
        for name, spec in raw["columns"].items()
    }
    if raw["primary_key"] not in cols:
        raise ValueError(f"{path}: primary_key {raw['primary_key']!r} is not a declared column")
    vol = raw.get("volume", {})
    return Contract(
        source=raw["source"],
        description=raw.get("description", ""),
        owner=raw.get("owner", ""),
        primary_key=raw["primary_key"],
        columns=cols,
        max_lag_days=int(raw.get("freshness", {}).get("max_lag_days", 1)),
        min_ratio_vs_median=float(vol.get("min_ratio_vs_median", 0.0)),
        max_ratio_vs_median=float(vol.get("max_ratio_vs_median", 1e9)),
    )


def load_contracts(directory: Path) -> dict[str, Contract]:
    contracts = [load_contract(p) for p in sorted(Path(directory).glob("*.yml"))]
    return {c.source: c for c in contracts}


def _unparseable_share(series: pd.Series, kind: str) -> float:
    values = series.dropna()
    if values.empty:
        return 0.0
    if kind == "date":
        parsed = pd.to_datetime(values, format="%Y-%m-%d", errors="coerce")
    else:
        parsed = pd.to_datetime(values, errors="coerce")
    return float(parsed.isna().mean())


def check_delivery(
    contract: Contract,
    df: pd.DataFrame,
    *,
    day: int,
    history_counts: list[int] | None = None,
    loaded_keys: set[str] | None = None,
) -> list[Finding]:
    """Run every contract check on one delivery and return all findings (passed or not)."""
    src, out = contract.source, []

    def add(check: str, severity: str, passed: bool, observed, threshold, detail: str = "") -> None:
        out.append(
            Finding(day, src, check, severity, passed, str(observed), str(threshold), detail)
        )

    declared, present = set(contract.columns), set(df.columns)
    missing, extra = sorted(declared - present), sorted(present - declared)
    add(
        "schema.required_columns", "error", not missing, missing or "all present", "all present", ""
    )
    hint = ""
    if missing and extra:
        hint = f"possible rename: {missing} -> {extra}"
    add("schema.unexpected_columns", "warn", not extra, extra or "none", "none", hint)
    if missing:  # nothing else can be checked meaningfully
        return out

    pk = contract.primary_key
    pk_nulls = int(df[pk].isna().sum())
    add("primary_key.not_null", "error", pk_nulls == 0, pk_nulls, 0)
    dupes = int(df[pk].dropna().duplicated().sum())
    add("primary_key.unique_in_delivery", "error", dupes == 0, dupes, 0)
    if loaded_keys is not None:
        again = int(df[pk].isin(loaded_keys).sum())
        add(
            "primary_key.redelivery",
            "warn",
            again == 0,
            again,
            0,
            "already loaded; skipped so the load stays idempotent" if again else "",
        )

    for name, spec in contract.columns.items():
        col = df[name]
        null_rate = float(col.isna().mean()) if len(df) else 0.0
        if not spec.nullable:
            add(f"nulls.{name}", "error", null_rate == 0.0, f"{null_rate:.3f}", "0.000")
        elif spec.max_null_rate is not None:
            add(
                f"nulls.{name}",
                "warn",
                null_rate <= spec.max_null_rate,
                f"{null_rate:.3f}",
                f"<= {spec.max_null_rate:.3f}",
            )
        if spec.type in ("date", "timestamp"):
            bad = _unparseable_share(col, spec.type)
            add(
                f"types.{name}",
                "error",
                bad == 0.0,
                f"{bad:.3f}",
                "0.000",
                f"values that do not parse as {spec.type}",
            )

    if len(df) and "ingested_at" in df.columns:
        newest = pd.to_datetime(df["ingested_at"], errors="coerce").max()
        batch_date: date = BASE_DATE + timedelta(days=day)
        lag = (batch_date - newest.date()).days if pd.notna(newest) else 999
        add(
            "freshness",
            "error",
            lag <= contract.max_lag_days,
            f"{lag}d",
            f"<= {contract.max_lag_days}d",
        )

    incremental = [c for c in (history_counts or []) if c is not None]
    if day >= 2 and len(incremental) >= 1:  # day 0 is the backlog; compare increments only
        median = statistics.median(incremental)
        ratio = len(df) / median if median else 1.0
        ok = contract.min_ratio_vs_median <= ratio <= contract.max_ratio_vs_median
        add(
            "volume",
            "warn",
            ok,
            f"{ratio:.2f}x median",
            f"{contract.min_ratio_vs_median}x..{contract.max_ratio_vs_median}x",
        )
    return out


def has_errors(findings: list[Finding]) -> bool:
    return any(f.severity == "error" and not f.passed for f in findings)


def to_frame(findings: list[Finding]) -> pd.DataFrame:
    return pd.DataFrame([f.__dict__ for f in findings])
