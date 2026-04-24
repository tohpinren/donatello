from __future__ import annotations

import json
import time
from pathlib import Path

from typer.testing import CliRunner

from sqlmt.cli import app
from tests.helpers import write_bird_fixture, write_config

runner = CliRunner()


def test_cli_config_validate_success() -> None:
    result = runner.invoke(
        app,
        [
            "config",
            "validate",
            "--config",
            "configs/smoke/bird-dev.yaml",
        ],
    )

    assert result.exit_code == 0
    assert "Config valid: bird-dev-smoke" in result.stdout


def test_cli_config_validate_failure(tmp_path: Path) -> None:
    config_path = tmp_path / "bad.yaml"
    config_path.write_text(
        """
experiment_name: bad
benchmark: invalid
bird_mode: dev
data_root: /tmp/bird
output_dir: outputs/bad
""",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["config", "validate", "--config", str(config_path)],
    )

    assert result.exit_code == 2
    assert "benchmark must be one of" in result.stdout + result.stderr


def test_cli_run_dry_run_does_not_require_data_root(tmp_path: Path) -> None:
    config_path = tmp_path / "dry-run.yaml"
    output_dir = tmp_path / "outputs" / "dry-run"
    config_path.write_text(
        f"""
experiment_name: dry-run
benchmark: bird
bird_mode: dev
data_root: /path/that/does/not/exist
output_dir: {output_dir}
""",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        [
            "run",
            "--config",
            str(config_path),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert "Experiment: dry-run" in result.stdout
    assert f"Output directory: {output_dir}" in result.stdout
    assert "Enabled stages: normalize, mutate, equivalence, execute" in result.stdout
    assert not output_dir.exists()


def test_cli_run_writes_normalized_output(tmp_path: Path) -> None:
    data_root = write_bird_fixture(tmp_path)
    output_dir = tmp_path / "outputs" / "bird-dev"
    config_path = write_config(
        tmp_path / "bird-dev.yaml",
        data_root=data_root,
        output_dir=output_dir,
    )

    result = runner.invoke(app, ["run", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Normalized 1 examples across 1 databases for bird/dev." in result.stdout
    assert output_dir.joinpath("config.yaml").exists()
    assert output_dir.joinpath("run.log").exists()
    artifact = output_dir / "normalized_examples.jsonl"
    assert artifact.exists()

    rows = [
        json.loads(line) for line in artifact.read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["example_id"] == "bird:dev:000000"
    assert rows[0]["schema"]["table_names"] == ["dev_table"]


def test_cli_run_reports_skipped_unimplemented_stages(tmp_path: Path) -> None:
    data_root = write_bird_fixture(tmp_path)
    output_dir = tmp_path / "outputs" / "bird-dev"
    config_path = write_config(
        tmp_path / "bird-dev.yaml",
        data_root=data_root,
        output_dir=output_dir,
        mutate=True,
    )

    result = runner.invoke(app, ["run", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Skipped unimplemented stages: mutate" in result.stdout


def test_cli_run_resumes_existing_normalized_output(tmp_path: Path) -> None:
    data_root = write_bird_fixture(tmp_path)
    output_dir = tmp_path / "outputs" / "bird-dev"
    config_path = write_config(
        tmp_path / "bird-dev.yaml",
        data_root=data_root,
        output_dir=output_dir,
        resume=True,
        overwrite=False,
    )

    first = runner.invoke(app, ["run", "--config", str(config_path)])

    artifact = output_dir / "normalized_examples.jsonl"
    config_snapshot = output_dir / "config.yaml"
    artifact_mtime = artifact.stat().st_mtime_ns
    config_mtime = config_snapshot.stat().st_mtime_ns
    time.sleep(0.01)

    second = runner.invoke(app, ["run", "--config", str(config_path)])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "Reused 1 examples across 1 databases for bird/dev." in second.stdout
    assert artifact.stat().st_mtime_ns == artifact_mtime
    assert config_snapshot.stat().st_mtime_ns == config_mtime


def test_cli_run_existing_output_requires_resume_or_overwrite(tmp_path: Path) -> None:
    data_root = write_bird_fixture(tmp_path)
    output_dir = tmp_path / "outputs" / "bird-dev"
    first_config = write_config(
        tmp_path / "bird-dev-first.yaml",
        data_root=data_root,
        output_dir=output_dir,
        resume=True,
        overwrite=False,
    )
    second_config = write_config(
        tmp_path / "bird-dev-second.yaml",
        data_root=data_root,
        output_dir=output_dir,
        resume=False,
        overwrite=False,
    )

    first = runner.invoke(app, ["run", "--config", str(first_config)])
    second = runner.invoke(app, ["run", "--config", str(second_config)])

    assert first.exit_code == 0
    assert second.exit_code == 2
    assert "Normalization outputs already exist" in second.stdout + second.stderr


def test_cli_run_rejects_unimplemented_benchmark(tmp_path: Path) -> None:
    output_dir = tmp_path / "outputs" / "spider2"
    config_path = write_config(
        tmp_path / "spider2.yaml",
        data_root=tmp_path / "spider2",
        output_dir=output_dir,
        benchmark="spider2",
        bird_mode=None,
    )

    result = runner.invoke(app, ["run", "--config", str(config_path)])

    assert result.exit_code == 2
    assert "not implemented yet" in result.stdout + result.stderr
    assert not output_dir.exists()


def test_cli_run_requires_normalize_stage(tmp_path: Path) -> None:
    data_root = write_bird_fixture(tmp_path)
    output_dir = tmp_path / "outputs" / "bird-dev"
    config_path = write_config(
        tmp_path / "bird-dev.yaml",
        data_root=data_root,
        output_dir=output_dir,
        normalize=False,
    )

    result = runner.invoke(app, ["run", "--config", str(config_path)])

    assert result.exit_code == 2
    assert "No implemented stage is enabled" in result.stdout + result.stderr
    assert not output_dir.exists()
