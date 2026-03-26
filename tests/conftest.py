"""Shared test fixtures."""

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def schema_path():
    """Path to schema.sql."""
    return Path(__file__).parent.parent / "schema.sql"


@pytest.fixture
def tmp_db(schema_path):
    """In-memory SQLite database with schema loaded."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(schema_path.read_text())
    yield conn
    conn.close()
