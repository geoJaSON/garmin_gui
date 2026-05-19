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
    kind        TEXT NOT NULL,              -- mosaic | tracks | combine
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

CREATE TABLE IF NOT EXISTS areas (
    id            TEXT PRIMARY KEY,
    our_name      TEXT NOT NULL,
    tpwd_app_no   TEXT NOT NULL,
    properties    TEXT NOT NULL,            -- JSON (full original feature props)
    geometry      TEXT NOT NULL,            -- JSON GeoJSON geometry, WGS84
    notes         TEXT NOT NULL DEFAULT '',
    mosaic_job_id TEXT,                     -- last successful deliverable
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_areas_key
    ON areas(our_name, tpwd_app_no);
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
        # One-shot column rename if an older DB was created with the
        # original (typo'd) column name. Idempotent.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(areas)")}
        if "tpdw_app_no" in cols and "tpwd_app_no" not in cols:
            c.execute("ALTER TABLE areas RENAME COLUMN "
                      "tpdw_app_no TO tpwd_app_no")


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


def create_done_job(kind: str, params: dict, result: dict,
                     job_id: str = None) -> str:
    """Insert an already-completed job (for importing historical results).

    Status is 'done' from the outset so the serial worker never claims it.
    A job_id may be supplied so the caller can keep the run directory name
    and the job id identical.
    """
    job_id = job_id or uuid.uuid4().hex
    now = time.time()
    with connect() as c:
        c.execute(
            "INSERT INTO jobs (id, kind, status, params, result, "
            "created_at, started_at, finished_at) "
            "VALUES (?, ?, 'done', ?, ?, ?, ?, ?)",
            (job_id, kind, json.dumps(params), json.dumps(result), now, now, now),
        )
    return job_id


def find_done_mosaic_by_rsd(rsd_name: str) -> Optional[dict]:
    """Existing completed mosaic run for this RSD name, if any (idempotent import)."""
    for j in list_jobs(100000):
        if (
            j["kind"] == "mosaic"
            and j["status"] == "done"
            and (j.get("params") or {}).get("rsd_path", "").rsplit("/", 1)[-1] == rsd_name
        ):
            return j
    return None


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


def find_mosaic_jobs_by_rsd(rsd_name: str) -> list[dict]:
    """Every mosaic-kind job whose RSD matches by params or result.

    Used by the cascade delete: a single uploaded RSD may have been
    mosaicked more than once.
    """
    out = []
    for j in list_jobs(100000):
        if j["kind"] != "mosaic":
            continue
        params = j.get("params") or {}
        result = j.get("result") or {}
        rsd_param = (params.get("rsd_path") or "").rsplit("/", 1)[-1]
        if rsd_param == rsd_name or result.get("rsd_name") == rsd_name:
            out.append(j)
    return out


def update_job_result(job_id: str, patch: dict) -> bool:
    """Merge `patch` into a job's result JSON. Used by the backfill tool."""
    with connect() as c:
        row = c.execute("SELECT result FROM jobs WHERE id=?",
                        (job_id,)).fetchone()
        if not row:
            return False
        cur = json.loads(row["result"]) if row["result"] else {}
        cur.update(patch)
        c.execute("UPDATE jobs SET result=? WHERE id=?",
                  (json.dumps(cur), job_id))
        return True


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


# ---- areas (Phase 6) ----------------------------------------------------
def _row_to_area(row: sqlite3.Row) -> dict:
    a = dict(row)
    a["properties"] = json.loads(a["properties"]) if a["properties"] else {}
    a["geometry"] = json.loads(a["geometry"]) if a["geometry"] else None
    return a


def upsert_area(our_name: str, tpwd_app_no: str,
                properties: dict, geometry: dict) -> str:
    """Insert if (our_name, tpwd_app_no) is new, else update geometry/properties.

    Notes and mosaic_job_id are PRESERVED on update so re-uploading the layer
    doesn't clobber per-area state.
    """
    now = time.time()
    props_j = json.dumps(properties or {})
    geom_j = json.dumps(geometry)
    with connect() as c:
        row = c.execute(
            "SELECT id FROM areas WHERE our_name=? AND tpwd_app_no=?",
            (our_name, tpwd_app_no),
        ).fetchone()
        if row:
            c.execute(
                "UPDATE areas SET properties=?, geometry=?, updated_at=? "
                "WHERE id=?",
                (props_j, geom_j, now, row["id"]),
            )
            return row["id"]
        area_id = uuid.uuid4().hex
        c.execute(
            "INSERT INTO areas (id, our_name, tpwd_app_no, properties, "
            "geometry, notes, mosaic_job_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, '', NULL, ?, ?)",
            (area_id, our_name, tpwd_app_no, props_j, geom_j, now, now),
        )
        return area_id


def list_areas() -> list[dict]:
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM areas ORDER BY our_name, tpwd_app_no"
        ).fetchall()
    return [_row_to_area(r) for r in rows]


def get_area(area_id: str) -> Optional[dict]:
    with connect() as c:
        row = c.execute("SELECT * FROM areas WHERE id=?", (area_id,)).fetchone()
    return _row_to_area(row) if row else None


def get_area_by_key(our_name: str, tpwd_app_no: str) -> Optional[dict]:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM areas WHERE our_name=? AND tpwd_app_no=?",
            (our_name, tpwd_app_no),
        ).fetchone()
    return _row_to_area(row) if row else None


def update_area_notes(area_id: str, notes: str) -> bool:
    with connect() as c:
        cur = c.execute(
            "UPDATE areas SET notes=?, updated_at=? WHERE id=?",
            (notes, time.time(), area_id),
        )
        return cur.rowcount > 0


def set_area_mosaic_job(area_id: str, job_id: str) -> None:
    with connect() as c:
        c.execute(
            "UPDATE areas SET mosaic_job_id=?, updated_at=? WHERE id=?",
            (job_id, time.time(), area_id),
        )


def delete_area(area_id: str) -> bool:
    with connect() as c:
        cur = c.execute("DELETE FROM areas WHERE id=?", (area_id,))
        return cur.rowcount > 0
