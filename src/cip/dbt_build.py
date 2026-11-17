"""Run the dbt project (survivorship, marts, tests) on the warehouse the pipeline wrote."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from cip.config import Settings

DBT_DIR = Path(__file__).resolve().parents[2] / "dbt"


def build(settings: Settings, *, select: str | None = None) -> bool:
    """Run ``dbt build`` (models + tests). Returns True if everything passed.

    dbt runs in a child process: dbt-duckdb keeps a read-write connection open for the life of
    its process, which would block the API and the pipeline from opening the same file here.
    """
    work = settings.data_dir.resolve() / "dbt"
    args = [
        "build",
        "--profiles-dir", str(DBT_DIR),
        "--project-dir", str(DBT_DIR),
        "--target-path", str(work / "target"),
        "--log-path", str(work / "logs"),
    ]  # fmt: skip
    if select:
        args += ["--select", select]
    env = {**os.environ, "CIP_DB_PATH": str(settings.db_path.resolve())}
    code = (
        "import sys; from dbt.cli.main import dbtRunner; "
        "sys.exit(0 if dbtRunner().invoke(sys.argv[1:]).success else 1)"
    )
    return subprocess.run([sys.executable, "-c", code, *args], env=env).returncode == 0
