"""SQLite helpers. Safe to run repeatedly."""

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "news.db"
SCHEMA = Path(__file__).resolve().parent / "schema.sql"


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = connect(path)
    conn.executescript(SCHEMA.read_text())
    conn.commit()
    return conn


if __name__ == "__main__":
    c = init()
    tables = [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    print(f"{DB_PATH} ready — tables: {', '.join(tables)}")
