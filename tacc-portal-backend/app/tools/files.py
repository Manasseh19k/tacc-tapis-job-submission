from dataclasses import dataclass, field

from pydantic_ai import RunContext

from app.deps import AgentDeps
from app.tapis_client import TapisClientError, summarize_tapis_error

# Hard cap on directory entries returned per list_files call, and on jobs
# fetched per page from Tapis (requested as +1 to detect truncation without a
# separate call — see list_files).
_MAX_LIST_ENTRIES = 200


@dataclass
class FileEntry:
    name: str
    path: str
    type: str
    size_bytes: int | None
    last_modified: str | None


@dataclass
class FileListing:
    entries: list[FileEntry] = field(default_factory=list)
    truncated: bool = False

    def __bool__(self) -> bool:
        return bool(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)


def _normalize_path(path: str) -> str:
    parts: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if parts:
                parts.pop()
            continue
        parts.append(segment)
    return "/".join(parts)


async def list_systems(ctx: RunContext[AgentDeps]) -> list[dict[str, str]]:
    try:
        systems = ctx.deps.tapis.systems.getSystems(
            listType="ALL", select="id,host,systemType,description"
        )
    except Exception as exc:
        raise TapisClientError(summarize_tapis_error(exc)) from exc

    return [
        {
            "id": system.get("id", ""),
            "host": system.get("host", ""),
            "system_type": system.get("systemType", ""),
            "description": system.get("description") or "",
        }
        for system in systems
    ]


async def list_files(
    ctx: RunContext[AgentDeps],
    system_id: str | None = None,
    path: str = "/",
) -> FileListing:
    if system_id is None:
        system_id = ctx.deps.default_system_id
    if system_id is None:
        raise ValueError(
            "No system was specified and no default system is set for this "
            "session. Ask the user which Tapis system to use."
        )

    try:
        raw_entries = ctx.deps.tapis.files.listFiles(
            systemId=system_id,
            path=_normalize_path(path),
            limit=_MAX_LIST_ENTRIES + 1,
        )
    except Exception as exc:
        raise TapisClientError(summarize_tapis_error(exc)) from exc

    truncated = len(raw_entries) > _MAX_LIST_ENTRIES
    raw_entries = raw_entries[:_MAX_LIST_ENTRIES]

    entries = [
        FileEntry(
            name=e.get("name", ""),
            path=e.get("path", ""),
            type=e.get("type", "unknown"),
            size_bytes=(e.get("size") * 1024 if e.get("size") is not None else None),
            last_modified=e.get("lastModified"),
        )
        for e in raw_entries
    ]
    return FileListing(entries=entries, truncated=truncated)


async def read_file(
    ctx: RunContext[AgentDeps],
    system_id: str,
    path: str,
    max_bytes: int = 20_000,
) -> str:
    try:
        raw = ctx.deps.tapis.files.getContents(
            systemId=system_id,
            path=_normalize_path(path),
            _tapis_headers={"range": f"range=0,{max_bytes}"},
        )
    except Exception as exc:
        raise TapisClientError(summarize_tapis_error(exc)) from exc

    if not isinstance(raw, (bytes, bytearray)):
        raw = str(raw).encode("utf-8", errors="replace")

    truncated = len(raw) > max_bytes
    content_bytes = bytes(raw[:max_bytes])

    if b"\x00" in content_bytes:
        return f"[{path} looks like a binary file — not displaying contents.]"

    try:
        text = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return f"[{path} looks like a binary file — not displaying contents.]"

    if truncated:
        text += f"\n\n[... truncated at {max_bytes} bytes ...]"
    return text
