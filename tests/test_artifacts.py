from pathlib import Path

from sqlmt.artifacts import artifacts_for


def test_artifact_layout_paths() -> None:
    artifacts = artifacts_for(Path("outputs/bird-dev"))

    assert artifacts.config_snapshot == Path("outputs/bird-dev/config.yaml")
    assert artifacts.run_log == Path("outputs/bird-dev/run.log")
    assert artifacts.normalized_examples == Path(
        "outputs/bird-dev/normalized_examples.jsonl"
    )
    assert artifacts.mutants == Path("outputs/bird-dev/mutants.jsonl")
    assert artifacts.equivalence == Path("outputs/bird-dev/equivalence.jsonl")
    assert artifacts.execution == Path("outputs/bird-dev/execution.jsonl")
    assert artifacts.counterexamples == Path("outputs/bird-dev/counterexamples.jsonl")
    assert artifacts.counterexamples_dir == Path("outputs/bird-dev/counterexamples")
    assert artifacts.augmented_databases_dir == Path(
        "outputs/bird-dev/augmented_databases"
    )
    assert artifacts.report == Path("outputs/bird-dev/report.md")
