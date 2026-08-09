from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tapipy.tapis import Tapis

    from app.rag.store import VectorStore
    from app.security import AuthenticatedUser


@dataclass
class AgentDeps:
    user: "AuthenticatedUser"
    tapis: "Tapis"
    knowledge: "VectorStore"
    default_system_id: str | None = None
