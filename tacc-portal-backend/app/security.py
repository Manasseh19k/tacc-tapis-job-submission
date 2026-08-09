import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
from fastapi import Request

from app.config import get_settings

logger = logging.getLogger(__name__)

TAPIS_TOKEN_HEADER = "X-Tapis-Token"

# Tapis signs tenant JWTs with RS256. Pinning the algorithm list is essential:
# accepting whatever the token's own header claims would allow an attacker to
# present an "alg: none" or HMAC-signed token and have it validated against the
# public key as a shared secret.
_ALLOWED_ALGORITHMS = ["RS256"]

# Tolerance for clock drift between this host and the Tapis token service.
_LEEWAY_SECONDS = 30

# Public keys rotate rarely; re-fetching per request would add a round trip to
# every message.
_PUBLIC_KEY_TTL_SECONDS = 3600


@dataclass(frozen=True)
class AuthenticatedUser:
    username: str
    tenant_id: str
    access_token: str
    claims: dict[str, Any]
    expires_at_epoch: int

    def __repr__(self) -> str:
        return (
            f"AuthenticatedUser(username={self.username!r}, "
            f"tenant_id={self.tenant_id!r}, access_token='<redacted>', "
            f"expires_at_epoch={self.expires_at_epoch})"
        )


class AuthError(Exception):
    def __init__(self, message: str, reason: str = "invalid_token") -> None:
        super().__init__(message)
        self.reason = reason


class _PublicKeyCache:
    def __init__(self) -> None:
        self._keys: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    async def get(self, tenant_url: str) -> str:
        """Return a cached key, fetching it if absent or stale."""
        cached = self._keys.get(tenant_url)
        if cached is not None and cached[1] > time.monotonic():
            return cached[0]

        async with self._lock:
            # Re-check inside the lock: another coroutine may have populated
            # the entry while this one waited.
            cached = self._keys.get(tenant_url)
            if cached is not None and cached[1] > time.monotonic():
                return cached[0]

            key = await _fetch_public_key_uncached(tenant_url)
            self._keys[tenant_url] = (key, time.monotonic() + _PUBLIC_KEY_TTL_SECONDS)
            return key

    def invalidate(self, tenant_url: str) -> None:
        """Drop a cached key so the next read re-fetches it."""
        self._keys.pop(tenant_url, None)


_public_key_cache = _PublicKeyCache()


def _tenant_id_from_url(tenant_url: str) -> str:
    host = urlsplit(tenant_url).hostname or ""
    return host.split(".")[0]


async def _fetch_public_key_uncached(tenant_url: str) -> str:
    settings = get_settings()
    tenant_id = _tenant_id_from_url(tenant_url)

    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        try:
            response = await client.get(f"{tenant_url}/v3/tenants/{tenant_id}")
            if response.status_code == 200:
                key = _extract_public_key(response.json())
                if key:
                    return key

            # Fallback: locate the tenant by base_url in the full listing.
            response = await client.get(f"{tenant_url}/v3/tenants")
            response.raise_for_status()
            payload = response.json()
            tenants = payload.get("result", payload)
            if isinstance(tenants, list):
                for tenant in tenants:
                    if str(tenant.get("base_url", "")).rstrip("/") == tenant_url:
                        key = tenant.get("public_key")
                        if key:
                            return key
        except httpx.HTTPError as exc:
            raise AuthError(
                f"Could not reach the Tapis tenants API at {tenant_url}: {exc}"
            ) from exc

    raise AuthError(f"No public key published for tenant at {tenant_url}")


def _extract_public_key(payload: Any) -> str | None:
    """Pull ``public_key`` from a tenant record, with or without the ``result`` wrapper."""
    if not isinstance(payload, dict):
        return None
    record = payload.get("result", payload)
    if isinstance(record, dict):
        key = record.get("public_key")
        if isinstance(key, str) and key.strip():
            return key
    return None


def extract_token(request: Request) -> str:
    token = (request.headers.get(TAPIS_TOKEN_HEADER) or "").strip()

    if not token:
        authorization = (request.headers.get("Authorization") or "").strip()
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()

    if not token:
        raise AuthError("No Tapis token supplied.")

    segments = token.split(".")
    if len(segments) != 3 or not all(segments):
        raise AuthError("Malformed Tapis token.")

    return token


async def fetch_tenant_public_key(tenant_url: str) -> str:
    return await _public_key_cache.get(tenant_url)


def _decode_unverified(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        raise AuthError(f"Could not decode Tapis token: {exc}") from exc


def _validate_tapis_claims(claims: dict[str, Any], expected_tenant: str) -> None:
    """Apply the Tapis-specific claim checks PyJWT does not know about."""
    token_type = claims.get("tapis/token_type")
    if token_type != "access":
        # A refresh token is a long-lived credential intended only for the
        # token endpoint. Accepting one here would let it act as a session.
        raise AuthError(f"Expected an access token, got token_type={token_type!r}.")

    tenant_id = claims.get("tapis/tenant_id")
    if tenant_id != expected_tenant:
        raise AuthError(
            f"Token belongs to tenant {tenant_id!r}, expected {expected_tenant!r}."
        )

    if not claims.get("tapis/username"):
        raise AuthError("Token has no tapis/username claim.")


async def verify_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    tenant_url = settings.tapis_tenant_url
    expected_tenant = _tenant_id_from_url(tenant_url)

    if not settings.require_token_verification:
        logger.warning(
            "Token verification is DISABLED; accepting token without validation. "
            "Do not run this way in production."
        )
        claims = _decode_unverified(token)
        return claims

    expected_issuer = f"{tenant_url}/v3/tokens"

    for attempt in (1, 2):
        public_key = await fetch_tenant_public_key(tenant_url)
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=public_key,
                algorithms=_ALLOWED_ALGORITHMS,
                issuer=expected_issuer,
                leeway=_LEEWAY_SECONDS,
                options={
                    # Tapis tokens carry no `aud` claim, so audience validation
                    # must stay off or every token would be rejected.
                    "verify_aud": False,
                    "require": ["exp", "iss", "sub"],
                },
            )
        except jwt.InvalidSignatureError:
            if attempt == 1:
                # Possible key rotation — drop the cached key and try once more.
                logger.info("JWT signature check failed; refreshing tenant public key.")
                _public_key_cache.invalidate(tenant_url)
                continue
            raise AuthError("Tapis token signature is invalid.") from None
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("Tapis token has expired.") from exc
        except jwt.InvalidIssuerError as exc:
            raise AuthError("Tapis token was issued by a different tenant.") from exc
        except jwt.PyJWTError as exc:
            raise AuthError(f"Tapis token is invalid: {exc}") from exc

        _validate_tapis_claims(claims, expected_tenant)
        return claims

    raise AuthError("Tapis token could not be verified.")


async def get_current_user(request: Request) -> AuthenticatedUser:
    token = extract_token(request)
    claims = await verify_token(token)

    username = claims.get("tapis/username")
    if not username:
        # Reachable when verification is disabled, since the Tapis-specific
        # claim checks are skipped on that path.
        raise AuthError("Token has no tapis/username claim.")

    tenant_id = claims.get("tapis/tenant_id") or _tenant_id_from_url(
        get_settings().tapis_tenant_url
    )

    user = AuthenticatedUser(
        username=str(username),
        tenant_id=str(tenant_id),
        access_token=token,
        claims=claims,
        expires_at_epoch=int(claims.get("exp", 0)),
    )

    request.state.username = user.username
    request.state.tenant_id = user.tenant_id

    return user
