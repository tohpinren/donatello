from __future__ import annotations

import sys
from pathlib import Path

from typer.testing import CliRunner

from donatello.cli import app
from tests.helpers import read_jsonl, write_bird_fixture, write_config

runner = CliRunner()


def test_cli_run_writes_equivalence_output(tmp_path: Path) -> None:
    data_root = write_bird_fixture(tmp_path)
    output_dir = tmp_path / "outputs" / "bird-dev"
    verieql_root = _write_fake_verieql(tmp_path)
    config_path = write_config(
        tmp_path / "bird-dev.yaml",
        data_root=data_root,
        output_dir=output_dir,
        mutate=True,
        equivalence=True,
        verieql_path=verieql_root,
        verieql_python=Path(sys.executable),
    )

    result = runner.invoke(app, ["run", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Filtered equivalence" in result.stdout
    assert output_dir.joinpath("equivalence.jsonl").exists()
    rows = read_jsonl(output_dir / "equivalence.jsonl")
    assert rows
    assert {row["outcome"] for row in rows} == {"proven_non_equivalent"}


def test_cli_run_equivalence_only_requires_existing_mutants(tmp_path: Path) -> None:
    data_root = write_bird_fixture(tmp_path)
    output_dir = tmp_path / "outputs" / "bird-dev"
    config_path = write_config(
        tmp_path / "bird-dev.yaml",
        data_root=data_root,
        output_dir=output_dir,
        normalize=False,
        mutate=False,
        equivalence=True,
        verieql_path=tmp_path / "VeriEQL",
        verieql_python=Path(sys.executable),
        resume=False,
    )

    result = runner.invoke(app, ["run", "--config", str(config_path)])

    assert result.exit_code == 2
    assert "mutants.jsonl does not exist" in result.stdout + result.stderr
    assert not output_dir.exists()


def _write_fake_verieql(tmp_path: Path) -> Path:
    root = tmp_path / "VeriEQL"
    script = root / "parallel" / "cli_within_timeout.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        """
from __future__ import annotations

import argparse
import json

parser = argparse.ArgumentParser()
parser.add_argument("-f", "--file", required=True)
parser.add_argument("-t", "--timeout", type=int)
parser.add_argument("-c", "--cores", type=int)
parser.add_argument("-o", "--out_file", required=True)
args = parser.parse_args()

with open(args.file, "r", encoding="utf-8") as reader, open(
    args.out_file, "w", encoding="utf-8"
) as writer:
    for line in reader:
        row = json.loads(line)
        writer.write(
            json.dumps(
                {
                    "index": row["index"],
                    "file": row["file"],
                    "states": ["NEQ"],
                    "times": [[0.01, 0.02]],
                    "counterexample": None,
                    "err": None,
                }
            )
            + "\\n"
        )
""",
        encoding="utf-8",
    )
    return root
