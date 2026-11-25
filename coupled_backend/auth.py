"""Neon Auth (Managed Better Auth) integration for FastAPI.

Handles:
- EdDSA (Ed25519) JWT verification against NEON_AUTH_JWKS_URL with cached JWKS lookup
- Browser session-cookie verification via NEON_AUTH_BASE_URL/get-session
- Same-origin auth proxy forwarding (/api/auth/* -> NEON_AUTH_BASE_URL/*)
- FastAPI auth dependencies (get_optional_user / require_user) and resource ownership checks
"""
from __future__ import annotations

import base64
import logging
import re
import threading
import time
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import HTTPException, Request, Response
from jwt import PyJWTError
from pydantic import BaseModel

from . import config

logger = logging.getLogger(__name__)

_JWKS_TTL_SECONDS = 300.0
_jwks_lock = threading.Lock()
_jwks_cache: dict[str, Any] | None = None
_jwks_cache_url: str = ""
_jwks_cache_ts: float = 0.0

_HOP_BY_HOP_HEADERS = frozenset({
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "content-encoding",
})


class AuthUser(BaseModel):
    id: str
    email: str = ""
    name: str = ""
    role: str = "authenticated"
    email_verified: bool = False


def auth_enabled() -> bool:
    return bool(config.NEON_AUTH_BASE_URL)


def auth_required() -> bool:
    return auth_enabled() and config.AUTH_REQUIRED


def auth_origin() -> str:
    parsed = urlparse(config.NEON_AUTH_BASE_URL)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""


def jwks_url() -> str:
    if config.NEON_AUTH_JWKS_URL:
        return config.NEON_AUTH_JWKS_URL
    if config.NEON_AUTH_BASE_URL:
        return f"{config.NEON_AUTH_BASE_URL}/.well-known/jwks.json"
    return ""


def _fetch_jwks(force: bool = False) -> dict[str, Any]:
    global _jwks_cache, _jwks_cache_url, _jwks_cache_ts
    url = jwks_url()
    if not url:
        raise RuntimeError("NEON_AUTH_JWKS_URL is not configured")

    now = time.monotonic()
    with _jwks_lock:
        if (
            not force
            and _jwks_cache is not None
            and _jwks_cache_url == url
            and (now - _jwks_cache_ts) < _JWKS_TTL_SECONDS
        ):
            return _jwks_cache

    resp = httpx.get(url, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()

    with _jwks_lock:
        _jwks_cache = data
        _jwks_cache_url = url
        _jwks_cache_ts = time.monotonic()
    return data


def _signing_key_for_token(token: str, jwks: dict[str, Any]):
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    keys = jwks.get("keys") or []
    for jwk in keys:
        if kid and jwk.get("kid") != kid:
            continue
        kty = jwk.get("kty")
        crv = jwk.get("crv")
        if kty == "OKP" and crv == "Ed25519" and jwk.get("x"):
            x = jwk["x"]
            raw = base64.urlsafe_b64decode(x + "=" * (-len(x) % 4))
            return Ed25519PublicKey.from_public_bytes(raw), jwk.get("alg") or "EdDSA"
        # Fallback for RSA/EC JWKs if ever encountered
        if kty == "RSA":
            return jwt.algorithms.RSAAlgorithm.from_jwk(jwk), jwk.get("alg") or "RS256"
        if kty == "EC":
            return jwt.algorithms.ECAlgorithm.from_jwk(jwk), jwk.get("alg") or "ES256"
    return None, None


def verify_jwt(token: str) -> AuthUser:
    """Verify a Managed Better Auth JWT (EdDSA / Ed25519) against NEON_AUTH_JWKS_URL."""
    if not auth_enabled():
        raise HTTPException(503, "Neon Auth is not configured")

    origin = auth_origin()
    try:
        jwks = _fetch_jwks(force=False)
        key, alg = _signing_key_for_token(token, jwks)
        if key is None:
            # Refresh once in case JWKS rotated
            jwks = _fetch_jwks(force=True)
            key, alg = _signing_key_for_token(token, jwks)
        if key is None:
            raise HTTPException(401, "Unknown signing key in token")

        payload = jwt.decode(
            token,
            key=key,
            algorithms=[alg or "EdDSA"],
            issuer=origin,
            audience=origin,
        )
    except HTTPException:
        raise
    except PyJWTError as exc:
        raise HTTPException(401, f"Invalid or expired auth token: {exc}") from exc
    except Exception as exc:
        logger.warning("JWT verification error: %s", exc)
        raise HTTPException(401, "Token verification failed") from exc

    uid = str(payload.get("sub") or payload.get("id") or "").strip()
    if not uid:
        raise HTTPException(401, "Token missing subject claim")

    return AuthUser(
        id=uid,
        email=str(payload.get("email") or ""),
        name=str(payload.get("name") or ""),
        role=str(payload.get("role") or "authenticated"),
        email_verified=bool(payload.get("emailVerified", False)),
    )


def verify_session_cookie(cookie_header: str, origin_header: str = "") -> AuthUser | None:
    """Validate an incoming browser session cookie via Managed Better Auth /get-session."""
    if not auth_enabled() or not cookie_header:
        return None
    if "neon-auth" not in cookie_header and "better-auth" not in cookie_header:
        return None

    headers = {"Cookie": cookie_header}
    if origin_header:
        headers["Origin"] = origin_header
    try:
        resp = httpx.get(
            f"{config.NEON_AUTH_BASE_URL}/get-session",
            headers=headers,
            timeout=30.0,
        )
        if resp.status_code != 200:
            return None
        # Prefer verifying the signed EdDSA JWT returned in set-auth-jwt when present
        token = resp.headers.get("set-auth-jwt")
        if token:
            try:
                return verify_jwt(token)
            except HTTPException:
                pass
        data = resp.json()
        if not isinstance(data, dict) or not data.get("user"):
            return None
        u = data["user"]
        uid = str(u.get("id") or "").strip()
        if not uid:
            return None
        return AuthUser(
            id=uid,
            email=str(u.get("email") or ""),
            name=str(u.get("name") or ""),
            role=str(u.get("role") or "authenticated"),
            email_verified=bool(u.get("emailVerified", False)),
        )
    except Exception as exc:
        logger.debug("Session cookie verification failed: %s", exc)
        return None


def get_optional_user(request: Request) -> AuthUser | None:
    """Resolve the current Neon Auth user from Bearer JWT or session cookie, if present."""
    if not auth_enabled():
        return None

    authz = request.headers.get("authorization", "").strip()
    if authz.lower().startswith("bearer "):
        token = authz[7:].strip()
        if not token:
            raise HTTPException(401, "Empty Bearer token")
        return verify_jwt(token)

    cookie = request.headers.get("cookie", "")
    if cookie:
        origin = request.headers.get("origin") or f"{request.url.scheme}://{request.url.netloc}"
        return verify_session_cookie(cookie, origin_header=origin)

    return None


def require_user(request: Request) -> AuthUser | None:
    """FastAPI dependency enforcing Neon Auth when configured and AUTH_REQUIRED=true."""
    if not auth_required():
        return get_optional_user(request)
    user = get_optional_user(request)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Sign in with Neon Auth.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def _rewrite_set_cookie_for_proxy(raw_cookie: str) -> str:
    """Adapt upstream Neon Auth Set-Cookie for same-origin FastAPI proxy delivery.

    Removes upstream Domain= so the cookie binds to the app origin (host-only),
    and removes Partitioned so same-origin browser requests always send it.
    Preserves __Secure- prefix and Secure attribute (supported on HTTPS and http://localhost).
    """
    cookie = re.sub(r";\s*Domain=[^;]+", "", raw_cookie, flags=re.IGNORECASE)
    cookie = re.sub(r";\s*Partitioned\b", "", cookie, flags=re.IGNORECASE)
    cookie = re.sub(r";\s*SameSite=None\b", "; SameSite=Lax", cookie, flags=re.IGNORECASE)
    return cookie


async def proxy_auth_request(path: str, request: Request) -> Response:
    """Same-origin reverse proxy from /api/auth/* to Managed Better Auth (NEON_AUTH_BASE_URL)."""
    if not auth_enabled():
        raise HTTPException(503, "Neon Auth is not configured (NEON_AUTH_BASE_URL missing)")

    target = f"{config.NEON_AUTH_BASE_URL}/{path.lstrip('/')}"
    if request.url.query:
        target = f"{target}?{request.url.query}"

    fwd_headers: dict[str, str] = {}
    for k, v in request.headers.items():
        if k.lower() in _HOP_BY_HOP_HEADERS:
            continue
        fwd_headers[k] = v

    # Ensure Origin is forwarded so Managed Better Auth validates trusted domain cleanly
    if not any(k.lower() == "origin" for k in fwd_headers):
        fwd_headers["Origin"] = f"{request.url.scheme}://{request.url.netloc}"

    body = await request.body()
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        upstream = await client.request(
            method=request.method,
            url=target,
            headers=fwd_headers,
            content=body if body else None,
        )

    resp = Response(
        content=upstream.content,
        status_code=upstream.status_code,
    )
    for k, v in upstream.headers.multi_items():
        kl = k.lower()
        if kl in _HOP_BY_HOP_HEADERS:
            continue
        if kl == "set-cookie":
            resp.headers.append("set-cookie", _rewrite_set_cookie_for_proxy(v))
        elif kl == "content-type":
            resp.headers["content-type"] = v
        else:
            resp.headers.append(k, v)
    return resp
