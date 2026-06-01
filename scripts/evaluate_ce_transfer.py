"""Evaluate Donatello counterexamples in standalone SpotIT-style transfer mode.

Each generated counterexample database is treated as a temporary witness DB for
its original BIRD question. For each target model, a prediction is newly failed
if it was correct on the original BIRD database but differs from the gold query
on at least one standalone counterexample DB for that question.
"""

from __future__ import annotations

import argparse
import csv
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from evaluate_spotit_on_augmented_bird import (
    TOTAL_EXAMPLES,
    evaluate_prediction,
    load_examples,
    load_predictions,
)

EXPECTED_TARGET_MODEL_COUNT = 9


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate exact per-counterexample transfer for Donatello mutation "
            "counterexamples using SpotIT prediction artifacts."
        )
    )
    parser.add_argument(
        "--supplementary-root",
        required=True,
        help=(
            "Path to the SpotIT artifact root containing BIRD_benchmarks/ "
            "and generated_sqls/."
        ),
    )
    parser.add_argument(
        "--bird-db-root",
        default="data/bird/dev/dev_databases",
        help="Path to the original BIRD dev database root.",
    )
    parser.add_argument(
        "--baseline-jsonl",
        required=True,
        help="Baseline EX jsonl produced on the original BIRD dev databases.",
    )
    parser.add_argument(
        "--counterexamples-jsonl",
        default="outputs/bird-full/counterexamples.jsonl",
        help="Path to the Donatello counterexamples.jsonl artifact.",
    )
    parser.add_argument(
        "--exclude-model",
        default="csc-32b_sql",
        help="Model to exclude from transfer targets for 9-model comparison.",
    )
    parser.add_argument(
        "--sqlite-timeout-seconds",
        type=int,
        default=30,
        help="Timeout for each SQLite query evaluation.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=12,
        help="Parallel workers for CE transfer evaluation.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for generated transfer artifacts.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repo_root = Path(__file__).resolve().parent.parent
    supplementary_root = Path(args.supplementary_root).resolve()
    if not supplementary_root.exists():
        supplementary_root = (repo_root / args.supplementary_root).resolve()
    bird_db_root = Path(args.bird_db_root).resolve()
    if not bird_db_root.exists():
        bird_db_root = (repo_root / args.bird_db_root).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    examples_by_id = load_examples(
        supplementary_root / "BIRD_benchmarks" / "dev.json",
        bird_db_root,
    )
    if len(examples_by_id) != TOTAL_EXAMPLES:
        raise ValueError(f"Expected {TOTAL_EXAMPLES} dev examples.")
    predictions_by_model = load_predictions(supplementary_root / "generated_sqls")
    baseline_by_model = load_baseline(Path(args.baseline_jsonl))
    target_models = select_target_models(
        predictions_by_model,
        baseline_by_model,
        exclude_model=args.exclude_model,
    )

    ces = load_donatello_counterexamples(
        Path(args.counterexamples_jsonl),
        repo_root=repo_root,
        examples_by_id=examples_by_id,
    )

    tasks = []
    for ce in ces:
        for model in target_models:
            tasks.append(
                {
                    "ce_id": ce["ce_id"],
                    "question_id": ce["question_id"],
                    "db_id": ce["db_id"],
                    "model": model,
                    "db_path": str(ce["db_path"]),
                    "gold_sql": ce["gold_sql"],
                    "prediction_sql": predictions_by_model[model][ce["question_id"]],
                }
            )

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        rows = list(
            executor.map(
                lambda payload: evaluate_ce_prediction(
                    payload,
                    baseline_by_model=baseline_by_model,
                    timeout_seconds=args.sqlite_timeout_seconds,
                ),
                tasks,
            )
        )

    write_jsonl(output_dir / "ce_transfer.jsonl", rows)
    summary, per_model_rows = summarize_ce_transfer(
        rows,
        target_models,
        len(ces),
        baseline_by_model=baseline_by_model,
        total_questions=len(examples_by_id),
        total_databases=len(
            {str(example["db_id"]) for example in examples_by_id.values()}
        ),
    )
    write_csv(output_dir / "per_model_summary.csv", per_model_rows)
    write_csv(output_dir / "summary.csv", [summary])


def load_baseline(path: Path) -> dict[str, dict[int, str]]:
    results: dict[str, dict[int, str]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            model = str(row["model"])
            qid = int(row["question_id"])
            label = str(row["baseline_ex_result"])
            results.setdefault(model, {})[qid] = label
    return results


def select_target_models(
    predictions_by_model: dict[str, dict[int, str]],
    baseline_by_model: dict[str, dict[int, str]],
    *,
    exclude_model: str,
    expected_target_count: int = EXPECTED_TARGET_MODEL_COUNT,
) -> list[str]:
    """Return the 9 transfer targets, failing fast on model-set drift."""

    if exclude_model not in predictions_by_model:
        available = ", ".join(sorted(predictions_by_model))
        raise ValueError(
            f"Excluded source model '{exclude_model}' is not present in predictions. "
            f"Available models: {available}"
        )
    if exclude_model not in baseline_by_model:
        available = ", ".join(sorted(baseline_by_model))
        raise ValueError(
            f"Excluded source model '{exclude_model}' is not present in baseline EX. "
            f"Available models: {available}"
        )

    target_models = sorted(
        model for model in predictions_by_model if model != exclude_model
    )
    missing_baseline = [
        model for model in target_models if model not in baseline_by_model
    ]
    if missing_baseline:
        raise ValueError(
            "Baseline EX is missing target models: "
            + ", ".join(sorted(missing_baseline))
        )
    if len(target_models) != expected_target_count:
        raise ValueError(
            f"Expected {expected_target_count} transfer target models after "
            f"excluding '{exclude_model}', found {len(target_models)}: "
            + ", ".join(target_models)
        )
    return target_models


def load_donatello_counterexamples(
    path: Path,
    *,
    repo_root: Path,
    examples_by_id: dict[int, dict],
) -> list[dict[str, object]]:
    ces: list[dict[str, object]] = []
    missing_paths: list[tuple[str, Path]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("outcome") != "generated":
                continue
            example_id = str(row.get("example_id") or "")
            if not example_id.startswith("bird:dev:"):
                continue
            question_id = int(example_id.split(":")[-1])
            ce_path = Path(str(row["counterexample_path"]))
            if not ce_path.is_absolute():
                ce_path = (repo_root / ce_path).resolve()
            if not ce_path.exists():
                missing_paths.append((str(row["mutant_id"]), ce_path))
                continue
            ces.append(
                {
                    "ce_id": str(row["mutant_id"]),
                    "question_id": question_id,
                    "db_id": str(examples_by_id[question_id]["db_id"]),
                    "db_path": ce_path,
                    "gold_sql": str(row["gold_sql"]).strip(),
                }
            )
    if missing_paths:
        raise ValueError(
            format_missing_counterexample_paths(
                "Donatello",
                manifest_path=path,
                missing_paths=missing_paths,
            )
        )
    return ces


def format_missing_counterexample_paths(
    source: str,
    *,
    manifest_path: Path,
    missing_paths: list[tuple[str, Path]],
) -> str:
    """Build a compact error message for stale CE manifests."""

    sample = "; ".join(
        f"{ce_id}: {path}" for ce_id, path in missing_paths[:5]
    )
    extra = "" if len(missing_paths) <= 5 else f"; ... +{len(missing_paths) - 5} more"
    return (
        f"{source} CE manifest references {len(missing_paths)} missing "
        f"counterexample DB file(s): {manifest_path}. First missing: "
        f"{sample}{extra}"
    )


def evaluate_ce_prediction(
    payload: dict[str, object],
    *,
    baseline_by_model: dict[str, dict[int, str]],
    timeout_seconds: int,
) -> dict[str, object]:
    model = str(payload["model"])
    question_id = int(payload["question_id"])
    baseline_label = baseline_by_model[model][question_id]
    result = evaluate_prediction(
        {
            "question_id": question_id,
            "model": model,
            "db_path": str(payload["db_path"]),
            "gold_sql": str(payload["gold_sql"]),
            "prediction_sql": str(payload["prediction_sql"]),
        },
        timeout_seconds=timeout_seconds,
        result_key="after_ce_result",
    )
    after_label = str(result["after_ce_result"])
    newly_failed = baseline_label == "correct" and after_label != "correct"
    return {
        "ce_id": str(payload["ce_id"]),
        "question_id": question_id,
        "db_id": str(payload["db_id"]),
        "target_model": model,
        "baseline_result": baseline_label,
        "after_ce_result": after_label,
        "newly_failed": newly_failed,
        "error": result["error"],
        "db_path": str(payload["db_path"]),
    }


def summarize_ce_transfer(
    rows: list[dict[str, object]],
    target_models: list[str],
    ce_count: int,
    *,
    baseline_by_model: dict[str, dict[int, str]],
    total_questions: int,
    total_databases: int,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    per_ce: dict[str, dict[str, object]] = {}
    per_model: dict[str, dict[str, int | set[int]]] = {
        model: {
            "raw_newly_failed": 0,
            "evaluated": 0,
            "affected_ces": 0,
            "newly_failed_questions": set(),
        }
        for model in target_models
    }
    covered_questions: set[int] = set()
    covered_databases: set[str] = set()
    for row in rows:
        ce_id = str(row["ce_id"])
        model = str(row["target_model"])
        newly_failed = bool(row["newly_failed"])
        question_id = int(row["question_id"])
        db_id = str(row["db_id"])
        slot = per_ce.setdefault(
            ce_id,
            {
                "newly_failed_predictions": 0,
                "affected_models": set(),
                "question_id": question_id,
                "db_id": db_id,
            },
        )
        per_model[model]["evaluated"] += 1
        if newly_failed:
            slot["newly_failed_predictions"] += 1
            slot["affected_models"].add(model)
            per_model[model]["raw_newly_failed"] += 1
            per_model[model]["newly_failed_questions"].add(question_id)

    for ce_stats in per_ce.values():
        covered_questions.add(int(ce_stats["question_id"]))
        covered_databases.add(str(ce_stats["db_id"]))

    transferred_ce_count = 0
    total_ce_triggered_failures = 0
    affected_models_global: set[str] = set()
    ex_drops: list[float] = []
    for ce_stats in per_ce.values():
        newly_failed_predictions = int(ce_stats["newly_failed_predictions"])
        if newly_failed_predictions > 0:
            transferred_ce_count += 1
        total_ce_triggered_failures += newly_failed_predictions
        affected_models = set(ce_stats["affected_models"])
        affected_models_global.update(affected_models)
        for model in affected_models:
            per_model[model]["affected_ces"] += 1

    transfer_rate = transferred_ce_count / ce_count if ce_count else 0.0
    reuse = total_ce_triggered_failures / ce_count if ce_count else 0.0
    model_coverage = (
        len(affected_models_global) / len(target_models) if target_models else 0.0
    )

    per_model_rows = []
    total_unique_newly_failed_predictions = 0
    for model in target_models:
        original_correct = sum(
            1 for result in baseline_by_model[model].values() if result == "correct"
        )
        newly_failed = len(set(per_model[model]["newly_failed_questions"]))
        total_unique_newly_failed_predictions += newly_failed
        original_ex = original_correct / total_questions if total_questions else 0.0
        after_ex = (
            (original_correct - newly_failed) / total_questions
            if total_questions
            else 0.0
        )
        delta_ex = original_ex - after_ex
        ex_drops.append(delta_ex)
        per_model_rows.append(
            {
                "target_model": model,
                "original_correct": original_correct,
                "newly_failed": newly_failed,
                "raw_newly_failed": int(per_model[model]["raw_newly_failed"]),
                "affected_ces": int(per_model[model]["affected_ces"]),
                "evaluated_ces": int(per_model[model]["evaluated"]),
                "original_ex": round(original_ex, 6),
                "after_ex": round(after_ex, 6),
                "delta_ex": round(delta_ex, 6),
                "new_fail_rate_per_ce": round(
                    newly_failed / ce_count if ce_count else 0.0, 6
                ),
            }
        )

    summary = {
        "# CEs": ce_count,
        "Transferred CEs": transferred_ce_count,
        "Avg EX Drop": round(sum(ex_drops) / len(ex_drops), 6) if ex_drops else 0.0,
        "Transfer Rate": round(transfer_rate, 6),
        "Model Coverage": round(model_coverage, 6),
        "Reuse": round(reuse, 6),
        "Total Newly Failed Predictions": total_unique_newly_failed_predictions,
        "CE-triggered Failures": total_ce_triggered_failures,
        "Question Coverage": round(
            len(covered_questions) / total_questions if total_questions else 0.0, 6
        ),
        "Covered Questions": len(covered_questions),
        "Total Questions": total_questions,
        "DB Coverage": round(
            len(covered_databases) / total_databases if total_databases else 0.0, 6
        ),
        "Covered DBs": len(covered_databases),
        "Total DBs": total_databases,
    }
    return summary, per_model_rows


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True) + "\n")


if __name__ == "__main__":
    main()
