"""Shared-password auth: one password, signed session cookie.

Matches the locked design (1-2 users, internal tool). The session is a
Starlette signed cookie (SECRET_KEY); no user table.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, status

from .settings import SHARED_PASSWORD


def password_ok(candidate: str) -> bool:
    if not SHARED_PASSWORD:
        # No password configured -> open (dev only). Logged loudly at startup.
        return True
    return hmac.compare_digest(candidate or "", SHARED_PASSWORD)


def login(request: Request) -> None:
    request.session["auth"] = True


def logout(request: Request) -> None:
    request.session.clear()


def is_authed(request: Request) -> bool:
    if not SHARED_PASSWORD:
        return True
    return bool(request.session.get("auth"))


async def require_auth(request: Request) -> None:
    """Route dependency: 401 unless logged in."""
    if not is_authed(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )


AuthDep = Depends(require_auth)
