"""Shared data contracts for normalized benchmark examples."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class NormalizedSchema:
    """Inline schema metadata shared across benchmarks."""

    table_names: list[str]
    table_names_original: list[str]
    column_names: list[tuple[int, str]]
    column_names_original: list[tuple[int, str]]
    column_types: list[str]
    primary_keys: list[int | tuple[int, ...]]
    foreign_keys: list[tuple[int, int]]

    def to_dict(self) -> dict[str, object]:
        return {
            "table_names": self.table_names,
            "table_names_original": self.table_names_original,
            "column_names": self.column_names,
            "column_names_original": self.column_names_original,
            "column_types": self.column_types,
            "primary_keys": self.primary_keys,
            "foreign_keys": self.foreign_keys,
        }


@dataclass(frozen=True)
class NormalizedExample:
    """One benchmark example in the shared downstream-friendly shape."""

    example_id: str
    benchmark: str
    subset: str
    source_id: str | None
    db_id: str
    question: str
    gold_sql: str
    sqlite_path: str
    sqlite_relpath: str
    schema: NormalizedSchema
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "example_id": self.example_id,
            "benchmark": self.benchmark,
            "subset": self.subset,
            "source_id": self.source_id,
            "db_id": self.db_id,
            "question": self.question,
            "gold_sql": self.gold_sql,
            "sqlite_path": self.sqlite_path,
            "sqlite_relpath": self.sqlite_relpath,
            "schema": self.schema.to_dict(),
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class NormalizationSummary:
    """High-level result details for a normalization run."""

    benchmark: str
    mode: str
    example_count: int
    database_count: int
    output_path: str
    skipped_stages: list[str] = field(default_factory=list)
