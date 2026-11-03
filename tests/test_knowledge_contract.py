"""Offline checks for the Stage 5.1 public package contract, not cloud evidence."""

from copy import deepcopy
import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schemas/pbmc-knowledge-v1.schema.json").read_text())
EXAMPLE = json.loads((ROOT / "examples/pbmc-knowledge-v1.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)


def test_schema_and_illustrative_example_are_structurally_valid():
    Draft202012Validator.check_schema(SCHEMA)
    VALIDATOR.validate(EXAMPLE)
    assert EXAMPLE["package_status"] == "illustrative"


@pytest.mark.parametrize("path,value", [
    (("schema_version",), "2.0.0"),
    (("identity", "release_id"), "not-a-release"),
    (("results", "metrics", "cells_pass"), -1),
    (("results", "metrics", "qc_pass_fraction"), 1.1),
    (("results", "evidence_refs"), []),
    (("results", "exclusions", "reasons_may_overlap"), False),
    (("evidence", "release", "sha256"), "bad-checksum"),
    (("dbt", "status"), "failed"),
    (("unexpected_field",), True),
])
def test_invalid_structures_are_rejected(path, value):
    package = deepcopy(EXAMPLE)
    target = package
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert not VALIDATOR.is_valid(package)


def test_missing_evidence_is_rejected():
    package = deepcopy(EXAMPLE)
    del package["evidence"]
    assert not VALIDATOR.is_valid(package)


def test_semantic_gate_is_required_in_addition_to_schema():
    # JSON Schema deliberately cannot prove arithmetic or source references.
    package = deepcopy(EXAMPLE)
    package["results"]["metrics"]["cells_total"] += 1
    package["results"]["evidence_refs"] = ["nonexistent_source"]
    VALIDATOR.validate(package)
