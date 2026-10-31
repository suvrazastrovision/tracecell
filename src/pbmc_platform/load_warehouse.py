"""Load a verified QC release into immutable BigQuery or local DuckDB tables."""

import argparse
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .ingestion import canonical_json
from .run_qc import validate_release, write_json


TABLE_KEYS = {
    "cells": ["cell_id"],
    "genes": ["gene_id"],
    "dataset_summary": ["dataset_id", "dataset_version", "policy_id", "policy_version"],
}


def validate_identifier(value, kind):
    pattern = r"[a-z][a-z0-9-]{4,28}[a-z0-9]" if kind == "project" else r"[A-Za-z_][A-Za-z0-9_]{0,255}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(f"Invalid {kind} identifier: {value!r}")
    return value


def arrow_type_name(arrow_type):
    if pa.types.is_string(arrow_type) or pa.types.is_large_string(arrow_type):
        return "STRING"
    if pa.types.is_boolean(arrow_type):
        return "BOOLEAN"
    if pa.types.is_integer(arrow_type) and arrow_type.bit_width <= 64:
        return "INTEGER"
    if pa.types.is_floating(arrow_type):
        return "FLOAT"
    raise ValueError(f"Unsupported warehouse column type: {arrow_type}")


def prepare_load(processed_dir="data/processed", release_id=None):
    root = Path(processed_dir).resolve()
    if release_id is None:
        pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
        release_id = pointer["release_id"]
        if pointer.get("path") != f"releases/{release_id}":
            raise ValueError("Current pointer must reference releases/<release_id>")
    if not isinstance(release_id, str) or not re.fullmatch(r"[a-f0-9]{64}", release_id):
        raise ValueError("Expected a full SHA-256 release ID")
    directory = root / "releases" / release_id
    metadata = validate_release(directory)
    identity_hash = hashlib.sha256(canonical_json(metadata["identity"]).encode()).hexdigest()
    if metadata["release_id"] != release_id or identity_hash != release_id:
        raise ValueError("Release ID does not match its recorded identity")

    tables = {}
    for name, keys in TABLE_KEYS.items():
        path = directory / f"{name}.parquet"
        arrow_schema = pq.read_schema(path)
        frame = pd.read_parquet(path)
        if frame.empty or frame[keys].isna().any().any() or frame.duplicated(keys).any():
            raise ValueError(f"Missing, empty, or duplicate keys in {name}")
        tables[name] = {
            "file": str(path),
            "table_name": f"{name}_r_{release_id}",
            "rows": len(frame),
            "sha256": metadata["outputs"][f"{name}.parquet"]["sha256"],
            "schema": [{"name": field.name, "type": arrow_type_name(field.type)} for field in arrow_schema],
        }
    expected = metadata["summary"]
    summary = pd.read_parquet(tables["dataset_summary"]["file"])
    if len(summary) != 1 or summary.iloc[0].to_dict() != expected:
        raise ValueError("Release manifest and exported summary disagree")
    cells = pd.read_parquet(tables["cells"]["file"])
    if (tables["cells"]["rows"] != expected["cells_total"]
            or tables["genes"]["rows"] != expected["features_total"]
            or int(cells.qc_pass.sum()) != expected["cells_pass"]
            or int((~cells.qc_pass).sum()) != expected["cells_fail"]
            or int(cells.total_counts.sum()) != expected["total_counts"]):
        raise ValueError("Exported data does not reconcile with its release summary")
    return {"release_id": release_id, "summary": expected, "tables": tables}


def table_description(plan, name):
    return canonical_json({
        "producer": "pbmc-platform",
        "release_id": plan["release_id"],
        "source_sha256": plan["tables"][name]["sha256"],
    })


def compare_rows(name, actual, expected):
    if set(actual.columns) != set(expected.columns):
        raise ValueError(f"Remote columns differ from local {name}")
    keys = TABLE_KEYS[name]
    actual = actual[expected.columns].sort_values(keys).reset_index(drop=True)
    expected = expected.sort_values(keys).reset_index(drop=True)
    try:
        pd.testing.assert_frame_equal(actual, expected, check_dtype=False, check_exact=True)
    except AssertionError as error:
        raise ValueError(f"Remote rows differ from local {name}") from error


def load_bigquery(plan, project, raw_dataset="pbmc_raw", location="europe-west1", client=None):
    from google.cloud import bigquery

    validate_identifier(project, "project")
    validate_identifier(raw_dataset, "dataset")
    if not re.fullmatch(r"[a-zA-Z0-9-]+", location):
        raise ValueError("Invalid BigQuery location")
    client = client or bigquery.Client(project=project, location=location)
    dataset = bigquery.Dataset(f"{project}.{raw_dataset}")
    dataset.location = location
    existing_dataset = client.create_dataset(dataset, exists_ok=True)
    if existing_dataset.location.lower() != location.lower():
        raise ValueError("Existing raw dataset is in a different location")

    results = {}
    for name, spec in plan["tables"].items():
        table_id = f"{project}.{raw_dataset}.{spec['table_name']}"
        schema = [bigquery.SchemaField(field["name"], field["type"]) for field in spec["schema"]]
        definition = bigquery.Table(table_id, schema=schema)
        definition.description = table_description(plan, name)
        definition.labels = {"application": "pbmc-platform", "release": plan["release_id"][:63]}
        existing = client.create_table(definition, exists_ok=True)
        actual_schema = [(field.name, field.field_type, field.mode) for field in existing.schema]
        expected_schema = [(field.name, field.field_type, field.mode) for field in schema]
        if existing.description != definition.description or actual_schema != expected_schema:
            raise ValueError(f"Existing table has a different source contract: {table_id}")
        reused = existing.num_rows == spec["rows"]
        if existing.num_rows == 0:
            config = bigquery.LoadJobConfig(
                schema=schema, source_format=bigquery.SourceFormat.PARQUET,
                create_disposition=bigquery.CreateDisposition.CREATE_NEVER,
                write_disposition=bigquery.WriteDisposition.WRITE_EMPTY,
            )
            with Path(spec["file"]).open("rb") as handle:
                job = client.load_table_from_file(
                    handle, table_id, job_config=config, location=location,
                    job_id_prefix=f"pbmc_{plan['release_id'][:16]}_{name}_",
                )
                job.result(timeout=300)
            job_id = job.job_id
        elif not reused:
            raise ValueError(f"Existing table row count differs: {table_id}")
        else:
            job_id = None
        # Full readback is intentional for this bounded portfolio dataset.
        # It detects modifications even when row count and metadata still match.
        actual = pd.DataFrame([dict(row.items()) for row in client.list_rows(table_id)])
        compare_rows(name, actual, pd.read_parquet(spec["file"]))
        results[name] = {"table_id": table_id, "rows": spec["rows"], "reused": reused, "job_id": job_id}
    return results


def load_local(plan, database_path="artifacts/pbmc.duckdb", raw_dataset="pbmc_raw"):
    import duckdb

    validate_identifier(raw_dataset, "dataset")
    database_path = Path(database_path).resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    results = {}
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{raw_dataset}"')
        for name, spec in plan["tables"].items():
            table = f'"{raw_dataset}"."{spec["table_name"]}"'
            found = connection.execute(
                "SELECT comment FROM duckdb_tables() WHERE schema_name = ? AND table_name = ?",
                [raw_dataset, spec["table_name"]],
            ).fetchone()
            if found is None:
                connection.execute("BEGIN TRANSACTION")
                try:
                    connection.execute(f"CREATE TABLE {table} AS SELECT * FROM read_parquet(?)", [spec["file"]])
                    description = table_description(plan, name).replace("'", "''")
                    connection.execute(f"COMMENT ON TABLE {table} IS '{description}'")
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
            elif found[0] != table_description(plan, name):
                raise ValueError(f"Existing local table has a different source contract: {table}")
            actual = connection.execute(f"SELECT * FROM {table}").fetchdf()
            compare_rows(name, actual, pd.read_parquet(spec["file"]))
            results[name] = {"table_id": table, "rows": spec["rows"], "reused": found is not None}
    return results


def run_load(target="bigquery", project=None, processed_dir="data/processed", release_id=None,
             raw_dataset="pbmc_raw", location="europe-west1", database_path="artifacts/pbmc.duckdb",
             artifact_dir="artifacts", dry_run=False, client=None):
    audit = {
        "run_id": uuid.uuid4().hex, "target": target,
        "started_at": datetime.now(timezone.utc).isoformat(), "status": "running",
    }
    try:
        validate_identifier(raw_dataset, "dataset")
        plan = prepare_load(processed_dir, release_id)
        audit["release_id"] = plan["release_id"]
        if target not in ("bigquery", "local"):
            raise ValueError("Target must be bigquery or local")
        if dry_run:
            audit["status"] = "dry_run"
            return plan
        if target == "bigquery":
            if not project:
                raise ValueError("Set GOOGLE_CLOUD_PROJECT or pass --project before loading BigQuery")
            results = load_bigquery(plan, project, raw_dataset, location, client)
        else:
            results = load_local(plan, database_path, raw_dataset)
        audit.update(status="success", tables=results, project=project, location=location)
        return {"release_id": plan["release_id"], "target": target, "tables": results, "summary": plan["summary"]}
    except Exception as error:
        audit.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        audit["finished_at"] = datetime.now(timezone.utc).isoformat()
        runs = Path(artifact_dir) / "warehouse_runs"
        runs.mkdir(parents=True, exist_ok=True)
        write_json(runs / f"{audit['run_id']}.json", audit)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("bigquery", "local"), default="bigquery")
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    parser.add_argument("--processed-dir", default="data/processed")
    parser.add_argument("--release-id")
    parser.add_argument("--raw-dataset", default=os.environ.get("PBMC_RAW_DATASET", "pbmc_raw"))
    parser.add_argument("--location", default=os.environ.get("PBMC_BQ_LOCATION", "europe-west1"))
    parser.add_argument("--database-path", default=os.environ.get("PBMC_DUCKDB_PATH", "artifacts/pbmc.duckdb"))
    parser.add_argument("--artifact-dir", default="artifacts")
    parser.add_argument("--dry-run", action="store_true", help="Validate local files and print the plan without cloud calls")
    args = parser.parse_args()
    try:
        result = run_load(
            target=args.target, project=args.project, processed_dir=args.processed_dir,
            release_id=args.release_id, raw_dataset=args.raw_dataset, location=args.location,
            database_path=args.database_path, artifact_dir=args.artifact_dir, dry_run=args.dry_run,
        )
    except Exception as error:
        parser.exit(1, f"Warehouse load failed: {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
