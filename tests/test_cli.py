from __future__ import annotations

import time
from pathlib import Path

from typer.testing import CliRunner

from donatello.cli import app
from tests.helpers import (
    read_jsonl,
    write_bird_fixture,
    write_config,
    write_spider2_fixture,
)

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

    rows = read_jsonl(artifact)
    assert len(rows) == 1
    assert rows[0]["example_id"] == "bird:dev:000000"
    assert rows[0]["schema"]["table_names"] == ["dev_table"]


def test_cli_run_writes_mutant_output_and_skips_only_later_stages(
    tmp_path: Path,
) -> None:
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
    assert "Mutated" in result.stdout
    assert output_dir.joinpath("mutants.jsonl").exists()
    assert "mutants.jsonl" in result.stdout


def test_cli_run_log_contains_stage_debug_markers(tmp_path: Path) -> None:
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
    run_log = output_dir / "run.log"
    log_text = run_log.read_text(encoding="utf-8")
    assert "Starting experiment name=test-run" in log_text
    assert "Running stage=normalize" in log_text
    assert "Normalization stage complete" in log_text
    assert "Running stage=mutate" in log_text
    assert "Mutation stage complete" in log_text
    assert "Experiment completed successfully" in log_text


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


def test_cli_run_supports_spider2_benchmark(tmp_path: Path, monkeypatch) -> None:
    data_root = write_spider2_fixture(tmp_path)
    output_dir = tmp_path / "outputs" / "spider2"
    config_path = write_config(
        tmp_path / "spider2.yaml",
        data_root=data_root,
        output_dir=output_dir,
        benchmark="spider2",
        bird_mode=None,
    )
    from donatello.benchmarks import spider2 as spider2_benchmark

    monkeypatch.setattr(
        spider2_benchmark,
        "EXPECTED_INSTANCE_IDS",
        ["local003", "local004"],
    )

    result = runner.invoke(app, ["run", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Normalized 2 examples across 1 databases for spider2/dev." in result.stdout
    artifact = output_dir / "normalized_examples.jsonl"
    assert artifact.exists()
    rows = read_jsonl(artifact)
    assert [row["example_id"] for row in rows] == [
        "spider2:dev:local003",
        "spider2:dev:local004",
    ]


def test_cli_run_requires_an_implemented_stage(tmp_path: Path) -> None:
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


def test_cli_run_mutate_only_uses_existing_normalized_examples(tmp_path: Path) -> None:
    data_root = write_bird_fixture(tmp_path)
    output_dir = tmp_path / "outputs" / "bird-dev"
    normalize_config = write_config(
        tmp_path / "bird-dev-normalize.yaml",
        data_root=data_root,
        output_dir=output_dir,
        normalize=True,
        mutate=False,
    )
    mutate_config = write_config(
        tmp_path / "bird-dev-mutate.yaml",
        data_root=data_root,
        output_dir=output_dir,
        normalize=False,
        mutate=True,
        resume=False,
    )

    first = runner.invoke(app, ["run", "--config", str(normalize_config)])
    second = runner.invoke(app, ["run", "--config", str(mutate_config)])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert output_dir.joinpath("normalized_examples.jsonl").exists()
    assert output_dir.joinpath("mutants.jsonl").exists()


def test_cli_run_mutate_only_requires_existing_normalized_examples(
    tmp_path: Path,
) -> None:
    data_root = write_bird_fixture(tmp_path)
    output_dir = tmp_path / "outputs" / "bird-dev"
    config_path = write_config(
        tmp_path / "bird-dev-mutate.yaml",
        data_root=data_root,
        output_dir=output_dir,
        normalize=False,
        mutate=True,
        resume=False,
    )

    result = runner.invoke(app, ["run", "--config", str(config_path)])

    assert result.exit_code == 2
    assert "normalized_examples.jsonl does not exist" in result.stdout + result.stderr
    assert not output_dir.exists()


def test_cli_run_resumes_existing_mutants(tmp_path: Path) -> None:
    data_root = write_bird_fixture(tmp_path)
    output_dir = tmp_path / "outputs" / "bird-dev"
    config_path = write_config(
        tmp_path / "bird-dev-mutate.yaml",
        data_root=data_root,
        output_dir=output_dir,
        normalize=True,
        mutate=True,
        resume=True,
        overwrite=False,
    )

    first = runner.invoke(app, ["run", "--config", str(config_path)])

    artifact = output_dir / "mutants.jsonl"
    artifact_mtime = artifact.stat().st_mtime_ns
    time.sleep(0.01)

    second = runner.invoke(app, ["run", "--config", str(config_path)])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "Reused mutations" in second.stdout
    assert artifact.stat().st_mtime_ns == artifact_mtime


def test_cli_run_existing_mutants_require_resume_or_overwrite(tmp_path: Path) -> None:
    data_root = write_bird_fixture(tmp_path)
    output_dir = tmp_path / "outputs" / "bird-dev"
    first_config = write_config(
        tmp_path / "bird-dev-first.yaml",
        data_root=data_root,
        output_dir=output_dir,
        normalize=True,
        mutate=True,
        resume=True,
        overwrite=False,
    )
    second_config = write_config(
        tmp_path / "bird-dev-second.yaml",
        data_root=data_root,
        output_dir=output_dir,
        normalize=False,
        mutate=True,
        resume=False,
        overwrite=False,
    )

    first = runner.invoke(app, ["run", "--config", str(first_config)])
    second = runner.invoke(app, ["run", "--config", str(second_config)])

    assert first.exit_code == 0
    assert second.exit_code == 2
    assert "Mutation outputs already exist" in second.stdout + second.stderr
