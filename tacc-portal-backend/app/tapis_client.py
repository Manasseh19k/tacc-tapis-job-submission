from typing import Any

from tapipy.tapis import Tapis
from tapipy import errors as tapipy_errors

from app.security import AuthenticatedUser
from app.config import get_settings


_SENSITIVE_KEYS = {
    "access_token",
    "refresh_token",
    "x-tapis-token",
    "password",
    "client_key",
    "authorization",
}

def build_user_client(user: AuthenticatedUser) -> Tapis:
    settings = get_settings()
    try:
        return Tapis(
            base_url=settings.tapis_tenant_url,
            access_token=user.access_token
        )
    except Exception as exc:
        raise TapisClientError(
            f"Failed to build Tapis client for {user.username}: {exc}"
        ) from exc


class TapisClientError(Exception):
    """Raised when a Tapis client cannot be built or a Tapis call fails fatally.

    Tool functions should let this propagate as a *tool* error rather than an
    HTTP error: the agent can then explain the failure conversationally ("that
    system is not available right now") instead of the whole run collapsing.
    """


def summarize_tapis_error(exc: Exception) -> str:
    if isinstance(exc, tapipy_errors.UnauthorizedError):
        return "Your session has expired. Please log in again."
    if isinstance(exc, tapipy_errors.ForbiddenError):
        return "You do not have permission to access that resource."
    if isinstance(exc, tapipy_errors.NotFoundError):
        return "No such Tapis system, path, or resource was found."
    if isinstance(exc, tapipy_errors.BaseTapyException):
        status = getattr(exc.response, "status_code", None)
        detail = exc.message or "no further detail provided."
        if status is not None:
            return f"Tapis returned an error (status {status}): {detail}"
        return f"Tapis returned an error: {detail}"
    return "An unexpected error occurred while contacting Tapis."


def redact(payload: Any) -> Any:
    """Recursively redact sensitive keys in a dictionary or list."""
    if isinstance(payload, dict):
        return {
            key: redact(value) if key.lower() not in _SENSITIVE_KEYS else "<REDACTED>"
            for key, value in payload.items()
        }
    if isinstance(payload, (list, tuple)):
        return type(payload)(redact(item) for item in payload)
    return payload
