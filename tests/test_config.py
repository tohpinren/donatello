from pathlib import Path

import pytest

from sqlmt.config import ConfigError, load_config


def test_valid_config_loading() -> None:
    config = load_config(Path("configs/smoke/bird-dev.yaml"))

    assert config.experiment_name == "bird-dev-smoke"
    assert config.benchmark == "bird"
    assert config.bird_mode == "dev"
    assert config.mutation_profile == "canonical"
    assert config.workers == 4
    assert config.stages.enabled() == ["normalize"]


def test_mutation_smoke_config_loading() -> None:
    config = load_config(Path("configs/smoke/bird-dev-mutate.yaml"))

    assert config.experiment_name == "bird-dev-mutate-smoke"
    assert config.stages.enabled() == ["normalize", "mutate"]


def test_train_bird_mode_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
experiment_name: train-mode
benchmark: bird
bird_mode: train
data_root: /tmp/bird
output_dir: outputs/train
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.bird_mode == "train"


def test_spider2_config_does_not_require_bird_mode(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
experiment_name: spider2-mode
benchmark: spider2
data_root: /tmp/spider2
output_dir: outputs/spider2
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.benchmark == "spider2"
    assert config.bird_mode is None


def test_invalid_benchmark_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
experiment_name: bad-benchmark
benchmark: other
bird_mode: dev
data_root: /tmp/bird
output_dir: outputs/bad
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="benchmark must be one of"):
        load_config(path)


def test_invalid_bird_mode_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
experiment_name: bad-mode
benchmark: bird
bird_mode: test
data_root: /tmp/bird
output_dir: outputs/bad
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="bird_mode must be one of"):
        load_config(path)


def test_environment_variable_expansion_in_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "bird"
    monkeypatch.setenv("BIRD_ROOT_TEST", str(data_root))
    path = tmp_path / "config.yaml"
    path.write_text(
        """
experiment_name: env-path
benchmark: bird
bird_mode: dev
data_root: ${BIRD_ROOT_TEST}
output_dir: ~/sqlmt-output-test
tools:
  verieql_path: ${BIRD_ROOT_TEST}/VeriEQL
  sqldriller_path: null
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.data_root == data_root
    assert config.output_dir == Path("~/sqlmt-output-test").expanduser()
    assert config.tools.verieql_path == data_root / "VeriEQL"
