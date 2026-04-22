from pathlib import Path

from typer.testing import CliRunner

from sqlmt.cli import app

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
    assert "normalize, mutate, equivalence, execute" in result.stdout
    assert not output_dir.exists()
