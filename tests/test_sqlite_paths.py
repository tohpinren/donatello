from __future__ import annotations

from pathlib import Path

from donatello.sqlite_paths import resolve_sqlite_path


def test_resolve_sqlite_path_uses_existing_persisted_path(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    candidate = tmp_path / "old-host" / "db.sqlite"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("sqlite", encoding="utf-8")

    resolved = resolve_sqlite_path(
        sqlite_path=str(candidate),
        sqlite_relpath="dev/db.sqlite",
        data_root=data_root,
    )

    assert resolved == candidate


def test_resolve_sqlite_path_remaps_safe_relative_path(
    tmp_path: Path,
) -> None:
    stale_candidate = tmp_path / "old-host" / "db.sqlite"
    data_root = tmp_path / "data"

    resolved = resolve_sqlite_path(
        sqlite_path=str(stale_candidate),
        sqlite_relpath="dev/db.sqlite",
        data_root=data_root,
    )

    assert resolved == data_root / "dev" / "db.sqlite"


def test_resolve_sqlite_path_rejects_absolute_relpath(
    tmp_path: Path,
) -> None:
    stale_candidate = tmp_path / "old-host" / "db.sqlite"

    resolved = resolve_sqlite_path(
        sqlite_path=str(stale_candidate),
        sqlite_relpath=str(tmp_path / "outside.sqlite"),
        data_root=tmp_path / "data",
    )

    assert resolved == stale_candidate


def test_resolve_sqlite_path_rejects_parent_traversal_relpath(
    tmp_path: Path,
) -> None:
    stale_candidate = tmp_path / "old-host" / "db.sqlite"

    resolved = resolve_sqlite_path(
        sqlite_path=str(stale_candidate),
        sqlite_relpath="dev/../../outside.sqlite",
        data_root=tmp_path / "data",
    )

    assert resolved == stale_candidate
