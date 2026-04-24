"""Command line interface for sqlmt."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Annotated

import typer

from sqlmt import __version__
from sqlmt.config import ConfigError, load_config
from sqlmt.logging import configure_logging
from sqlmt.pipeline import (
    PipelineError,
    build_dry_run_plan,
    prepare_run,
    run_normalization,
)

app = typer.Typer(
    help="SQL mutation testing framework for Text-to-SQL benchmark evaluation.",
    no_args_is_help=True,
)
config_app = typer.Typer(help="Inspect and validate experiment configs.")
app.add_typer(config_app, name="config")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"sqlmt {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the sqlmt version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Root command callback."""


@app.command("check-env")
def check_env() -> None:
    """Check local foundation dependencies and external tool availability."""

    checks = {
        "python_sqlite": sqlite3.sqlite_version,
        "uv": shutil.which("uv"),
        "ruff": shutil.which("ruff"),
        "ty": shutil.which("ty"),
        "VeriEQL": shutil.which("VeriEQL"),
        "SQLDriller": shutil.which("SQLDriller"),
    }

    for name, value in checks.items():
        if value:
            typer.echo(f"OK   {name}: {value}")
        elif name in {"VeriEQL", "SQLDriller"}:
            typer.echo(f"WARN {name}: not found on PATH")
        else:
            typer.echo(f"FAIL {name}: not found")
            raise typer.Exit(code=1)


@config_app.command("validate")
def validate_config(
    config: Annotated[
        Path,
        typer.Option(
            "--config",
            "-c",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="Path to an experiment YAML config.",
        ),
    ],
) -> None:
    """Validate an experiment config file."""

    try:
        loaded = load_config(config)
    except ConfigError as exc:
        typer.echo(f"Invalid config: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(f"Config valid: {loaded.experiment_name}")


@app.command()
def run(
    config: Annotated[
        Path,
        typer.Option(
            "--config",
            "-c",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="Path to an experiment YAML config.",
        ),
    ],
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Resolve config and artifacts without running pipeline stages.",
        ),
    ] = False,
) -> None:
    """Run an experiment pipeline."""

    try:
        loaded = load_config(config)
    except ConfigError as exc:
        typer.echo(f"Invalid config: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if dry_run:
        plan = build_dry_run_plan(loaded)
        typer.echo(f"Experiment: {plan.experiment_name}")
        typer.echo(f"Output directory: {plan.output_dir}")
        typer.echo(f"Enabled stages: {', '.join(plan.enabled_stages)}")
        typer.echo("Artifacts:")
        for name, path in plan.artifacts.items():
            typer.echo(f"  {name}: {path}")
        return

    artifacts = prepare_run(loaded)
    configure_logging(artifacts.run_log)

    try:
        summary = run_normalization(loaded, config_path=config, artifacts=artifacts)
    except PipelineError as exc:
        typer.echo(f"Pipeline error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    typer.echo(
        "Normalized "
        f"{summary.example_count} examples across {summary.database_count} databases "
        f"for {summary.benchmark}/{summary.mode}."
    )
    typer.echo(f"Wrote: {summary.output_path}")
    if summary.skipped_stages:
        typer.echo("Skipped unimplemented stages: " + ", ".join(summary.skipped_stages))
