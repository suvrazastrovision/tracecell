import json
from pathlib import Path

import pytest

from pbmc_platform.load_warehouse import load_local, prepare_load, run_load


def test_dry_run_validates_without_credentials_or_client(warehouse_release, tmp_path):
    plan, processed = warehouse_release
    result = run_load(processed_dir=processed, artifact_dir=tmp_path, dry_run=True, client=object())
    assert result == plan
    audit = json.loads(next((tmp_path / "warehouse_runs").glob("*.json")).read_text())
    assert audit["status"] == "dry_run"


def test_local_load_and_replay_preserve_rows(warehouse_release, tmp_path):
    duckdb = pytest.importorskip("duckdb")
    plan, _ = warehouse_release
    database = tmp_path / "warehouse.duckdb"
    first = load_local(plan, database)
    second = load_local(plan, database)
    assert all(not table["reused"] for table in first.values())
    assert all(table["reused"] for table in second.values())
    table = plan["tables"]["cells"]["table_name"]
    with duckdb.connect(str(database)) as connection:
        assert connection.execute(f'SELECT count(*) FROM pbmc_raw."{table}"').fetchone()[0] == 3


def test_local_readback_detects_modified_values(warehouse_release, tmp_path):
    duckdb = pytest.importorskip("duckdb")
    plan, _ = warehouse_release
    database = tmp_path / "warehouse.duckdb"
    load_local(plan, database)
    table = plan["tables"]["cells"]["table_name"]
    with duckdb.connect(str(database)) as connection:
        connection.execute(f'UPDATE pbmc_raw."{table}" SET total_counts = 999')
    with pytest.raises(ValueError, match="Remote rows differ"):
        load_local(plan, database)


def test_modified_parquet_is_rejected_before_loading(warehouse_release):
    plan, processed = warehouse_release
    Path(plan["tables"]["cells"]["file"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum mismatch"):
        prepare_load(processed)


def test_modified_identity_is_rejected(warehouse_release):
    plan, processed = warehouse_release
    path = processed / "releases" / plan["release_id"] / "release.json"
    metadata = json.loads(path.read_text())
    metadata["identity"]["qc_policy"]["policy_version"] = "tampered"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="recorded identity"):
        prepare_load(processed)


def test_pointer_cannot_escape_release_directory(warehouse_release):
    plan, processed = warehouse_release
    (processed / "current.json").write_text(json.dumps({"release_id": plan["release_id"], "path": "../../other"}))
    with pytest.raises(ValueError, match="Current pointer"):
        prepare_load(processed)


def test_live_load_requires_explicit_project(warehouse_release, tmp_path):
    _, processed = warehouse_release
    with pytest.raises(ValueError, match="GOOGLE_CLOUD_PROJECT"):
        run_load(processed_dir=processed, artifact_dir=tmp_path)
    audit = json.loads(next((tmp_path / "warehouse_runs").glob("*.json")).read_text())
    assert audit["status"] == "failed"
