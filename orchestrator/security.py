"""Authentication and request-integrity helpers.

Two distinct trust boundaries exist:

* **Operators / users** call the control plane with a static API key
  (``X-API-Key``). In production this should be swapped for Entra ID bearer
  tokens - the dependency below is the single place to change.
* **Agents** running on Azure VMs or network PCs authenticate every call with an
  HMAC-SHA256 signature over the canonical request. That protects against replay
  (timestamp + nonce window) and tampering, and it never puts a bearer secret on
  the wire.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Header, HTTPException, Request, status

from . import db
from .config import get_settings

SIGNATURE_VERSION = "v1"


@dataclass(frozen=True)
class Principal:
    """Who is making the call, used for audit records."""

    kind: str  # "operator" | "agent"
    identifier: str


def new_agent_secret() -> str:
    return secrets.token_urlsafe(32)


def canonical_string(method: str, path: str, timestamp: str, nonce: str, body: bytes) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    return "\n".join([SIGNATURE_VERSION, method.upper(), path, timestamp, nonce, body_hash])


def sign(secret: str, method: str, path: str, timestamp: str, nonce: str, body: bytes) -> str:
    message = canonical_string(method, path, timestamp, nonce, body)
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256)
    return f"{SIGNATURE_VERSION}={digest.hexdigest()}"


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "HMAC"},
    )


async def require_operator(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Principal:
    settings = get_settings()
    if not x_api_key:
        raise _unauthorized("missing X-API-Key header")
    for candidate in settings.api_keys:
        if hmac.compare_digest(candidate, x_api_key):
            # Only a short fingerprint is retained so audit logs never leak keys.
            fingerprint = hashlib.sha256(x_api_key.encode()).hexdigest()[:12]
            request.state.principal = Principal("operator", f"key:{fingerprint}")
            return request.state.principal
    raise _unauthorized("invalid API key")


async def require_bootstrap_token(
    x_bootstrap_token: str | None = Header(default=None, alias="X-Bootstrap-Token"),
) -> None:
    settings = get_settings()
    if not x_bootstrap_token or not hmac.compare_digest(settings.bootstrap_token, x_bootstrap_token):
        raise _unauthorized("invalid or missing agent bootstrap token")


class _NonceCache:
    """Bounded in-memory replay guard, sized for the signature skew window."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}

    def check_and_add(self, key: str, ttl: int) -> bool:
        now = time.monotonic()
        if len(self._seen) > 50_000:
            self._seen = {k: v for k, v in self._seen.items() if v > now}
        expiry = self._seen.get(key)
        if expiry is not None and expiry > now:
            return False
        self._seen[key] = now + ttl
        return True


_nonce_cache = _NonceCache()


async def require_agent(
    request: Request,
    x_agent_id: str | None = Header(default=None, alias="X-Agent-Id"),
    x_timestamp: str | None = Header(default=None, alias="X-Timestamp"),
    x_nonce: str | None = Header(default=None, alias="X-Nonce"),
    x_signature: str | None = Header(default=None, alias="X-Signature"),
) -> Principal:
    settings = get_settings()
    if not (x_agent_id and x_timestamp and x_nonce and x_signature):
        raise _unauthorized("missing agent signature headers")

    try:
        sent_at = datetime.fromisoformat(x_timestamp)
    except ValueError:
        raise _unauthorized("X-Timestamp must be an ISO-8601 timestamp") from None
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    skew = abs((datetime.now(timezone.utc) - sent_at).total_seconds())
    if skew > settings.max_clock_skew:
        raise _unauthorized("request timestamp outside the accepted window")

    row = db.query_one("SELECT secret FROM agents WHERE id = ?", (x_agent_id,))
    if row is None:
        raise _unauthorized("unknown agent")

    body = await request.body()
    expected = sign(row["secret"], request.method, request.url.path, x_timestamp, x_nonce, body)
    if not hmac.compare_digest(expected, x_signature):
        raise _unauthorized("signature verification failed")

    if not _nonce_cache.check_and_add(f"{x_agent_id}:{x_nonce}", settings.max_clock_skew):
        raise _unauthorized("replayed nonce rejected")

    # Path parameters must match the signing identity - an agent may never act
    # on behalf of another node.
    path_agent = request.path_params.get("agent_id")
    if path_agent is not None and path_agent != x_agent_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "agent identity mismatch")

    request.state.principal = Principal("agent", x_agent_id)
    return request.state.principal
