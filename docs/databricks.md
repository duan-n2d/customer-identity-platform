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
