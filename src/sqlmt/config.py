"""YAML experiment config loading and validation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

import yaml

Benchmark = Literal["bird", "spider2"]
BirdMode = Literal["dev", "test", "full"]
MutationProfile = Literal["canonical"]

VALID_BENCHMARKS = {"bird", "spider2"}
VALID_BIRD_MODES = {"dev", "test", "full"}
VALID_MUTATION_PROFILES = {"canonical"}


class ConfigError(ValueError):
    """Raised when an experiment config is missing or invalid."""


@dataclass(frozen=True)
class StageConfig:
    normalize: bool = True
    mutate: bool = True
    equivalence: bool = True
    execute: bool = True
    counterexamples: bool = True
    augment: bool = True
    report: bool = True

    @classmethod
    def from_mapping(cls, value: object) -> StageConfig:
        mapping = _optional_mapping(value, "stages")
        return cls(**_known_bool_fields(mapping, cls, "stages"))

    def enabled(self) -> list[str]:
        return [name for name, enabled in vars(self).items() if enabled]


@dataclass(frozen=True)
class TimeoutConfig:
    verieql_seconds: int = 120
    sqldriller_seconds: int = 300

    @classmethod
    def from_mapping(cls, value: object) -> TimeoutConfig:
        mapping = _optional_mapping(value, "timeouts")
        data = _known_int_fields(mapping, cls, "timeouts")
        return cls(**data)


@dataclass(frozen=True)
class ToolConfig:
    verieql_path: Path | None = None
    sqldriller_path: Path | None = None

    @classmethod
    def from_mapping(cls, value: object) -> ToolConfig:
        mapping = _optional_mapping(value, "tools")
        _reject_unknown_fields(mapping, cls, "tools")
        return cls(
            verieql_path=_optional_path(mapping.get("verieql_path")),
            sqldriller_path=_optional_path(mapping.get("sqldriller_path")),
        )


@dataclass(frozen=True)
class ExperimentConfig:
    experiment_name: str
    benchmark: Benchmark
    bird_mode: BirdMode
    data_root: Path
    output_dir: Path
    mutation_profile: MutationProfile = "canonical"
    workers: int = 1
    resume: bool = True
    overwrite: bool = False
    stages: StageConfig = field(default_factory=StageConfig)
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    tools: ToolConfig = field(default_factory=ToolConfig)

    @classmethod
    def from_mapping(cls, value: object) -> ExperimentConfig:
        mapping = _required_mapping(value, "config")
        _reject_unknown_fields(mapping, cls, "config")

        experiment_name = _required_str(mapping, "experiment_name")
        benchmark = _required_choice(
            mapping,
            "benchmark",
            VALID_BENCHMARKS,
        )
        bird_mode = _required_choice(mapping, "bird_mode", VALID_BIRD_MODES)
        mutation_profile = _optional_choice(
            mapping,
            "mutation_profile",
            VALID_MUTATION_PROFILES,
            default="canonical",
        )

        return cls(
            experiment_name=experiment_name,
            benchmark=cast(Benchmark, benchmark),
            bird_mode=cast(BirdMode, bird_mode),
            data_root=_required_path(mapping, "data_root"),
            output_dir=_required_path(mapping, "output_dir"),
            mutation_profile=cast(MutationProfile, mutation_profile),
            workers=_optional_int(mapping, "workers", default=1),
            resume=_optional_bool(mapping, "resume", default=True),
            overwrite=_optional_bool(mapping, "overwrite", default=False),
            stages=StageConfig.from_mapping(mapping.get("stages")),
            timeouts=TimeoutConfig.from_mapping(mapping.get("timeouts")),
            tools=ToolConfig.from_mapping(mapping.get("tools")),
        )

    def validate_runtime_flags(self) -> None:
        if self.workers < 1:
            raise ConfigError("workers must be at least 1.")
        if self.timeouts.verieql_seconds < 1:
            raise ConfigError("timeouts.verieql_seconds must be at least 1.")
        if self.timeouts.sqldriller_seconds < 1:
            raise ConfigError("timeouts.sqldriller_seconds must be at least 1.")
        if self.resume and self.overwrite:
            raise ConfigError("resume and overwrite cannot both be true.")


def load_config(path: Path | str) -> ExperimentConfig:
    """Load and validate a YAML experiment config."""

    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file does not exist: {config_path}")

    try:
        with config_path.open("r", encoding="utf-8") as file:
            raw = yaml.safe_load(file)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {config_path}: {exc}") from exc

    config = ExperimentConfig.from_mapping(raw)
    config.validate_runtime_flags()
    return config


def _expand_path(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser()


def _required_path(mapping: dict[str, object], field_name: str) -> Path:
    value = mapping.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field_name} must be a non-empty path string.")
    return _expand_path(value)


def _optional_path(value: object) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("tool paths must be null or non-empty path strings.")
    return _expand_path(value)


def _required_str(mapping: dict[str, object], field_name: str) -> str:
    value = mapping.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field_name} must be a non-empty string.")
    return value


def _required_choice(
    mapping: dict[str, object],
    field_name: str,
    allowed: set[str],
) -> str:
    value = _required_str(mapping, field_name)
    if value not in allowed:
        allowed_values = ", ".join(sorted(allowed))
        raise ConfigError(f"{field_name} must be one of: {allowed_values}.")
    return value


def _optional_choice(
    mapping: dict[str, object],
    field_name: str,
    allowed: set[str],
    *,
    default: str,
) -> str:
    value = mapping.get(field_name, default)
    if not isinstance(value, str):
        raise ConfigError(f"{field_name} must be a string.")
    if value not in allowed:
        allowed_values = ", ".join(sorted(allowed))
        raise ConfigError(f"{field_name} must be one of: {allowed_values}.")
    return value


def _optional_int(
    mapping: dict[str, object],
    field_name: str,
    *,
    default: int,
) -> int:
    value = mapping.get(field_name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{field_name} must be an integer.")
    return value


def _optional_bool(
    mapping: dict[str, object],
    field_name: str,
    *,
    default: bool,
) -> bool:
    value = mapping.get(field_name, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{field_name} must be true or false.")
    return value


def _optional_mapping(value: object, name: str) -> dict[str, object]:
    if value is None:
        return {}
    return _required_mapping(value, name)


def _required_mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping.")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ConfigError(f"{name} keys must be strings.")
        result[key] = item
    return result


def _reject_unknown_fields(
    mapping: dict[str, object],
    model: type[Any],
    context: str,
) -> None:
    allowed = set(model.__dataclass_fields__)
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        fields = ", ".join(unknown)
        raise ConfigError(f"Unknown {context} field(s): {fields}.")


def _known_bool_fields(
    mapping: dict[str, object],
    model: type[Any],
    context: str,
) -> dict[str, bool]:
    _reject_unknown_fields(mapping, model, context)
    result: dict[str, bool] = {}
    for key, value in mapping.items():
        if not isinstance(value, bool):
            raise ConfigError(f"{context}.{key} must be true or false.")
        result[key] = value
    return result


def _known_int_fields(
    mapping: dict[str, object],
    model: type[Any],
    context: str,
) -> dict[str, int]:
    _reject_unknown_fields(mapping, model, context)
    result: dict[str, int] = {}
    for key, value in mapping.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{context}.{key} must be an integer.")
        result[key] = value
    return result
