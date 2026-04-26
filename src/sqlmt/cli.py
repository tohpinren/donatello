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
    preflight_run,
    prepare_run,
    run_equivalence_filtering,
    run_mutation_generation,
    run_normalization,
    run_sqlite_execution,
)

app = typer.Typer(
    help="SQL mutation testing framework for Text-to-SQL benchmark evaluation.",
    no_args_is_help=True,
)
config_app = typer.Typer(help="Inspect and validate experiment configs.")
app.add_typer(config_app, name="config")


def _version_callback(value: bool) -> None:
    """Print the package version and stop command execution."""

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
def check_env(
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            "-c",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="Optional experiment config for path and tool checks.",
        ),
    ] = None,
) -> None:
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

    if config is None:
        return

    try:
        loaded = load_config(config)
    except ConfigError as exc:
        typer.echo(f"FAIL config: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    _check_path("data_root", loaded.data_root, must_exist=True)
    output_parent = loaded.output_dir.parent
    _check_path("output_parent", output_parent, must_exist=output_parent.exists())
    disk_usage = shutil.disk_usage(
        output_parent if output_parent.exists() else Path(".")
    )
    typer.echo(f"OK   output_disk_free: {disk_usage.free // (1024 * 1024)} MiB")
    if loaded.tools.verieql_path is not None:
        _check_path("tools.verieql_path", loaded.tools.verieql_path, must_exist=True)
    if loaded.tools.verieql_python is not None:
        _check_path(
            "tools.verieql_python",
            loaded.tools.verieql_python,
            must_exist=True,
        )
    if loaded.tools.sqldriller_path is not None:
        _check_path(
            "tools.sqldriller_path",
            loaded.tools.sqldriller_path,
            must_exist=True,
        )


def _check_path(name: str, path: Path, *, must_exist: bool) -> None:
    """Print a simple environment path check."""

    if must_exist and path.exists():
        typer.echo(f"OK   {name}: {path}")
    elif must_exist:
        typer.echo(f"FAIL {name}: {path} does not exist")
        raise typer.Exit(code=1)
    else:
        typer.echo(f"WARN {name}: {path} does not exist")


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

    try:
        preflight_run(loaded)
    except PipelineError as exc:
        typer.echo(f"Pipeline error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    artifacts = prepare_run(loaded)
    configure_logging(artifacts.run_log)

    normalization_summary = None
    mutation_summary = None
    equivalence_summary = None
    execution_summary = None

    try:
        if loaded.stages.normalize:
            normalization_summary = run_normalization(
                loaded,
                config_path=config,
                artifacts=artifacts,
            )
        if loaded.stages.mutate:
            mutation_summary = run_mutation_generation(
                loaded,
                config_path=config,
                artifacts=artifacts,
            )
        if loaded.stages.equivalence:
            equivalence_summary = run_equivalence_filtering(
                loaded,
                artifacts=artifacts,
            )
        if loaded.stages.execute:
            execution_summary = run_sqlite_execution(
                loaded,
                artifacts=artifacts,
            )
    except PipelineError as exc:
        typer.echo(f"Pipeline error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if normalization_summary is not None:
        action = "Reused" if normalization_summary.resumed else "Normalized"
        typer.echo(
            f"{action} "
            f"{normalization_summary.example_count} examples across "
            f"{normalization_summary.database_count} databases "
            f"for {normalization_summary.benchmark}/{normalization_summary.mode}."
        )
        typer.echo(f"Wrote: {normalization_summary.output_path}")

    if mutation_summary is not None:
        action = "Reused mutations" if mutation_summary.resumed else "Mutated"
        typer.echo(
            f"{action} "
            f"{mutation_summary.generated_count} generated and "
            f"{mutation_summary.skipped_count} skipped mutation attempts "
            f"for {mutation_summary.benchmark}/{mutation_summary.profile}."
        )
        typer.echo(f"Wrote: {mutation_summary.output_path}")

    if equivalence_summary is not None:
        action = (
            "Reused equivalence"
            if equivalence_summary.resumed
            else "Filtered equivalence"
        )
        typer.echo(
            f"{action} for {equivalence_summary.checked_count} generated mutants: "
            f"{equivalence_summary.proven_equivalent_count} proven equivalent, "
            f"{equivalence_summary.proven_non_equivalent_count} proven "
            f"non-equivalent, {equivalence_summary.timeout_count} timeout, "
            f"{equivalence_summary.unsupported_count} unsupported, "
            f"{equivalence_summary.error_count} error."
        )
        typer.echo(f"Wrote: {equivalence_summary.output_path}")

    if execution_summary is not None:
        action = (
            "Reused execution" if execution_summary.resumed else "Executed"
        )
        score = (
            f"{execution_summary.mutation_score:.4f}"
            if execution_summary.mutation_score is not None
            else "n/a"
        )
        typer.echo(
            f"{action} {execution_summary.executed_count} retained mutants: "
            f"{execution_summary.killed_count} killed, "
            f"{execution_summary.survived_count} survived, "
            f"{execution_summary.error_count} error, "
            f"mutation score {score}."
        )
        typer.echo(f"Wrote: {execution_summary.output_path}")

    skipped_stages = (
        execution_summary.skipped_stages
        if execution_summary is not None
        else (
            equivalence_summary.skipped_stages
            if equivalence_summary is not None
            else (
                mutation_summary.skipped_stages
                if mutation_summary is not None
                else (
                    normalization_summary.skipped_stages
                    if normalization_summary is not None
                    else []
                )
            )
        )
    )
    if skipped_stages:
        typer.echo("Skipped unimplemented stages: " + ", ".join(skipped_stages))
