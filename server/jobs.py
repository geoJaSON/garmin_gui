"""Single serial worker.

A background thread claims the oldest queued job and runs it as an isolated
`python -m server.run_job` subprocess, one at a time. This matches the locked
design (1-2 occasional users); a second worker later is just raising the
spawn count, not a redesign.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

from . import db
from .settings import JOB_PYTHON

_worker: "threading.Thread | None" = None
_stop = threading.Event()


def enqueue(kind: str, params: dict) -> str:
    return db.create_job(kind, params)


def _python() -> str:
    return JOB_PYTHON or sys.executable


def _run_one(job: dict) -> None:
    proc = subprocess.run(
        [_python(), "-m", "server.run_job", job["id"]],
        capture_output=True,
        text=True,
    )
    # The subprocess marks the job done/error itself. If it died before
    # doing so (segfault, OOM kill), the row is still 'running' -> fail it.
    fresh = db.get_job(job["id"])
    if fresh and fresh["status"] == "running":
        tail = (proc.stderr or proc.stdout or "")[-2000:]
        db.finish_job(
            job["id"],
            error=f"worker subprocess exited {proc.returncode} "
                  f"without finishing\n{tail}",
        )


def _loop() -> None:
    while not _stop.is_set():
        job = db.claim_next_queued()
        if job is None:
            _stop.wait(2.0)
            continue
        try:
            _run_one(job)
        except Exception as e:  # never let the worker thread die
            db.finish_job(job["id"], error=f"worker error: {e}")


def start_worker() -> None:
    global _worker
    if _worker and _worker.is_alive():
        return
    _stop.clear()
    _worker = threading.Thread(target=_loop, name="garmin-worker", daemon=True)
    _worker.start()


def stop_worker() -> None:
    _stop.set()
