"""Helpers for resolving persisted SQLite artifact paths across environments."""

from __future__ import annotations

from pathlib import Path


def resolve_sqlite_path(
    *,
    sqlite_path: str,
    sqlite_relpath: str | None,
    data_root: Path,
) -> Path:
    """Resolve a persisted SQLite path against the current configured data root.

    Artifacts may contain absolute host paths from an earlier run environment.
    When those paths no longer exist, prefer rebuilding the location from the
    stable benchmark-relative ``sqlite_relpath`` under the current ``data_root``.
    """

    candidate = Path(sqlite_path)
    if candidate.exists() or not sqlite_relpath:
        return candidate
    return data_root / sqlite_relpath
