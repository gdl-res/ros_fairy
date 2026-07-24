"""SQLite mission index.

The index is a query cache for `ros2 fair list`; the archive directories'
mission_record.json files are the source of truth, and reindex() can rebuild
the database from them at any time.
"""

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from fair_ros.manifest.schema import MissionRecord
from fair_ros.utils import paths

DB_VERSION = "2"


class IndexUnavailableError(Exception):
    """The index exists but this account cannot read it (permissions)."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS missions (
    mission_id        TEXT PRIMARY KEY,
    created_at        TEXT NOT NULL,
    operator          TEXT NOT NULL,
    location          TEXT NOT NULL,
    goal              TEXT NOT NULL,
    archive_path      TEXT NOT NULL UNIQUE,
    duration_s        REAL NOT NULL DEFAULT 0,
    size_bytes        INTEGER NOT NULL DEFAULT 0,
    bag_count         INTEGER NOT NULL DEFAULT 0,
    warning_count     INTEGER NOT NULL DEFAULT 0,
    robot_name        TEXT,
    fair_ros_version  TEXT,
    schema_version    TEXT NOT NULL,
    data_quality      TEXT
);
CREATE INDEX IF NOT EXISTS idx_missions_created
    ON missions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_missions_operator
    ON missions(operator COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_missions_location
    ON missions(location COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
"""


# Column order is the single source of truth for the positional INSERTs below.
_COLUMNS = (
    "mission_id", "created_at", "operator", "location", "goal", "archive_path",
    "duration_s", "size_bytes", "bag_count", "warning_count", "robot_name",
    "fair_ros_version", "schema_version", "data_quality",
)
_INSERT = (f"INSERT OR REPLACE INTO missions ({', '.join(_COLUMNS)}) VALUES "
           f"({', '.join('?' for _ in _COLUMNS)})")


def _connect() -> sqlite3.Connection:
    db = paths.index_db_path()
    con = sqlite3.connect(db, timeout=5.0)
    con.row_factory = sqlite3.Row
    # Group-writable so an index created by one account (e.g. root during
    # setup) doesn't lock out the next operator's mission_close insert.
    try:
        os.chmod(db, 0o664)
    except OSError:
        pass
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 5000")
    con.executescript(_SCHEMA)
    # Migrate pre-2 databases: add columns the current schema expects.
    existing = {row[1] for row in con.execute("PRAGMA table_info(missions)")}
    if "data_quality" not in existing:
        con.execute("ALTER TABLE missions ADD COLUMN data_quality TEXT")
    con.execute("INSERT OR REPLACE INTO meta VALUES ('db_version', ?)",
                (DB_VERSION,))
    return con


def _connect_readonly() -> sqlite3.Connection:
    """A connection that cannot write — queries must not create or migrate.

    WAL reads still need the -shm sidecar, so an account without write access
    to the index directory raises IndexUnavailableError with a plain-language
    fix instead of sqlite3's traceback.
    """
    db = paths.index_db_path()
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout = 5000")
        # Probe the WAL sidecar now so the failure is caught in one place.
        con.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        return con
    except sqlite3.OperationalError as exc:
        raise IndexUnavailableError(
            f"I don't have permission to read the mission index ({db}). "
            "Ask your engineer to add your account to the 'fair-ros' group "
            "(sudo usermod -aG fair-ros <your username>), then log out and "
            "back in.") from exc


def _row_from_record(record: MissionRecord, archive_path: Path) -> tuple:
    return (
        record.identity.mission_id,
        record.identity.created_at.isoformat(),
        record.identity.operator_name,
        record.intent.location_name,
        record.intent.goal,
        str(archive_path),
        sum(b.duration_s or 0 for b in record.bags),
        sum(b.size_bytes for b in record.bags),
        len(record.bags),
        sum(len(b.health_warnings) for b in record.bags),
        record.robot.name if record.robot else None,
        record.provenance.fair_ros_version,
        record.schema_version,
        record.provenance.data_quality,
    )


def insert(record: MissionRecord, archive_path: Path) -> None:
    with _connect() as con:
        con.execute(_INSERT, _row_from_record(record, archive_path))


def query(operator: str | None = None, location: str | None = None,
          since: str | None = None, until: str | None = None,
          quality: str | None = None,
          limit: int = 20) -> tuple[list[dict[str, Any]], int]:
    """Filtered mission rows, newest first. Returns (rows, total_matching)."""
    where, params = [], []
    if operator:
        where.append("operator LIKE ? COLLATE NOCASE")
        params.append(f"%{operator}%")
    if location:
        where.append("location LIKE ? COLLATE NOCASE")
        params.append(f"%{location}%")
    if quality:
        where.append("data_quality = ?")
        params.append(quality)
    if since:
        where.append("created_at >= ?")
        params.append(since)
    if until:
        where.append("created_at < ?")
        # until is an inclusive date; bump to the end of that day
        params.append(until + "T23:59:59.999999+00:00"
                      if len(until) == 10 else until)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    if not Path(paths.index_db_path()).exists():
        return [], 0
    try:
        con = _connect()
    except sqlite3.OperationalError:
        # No write access (index dir belongs to the fair-ros group): reading
        # is still fine — WAL setup and schema migration belong to writers.
        con = _connect_readonly()
    try:
        with con:
            total = con.execute(
                f"SELECT COUNT(*) FROM missions{clause}", params).fetchone()[0]
            rows = con.execute(
                f"SELECT * FROM missions{clause} ORDER BY created_at DESC "
                f"LIMIT ?", [*params, limit]).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return [], 0
        raise
    finally:
        con.close()
    return [dict(r) for r in rows], total


def reindex(archive_root: Path | None = None) -> int:
    """Rebuild the index by scanning archive dirs for mission_record.json."""
    archive_root = archive_root or paths.archive_dir()
    count = 0
    with _connect() as con:
        con.execute("DELETE FROM missions")
        for record_file in sorted(archive_root.glob("*/mission_record.json")):
            try:
                record = MissionRecord.model_validate(
                    json.loads(record_file.read_text()))
            except Exception:
                continue
            con.execute(_INSERT, _row_from_record(record, record_file.parent))
            count += 1
    return count
