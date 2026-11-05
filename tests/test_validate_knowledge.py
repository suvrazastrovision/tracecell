"""Independent validation against real fixture releases and read-only fake services."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from pbmc_platform import generate_knowledge as generator
from pbmc_platform import validate_knowledge as validator
from pbmc_platform.load_warehouse import prepare_load, table_description
from test_generate_knowledge import source_bundle


@pytest.fixture
def validation_bundle(source_bundle, tmp_path):
    kwargs, metric, exclusions = source_bundle
    release_id = kwargs["release_id"]
    processed = tmp_path / "knowledge-processed"
    plan = prepare_load(processed, release_id)
    cells = pd.read_parquet(plan["tables"]["cells"]["file"])
    genes = pd.read_parquet(plan["tables"]["genes"]["file"])
    metric.update({f"cells_{reason}": int(cells[reason].sum()) for reason in generator.REASONS})
    metric.update(mean_counts_per_cell=float(cells.total_counts.mean()),
                  mean_genes_per_cell=float(cells.n_genes_by_counts.mean()),
                  mean_pct_counts_mito=float(cells.pct_counts_mito.mean()))
    dataset = f"pbmc_analytics_r_{release_id}"
    project = kwargs["project"]
    rows, definitions = {}, {}
    for name, spec in plan["tables"].items():
        table_id = f"{project}.pbmc_raw.{spec['table_name']}"
        frame = pd.read_parquet(spec["file"])
        rows[table_id] = frame.to_dict("records")
        definitions[table_id] = SimpleNamespace(
            description=table_description(plan, name),
            schema=[SimpleNamespace(name=f["name"], field_type=f["type"], mode="NULLABLE") for f in spec["schema"]])
    cohort = cells[cells.qc_pass].copy()
    cohort.insert(0, "release_id", release_id)
    for name, values, columns in [("mart_dataset_qc", [metric], list(metric)),
                                   ("mart_qc_exclusions", exclusions, list(exclusions[0])),
                                   ("mart_cohort_cells", cohort.to_dict("records"), list(cohort.columns))]:
        table_id = f"{project}.{dataset}.{name}"
        rows[table_id] = values
        definitions[table_id] = SimpleNamespace(schema=[SimpleNamespace(name=c) for c in columns])

    class Reader:
        def __init__(self):
            self.rows = rows
            self.versions = {name: "version-1" for name in rows}
            self.reads = []

        def get_dataset(self, name):
            assert name in (f"{project}.pbmc_raw", f"{project}.{dataset}")
            return SimpleNamespace(location="europe-west1")

        def get_table(self, name):
            return SimpleNamespace(**vars(definitions[name]), table_type="TABLE",
                                   etag=self.versions[name], modified="fixture-time", num_rows=len(rows[name]))

        def list_rows(self, name, max_results):
            self.reads.append(name)
            return deepcopy(rows[name][:max_results])

    reader = Reader()
    kwargs["bigquery_client"] = reader
    storage = kwargs["storage_client"]
    audit_name = f"runs/cloud_dbt/{kwargs['dbt_run_id']}.json"
    audit, generation = storage.remote.objects[audit_name]
    audit = json.loads(audit)
    audit["raw_dataset"] = "pbmc_raw"
    storage.remote.objects[audit_name] = (json.dumps(audit).encode(), generation)
    manifest_name = f"runs/dbt/{kwargs['dbt_run_id']}/manifest.json"
    manifest_data, generation = storage.remote.objects[manifest_name]
    manifest_data = json.loads(manifest_data)
    for node in manifest_data["nodes"].values():
        if node["resource_type"] == "model":
            node["database"] = project
    storage.remote.objects[manifest_name] = (json.dumps(manifest_data).encode(), generation)
    generated = generator.run_generate(**kwargs)
    reader.reads.clear()
    validation_args = {k: kwargs[k] for k in ("project", "bucket", "release_id", "dbt_run_id", "storage_client", "bigquery_client")}
    validation_args.update(package_path=generated["package_path"], output_dir=tmp_path / "validation")
    return validation_args, processed, reader


def change_package(kwargs, action):
    path = Path(kwargs["package_path"])
    package = json.loads(path.read_text())
    action(package)
    path.write_text(json.dumps(package))


def test_success_preserves_draft_and_writes_complete_verified_bundle(validation_bundle):
    kwargs, _, reader = validation_bundle
    original = Path(kwargs["package_path"]).read_bytes()
    writes = list(kwargs["storage_client"].remote.writes)
    result = validator.run_validate(**kwargs)
    verified = json.loads(Path(result["package_path"]).read_text())
    audit = json.loads(Path(result["validation_path"]).read_text())
    assert result["status"] == verified["package_status"] == audit["status"] == "verified"
    assert verified["limitations"] == generator.LIMITATIONS[1:]
    assert audit["metrics"]["cells_total"] == 3
    assert audit["cohort_rows"] == 1
    assert len(audit["checks_passed"]) == 6
    assert len(audit["objects"]) == 8 and len(audit["tables"]) == 6
    assert len(set(reader.reads)) == 6
    assert Path(result["report_path"]).read_bytes() == generator.render_report(verified).encode()
    assert Path(kwargs["package_path"]).read_bytes() == original
    assert kwargs["storage_client"].remote.writes == writes
    for name, expected in audit["outputs"].items():
        assert validator.sha256_file(Path(result["validation_path"]).parent / name) == expected


@pytest.mark.parametrize("fault", ["package_total", "wrong_policy", "dataset_description", "wrong_mart_ref",
                                   "unresolved_ref", "removed_limitations", "forged_verified_status",
                                   "artifact_hash", "failed_dbt", "dbt_wrong_project", "cross_bucket",
                                   "warehouse_metric", "cohort_member",
                                   "missing_cohort", "raw_tampering", "duplicate_exclusion",
                                   "object_changed", "table_changed"])
def test_tampering_and_moving_sources_are_rejected(validation_bundle, fault):
    kwargs, _, reader = validation_bundle
    package = json.loads(Path(kwargs["package_path"]).read_text())
    remote = kwargs["storage_client"].remote
    dataset = f"{kwargs['project']}.pbmc_analytics_r_{kwargs['release_id']}"
    if fault == "package_total":
        change_package(kwargs, lambda p: p["results"]["metrics"].update(cells_total=999))
    elif fault == "wrong_policy":
        change_package(kwargs, lambda p: p["qc_policy"].update(max_mito_pct_exclusive=4))
    elif fault == "dataset_description":
        change_package(kwargs, lambda p: p["dataset"].update(tissue="brain"))
    elif fault == "wrong_mart_ref":
        change_package(kwargs, lambda p: p["results"].update(evidence_refs=["release", "qc_exclusions"]))
    elif fault == "unresolved_ref":
        change_package(kwargs, lambda p: p["dataset"].update(evidence_refs=["missing"]))
    elif fault == "removed_limitations":
        change_package(kwargs, lambda p: p["limitations"].__setitem__(1, "Any disease inference is supported."))
    elif fault == "forged_verified_status":
        change_package(kwargs, lambda p: p.update(package_status="verified"))
    elif fault == "artifact_hash":
        change_package(kwargs, lambda p: p["evidence"]["dbt_manifest"].update(sha256="f" * 64))
    elif fault == "cross_bucket":
        change_package(kwargs, lambda p: p["evidence"]["release"].update(uri="gs://another-bucket/release.json"))
    elif fault == "dbt_wrong_project":
        name = f"runs/dbt/{kwargs['dbt_run_id']}/manifest.json"
        data, generation = remote.objects[name]
        data = json.loads(data)
        data["nodes"]["model.fixture.qc"]["database"] = "another-project"
        data = json.dumps(data).encode()
        remote.objects[name] = (data, generation)
        change_package(kwargs, lambda p: p["evidence"]["dbt_manifest"].update(sha256=validator.hashlib.sha256(data).hexdigest()))
    elif fault == "failed_dbt":
        name = f"runs/dbt/{kwargs['dbt_run_id']}/run_results.json"
        data, generation = remote.objects[name]
        data = json.loads(data)
        data["results"][1]["status"] = "fail"
        data = json.dumps(data).encode()
        remote.objects[name] = (data, generation)
        # Update the draft hash too, so semantic verification must catch the failed test.
        change_package(kwargs, lambda p: p["evidence"]["dbt_results"].update(sha256=validator.hashlib.sha256(data).hexdigest()))
    elif fault == "warehouse_metric":
        reader.rows[f"{dataset}.mart_dataset_qc"][0]["mean_counts_per_cell"] += 1
    elif fault == "cohort_member":
        reader.rows[f"{dataset}.mart_cohort_cells"][0]["cell_id"] = "wrong-cell"
    elif fault == "missing_cohort":
        reader.rows[f"{dataset}.mart_cohort_cells"].clear()
    elif fault == "raw_tampering":
        raw = next(name for name in reader.rows if ".pbmc_raw.cells_" in name)
        reader.rows[raw][0]["total_counts"] += 1
    elif fault == "duplicate_exclusion":
        values = reader.rows[f"{dataset}.mart_qc_exclusions"]
        values[1]["exclusion_reason"] = values[0]["exclusion_reason"]
    elif fault == "object_changed":
        original = remote.get_blob
        object_name = package["evidence"]["dbt_manifest"]["uri"].split(f"gs://{kwargs['bucket']}/")[1]
        calls = []

        def changed(name, *, timeout):
            if name == object_name:
                calls.append(name)
                if len(calls) == 2:
                    data, generation = remote.objects[name]
                    remote.objects[name] = (data, generation + 100)
            return original(name, timeout=timeout)

        remote.get_blob = changed
    else:
        original = reader.list_rows

        def changed(name, max_results):
            values = original(name, max_results)
            if name.endswith("mart_cohort_cells"):
                reader.versions[name] = "changed-version"
            return values

        reader.list_rows = changed
    with pytest.raises(ValueError, match="Validation failed"):
        validator.run_validate(**kwargs)
    reports = list(Path(kwargs["output_dir"]).glob("*/validation.json"))
    assert len(reports) == 1
    assert json.loads(reports[0].read_text())["status"] == "failed"
    assert not list(Path(kwargs["output_dir"]).glob("*/knowledge.json"))


@pytest.mark.parametrize("fault", ["matrix_measurement", "policy_flag"])
def test_recomputation_rejects_internally_inconsistent_release(validation_bundle, fault):
    kwargs, processed, _ = validation_bundle
    directory = processed / "releases" / kwargs["release_id"]
    metadata = json.loads((directory / "release.json").read_text())
    path = directory / "cells.parquet"
    cells = pd.read_parquet(path)
    if fault == "matrix_measurement":
        cells.loc[0, "total_counts"] += 1
    else:
        cells.loc[0, "high_mito"] = not cells.loc[0, "high_mito"]
    cells.to_parquet(path, index=False)
    with pytest.raises(ValueError, match="differs"):
        validator.recompute_release(directory, metadata)


@pytest.mark.parametrize("payload", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'])
def test_nonstandard_or_ambiguous_json_is_rejected(payload):
    with pytest.raises(ValueError):
        validator.strict_json(payload)


def test_distributed_mean_rounding_is_accepted():
    validator.compare_aggregate({"mean_counts_per_cell": 2366.900370370373},
                                {"mean_counts_per_cell": 2366.90037037037}, "QC mart")


def test_changed_mean_is_rejected():
    with pytest.raises(ValueError, match="mean_counts_per_cell"):
        validator.compare_aggregate({"mean_counts_per_cell": 2366.901},
                                    {"mean_counts_per_cell": 2366.90037037037}, "QC mart")


def test_fraction_tolerance_remains_absolute():
    with pytest.raises(ValueError, match="qc_pass_fraction"):
        validator.compare_aggregate({"qc_pass_fraction": 0.5 + 2e-12},
                                    {"qc_pass_fraction": 0.5}, "QC mart")
