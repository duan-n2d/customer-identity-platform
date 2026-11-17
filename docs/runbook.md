# Runbook

| Situation | Signal | Action |
|---|---|---|
| A source file is quarantined | `quality` quarantine table, alert in `alerts.jsonl` | Fix upstream; re-land that day (loads are idempotent) |
| `largest_cluster` alert | monitor warn/error | Look for a new hub number; check `resolve.hub_identifiers` |
| `singleton_share` jumps | monitor | Usually a source column rename (schema drift); check contracts |
| Merge/split rate spikes | monitor | Compare `resolve.person_id_events` against yesterday |
| Erasure request | | `cip erase <person_id>` (exit 0 only if residue is zero) or the admin API |
| dbt test fails | `cip dbt` exits 1 | Read the failing test; singular tests guard hubs and PII |
| Airflow run fails at `quality_gate` | a monitor is at `fail` | Nothing was published; fix the cause (see monitor rows above), clear the task and rerun |
