"""Bounded, versioned application inputs. No client-selected resource paths."""
from datetime import datetime, timezone
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_BYTES = 250 * 1024 * 1024
MAX_CELLS = 20000
MAX_GENES = 40000
MAX_NNZ = 10000000
REASONS = ("zero_counts", "low_genes", "high_genes", "high_mito")


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Upload(Strict):
    filename: str = Field(pattern=r"^[A-Za-z0-9_.-]+\.h5ad$", max_length=120)
    size: int = Field(gt=0, le=MAX_BYTES)
    public_data: Literal[True]


class Configuration(Strict):
    schema_version: Literal["2.0.0"] = "2.0.0"
    species: Literal["human", "mouse"]
    counts: str = Field(min_length=1, max_length=128)
    gene_ids: str = Field(min_length=1, max_length=128)
    gene_symbols: str = Field(min_length=1, max_length=128)
    mito_mode: Literal["prefix", "column"]
    mito_value: str = Field(min_length=1, max_length=128)
    min_genes: int = Field(ge=0, le=MAX_GENES)
    max_genes: int = Field(gt=0, le=MAX_GENES + 1)
    max_mito_pct: float = Field(gt=0, le=100)
    tissue: str = Field(min_length=1, max_length=200)
    reference_genome: str = Field(min_length=1, max_length=100)
    attribution: str = Field(min_length=1, max_length=500)
    source: str = Field(min_length=1, max_length=1000)
    single_sample: Literal[True]
    public_data: Literal[True]

    @model_validator(mode="after")
    def thresholds(self):
        if self.min_genes >= self.max_genes:
            raise ValueError("Minimum genes must be smaller than maximum genes")
        return self

    def mappings(self):
        return self.model_dump(include={"species", "counts", "gene_ids", "gene_symbols", "mito_mode", "mito_value"})


class Submission(Strict):
    upload_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_-]{8,100}$")
    configuration: Configuration

