import asyncio
import logging
from dataclasses import dataclass

from pydantic_ai import RunContext

from app.deps import AgentDeps

logger = logging.getLogger(__name__)

# Similarity floor for a chunk to count as relevant. Below this, treat the
# knowledge base as having nothing to say rather than feeding the model
# tangentially-related text it will over-trust. Tune against real questions.
_MIN_SCORE = 0.45


@dataclass
class KnowledgeAnswer:
    """Retrieved passages plus their provenance.

    Attributes:
        passages: The retrieved text, ordered most relevant first, for the model
            to ground its answer in.
        sources: Deduplicated source identifiers backing those passages. The
            agent should cite these, so the user can verify a claim instead of
            trusting the model's paraphrase of documentation it may have
            partially retrieved.
        found: False when nothing cleared the relevance threshold. An explicit
            flag rather than an empty list, because it gives the model an
            unambiguous signal to say "I don't have documentation on that"
            instead of quietly answering from parametric memory.
    """

    passages: list[str]
    sources: list[str]
    found: bool


async def search_documentation(
    ctx: RunContext[AgentDeps],
    query: str,
    top_k: int = 5,
) -> KnowledgeAnswer:
    """Search Tapis and HPC documentation for reference material.

    Call this for questions about how Tapis works, what an app or parameter
    means, queue policies, or general HPC usage. Do *not* call it for questions
    about the user's own files or jobs — those need the files and jobs tools,
    which query live state. """
    store = ctx.deps.knowledge
    if store is None:
        # Ingestion never ran / Chroma unavailable. Report "not found" so the
        # model says it lacks documentation rather than erroring the run.
        return KnowledgeAnswer(passages=[], sources=[], found=False)

    try:
        # Chroma's client is synchronous and the query makes a blocking HTTP
        # call to the embedding endpoint; offload it so we don't stall the
        # event loop serving other concurrent requests.
        chunks = await asyncio.to_thread(
            store.search, query, top_k=top_k, min_score=_MIN_SCORE
        )
    except Exception as exc:
        # A retrieval failure (embedding endpoint down, etc.) should degrade to
        # "no documentation", not crash the conversation.
        logger.warning("documentation search failed: %s", exc)
        return KnowledgeAnswer(passages=[], sources=[], found=False)

    if not chunks:
        return KnowledgeAnswer(passages=[], sources=[], found=False)

    passages = [c.text for c in chunks]
    # Deduplicate sources while preserving first-seen (most-relevant) order.
    sources = list(dict.fromkeys(c.source for c in chunks))
    return KnowledgeAnswer(passages=passages, sources=sources, found=True)
