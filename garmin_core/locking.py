"""Cross-process advisory file lock (stdlib only).

The track inventory (rsd_tracks.geojson) is read-modify-written by both the
API process and job subprocesses; this serializes those cycles so concurrent
writers can't clobber each other. Locks are OS-level (flock / msvcrt), so
they die with the holding process — no stale-lock cleanup needed.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def locked(target: Path | str):
    """Hold an exclusive lock on <target>.lock for the with-block."""
    lock_path = Path(str(target) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            # LK_LOCK retries for ~10s, then raises OSError — fine here:
            # inventory writers hold the lock for milliseconds.
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()
