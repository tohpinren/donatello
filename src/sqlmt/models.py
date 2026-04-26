"""Shared data contracts for normalized examples and mutation artifacts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, cast


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
        """Serialize the schema into the JSONL artifact shape."""

        return {
            "table_names": self.table_names,
            "table_names_original": self.table_names_original,
            "column_names": self.column_names,
            "column_names_original": self.column_names_original,
            "column_types": self.column_types,
            "primary_keys": self.primary_keys,
            "foreign_keys": self.foreign_keys,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> NormalizedSchema:
        """Parse a schema row read back from a JSON artifact."""

        return cls(
            table_names=[str(value) for value in row["table_names"]],
            table_names_original=[str(value) for value in row["table_names_original"]],
            column_names=[
                (int(table_index), str(name))
                for table_index, name in row["column_names"]
            ],
            column_names_original=[
                (int(table_index), str(name))
                for table_index, name in row["column_names_original"]
            ],
            column_types=[str(value) for value in row["column_types"]],
            primary_keys=[
                tuple(int(part) for part in key) if isinstance(key, list) else int(key)
                for key in row["primary_keys"]
            ],
            foreign_keys=[
                (int(source), int(target)) for source, target in row["foreign_keys"]
            ],
        )


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
        """Serialize one normalized example for artifact storage."""

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

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> NormalizedExample:
        """Parse a normalized example previously written to JSONL."""

        return cls(
            example_id=str(row["example_id"]),
            benchmark=str(row["benchmark"]),
            subset=str(row["subset"]),
            source_id=(
                str(row["source_id"]) if row.get("source_id") is not None else None
            ),
            db_id=str(row["db_id"]),
            question=str(row["question"]),
            gold_sql=str(row["gold_sql"]),
            sqlite_path=str(row["sqlite_path"]),
            sqlite_relpath=str(row["sqlite_relpath"]),
            schema=NormalizedSchema.from_dict(dict(row["schema"])),
            metadata=dict(row.get("metadata", {})),
        )


@dataclass(frozen=True)
class NormalizationSummary:
    """High-level result details for a normalization run."""

    benchmark: str
    mode: str
    example_count: int
    database_count: int
    output_path: str
    resumed: bool = False
    skipped_stages: list[str] = field(default_factory=list)


MutationStatus = Literal["generated", "skipped"]
MutationSkipReason = Literal[
    "parse_error",
    "rule_not_applicable",
    "invalid_mutant_sql",
    "same_as_gold",
    "duplicate_sql",
]
EquivalenceOutcome = Literal[
    "proven_equivalent",
    "proven_non_equivalent",
    "timeout",
    "unsupported",
    "error",
]


@dataclass(frozen=True)
class MutationRecord:
    """One generated or skipped mutation attempt for a gold SQL query."""

    mutant_id: str
    example_id: str
    benchmark: str
    subset: str
    db_id: str
    source_id: str | None
    question: str
    rule: str
    profile: str
    status: MutationStatus
    skip_reason: MutationSkipReason | None
    gold_sql: str
    mutant_sql: str | None
    sqlite_path: str
    sqlite_relpath: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Serialize the mutation record into the persisted JSONL shape."""

        return {
            "mutant_id": self.mutant_id,
            "example_id": self.example_id,
            "benchmark": self.benchmark,
            "subset": self.subset,
            "db_id": self.db_id,
            "source_id": self.source_id,
            "question": self.question,
            "rule": self.rule,
            "profile": self.profile,
            "status": self.status,
            "skip_reason": self.skip_reason,
            "gold_sql": self.gold_sql,
            "mutant_sql": self.mutant_sql,
            "sqlite_path": self.sqlite_path,
            "sqlite_relpath": self.sqlite_relpath,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> MutationRecord:
        """Parse a mutation record read back from ``mutants.jsonl``."""

        return cls(
            mutant_id=str(row["mutant_id"]),
            example_id=str(row["example_id"]),
            benchmark=str(row["benchmark"]),
            subset=str(row["subset"]),
            db_id=str(row["db_id"]),
            source_id=(
                str(row["source_id"]) if row.get("source_id") is not None else None
            ),
            question=str(row["question"]),
            rule=str(row["rule"]),
            profile=str(row["profile"]),
            status=cast(MutationStatus, row["status"]),
            skip_reason=cast(MutationSkipReason | None, row.get("skip_reason")),
            gold_sql=str(row["gold_sql"]),
            mutant_sql=(
                str(row["mutant_sql"]) if row.get("mutant_sql") is not None else None
            ),
            sqlite_path=str(row["sqlite_path"]),
            sqlite_relpath=str(row["sqlite_relpath"]),
            metadata=dict(row.get("metadata", {})),
        )


@dataclass(frozen=True)
class MutationSummary:
    """High-level counts and output metadata for a mutation run."""

    benchmark: str
    profile: str
    generated_count: int
    skipped_count: int
    output_path: str
    resumed: bool = False
    skipped_stages: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EquivalenceRecord:
    """One VeriEQL classification for a generated mutant."""

    mutant_id: str
    example_id: str
    outcome: EquivalenceOutcome
    raw_state: str | None
    timeout_seconds: int
    verieql_index: int | None = None
    times: list[Any] = field(default_factory=list)
    counterexample_sql: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Serialize the equivalence record into the JSONL artifact shape."""

        return {
            "mutant_id": self.mutant_id,
            "example_id": self.example_id,
            "outcome": self.outcome,
            "raw_state": self.raw_state,
            "timeout_seconds": self.timeout_seconds,
            "verieql_index": self.verieql_index,
            "times": self.times,
            "counterexample_sql": self.counterexample_sql,
            "error": self.error,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> EquivalenceRecord:
        """Parse an equivalence record read back from ``equivalence.jsonl``."""

        return cls(
            mutant_id=str(row["mutant_id"]),
            example_id=str(row["example_id"]),
            outcome=cast(EquivalenceOutcome, row["outcome"]),
            raw_state=(
                str(row["raw_state"]) if row.get("raw_state") is not None else None
            ),
            timeout_seconds=int(row["timeout_seconds"]),
            verieql_index=(
                int(row["verieql_index"])
                if row.get("verieql_index") is not None
                else None
            ),
            times=list(row.get("times", [])),
            counterexample_sql=(
                str(row["counterexample_sql"])
                if row.get("counterexample_sql") is not None
                else None
            ),
            error=str(row["error"]) if row.get("error") is not None else None,
            metadata=dict(row.get("metadata", {})),
        )


@dataclass(frozen=True)
class EquivalenceSummary:
    """High-level counts and output metadata for VeriEQL filtering."""

    checked_count: int
    proven_equivalent_count: int
    proven_non_equivalent_count: int
    timeout_count: int
    unsupported_count: int
    error_count: int
    output_path: str
    resumed: bool = False
    skipped_stages: list[str] = field(default_factory=list)
