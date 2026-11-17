# Databricks notebook source
# MAGIC %md
# MAGIC # Customer Identity Platform: verify the Delta export on Databricks
# MAGIC
# MAGIC Runs the whole pipeline on the workspace's own compute, exports the gold layer as Delta
# MAGIC into a Unity Catalog **Volume**, registers it as managed tables, and checks:
# MAGIC 1. row counts in Databricks equal the warehouse counts,
# MAGIC 2. the tables are real Delta tables (history, schema),
# MAGIC 3. an erasure `DELETE` works and is visible in the table history.
# MAGIC
# MAGIC Works on Databricks Free Edition (serverless). Run top to bottom.

# COMMAND ----------

# MAGIC %pip install --quiet "git+https://github.com/duan-n2d/customer-identity-platform.git" dbt-duckdb

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("catalog", "workspace")
dbutils.widgets.text("schema", "cip")
dbutils.widgets.text("persons", "3000")
CATALOG = dbutils.widgets.get("catalog")
SCHEMA = dbutils.widgets.get("schema")
PERSONS = dbutils.widgets.get("persons")

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")
spark.sql(f"CREATE VOLUME IF NOT EXISTS {CATALOG}.{SCHEMA}.delta_export")
VOLUME = f"/Volumes/{CATALOG}/{SCHEMA}/delta_export"
print(VOLUME)

# COMMAND ----------

# DuckDB needs a normal local disk (Volumes do not support random writes), so the pipeline runs
# in local scratch space and only the finished Delta folders are copied to the Volume.
import json
import subprocess
import sys
from pathlib import Path

WORK = "/local_disk0/cip_work"
run = subprocess.run(
    [sys.executable, "-m", "cip.cli", "--data-dir", WORK, "demo", "--persons", PERSONS, "--days", "2"],
    capture_output=True,
    text=True,
)
print(run.stdout[-3000:])
print(run.stderr[-2000:])
assert run.returncode == 0, "cip demo failed (dbt checks or pipeline)"

# COMMAND ----------

dbutils.fs.rm(f"{VOLUME}/delta", recurse=True)
dbutils.fs.cp(f"file:{WORK}/delta", f"{VOLUME}/delta", recurse=True)
manifest = json.loads(Path(f"{WORK}/delta/_manifest.json").read_text())
print(manifest)

# COMMAND ----------

# The manifest holds the row count each table had in the DuckDB warehouse at export time.
rows = []
for name, want in manifest["rows"].items():
    df = spark.read.format("delta").load(f"{VOLUME}/delta/{name}")
    df.write.mode("overwrite").saveAsTable(f"{CATALOG}.{SCHEMA}.{name}")
    got = spark.table(f"{CATALOG}.{SCHEMA}.{name}").count()
    rows.append((name, want, got, "ok" if want == got else "MISMATCH"))
display(spark.createDataFrame(rows, ["table", "expected_rows", "databricks_rows", "status"]))
assert all(r[3] == "ok" for r in rows)

# COMMAND ----------

display(spark.sql(f"DESCRIBE DETAIL {CATALOG}.{SCHEMA}.dim_customer_public"))

# COMMAND ----------

# Erasure on Delta: a DELETE is a new table version. The old files remain until VACUUM runs
# after the retention window, which is why the project's erasure docs say DELETE + VACUUM.
victim = spark.sql(f"SELECT person_id FROM {CATALOG}.{SCHEMA}.dim_customer_public LIMIT 1").first()[0]
spark.sql(f"DELETE FROM {CATALOG}.{SCHEMA}.dim_customer_public WHERE person_id = '{victim}'")
left = spark.sql(
    f"SELECT count(*) FROM {CATALOG}.{SCHEMA}.dim_customer_public WHERE person_id = '{victim}'"
).first()[0]
print("rows left for the erased person:", left)
assert left == 0
display(spark.sql(f"DESCRIBE HISTORY {CATALOG}.{SCHEMA}.dim_customer_public"))
