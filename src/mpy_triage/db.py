"""Database connection management, schema initialization, and sync state helpers."""

import sqlite3
from pathlib import Path


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode and Row factory."""
    raise NotImplementedError


def init_db(conn: sqlite3.Connection, schema_path: Path) -> None:
    """Initialize database schema from schema.sql."""
    raise NotImplementedError


def get_sync_state(conn: sqlite3.Connection, key: str) -> str | None:
    """Get a sync state value by key."""
    raise NotImplementedError


def set_sync_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Set a sync state value."""
    raise NotImplementedError


def load_vec_extension(conn: sqlite3.Connection) -> None:
    """Load the sqlite-vec extension."""
    raise NotImplementedError
