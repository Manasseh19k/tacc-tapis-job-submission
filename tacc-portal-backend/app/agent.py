from functools import lru_cache, wraps
from typing import Any, Awaitable, Callable

from pydantic_ai import Agent, DeferredToolRequests, ModelRetry
from pydantic_ai.models import Model
from pydantic_ai_litellm import LiteLLMModel

from app.config import get_settings
from app.deps import AgentDeps
from app.tapis_client import TapisClientError
from app.tools import list_files, list_systems, read_file
from app.tools.jobs import (
   cancel_job, submit_job, submit_job_request, build_job_request,
   describe_app, get_job_output, list_apps, list_jobs,
   validate_job_spec, get_job_status
)
from app.tools.knowledge import search_documentation


def _surface_tapis_errors(tool: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:

    @wraps(tool)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await tool(*args, **kwargs)
        except TapisClientError as exc:
            raise ModelRetry(str(exc)) from exc

    return wrapper


SYSTEM_PROMPT = """\
   You are the TACC portal assistant. You help users work with Tapis systems,
   files, and jobs on TACC infrastructure. Decline requests outside that scope.

   Tool discipline:
   - Never state anything about the user's systems, files, or jobs from memory.
   Call a tool and report what it returned.
   - Never invent a system id, app id, file path, or job UUID. If you need one and
   do not have it, call the listing tool or ask the user.
   - If a tool returns an error, tell the user what it said. Do not retry blindly.

   Grounding:
   - Answer questions about how Tapis or HPC works using search_documentation, and
   cite the sources it returns.
   - If it finds nothing, say so. Do not fall back on general knowledge, which is
   often wrong about site-specific policy.

   Submitting and cancelling jobs:
   - Build the full spec first: use list_apps and describe_app to get valid ids and
   defaults, confirm input files exist with list_files, then run
   validate_job_spec and fix anything it reports.
   - Then call submit_job (or cancel_job) directly. The user gets a separate
   approval prompt showing the exact spec; that prompt is the confirmation.
   - Do not ask "shall I submit?" in chat. Asking in prose trains users to approve
   without reading, which defeats the approval step.

   Editing the raw job request as JSON:
   - If the user wants to see or hand-edit the raw Tapis request (e.g. to set
   fields the standard flow does not cover), call build_job_request to get the
   exact JSON and show it to them.
   - When they give you a job request JSON to submit, call submit_job_request
   with it verbatim. Do not silently change their JSON. If it is missing a
   required field, ask them to add it rather than guessing.
   - submit_job_request is also approval-gated; the same approval prompt applies.
"""

# Tools that never mutate anything. Safe to register unconditionally.
# build_job_request only renders JSON (no Tapis call), so it belongs here.
_READ_ONLY_TOOLS = [
    list_systems, list_files, read_file,
    list_apps, describe_app, validate_job_spec, build_job_request,
    get_job_status, list_jobs, get_job_output,
]

# Tools that consume allocation or destroy work. Each gates itself internally
# via `if not ctx.tool_call_approved: raise ApprovalRequired(metadata=...)`.
# Registered as PLAIN tools on purpose — see build_agent.
_APPROVAL_GATED_TOOLS = [submit_job, submit_job_request, cancel_job]


def build_system_prompt() -> str:
   return SYSTEM_PROMPT
   
def build_model() -> Model:
    """Return the configured LLM model from settings."""
    settings = get_settings()
    return LiteLLMModel(
         model_name=settings.llm_model,
         api_key=settings.openai_api_key,
         api_base=settings.base_url,
         custom_llm_provider=settings.llm_provider if settings.base_url else None
    )


def build_agent(model: Model | str | None = None, *, include_knowledge: bool = True) -> "Agent[AgentDeps, Any]":
   raw_tools: list[Any] = [*_READ_ONLY_TOOLS, *_APPROVAL_GATED_TOOLS]
   if include_knowledge:
      raw_tools.append(search_documentation)
   # Every tool goes through the same policy: a Tapis rejection becomes a
   # message the model relays, not a run-ending crash.
   tools = [_surface_tapis_errors(tool) for tool in raw_tools]
   return Agent(
      model=model or build_model(),
      deps_type=AgentDeps,
      output_type=[str, DeferredToolRequests],
      instructions=build_system_prompt(),
      tools=tools,
      # Headroom for the ModelRetry-based error relay: if the model retries a
      # failing tool once before relaying the error to the user, the second
      # ModelRetry should not immediately end the run.
      retries=2,
      name="tacc-portal-agent",
   )


@lru_cache(maxsize=1)
def get_agent() -> "Agent[AgentDeps, Any]":
   return build_agent()
