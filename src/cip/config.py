"""Central configuration: paths, thresholds and source trust ranking.

Everything tunable lives here so an experiment is one diff, and the evaluation report can
print the exact configuration it was produced with.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Higher number = more trusted when two sources disagree (survivorship).
SOURCE_TRUST: dict[str, int] = {"crm": 4, "loyalty": 3, "ecom": 2, "pos": 1}

SOURCES: tuple[str, ...] = ("pos", "ecom", "crm", "loyalty")


@dataclass(frozen=True)
class Settings:
    """Run configuration. Frozen so a run cannot mutate it halfway."""

    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("CIP_DATA_DIR", "data")))

    # --- hub detection (identifiers shared by too many unrelated records) ---
    hub_min_records: int = 40
    hub_min_distinct_names: int = 12

    # --- matching ---
    predict_threshold: float = 0.50  # keep every pair above this as a graph edge
    cluster_threshold: float = 0.90  # edges below this never merge two records
    max_cluster_size: int = 25  # a "person" with more records than this is suspicious
    em_max_pairs: float = 2e6  # sampling budget for the u-probabilities

    # --- privacy ---
    # HMAC key for identifier tokens. The default exists only so the demo runs offline;
    # a real deployment must set CIP_PII_KEY (the pipeline logs a warning otherwise).
    pii_key: str = field(default_factory=lambda: os.environ.get("CIP_PII_KEY", ""))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "warehouse.duckdb"

    @property
    def landing_dir(self) -> Path:
        return self.data_dir / "landing"

    @property
    def truth_dir(self) -> Path:
        return self.data_dir / "truth"

    @property
    def delta_dir(self) -> Path:
        return self.data_dir / "delta"

    @property
    def results_dir(self) -> Path:
        return Path(os.environ.get("CIP_RESULTS_DIR", "results"))

    def as_dict(self) -> dict:
        d = asdict(self)
        d["data_dir"] = str(self.data_dir)
        d["pii_key"] = "<set>" if self.pii_key else "<default-dev-key>"
        return d
