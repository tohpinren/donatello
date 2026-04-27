from pathlib import Path


def test_stage_6_docs_state_exec_mode_and_defer_validation() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "SQLDriller `exec` mode only" in readme
    assert "augmentation-time validation is deferred" in readme
    design_path = Path("design.md")
    if design_path.exists():
        design = design_path.read_text(encoding="utf-8")
        assert "SQLDriller `exec` mode only" in design
        assert "validate every generated counterexample through SQLite" not in design
