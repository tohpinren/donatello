from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from pathlib import Path

TOTAL_EXAMPLES = 1534


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure before/after execution accuracy for SpotIT model predictions "
            "on original BIRD dev databases versus donatello augmented "
            "databases."
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
        "--augmentation-manifest",
        default="outputs/bird-full/augmented_databases/augmentation.jsonl",
        help="Path to the donatello augmentation manifest.",
    )
    parser.add_argument(
        "--augmentation-root",
        default=None,
        help=(
            "Optional base directory for resolving relative augmented DB paths in "
            "the augmentation manifest. Defaults to the current repo root."
        ),
    )
    parser.add_argument(
        "--bird-db-root",
        default="data/bird/dev/dev_databases",
        help="Path to the original BIRD dev database root.",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs/spotit_donatello_transfer",
        help="Directory for generated evaluation artifacts.",
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
        help="Parallel workers for after-augmentation EX evaluation.",
    )
    parser.add_argument(
        "--question-limit",
        type=int,
        default=None,
        help="Optional question limit for smoke testing.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repo_root = Path(__file__).resolve().parent.parent
    supplementary_root = (repo_root / args.supplementary_root).resolve()
    augmentation_manifest = (repo_root / args.augmentation_manifest).resolve()
    augmentation_root = (
        (repo_root / args.augmentation_root).resolve()
        if args.augmentation_root is not None
        else repo_root
    )
    bird_db_root = (repo_root / args.bird_db_root).resolve()
    output_dir = (repo_root / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    examples_by_id = load_examples(
        supplementary_root / "BIRD_benchmarks" / "dev.json",
        bird_db_root,
    )
    predictions_by_model = load_predictions(supplementary_root / "generated_sqls")
    augmented_db_by_id = load_augmented_db_map(
        augmentation_manifest,
        repo_root=augmentation_root,
    )

    model_names = sorted(predictions_by_model)
    question_ids = sorted(examples_by_id)
    if args.question_limit is not None:
        question_ids = question_ids[: args.question_limit]

    baseline_tasks = [
        {
            "question_id": question_id,
            "model": model_name,
            "db_path": str(Path(examples_by_id[question_id]["sqlite_path"]).resolve()),
            "gold_sql": examples_by_id[question_id]["gold_sql"],
            "prediction_sql": predictions_by_model[model_name][question_id],
        }
        for question_id in question_ids
        for model_name in model_names
    ]

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        baseline_rows = list(
            executor.map(
                lambda payload: evaluate_prediction(
                    payload,
                    timeout_seconds=args.sqlite_timeout_seconds,
                    result_key="baseline_ex_result",
                ),
                baseline_tasks,
            )
        )

    write_jsonl(output_dir / "baseline_ex.jsonl", baseline_rows)
    baseline_by_model = build_result_index(
        baseline_rows,
        result_key="baseline_ex_result",
    )

    tasks = [
        {
            "question_id": question_id,
            "model": model_name,
            "db_path": str(
                augmented_db_by_id.get(
                    examples_by_id[question_id]["db_id"],
                    Path(examples_by_id[question_id]["sqlite_path"]),
                )
            ),
            "gold_sql": examples_by_id[question_id]["gold_sql"],
            "prediction_sql": predictions_by_model[model_name][question_id],
        }
        for question_id in question_ids
        for model_name in model_names
    ]

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        after_rows = list(
            executor.map(
                lambda payload: evaluate_prediction(
                    payload,
                    timeout_seconds=args.sqlite_timeout_seconds,
                    result_key="after_ex_result",
                ),
                tasks,
            )
        )

    write_jsonl(output_dir / "after_ex.jsonl", after_rows)
    per_model_rows, summary_row = summarize_after_results(
        baseline_by_model=baseline_by_model,
        after_rows=after_rows,
        model_names=model_names,
    )
    write_csv(output_dir / "per_model_summary.csv", per_model_rows)
    write_csv(output_dir / "summary.csv", [summary_row])


def load_examples(dev_json_path: Path, bird_db_root: Path) -> dict[int, dict]:
    rows = json.loads(dev_json_path.read_text(encoding="utf-8"))
    if len(rows) != TOTAL_EXAMPLES:
        raise ValueError(
            f"Expected {TOTAL_EXAMPLES} dev examples, found {len(rows)}."
        )
    examples: dict[int, dict] = {}
    for row in rows:
        question_id = int(row["question_id"])
        examples[question_id] = {
            "question_id": question_id,
            "db_id": row["db_id"],
            "gold_sql": row["SQL"].strip(),
            "sqlite_path": str(
                (bird_db_root / row["db_id"] / f"{row['db_id']}.sqlite").resolve()
            ),
        }
    return examples


def load_predictions(predictions_dir: Path) -> dict[str, dict[int, str]]:
    results: dict[str, dict[int, str]] = {}
    for path in sorted(predictions_dir.glob("*.json")):
        model_name = path.stem
        payload = json.loads(path.read_text(encoding="utf-8"))
        results[model_name] = {
            int(question_id): strip_prediction_metadata(str(sql))
            for question_id, sql in payload.items()
        }
    return results


def load_augmented_db_map(
    manifest_path: Path,
    *,
    repo_root: Path,
) -> dict[str, Path]:
    text = manifest_path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if not stripped:
        return {}

    if stripped[0] == "{":
        return load_augmented_db_map_from_json(
            json.loads(text),
            repo_root=repo_root,
        )
    return load_augmented_db_map_from_jsonl(
        text,
        repo_root=repo_root,
    )


def load_augmented_db_map_from_jsonl(
    manifest_text: str,
    *,
    repo_root: Path,
) -> dict[str, Path]:
    mapping: dict[str, Path] = {}
    for line in manifest_text.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        augmented_path = Path(str(row["augmented_sqlite_path"]))
        if not augmented_path.is_absolute():
            augmented_path = (repo_root / augmented_path).resolve()
        mapping[str(row["db_id"])] = augmented_path
    return mapping


def load_augmented_db_map_from_json(
    manifest_obj: dict[str, object],
    *,
    repo_root: Path,
) -> dict[str, Path]:
    mapping: dict[str, Path] = {}
    for row in manifest_obj.get("per_database", []):
        augmented_path_str = row.get("augmented_db")
        db_id = row.get("db_id")
        if not augmented_path_str or not db_id:
            continue
        augmented_path = Path(str(augmented_path_str))
        if not augmented_path.is_absolute():
            augmented_path = (repo_root / augmented_path).resolve()
        mapping[str(db_id)] = augmented_path
    return mapping


def strip_prediction_metadata(raw_sql: str) -> str:
    if "----- bird -----" in raw_sql:
        raw_sql = raw_sql.split("----- bird -----", 1)[0]
    return raw_sql.strip()


def evaluate_prediction(
    payload: dict[str, str | int],
    *,
    timeout_seconds: int,
    result_key: str,
) -> dict[str, object]:
    question_id = int(payload["question_id"])
    model_name = str(payload["model"])
    db_path = str(payload["db_path"])
    gold_sql = str(payload["gold_sql"])
    prediction_sql = str(payload["prediction_sql"])

    gold_outcome = execute_query_cached(db_path, gold_sql, timeout_seconds)
    prediction_outcome = execute_query_cached(db_path, prediction_sql, timeout_seconds)

    if not gold_outcome["ok"]:
        correct = False
        error = f"gold_error: {gold_outcome['error']}"
    elif not prediction_outcome["ok"]:
        correct = False
        error = f"prediction_error: {prediction_outcome['error']}"
    else:
        correct = compare_query_results(
            prediction_sql,
            gold_sql,
            prediction_outcome["rows"],
            gold_outcome["rows"],
        )
        error = None

    return {
        "model": model_name,
        "question_id": question_id,
        "db_path": db_path,
        result_key: "correct" if correct else "incorrect",
        "error": error,
    }


def build_result_index(
    rows: list[dict[str, object]],
    *,
    result_key: str,
) -> dict[str, dict[int, str]]:
    results: dict[str, dict[int, str]] = {}
    for row in rows:
        model_name = str(row["model"])
        question_id = int(row["question_id"])
        results.setdefault(model_name, {})[question_id] = str(row[result_key])
    return results


@cache
def execute_query_cached(
    sqlite_path: str,
    sql: str,
    timeout_seconds: int,
) -> dict[str, object]:
    command = [sys.executable, "-c", QUERY_RUNNER_SCRIPT, sqlite_path]
    try:
        completed = subprocess.run(
            command,
            input=sql,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"query execution exceeded {timeout_seconds}s"}

    stdout = completed.stdout.strip()
    if not stdout:
        stderr = completed.stderr.strip()
        return {"ok": False, "error": stderr or "query execution produced no output"}

    payload = json.loads(stdout)
    if not payload.get("ok"):
        return {"ok": False, "error": str(payload.get("error") or "query failed")}
    return {"ok": True, "rows": tuple(tuple(row) for row in payload.get("rows", []))}


def compare_query_results(
    sql_a: str,
    sql_b: str,
    rows_a: tuple[tuple[object, ...], ...],
    rows_b: tuple[tuple[object, ...], ...],
) -> bool:
    if "ORDER BY" in sql_a.upper() or "ORDER BY" in sql_b.upper():
        return rows_a == rows_b
    return Counter(rows_a) == Counter(rows_b)


def summarize_after_results(
    *,
    baseline_by_model: dict[str, dict[int, str]],
    after_rows: list[dict[str, object]],
    model_names: list[str],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    after_by_model: dict[str, dict[int, str]] = {model: {} for model in model_names}
    for row in after_rows:
        after_by_model[str(row["model"])][int(row["question_id"])] = str(
            row["after_ex_result"]
        )

    per_model_rows: list[dict[str, object]] = []
    original_ex_values: list[float] = []
    after_ex_values: list[float] = []
    delta_values: list[float] = []
    total_newly_failed = 0
    total_newly_fixed = 0
    for model_name in model_names:
        baseline = baseline_by_model[model_name]
        after = after_by_model[model_name]
        evaluated_question_ids = sorted(after)
        denominator = len(evaluated_question_ids)
        original_correct = sum(
            1
            for question_id in evaluated_question_ids
            if baseline[question_id] == "correct"
        )
        after_correct = sum(1 for value in after.values() if value == "correct")
        newly_failed = sum(
            1
            for qid in evaluated_question_ids
            for value in [baseline[qid]]
            if value == "correct" and after[qid] != "correct"
        )
        newly_fixed = sum(
            1
            for qid in evaluated_question_ids
            for value in [baseline[qid]]
            if value != "correct" and after[qid] == "correct"
        )
        original_ex = original_correct / denominator if denominator else 0.0
        after_ex = after_correct / denominator if denominator else 0.0
        delta_ex = original_ex - after_ex
        total_newly_failed += newly_failed
        total_newly_fixed += newly_fixed
        original_ex_values.append(original_ex)
        after_ex_values.append(after_ex)
        delta_values.append(delta_ex)
        per_model_rows.append(
            {
                "target_model": model_name,
                "original_correct": original_correct,
                "after_correct": after_correct,
                "newly_failed": newly_failed,
                "newly_fixed": newly_fixed,
                "delta_ex": round(delta_ex, 6),
                "original_ex": round(original_ex, 6),
                "after_ex": round(after_ex, 6),
            }
        )

    summary_row = {
        "Models": len(model_names),
        "Avg Original EX": round(sum(original_ex_values) / len(original_ex_values), 6),
        "Avg After EX": round(sum(after_ex_values) / len(after_ex_values), 6),
        "Avg EX Drop": round(sum(delta_values) / len(delta_values), 6),
        "Total Newly Failed": total_newly_failed,
        "Total Newly Fixed": total_newly_fixed,
    }
    return per_model_rows, summary_row


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True))
            handle.write("\n")


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


QUERY_RUNNER_SCRIPT = """
import json
import sqlite3
import sys

db_path = sys.argv[1]
sql = sys.stdin.read()

try:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    try:
        cursor = connection.execute(sql)
        rows = cursor.fetchall()
    finally:
        connection.close()
except Exception as exc:
    print(json.dumps({"ok": False, "error": str(exc)}))
    sys.exit(0)

print(json.dumps({"ok": True, "rows": rows}))
"""


if __name__ == "__main__":
    main()
