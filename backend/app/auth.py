"""
Request authentication with Clerk session tokens.

The frontend sends `Authorization: Bearer <Clerk session JWT>`. The token is verified against
Clerk's JWKS (fetched with CLERK_SECRET_KEY, cached) and its `sub` is the user id.

AUTH_DISABLED=1 turns verification off for local development; the user id is then taken from
the `user_id` query parameter (or "local").
"""

import os
import threading
from typing import Optional

import jwt
from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

CLERK_SECRET_KEY = os.getenv("CLERK_SECRET_KEY", "")
CLERK_JWKS_URL = os.getenv("CLERK_JWKS_URL", "https://api.clerk.com/v1/jwks")
AUTH_DISABLED = os.getenv("AUTH_DISABLED", "0").lower() in ("1", "true", "yes")

_jwks_client: Optional[jwt.PyJWKClient] = None
_jwks_lock = threading.Lock()

if AUTH_DISABLED:
    print("Warning: AUTH_DISABLED=1 - API requests are NOT authenticated (local development only)")
elif not CLERK_SECRET_KEY:
    print("Warning: CLERK_SECRET_KEY is not set - authenticated API requests will be rejected. "
          "Copy it from frontend/.env.local into backend/.env (or set AUTH_DISABLED=1 for local testing).")


def _jwks():
    global _jwks_client
    with _jwks_lock:
        if _jwks_client is None:
            _jwks_client = jwt.PyJWKClient(CLERK_JWKS_URL, cache_keys=True, lifespan=3600,
                                           headers={"Authorization": f"Bearer {CLERK_SECRET_KEY}",
                                                    "User-Agent": "AetherCAD-backend/1.0"})
        return _jwks_client


def _verify(token: str) -> str:
    key = _jwks().get_signing_key_from_jwt(token).key
    claims = jwt.decode(token, key, algorithms=["RS256"], leeway=10,
                        options={"require": ["exp", "sub"], "verify_aud": False})
    return claims["sub"]


async def current_user(request: Request) -> str:
    """FastAPI dependency: the authenticated Clerk user id."""
    if AUTH_DISABLED:
        return request.query_params.get("user_id") or "local"
    if not CLERK_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Server authentication is not configured "
                                                    "(set CLERK_SECRET_KEY in backend/.env).")
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Not signed in")
    try:
        return await run_in_threadpool(_verify, header[7:].strip())
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired; reload the page")
    except jwt.PyJWKClientConnectionError as e:
        raise HTTPException(status_code=503, detail=f"Could not reach Clerk to verify the session ({e})")
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"Invalid session token ({e})")
