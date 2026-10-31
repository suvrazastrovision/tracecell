from io import BytesIO
from types import SimpleNamespace

import pandas as pd
import pytest

bigquery = pytest.importorskip("google.cloud.bigquery")

from pbmc_platform.load_warehouse import load_bigquery


class FakeBigQuery:
    """In-memory service double using real BigQuery configuration objects."""

    def __init__(self):
        self.tables = {}
        self.loads = []
        self.location = "europe-west1"
        self.fail_once = None

    def create_dataset(self, dataset, exists_ok):
        assert exists_ok
        return SimpleNamespace(location=self.location)

    def create_table(self, table, exists_ok):
        assert exists_ok
        table_id = f"{table.project}.{table.dataset_id}.{table.table_id}"
        self.tables.setdefault(table_id, {
            "schema": table.schema, "description": table.description,
            "rows": pd.DataFrame(columns=[field.name for field in table.schema]),
        })
        saved = self.tables[table_id]
        return SimpleNamespace(schema=saved["schema"], description=saved["description"], num_rows=len(saved["rows"]))

    def load_table_from_file(self, handle, table_id, job_config, location, job_id_prefix):
        assert job_config.source_format == bigquery.SourceFormat.PARQUET
        assert job_config.write_disposition == bigquery.WriteDisposition.WRITE_EMPTY
        assert job_config.create_disposition == bigquery.CreateDisposition.CREATE_NEVER
        assert location == "europe-west1"
        self.loads.append(table_id)
        if self.fail_once and self.fail_once in table_id:
            self.fail_once = None
            raise RuntimeError("Injected load failure")
        assert self.tables[table_id]["rows"].empty
        self.tables[table_id]["rows"] = pd.read_parquet(BytesIO(handle.read()))
        return SimpleNamespace(job_id=job_id_prefix + "fixture", result=lambda timeout: None)

    def list_rows(self, table_id):
        return self.tables[table_id]["rows"].to_dict(orient="records")


def test_bigquery_load_then_retry_does_not_duplicate(warehouse_release):
    plan, _ = warehouse_release
    client = FakeBigQuery()
    first = load_bigquery(plan, "portfolio-project", client=client)
    second = load_bigquery(plan, "portfolio-project", client=client)
    assert len(client.loads) == 3
    assert all(not table["reused"] for table in first.values())
    assert all(table["reused"] for table in second.values())
    assert first["cells"]["rows"] == 3


def test_partial_failure_reuses_completed_tables(warehouse_release):
    plan, _ = warehouse_release
    client = FakeBigQuery()
    client.fail_once = "genes_r_"
    with pytest.raises(RuntimeError, match="Injected load"):
        load_bigquery(plan, "portfolio-project", client=client)
    results = load_bigquery(plan, "portfolio-project", client=client)
    assert results["cells"]["reused"]
    assert not results["genes"]["reused"]
    assert not results["dataset_summary"]["reused"]
    assert len(client.loads) == 4  # Three successful loads plus the injected failure.


def test_dataset_location_mismatch_fails_before_upload(warehouse_release):
    plan, _ = warehouse_release
    client = FakeBigQuery()
    client.location = "US"
    with pytest.raises(ValueError, match="different location"):
        load_bigquery(plan, "portfolio-project", client=client)
    assert client.loads == []


def test_existing_table_with_different_owner_is_not_overwritten(warehouse_release):
    plan, _ = warehouse_release
    client = FakeBigQuery()
    load_bigquery(plan, "portfolio-project", client=client)
    next(iter(client.tables.values()))["description"] = "different source"
    with pytest.raises(ValueError, match="different source contract"):
        load_bigquery(plan, "portfolio-project", client=client)
    assert len(client.loads) == 3


def test_remote_modification_is_detected_even_if_row_count_matches(warehouse_release):
    plan, _ = warehouse_release
    client = FakeBigQuery()
    load_bigquery(plan, "portfolio-project", client=client)
    next(iter(client.tables.values()))["rows"].loc[0, "total_counts"] = 999
    with pytest.raises(ValueError, match="Remote rows differ"):
        load_bigquery(plan, "portfolio-project", client=client)
    assert len(client.loads) == 3


def test_unsafe_identifiers_are_rejected_without_service_calls(warehouse_release):
    plan, _ = warehouse_release
    client = FakeBigQuery()
    with pytest.raises(ValueError, match="Invalid project"):
        load_bigquery(plan, "bad`project", client=client)
    assert client.tables == {}
