"""Write generated data as a landing zone (Hive-partitioned Parquet) plus the hidden truth.

Layout::

    data/landing/<source>/ingest_day=<d>/part.parquet     what the source systems deliver
    data/truth/persons.parquet, records.parquet           ground truth, used only by evaluation
    data/truth/meta.json                                   config + dataset fingerprint
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from cip.config import Settings
from cip.generator.world import GeneratedData


def fingerprint(data: GeneratedData) -> str:
    """Stable hash of the generated data: same config and seed must give the same fingerprint."""
    h = hashlib.sha256()
    for name in sorted(data.sources):
        frame = data.sources[name].sort_index(axis=1)
        h.update(name.encode())
        h.update(pd.util.hash_pandas_object(frame.astype(str), index=False).values.tobytes())
    h.update(pd.util.hash_pandas_object(data.truth_records.astype(str), index=False).values.tobytes())
    return h.hexdigest()[:16]


def _apply_drift(source: str, frame: pd.DataFrame, day: int, drift_day: int | None) -> pd.DataFrame:
    """From ``drift_day`` the CRM feed delivers ``phone_number`` instead of ``phone``."""
    if source == "crm" and drift_day is not None and day >= drift_day:
        return frame.rename(columns={"phone": "phone_number"})
    return frame


def write_landing(data: GeneratedData, settings: Settings) -> dict:
    landing = settings.landing_dir
    truth = settings.truth_dir
    truth.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for source, frame in data.sources.items():
        for day, part in frame.groupby("ingest_day"):
            out = landing / source / f"ingest_day={int(day)}"
            out.mkdir(parents=True, exist_ok=True)
            part = part.drop(columns=["ingest_day"])
            part = _apply_drift(source, part, int(day), data.config.drift_day)
            part.to_parquet(out / "part.parquet", index=False)
        counts[source] = len(frame)
    data.truth_persons.to_parquet(truth / "persons.parquet", index=False)
    data.truth_records.to_parquet(truth / "records.parquet", index=False)
    meta = {"config": asdict(data.config), "fingerprint": fingerprint(data), "records": counts}
    (truth / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def landing_files(settings: Settings, source: str, day: int) -> list[Path]:
    return sorted((settings.landing_dir / source / f"ingest_day={day}").glob("*.parquet"))
