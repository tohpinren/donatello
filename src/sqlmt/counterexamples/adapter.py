"""Thin subprocess adapter around SQLDriller exec-mode counterexample generation."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


def main() -> None:
    """Generate one counterexample through SQLDriller and write JSON output."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--request-json", required=True)
    parser.add_argument("--result-json", required=True)
    args = parser.parse_args()

    request = json.loads(Path(args.request_json).read_text(encoding="utf-8"))
    sqldriller_root = Path(str(request["sqldriller_root"]))
    sys.path.insert(0, str(sqldriller_root))

    mutant_id = str(request["mutant_id"])
    db_id = str(request["db_id"])
    benchmark = str(request["benchmark"])
    gold_sql = str(request["gold_sql"])
    mutant_sql = str(request["mutant_sql"])
    source_sqlite_path = Path(str(request["source_sqlite_path"]))
    output_dir = Path(str(request["output_dir"]))

    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="sqldriller-adapter-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        workspace = temp_dir / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        os.chdir(workspace)
        _symlink_dir(sqldriller_root / "utils", workspace / "utils")
        _symlink_dir(sqldriller_root / "third_party", workspace / "third_party")
        _materialize_sample_value_db(
            source_sqlite_path,
            benchmark=benchmark,
            db_id=db_id,
            workspace=workspace,
        )

        constants = importlib.import_module("utils.constants")
        utils = importlib.import_module("utils.utils")
        sql_equiv_mode = constants.sql_equiv_mode
        check_equivalence = utils.check_equivalence
        simplify_ce = utils.simplify_ce

        schema_db_dir = temp_dir / "schema"
        db_dir = temp_dir / "fuzz"
        _materialize_db_tree(
            source_sqlite_path,
            root=schema_db_dir,
            db_id=db_id,
            sqlite_name=f"{db_id}.sqlite",
        )
        _materialize_db_tree(
            source_sqlite_path,
            root=db_dir,
            db_id=db_id,
            sqlite_name=f"{db_id}.sqlite",
        )

        raw_tag, counterexample_path = check_equivalence(
            gold_sql,
            mutant_sql,
            None,
            str(schema_db_dir),
            str(db_dir),
            db_id,
            sql_equiv_mode.exec,
            benchmark,
        )
        final_path = None
        if raw_tag == 0:
            if counterexample_path is None:
                raise RuntimeError(
                    "SQLDriller reported a counterexample without a path."
                )
            final_path = simplify_ce(
                str(counterexample_path),
                gold_sql,
                mutant_sql,
                mutant_id,
                str(output_dir),
            )
            if final_path is None:
                raise RuntimeError("SQLDriller counterexample minimization failed.")

    Path(args.result_json).write_text(
        json.dumps(
            {
                "raw_tag": raw_tag,
                "counterexample_path": final_path,
            },
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )


def _materialize_db_tree(
    source_sqlite_path: Path,
    *,
    root: Path,
    db_id: str,
    sqlite_name: str,
) -> None:
    """Create the db directory layout SQLDriller expects for one database."""

    target_dir = root / db_id
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_sqlite_path, target_dir / sqlite_name)


def _materialize_sample_value_db(
    source_sqlite_path: Path,
    *,
    benchmark: str,
    db_id: str,
    workspace: Path,
) -> None:
    """Create the benchmark-relative sample-value SQLite path SQLDriller expects."""

    target_dir = workspace / "data" / benchmark / "schema_for_sample_values" / db_id
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_sqlite_path, target_dir / f"{db_id}.sqlite")


def _symlink_dir(source: Path, target: Path) -> None:
    """Expose SQLDriller's relative third-party paths inside the temp workspace."""

    if target.exists():
        return
    target.symlink_to(source, target_is_directory=True)


if __name__ == "__main__":
    main()
