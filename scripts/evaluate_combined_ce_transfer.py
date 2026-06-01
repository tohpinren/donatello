from __future__ import annotations

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from evaluate_ce_transfer import (
    evaluate_ce_prediction,
    load_baseline,
    load_donatello_counterexamples,
    write_csv,
    write_jsonl,
)
from evaluate_spotit_on_augmented_bird import (
    TOTAL_EXAMPLES,
    load_examples,
    load_predictions,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate standalone transfer on the union of official SpotIt+ CEs and "
            "Donatello mutation-derived CEs."
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
        "--spotit-counterexamples-jsonl",
        required=True,
        help="Official SpotIt+ CE manifest.",
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
        default="outputs/spotit_donatello_combined_ce_transfer",
        help="Directory for generated transfer artifacts.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repo_root = Path(__file__).resolve().parent.parent
    supplementary_root = Path(args.supplementary_root).resolve()
    bird_db_root = Path(args.bird_db_root).resolve()
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
    target_models = sorted(
        model for model in predictions_by_model if model != args.exclude_model
    )

    spotit_ces = load_spotit_counterexamples(
        Path(args.spotit_counterexamples_jsonl),
    )
    donatello_ces = load_donatello_counterexamples(
        Path(args.counterexamples_jsonl),
        repo_root=repo_root,
        examples_by_id=examples_by_id,
    )
    combined_ces = deduplicate_counterexamples(spotit_ces + donatello_ces)
    write_jsonl(output_dir / "combined_counterexamples.jsonl", combined_ces)

    tasks = []
    for ce in combined_ces:
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
    summary, per_model_rows = summarize_combined_transfer(
        rows,
        target_models,
        ce_count=len(combined_ces),
        baseline_by_model=baseline_by_model,
        total_questions=len(examples_by_id),
        total_databases=len(
            {str(example["db_id"]) for example in examples_by_id.values()}
        ),
    )
    write_csv(output_dir / "per_model_summary.csv", per_model_rows)
    write_csv(output_dir / "summary.csv", [summary])


def load_spotit_counterexamples(path: Path) -> list[dict[str, object]]:
    ces: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if not row.get("validated"):
                continue
            db_path_raw = row.get("counterexample_db_path")
            if not db_path_raw:
                continue
            db_path = Path(str(db_path_raw)).resolve()
            if not db_path.exists():
                continue
            ces.append(
                {
                    "ce_id": str(row["ce_id"]),
                    "question_id": int(row["question_id"]),
                    "db_id": str(row["db_id"]),
                    "db_path": db_path,
                    "gold_sql": str(row["gold_sql"]).strip(),
                    "source": "spotit",
                }
            )
    return ces


def deduplicate_counterexamples(
    ces: list[dict[str, object]],
) -> list[dict[str, object]]:
    deduped: list[dict[str, object]] = []
    seen: set[tuple[int, str]] = set()
    for ce in sorted(
        ces,
        key=lambda row: (int(row["question_id"]), str(row["ce_id"])),
    ):
        signature = (int(ce["question_id"]), sqlite_file_hash(Path(ce["db_path"])))
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(
            {
                **ce,
                "db_path": str(ce["db_path"]),
            }
        )
    return deduped


def sqlite_file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def summarize_combined_transfer(
    rows: list[dict[str, object]],
    target_models: list[str],
    *,
    ce_count: int,
    baseline_by_model: dict[str, dict[int, str]],
    total_questions: int,
    total_databases: int,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    question_failures: dict[tuple[str, int], bool] = {}
    covered_questions: set[int] = set()
    covered_databases: set[str] = set()
    transferred_ce_ids: set[str] = set()
    total_ce_triggered_failures = 0

    for row in rows:
        model = str(row["target_model"])
        question_id = int(row["question_id"])
        db_id = str(row["db_id"])
        ce_id = str(row["ce_id"])
        newly_failed = bool(row["newly_failed"])
        covered_questions.add(question_id)
        covered_databases.add(db_id)
        if newly_failed:
            transferred_ce_ids.add(ce_id)
            total_ce_triggered_failures += 1
            question_failures[(model, question_id)] = True
        else:
            question_failures.setdefault((model, question_id), False)

    ex_drops: list[float] = []
    per_model_rows: list[dict[str, object]] = []
    affected_models = 0
    total_unique_newly_failed_predictions = 0
    for model in target_models:
        original_correct = sum(
            1 for result in baseline_by_model[model].values() if result == "correct"
        )
        newly_failed = sum(
            1
            for qid, result in baseline_by_model[model].items()
            if result == "correct" and question_failures.get((model, qid), False)
        )
        if newly_failed > 0:
            affected_models += 1
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
                "original_ex": round(original_ex, 6),
                "after_ex": round(after_ex, 6),
                "delta_ex": round(delta_ex, 6),
            }
        )

    transfer_rate = len(transferred_ce_ids) / ce_count if ce_count else 0.0
    reuse = total_ce_triggered_failures / ce_count if ce_count else 0.0
    model_coverage = affected_models / len(target_models) if target_models else 0.0
    summary = {
        "# CEs": ce_count,
        "Transferred CEs": len(transferred_ce_ids),
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


if __name__ == "__main__":
    main()
