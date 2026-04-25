"""Mutation generation over normalized examples using SQLGlot."""

from __future__ import annotations

import json
from pathlib import Path

from sqlglot.errors import ParseError

from sqlmt.models import (
    MutationRecord,
    MutationSkipReason,
    MutationSummary,
    NormalizedExample,
)
from sqlmt.mutation.helpers import normalize_sql, parse_sql, serialize_sql
from sqlmt.mutation.profile import MutationRule, ensure_rule_sequence

DEFAULT_MAX_GENERATED_MUTANTS_PER_RULE = 3


def generate_mutations(
    examples: list[NormalizedExample],
    *,
    rules: list[MutationRule] | None = None,
    profile: str = "canonical",
    max_generated_per_rule: int = DEFAULT_MAX_GENERATED_MUTANTS_PER_RULE,
) -> list[MutationRecord]:
    if max_generated_per_rule < 1:
        raise ValueError("max_generated_per_rule must be at least 1.")

    ordered_rules = ensure_rule_sequence(rules)
    records: list[MutationRecord] = []

    for example in examples:
        try:
            parsed = parse_sql(example.gold_sql)
            canonical_gold_sql = serialize_sql(parsed)
        except ParseError:
            for rule in ordered_rules:
                records.extend(
                    _skip_record(
                        example,
                        rule.rule_id,
                        profile,
                        0,
                        "parse_error",
                    )
                )
            continue

        generated_sqls: set[str] = set()
        for rule in ordered_rules:
            try:
                candidates = rule.apply(example, parsed)
            except Exception as exc:  # pragma: no cover - defensive isolation
                records.extend(
                    _skip_record(
                        example,
                        rule.rule_id,
                        profile,
                        0,
                        "rule_not_applicable",
                        metadata={"error": str(exc)},
                    )
                )
                continue

            if not candidates:
                records.extend(
                    _skip_record(
                        example,
                        rule.rule_id,
                        profile,
                        0,
                        "rule_not_applicable",
                    )
                )
                continue

            generated_for_rule = 0
            emitted_any = False
            for attempt_index, candidate in enumerate(candidates):
                if generated_for_rule >= max_generated_per_rule:
                    break

                emitted_any = True
                try:
                    canonical_mutant_sql = normalize_sql(candidate.mutant_sql)
                except ParseError:
                    records.extend(
                        _skip_record(
                            example,
                            rule.rule_id,
                            profile,
                            attempt_index,
                            "invalid_mutant_sql",
                            metadata=candidate.metadata,
                        )
                    )
                    continue

                if canonical_mutant_sql == canonical_gold_sql:
                    records.extend(
                        _skip_record(
                            example,
                            rule.rule_id,
                            profile,
                            attempt_index,
                            "same_as_gold",
                            metadata=candidate.metadata,
                        )
                    )
                    continue

                if canonical_mutant_sql in generated_sqls:
                    records.extend(
                        _skip_record(
                            example,
                            rule.rule_id,
                            profile,
                            attempt_index,
                            "duplicate_sql",
                            metadata=candidate.metadata,
                        )
                    )
                    continue

                generated_sqls.add(canonical_mutant_sql)
                records.append(
                    MutationRecord(
                        mutant_id=_mutant_id(
                            example.example_id,
                            rule.rule_id,
                            attempt_index,
                        ),
                        example_id=example.example_id,
                        benchmark=example.benchmark,
                        subset=example.subset,
                        db_id=example.db_id,
                        source_id=example.source_id,
                        question=example.question,
                        rule=rule.rule_id,
                        profile=profile,
                        status="generated",
                        skip_reason=None,
                        gold_sql=example.gold_sql,
                        mutant_sql=canonical_mutant_sql,
                        sqlite_path=example.sqlite_path,
                        sqlite_relpath=example.sqlite_relpath,
                        metadata=dict(candidate.metadata),
                    )
                )
                generated_for_rule += 1

            if not emitted_any:
                records.extend(
                    _skip_record(
                        example,
                        rule.rule_id,
                        profile,
                        0,
                        "rule_not_applicable",
                    )
                )

    return records


def read_normalized_examples(path: Path) -> list[NormalizedExample]:
    examples: list[NormalizedExample] = []
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"normalized_examples.jsonl is not valid JSONL: line {line_number}."
                ) from exc
            if not isinstance(row, dict):
                raise ValueError(
                    "normalized_examples.jsonl row "
                    f"{line_number} must be a JSON object."
                )
            examples.append(NormalizedExample.from_dict(row))
    return examples


def read_mutation_records(path: Path) -> list[MutationRecord]:
    records: list[MutationRecord] = []
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"mutants.jsonl is not valid JSONL: line {line_number}."
                ) from exc
            if not isinstance(row, dict):
                raise ValueError(
                    f"mutants.jsonl row {line_number} must be a JSON object."
                )
            records.append(MutationRecord.from_dict(row))
    return records


def write_mutation_records(path: Path, records: list[MutationRecord]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record.to_dict(), ensure_ascii=True))
            file.write("\n")


def summarize_mutations(
    records: list[MutationRecord],
    *,
    benchmark: str,
    profile: str,
    output_path: Path,
    resumed: bool = False,
    skipped_stages: list[str] | None = None,
) -> MutationSummary:
    return MutationSummary(
        benchmark=benchmark,
        profile=profile,
        generated_count=sum(1 for record in records if record.status == "generated"),
        skipped_count=sum(1 for record in records if record.status == "skipped"),
        output_path=str(output_path),
        resumed=resumed,
        skipped_stages=list(skipped_stages or []),
    )


def _mutant_id(example_id: str, rule_id: str, index: int) -> str:
    return f"{example_id}:{rule_id}:{index:02d}"


def _skip_record(
    example: NormalizedExample,
    rule_id: str,
    profile: str,
    index: int,
    reason: MutationSkipReason,
    *,
    metadata: dict[str, object] | None = None,
) -> list[MutationRecord]:
    return [
        MutationRecord(
            mutant_id=_mutant_id(example.example_id, rule_id, index),
            example_id=example.example_id,
            benchmark=example.benchmark,
            subset=example.subset,
            db_id=example.db_id,
            source_id=example.source_id,
            question=example.question,
            rule=rule_id,
            profile=profile,
            status="skipped",
            skip_reason=reason,
            gold_sql=example.gold_sql,
            mutant_sql=None,
            sqlite_path=example.sqlite_path,
            sqlite_relpath=example.sqlite_relpath,
            metadata=dict(metadata or {}),
        )
    ]
