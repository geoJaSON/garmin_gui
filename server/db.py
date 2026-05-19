"""SQLite state. One `jobs` table drives the serial work queue.

SQLite is intentional: 1-2 occasional users, a single serial worker, and we
want the queue to survive restarts with zero infra. WAL mode lets the API
read job state while the worker writes it.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from .settings import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,              -- mosaic | tracks | mosaic_tracks
    status      TEXT NOT NULL,              -- queued | running | done | error | cancelled
    params      TEXT NOT NULL,              -- JSON input
    progress    TEXT,                       -- JSON {desc,n,total,pct}
    result      TEXT,                       -- JSON output (paths, counts)
    error       TEXT,
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with connect() as c:
        c.executescript(SCHEMA)


def _row_to_job(row: sqlite3.Row) -> dict:
    j = dict(row)
    for k in ("params", "progress", "result"):
        j[k] = json.loads(j[k]) if j[k] else None
    return j


def create_job(kind: str, params: dict) -> str:
    job_id = uuid.uuid4().hex
    with connect() as c:
        c.execute(
            "INSERT INTO jobs (id, kind, status, params, created_at) "
            "VALUES (?, ?, 'queued', ?, ?)",
            (job_id, kind, json.dumps(params), time.time()),
        )
    return job_id


def get_job(job_id: str) -> Optional[dict]:
    with connect() as c:
        row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _row_to_job(row) if row else None


def list_jobs(limit: int = 100) -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_job(r) for r in rows]


def claim_next_queued() -> Optional[dict]:
    """Atomically move the oldest queued job to running. Single-worker safe."""
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            "SELECT * FROM jobs WHERE status='queued' "
            "ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is None:
            c.execute("COMMIT")
            return None
        c.execute(
            "UPDATE jobs SET status='running', started_at=? WHERE id=?",
            (time.time(), row["id"]),
        )
        c.execute("COMMIT")
        return _row_to_job(row)


def set_progress(job_id: str, progress: dict) -> None:
    with connect() as c:
        c.execute(
            "UPDATE jobs SET progress=? WHERE id=?",
            (json.dumps(progress), job_id),
        )


def finish_job(job_id: str, *, result: Any = None, error: str = None) -> None:
    status = "error" if error else "done"
    with connect() as c:
        c.execute(
            "UPDATE jobs SET status=?, result=?, error=?, finished_at=? WHERE id=?",
            (
                status,
                json.dumps(result) if result is not None else None,
                error,
                time.time(),
                job_id,
            ),
        )
