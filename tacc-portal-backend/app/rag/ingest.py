"""Offline ingestion pipeline for the knowledge base.

It can be run as a standalone script, not from a request handler — ingestion is slow,
network-bound, and does not belong in a user's conversation turn. USE CASE:

    uv run python -m app.rag.ingest --source ./docs
    uv run python -m app.rag.ingest --url https://tapis.readthedocs.io/en/latest/technical/jobs.html
    uv run python -m app.rag.ingest --source ./docs --reset

The output is the Chroma collection that :mod:`app.rag.store` serves at query
time.
"""

import argparse
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import tiktoken

logger = logging.getLogger(__name__)

_SUPPORTED_EXTENSIONS = {".md", ".txt", ".html", ".htm", ".pdf"}

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

# tiktoken is only a size proxy here — the real embedding model tokenizes
# differently, but cl100k gives a stable, fast token estimate for sizing chunks.
_ENCODER: "tiktoken.Encoding | None" = None


def _encoder() -> "tiktoken.Encoding":
    global _ENCODER
    if _ENCODER is None:
        _ENCODER = tiktoken.get_encoding("cl100k_base")
    return _ENCODER


@dataclass(frozen=True)
class SourceDocument:
    """A document fetched for indexing, before chunking.

    Attributes:
        text: Full plain-text body, already stripped of markup.
        source: Canonical origin (URL or path), carried through to every chunk
            so retrieved answers stay attributable.
        metadata: Extra fields to attach to each derived chunk — ``title``,
            ``mtime``, section, product version. Version matters especially for
            Tapis docs, where advice differs between v2 and v3.
    """

    text: str
    source: str
    metadata: dict[str, str]


def load_sources(root: Path) -> list[SourceDocument]:
    """Collect documents to index from a local directory.

    Walks ``root`` recursively for supported extensions (``.md``, ``.txt``,
    ``.html``/``.htm``, ``.pdf``), converts each to plain text, and records the
    relative path as ``source`` plus the file mtime in metadata. Files that
    fail to read or yield no text are logged and skipped rather than aborting.
    """
    if not root.exists():
        raise FileNotFoundError(f"Source root does not exist: {root}")

    documents: list[SourceDocument] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
            continue
        try:
            text = _read_file_text(path)
        except Exception as exc:
            logger.warning("skipping %s: %s", path, exc)
            continue
        if not text.strip():
            continue
        documents.append(
            SourceDocument(
                text=text,
                source=str(path.relative_to(root)),
                metadata={"title": path.stem, "mtime": str(int(path.stat().st_mtime))},
            )
        )
    logger.info("loaded %d document(s) from %s", len(documents), root)
    return documents


def _read_file_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".md", ".txt"}:
        return path.read_text(encoding="utf-8", errors="replace")
    if ext in {".html", ".htm"}:
        return _html_to_text(path.read_text(encoding="utf-8", errors="replace"))
    if ext == ".pdf":
        return _read_pdf(path)
    return ""


def _read_pdf(path: Path) -> str:
    try:
        import pypdf
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "pypdf is not installed; cannot read PDF. Add it with `uv add pypdf`."
        ) from exc
    reader = pypdf.PdfReader(str(path))
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


def _html_to_text(html: str) -> str:
    """Extract the main article text from an HTML page, dropping chrome.

    Strips script/style/nav/header/footer/aside — for ReadTheDocs pages that
    removes the sidebar navigation, which would otherwise flood the index with
    hundreds of near-identical low-signal chunks.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    return main.get_text(separator="\n")


def fetch_web_sources(urls: list[str], *, delay_seconds: float = 0.5) -> list[SourceDocument]:
    """Fetch and clean remote documentation pages.

    Retrieves each URL sequentially (with a small politeness delay), isolates
    the main article body, and returns one cleaned document per successful
    fetch.
    """
    import httpx

    from bs4 import BeautifulSoup

    documents: list[SourceDocument] = []
    with httpx.Client(
        timeout=30.0,
        follow_redirects=True,
        headers={"User-Agent": "tacc-portal-ingest"},
    ) as client:
        for url in urls:
            try:
                response = client.get(url)
                response.raise_for_status()
            except Exception as exc:
                logger.warning("fetch failed for %s: %s", url, exc)
                continue

            soup = BeautifulSoup(response.text, "html.parser")
            title = soup.title.string.strip() if soup.title and soup.title.string else url
            text = _html_to_text(response.text)
            if text.strip():
                documents.append(
                    SourceDocument(text=text, source=url, metadata={"title": title})
                )
            time.sleep(delay_seconds)
    logger.info("fetched %d document(s) from %d URL(s)", len(documents), len(urls))
    return documents


def _segment(text: str) -> list[tuple[str, str]]:
    """Split text into ``(heading_path, paragraph)`` pairs.

    Markdown headings update a running heading stack; blank lines separate
    paragraphs. Every paragraph is tagged with the ``a > b > c`` heading path
    it sits under, so chunking can keep a section together and label it.
    """
    segments: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        paragraph = "\n".join(buffer).strip()
        buffer = []
        if paragraph:
            heading_path = " > ".join(title for _, title in stack)
            segments.append((heading_path, paragraph))

    for line in text.splitlines():
        heading = _HEADING_RE.match(line.strip())
        if heading:
            flush()
            level = len(heading.group(1))
            title = heading.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
        elif not line.strip():
            flush()
        else:
            buffer.append(line)
    flush()
    return segments


def chunk_document(doc: SourceDocument, *, max_tokens: int = 512, overlap: int = 64) -> list[SourceDocument]:
    """Split a document into retrieval-sized passages.

    Splits on structure first (headings, then paragraphs), packing paragraphs
    into chunks up to ``max_tokens`` and carrying ``overlap`` tokens across
    boundaries so a passage straddling a split is retrievable from either side.
    A single oversized paragraph is hard-split at the token limit. Each chunk's
    text is prefixed with the document title and heading path, because chunks
    are retrieved in isolation and "run this with --force" is useless without
    the context naming what it applies to.
    """
    enc = _encoder()
    title = (doc.metadata.get("title") or doc.source).strip()
    chunks: list[SourceDocument] = []

    def add_chunk(heading: str, body_token_ids: list[int]) -> None:
        body = enc.decode(body_token_ids).strip()
        if not body:
            return
        header = f"{title} — {heading}" if heading else title
        index = len(chunks)
        metadata = {**doc.metadata, "chunk_index": str(index)}
        if heading:
            metadata["heading"] = heading
        chunks.append(
            SourceDocument(text=f"{header}\n\n{body}", source=doc.source, metadata=metadata)
        )

    current_heading: str | None = None
    current: list[int] = []

    for heading_path, paragraph in _segment(doc.text):
        # A heading change closes the current chunk cleanly (no overlap carried
        # across sections — the sections are about different things).
        if heading_path != current_heading:
            if current:
                add_chunk(current_heading or "", current)
                current = []
            current_heading = heading_path

        for piece in _split_tokens(enc.encode(paragraph), max_tokens):
            if current and len(current) + len(piece) > max_tokens:
                add_chunk(current_heading or "", current)
                current = current[-overlap:] if overlap > 0 else []
            current.extend(piece)

    if current:
        add_chunk(current_heading or "", current)
    return chunks


def _split_tokens(token_ids: list[int], max_tokens: int) -> list[list[int]]:
    """Yield token slices no larger than ``max_tokens`` (for oversized paragraphs)."""
    if len(token_ids) <= max_tokens:
        return [token_ids]
    return [token_ids[i : i + max_tokens] for i in range(0, len(token_ids), max_tokens)]


def stable_chunk_id(chunk: SourceDocument, index: int) -> str:
    """Derive a deterministic id from source + index."""
    key = f"{chunk.source}::{index}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


def run_ingestion(source_root: Path | None = None, urls: list[str] | None = None) -> int:
    """Execute the full load → chunk → embed → upsert pipeline."""
    if source_root is None and not urls:
        raise ValueError("Provide a source root and/or URLs to ingest.")

    # Imported here so `load_sources`/`chunk_document` stay usable (and testable)
    # without opening Chroma or reaching the embedding endpoint.
    from app.config import get_settings
    from app.rag.store import RetrievedChunk, VectorStore

    settings = get_settings()
    store = VectorStore(
        persist_dir=settings.chroma_persist_dir,
        collection_name=settings.chroma_collection,
        embedding_model=settings.embedding_model,
    )

    documents: list[SourceDocument] = []
    if source_root is not None:
        documents.extend(load_sources(source_root))
    if urls:
        documents.extend(fetch_web_sources(urls))

    total_chunks = 0
    for doc in documents:
        chunks = chunk_document(doc)
        if not chunks:
            continue
        ids = [stable_chunk_id(chunk, i) for i, chunk in enumerate(chunks)]
        store.upsert(
            [
                RetrievedChunk(text=c.text, source=c.source, score=0.0, metadata=c.metadata)
                for c in chunks
            ],
            ids=ids,
        )
        total_chunks += len(chunks)
        logger.info("ingested %s -> %d chunk(s)", doc.source, len(chunks))

    logger.info(
        "ingestion complete: %d chunk(s) across %d document(s); collection now holds %d",
        total_chunks,
        len(documents),
        store.count(),
    )
    return total_chunks


def main() -> None:
    """CLI entrypoint. ``--source`` and/or ``--url`` (repeatable); ``--reset`` drops the collection first."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Ingest documentation into the knowledge base.")
    parser.add_argument("--source", type=Path, help="Local directory to index recursively.")
    parser.add_argument("--url", action="append", default=[], help="Documentation URL to index (repeatable).")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop and recreate the collection first (do this after changing the embedding model or chunking).",
    )
    args = parser.parse_args()

    if args.source is None and not args.url:
        parser.error("supply --source and/or --url")

    if args.reset:
        import chromadb

        from app.config import get_settings

        settings = get_settings()
        client = chromadb.PersistentClient(path=settings.chroma_persist_dir)
        try:
            client.delete_collection(settings.chroma_collection)
            logger.info("reset: dropped collection %s", settings.chroma_collection)
        except Exception as exc:
            logger.info("reset: nothing to drop (%s)", exc)

    written = run_ingestion(source_root=args.source, urls=args.url)
    logger.info("done: %d chunk(s) written", written)


if __name__ == "__main__":
    main()
