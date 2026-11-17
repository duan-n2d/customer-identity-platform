# Databricks / Delta notes

`cip export-delta` writes the gold layer as Delta tables with `deltalake`; `verify` reads each
table back and compares row counts. By default only PII-free tables are exported
(`dim_customer_public`, `fct_identity_coverage`, `person_id_events`, `person_registry`);
`--include-pii` adds `dim_customer` and `bridge_person_record` (partitioned by source).

**Verified:** local export and read-back, covered by tests.
**Not verified:** a run in a real Databricks workspace or Unity Catalog.

Planned mapping (untested): register the folders as external tables in Unity Catalog; apply
column masks to the PII tables; implement erasure as `DELETE` followed by `VACUUM` after the
retention window, because a Delta `DELETE` alone leaves the data in older file versions.

## Verifying on a real workspace

[databricks/verify_delta.py](../databricks/verify_delta.py) is a Databricks source notebook that
runs the pipeline on the workspace compute, exports the Delta tables into a Unity Catalog
Volume, registers them as managed tables, checks row counts against the warehouse, and runs an
erasure `DELETE` followed by `DESCRIBE HISTORY`.

1. Workspace -> Import -> upload `databricks/verify_delta.py` (it is detected as a notebook).
2. Attach serverless compute and run all cells. Widgets: `catalog` (default `workspace`),
   `schema` (default `cip`), `persons` (default 3000).

Status: **written but not yet run.** This file will be updated with the real outcome (and any
fixes the run needed) once it has been executed.
