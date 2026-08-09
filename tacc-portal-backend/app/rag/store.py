import logging
from dataclasses import dataclass
from typing import Any

import chromadb
import numpy as np
from chromadb.api.types import Documents, Embeddings
from chromadb.utils.embedding_functions.openai_embedding_function import (
    OpenAIEmbeddingFunction,
)

from app.config import get_settings

logger = logging.getLogger(__name__)


class _SingleInputOpenAIEmbeddingFunction(OpenAIEmbeddingFunction):
    """OpenAI-compatible embedding function that sends one string per request."""

    def __call__(self, input: Documents) -> Embeddings:
        if not input:
            return []
        embeddings: list[Any] = []
        for text in input:
            params: dict[str, Any] = {"model": self.model_name, "input": text}
            if self.dimensions is not None and "text-embedding-3" in self.model_name:
                params["dimensions"] = self.dimensions
            response = self.client.embeddings.create(**params)
            embeddings.append(np.array(response.data[0].embedding, dtype=np.float32))
        return embeddings


_EMBEDDING_MODEL_KEY = "embedding_model"

# Cosine space: distance in [0, 2]; similarity = 1 - distance lands in the
# intuitive [-1, 1] (and ~[0, 1] for the non-negative embeddings), so a
# `min_score` threshold reads naturally as "higher is better".
_HNSW_SPACE = "cosine"

# Metadata key under which upsert stores the human-readable origin.
_SOURCE_KEY = "source"

_E5_QUERY_INSTRUCTION = (
    "Instruct: Given a question about Tapis, TACC systems, or HPC usage, "
    "retrieve relevant documentation passages.\nQuery: "
)


def _query_instruction_for(embedding_model: str) -> str:
    """Return the query-side instruction prefix for a model, or '' if none applies."""
    name = embedding_model.lower()
    if "e5" in name and "instruct" in name:
        return _E5_QUERY_INSTRUCTION
    return ""


@dataclass(frozen=True)
class RetrievedChunk:
    """One passage returned by a similarity search."""

    text: str
    source: str
    score: float
    metadata: dict[str, str]


class VectorStore:

    def __init__(self, persist_dir: str, collection_name: str, embedding_model: str) -> None:
        settings = get_settings()
        self._embedding_model = embedding_model
        self._query_instruction = _query_instruction_for(embedding_model)

        try:
            self._client = chromadb.PersistentClient(path=persist_dir)
        except Exception as exc:
            raise RuntimeError(
                f"Could not open Chroma persistent store at {persist_dir!r}: {exc}"
            ) from exc

        embed_fn = _SingleInputOpenAIEmbeddingFunction(
            # The embedding endpoint may not require a key, but the function
            # insists on a non-empty one; settings guarantees a key when a
            # custom base_url is set (see config._check_llm_endpoint).
            api_key=settings.openai_api_key or "not-needed",
            api_base=settings.base_url,
            model_name=embedding_model,
        )

        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            embedding_function=embed_fn,
            metadata={"hnsw:space": _HNSW_SPACE, _EMBEDDING_MODEL_KEY: embedding_model},
        )

        existing_model = (self._collection.metadata or {}).get(_EMBEDDING_MODEL_KEY)
        if existing_model and existing_model != embedding_model:
            raise RuntimeError(
                f"Collection {collection_name!r} was built with embedding model "
                f"{existing_model!r}, but {embedding_model!r} is now configured. "
                "Re-ingest with `--reset` after changing the embedding model."
            )

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        min_score: float | None = None,
        where: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]:
        """Return the passages most relevant to ``query``."""
        if not query.strip():
            return []

        result = self._collection.query(
            query_texts=[self._query_instruction + query],
            n_results=top_k,
            where=where or None,
            include=["documents", "metadatas", "distances"],
        )

        # Chroma returns one list per query.
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]

        chunks: list[RetrievedChunk] = []
        for text, meta, distance in zip(documents, metadatas, distances):
            meta_dict = {str(k): str(v) for k, v in (meta or {}).items()}
            source = meta_dict.pop(_SOURCE_KEY, "unknown")
            score = 1.0 - float(distance)
            if min_score is not None and score < min_score:
                continue
            chunks.append(
                RetrievedChunk(text=text, source=source, score=score, metadata=meta_dict)
            )
        return chunks

    def upsert(self, chunks: list["RetrievedChunk"], *, ids: list[str]) -> None:
        """Insert or replace chunks, embedding them via the collection function."""
        if len(ids) != len(chunks):
            raise ValueError(
                f"ids and chunks must be the same length; got {len(ids)} ids "
                f"for {len(chunks)} chunks."
            )
        if not chunks:
            return

        batch_size = 100
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            batch_ids = ids[start : start + batch_size]
            self._collection.upsert(
                ids=batch_ids,
                documents=[c.text for c in batch],
                metadatas=[{_SOURCE_KEY: c.source, **c.metadata} for c in batch],
            )

    def count(self) -> int:
        """Return the number of indexed chunks (0 means ingestion never ran)."""
        return self._collection.count()
