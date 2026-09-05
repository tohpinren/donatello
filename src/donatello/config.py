"""YAML experiment config loading and validation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

import yaml

Benchmark = Literal["bird", "spider2"]
BirdMode = Literal["dev", "train", "full"]
MutationProfile = Literal["canonical"]

BENCHMARKS = {"bird", "spider2"}
BIRD_MODES = {"dev", "train", "full"}
MUTATION_PROFILES = {"canonical"}
STAGE_FIELDS = (
    "normalize",
    "mutate",
    "equivalence",
    "execute",
    "counterexamples",
    "augment",
    "report",
)


class ConfigError(ValueError):
    """Raised when an experiment config is missing or invalid."""


@dataclass(frozen=True)
class StageConfig:
    """Boolean stage switches for one experiment run."""

    normalize: bool = True
    mutate: bool = True
    equivalence: bool = True
    execute: bool = True
    counterexamples: bool = True
    augment: bool = True
    report: bool = True

    def enabled(self) -> list[str]:
        """Return enabled stage names in declaration order."""

        return [name for name, enabled in vars(self).items() if enabled]


@dataclass(frozen=True)
class TimeoutConfig:
    """Timeout settings for external tools used in later stages."""

    verieql_seconds: int = 120
    sqlite_seconds: int = 120
    sqldriller_seconds: int = 300


@dataclass(frozen=True)
class ToolConfig:
    """Optional paths to external non-Python tools."""

    verieql_path: Path | None = None
    verieql_python: Path | None = None
    sqldriller_path: Path | None = None
    sqldriller_python: Path | None = None


@dataclass(frozen=True)
class ExperimentConfig:
    """Validated experiment settings consumed by the pipeline."""

    experiment_name: str
    benchmark: Benchmark
    bird_mode: BirdMode | None
    data_root: Path
    output_dir: Path
    mutation_profile: MutationProfile = "canonical"
    workers: int = 1
    resume: bool = True
    overwrite: bool = False
    stages: StageConfig = field(default_factory=StageConfig)
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    tools: ToolConfig = field(default_factory=ToolConfig)


def load_config(path: Path | str) -> ExperimentConfig:
    """Load and validate a YAML experiment config."""

    raw = _read_yaml(Path(path))
    stages = _mapping(raw.get("stages"), "stages", allow_none=True)
    timeouts = _mapping(raw.get("timeouts"), "timeouts", allow_none=True)
    tools = _mapping(raw.get("tools"), "tools", allow_none=True)
    benchmark = cast(Benchmark, _choice(raw, "benchmark", BENCHMARKS))

    config = ExperimentConfig(
        experiment_name=_str(raw, "experiment_name"),
        benchmark=benchmark,
        bird_mode=_bird_mode(raw, benchmark),
        data_root=_path(raw, "data_root"),
        output_dir=_path(raw, "output_dir"),
        mutation_profile=cast(
            MutationProfile,
            _choice(raw, "mutation_profile", MUTATION_PROFILES, "canonical"),
        ),
        workers=_int(raw, "workers", 1),
        resume=_bool(raw, "resume", True),
        overwrite=_bool(raw, "overwrite", False),
        stages=StageConfig(
            **cast(Any, {name: _bool(stages, name, True) for name in STAGE_FIELDS})
        ),
        timeouts=TimeoutConfig(
            verieql_seconds=_int(timeouts, "verieql_seconds", 120),
            sqlite_seconds=_int(timeouts, "sqlite_seconds", 120),
            sqldriller_seconds=_int(timeouts, "sqldriller_seconds", 300),
        ),
        tools=ToolConfig(
            verieql_path=_maybe_path(tools.get("verieql_path")),
            verieql_python=_maybe_path(tools.get("verieql_python")),
            sqldriller_path=_maybe_path(tools.get("sqldriller_path")),
            sqldriller_python=_maybe_path(tools.get("sqldriller_python")),
        ),
    )
    _validate(config)
    return config


def _read_yaml(path: Path) -> dict[str, object]:
    """Read a YAML config file into a string-keyed mapping."""

    if not path.exists():
        raise ConfigError(f"Config file does not exist: {path}")

    try:
        with path.open("r", encoding="utf-8") as file:
            raw = yaml.safe_load(file)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc

    return _mapping(raw, "config")


def _validate(config: ExperimentConfig) -> None:
    """Validate cross-field constraints after parsing a config."""

    if config.workers < 1:
        raise ConfigError("workers must be at least 1.")
    if config.timeouts.verieql_seconds < 1:
        raise ConfigError("timeouts.verieql_seconds must be at least 1.")
    if config.timeouts.sqlite_seconds < 1:
        raise ConfigError("timeouts.sqlite_seconds must be at least 1.")
    if config.timeouts.sqldriller_seconds < 1:
        raise ConfigError("timeouts.sqldriller_seconds must be at least 1.")
    if config.resume and config.overwrite:
        raise ConfigError("resume and overwrite cannot both be true.")


def _mapping(
    value: object, name: str, *, allow_none: bool = False
) -> dict[str, object]:
    """Validate and normalize a YAML mapping node."""

    if value is None and allow_none:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping.")

    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ConfigError(f"{name} keys must be strings.")
        result[key] = item
    return result


def _str(mapping: dict[str, object], name: str, default: str | None = None) -> str:
    """Read a required non-empty string field from a mapping."""

    value = mapping.get(name, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string.")
    return value


def _choice(
    mapping: dict[str, object],
    name: str,
    allowed: set[str],
    default: str | None = None,
) -> str:
    """Read a string field constrained to a finite set of values."""

    value = _str(mapping, name, default)
    if value not in allowed:
        raise ConfigError(f"{name} must be one of: {', '.join(sorted(allowed))}.")
    return value


def _bird_mode(mapping: dict[str, object], benchmark: Benchmark) -> BirdMode | None:
    """Read the BIRD mode while allowing non-BIRD configs to omit it."""

    if benchmark == "bird":
        return cast(BirdMode, _choice(mapping, "bird_mode", BIRD_MODES))

    value = mapping.get("bird_mode")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("bird_mode must be a non-empty string when provided.")
    if value not in BIRD_MODES:
        raise ConfigError(f"bird_mode must be one of: {', '.join(sorted(BIRD_MODES))}.")
    return cast(BirdMode, value)


def _path(mapping: dict[str, object], name: str) -> Path:
    """Expand environment variables and `~` in a required path field."""

    return Path(os.path.expandvars(_str(mapping, name))).expanduser()


def _maybe_path(value: object) -> Path | None:
    """Normalize an optional path-like config value."""

    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("tool paths must be null or non-empty path strings.")
    return Path(os.path.expandvars(value)).expanduser()


def _int(mapping: dict[str, object], name: str, default: int) -> int:
    """Read an integer field while rejecting booleans."""

    value = mapping.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name} must be an integer.")
    return value


def _bool(mapping: dict[str, object], name: str, default: bool) -> bool:
    """Read a strict boolean field from a mapping."""

    value = mapping.get(name, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be true or false.")
    return value
