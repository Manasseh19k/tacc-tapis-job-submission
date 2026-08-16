import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, AsyncIterator, Sequence

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    RetryPromptPart,
    ToolReturnPart,
)
from pydantic_ai.tools import DeferredToolResults
from pydantic_ai.ui.ag_ui import AGUIAdapter

from app.agent import build_agent
from app.config import get_settings
from app.deps import AgentDeps
from app.security import AuthError, get_current_user, fetch_tenant_public_key
from app.tapis_client import build_user_client, TapisClientError

logger = logging.getLogger(__name__)


class _ApprovalReconcilingAGUIAdapter(AGUIAdapter):

    def sanitize_messages(
        self,
        messages: Sequence[ModelMessage],
        *,
        deferred_tool_results: DeferredToolResults | None = None,
    ) -> list[ModelMessage]:
        sanitized = super().sanitize_messages(
            messages, deferred_tool_results=deferred_tool_results
        )
        if deferred_tool_results is None:
            return sanitized

        # tool_call_ids that will be supplied via resume[]; their results must
        # come from the resume, not from a client-injected tool-return part.
        resolved = set(deferred_tool_results.approvals) | set(deferred_tool_results.calls)
        if not resolved:
            return sanitized

        cleaned: list[ModelMessage] = []
        for message in sanitized:
            if isinstance(message, ModelRequest):
                kept = [
                    part
                    for part in message.parts
                    if not (
                        isinstance(part, (ToolReturnPart, RetryPromptPart))
                        and part.tool_call_id in resolved
                    )
                ]
                # Drop a request that held only the injected tool-return.
                if kept:
                    cleaned.append(replace(message, parts=kept))
            else:
                cleaned.append(message)
        return cleaned


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start-up and shutdown: do everything that needs to be done once per process, not once per request."""
    settings = get_settings()
    try:
        # Fetch the tenant public key once at startup, so it can fail fast if the
        # tenant is misconfigured. The key is cached in memory for the lifetime of
        # the process, so we don't need to fetch it per request. Keyed by the
        # tenant *URL* (what fetch_tenant_public_key and verify_token both use),
        # so the cache entry populated here is the one later requests hit.
        await fetch_tenant_public_key(settings.tapis_tenant_url)
    except AuthError as e:
        logger.warning("Failed to fetch tenant public key at startup: %s", e)
    
    knowledge = None
    try:
        from app.rag.store import VectorStore
        knowledge = VectorStore(
            persist_dir=settings.chroma_persist_dir,
            collection_name=settings.chroma_collection,
            embedding_model=settings.embedding_model
        )
    except Exception as exec:
        logger.warning(
            "Knowledge base unavailable (%s); documentation search is disabled.",
            type(exec).__name__
        )
    
    app.state.knowledge = knowledge
    if getattr(app.state, "agent", None) is None:
        app.state.agent = build_agent(include_knowledge=knowledge is not None)
    
    yield
    
    if knowledge is not None and hasattr(knowledge, "close"):
        knowledge.close()


def create_app(agent: Any = None) -> FastAPI:
    """Create the FastAPI app, optionally with a prebuilt agent."""
    settings = get_settings()
    app = FastAPI(title="TACC Portal Agent", lifespan=lifespan)

    if agent is not None:
        app.state.agent = agent

    # Off by default: the browser never calls this service directly, the
    # Next.js CopilotKit route proxies server-side.
    if settings.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.allowed_origins),
            allow_credentials=True,
            allow_methods=["POST"],
            allow_headers=["X-Tapis-Token", "Content-Type", "Accept"],
        )

    app.add_exception_handler(AuthError, auth_error_handler)
    app.add_exception_handler(TapisClientError, tapis_error_handler)
    app.add_api_route("/", agent_endpoint, methods=["POST"])
    app.add_api_route("/health", health_endpoint, methods=["GET"])
    return app


async def agent_endpoint(request: Request) -> Response:
    """Handle one AG-UI agent run. Mounted at ``POST /``."""
    # Authenticate before any model call: an unauthenticated request must not
    # cost LLM spend.
    user = await get_current_user(request)
    tapis = build_user_client(user)

    # Per request, never module-level: a shared client would serve one user's
    # token on another user's request.
    deps = AgentDeps(
        user=user,
        tapis=tapis,
        knowledge=request.app.state.knowledge,
        default_system_id=None,
    )

    logger.info("agent run for user=%s", user.username)

    # message_history and deferred_tool_results are deliberately NOT passed.
    # The adapter reads both off the AG-UI request body, including the
    # resume[] array carrying the user's approve/deny decision, which it maps
    # to DeferredToolResults deny-by-default. Passing them explicitly here
    # overrides that and breaks the approval round trip.
    return await _ApprovalReconcilingAGUIAdapter.dispatch_request(
        request,
        agent=request.app.state.agent,
        deps=deps,
    )


async def health_endpoint(request: Request) -> JSONResponse:
    """Return a simple JSON health check."""
    knowledge = getattr(request.app.state, "knowledge", None)
    chunks = None
    if knowledge is not None:
        try:
            chunks = knowledge.count()
        except Exception:
            chunks = None

    return JSONResponse(
        {
            "status": "ok",
            "agent_ready": getattr(request.app.state, "agent", None) is not None,
            "knowledge_enabled": knowledge is not None,
            "knowledge_chunks": chunks,
        }
    )


async def auth_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Convert an :class:`app.security.AuthError` into a 401."""
    reason = getattr(exc, "reason", "invalid_token")
    # Log the detail; return only a coarse reason. "signature invalid" vs
    # "expired" vs "wrong tenant" helps someone probing the endpoint and does
    # nothing for the frontend, which just needs to restart the login flow.
    logger.warning("auth failed (%s): %s", reason, exc)
    return JSONResponse(
        {"error": "not_authenticated", "reason": reason}, status_code=401
    )

async def tapis_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Convert a TapisClientError raised outside a tool into a 502.

    Inside a tool these are caught and handed to the model as text. Reaching
    here means client construction failed, which is infrastructure, not
    something the model can explain away.
    """
    logger.warning("tapis client error: %s", exc)
    return JSONResponse(
        {"error": "tapis_unavailable", "detail": str(exc)}, status_code=502
    )


app = create_app()
