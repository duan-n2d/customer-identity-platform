"""The Airflow DAG imports cleanly and has the intended shape.

Skipped when Airflow is not installed (it does not run natively on Windows); the CI job
``airflow-dag`` installs it and runs this file.
"""

from pathlib import Path

import pytest

pytest.importorskip("airflow")

DAGS = Path(__file__).resolve().parents[1] / "dags"


@pytest.fixture(scope="module")
def dag():
    from airflow.models import DagBag

    bag = DagBag(dag_folder=str(DAGS), include_examples=False)
    assert not bag.import_errors, bag.import_errors
    return bag.dags["cip_daily"]


def test_the_gate_sits_between_matching_and_publishing(dag):
    assert dag.task_ids == ["land_and_resolve", "quality_gate", "dbt_build", "export_delta"]
    assert dag.get_task("quality_gate").upstream_task_ids == {"land_and_resolve"}
    assert dag.get_task("dbt_build").upstream_task_ids == {"quality_gate"}
    assert dag.get_task("export_delta").upstream_task_ids == {"dbt_build"}


def test_runs_are_serialised_because_duckdb_has_one_writer(dag):
    assert dag.max_active_runs == 1
    assert dag.catchup is False
