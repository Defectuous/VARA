import sqlite3
from contextlib import closing
from pathlib import Path


def _check(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise RuntimeError(f"{path} failed its integrity check: {result}")


def _copy(source: Path, destination: Path) -> None:
    with (
        closing(sqlite3.connect(source)) as src,
        closing(sqlite3.connect(destination)) as dst,
    ):
        src.backup(dst)


def backup_database(database: Path, destination: Path) -> None:
    """Consistent online copy of the database (safe while the server is running), verified."""
    if not database.exists():
        raise FileNotFoundError(database)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"{destination} already exists; choose a new backup file")
    _copy(database, destination)
    _check(destination)


def restore_database(backup: Path, database: Path) -> None:
    """Replace the database with a verified backup. Stop the server first."""
    _check(backup)
    database.parent.mkdir(parents=True, exist_ok=True)
    _copy(backup, database)
    _check(database)
