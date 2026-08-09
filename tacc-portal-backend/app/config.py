from functools import lru_cache
from typing import Annotated, Any
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


# The directory containing the backend code. This is used to locate the .env file and other resources relative to the backend code.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Validated runtime configuration. For each field, the default value is used if the environment variable is not set. """
    model_config = SettingsConfigDict(
        env_file=(_BACKEND_ROOT / "etc" / ".env", _BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        # Frozen so a request handler cannot mutate shared configuration.
        frozen=True,
    )

    tapis_tenant_url: str = "https://portals.tapis.io"
    require_token_verification: bool = True
    chroma_persist_dir: str = "./my_chroma_db"
    chroma_collection: str = "documentation"
    embedding_model: str = "E5-Mistral-7B-Instruct"
    llm_model: str = "gpt-oss-120b"
    
    # OpenAI-compatible inference endpoint
    base_url: str | None = None
    openai_api_key: str | None = None
    llm_provider: str = "openai"  # or "tapis"
    # NoDecode: stop pydantic-settings from trying to JSON-parse this from the
    # environment, so ALLOWED_ORIGINS can be written as a plain comma-separated
    # list rather than a JSON array.
    allowed_origins: Annotated[tuple[str, ...], NoDecode] = ()
    request_timeout_seconds: float = 30.0

    @field_validator("tapis_tenant_url")
    @classmethod
    def _normalize_tenant_url(cls, value: str) -> str:
        """Strip trailing slashes and validate that the URL is absolute."""
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError(
                f"tapis_tenant_url must be an absolute http(s) URL, got {value!r}"
            )
        return value

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> Any:
        """Split a comma-separated string into a tuple of origins."""
        if isinstance(value, str):
            value = tuple(part.strip() for part in value.split(",") if part.strip())
        if isinstance(value, (list, tuple)) and "*" in value:
            raise ValueError(
                "allowed_origins must not contain '*'. List explicit origins instead."
            )
        return value
    
    @field_validator("base_url")
    @classmethod
    def _normalize_base_url(cls, value: str | None) -> str | None:
        """Strip trailing slashes; treat empty string as unset."""
        if value is not None:
            value = value.strip().rstrip("/")
            if not value:
                value = None
        return value
    
    @model_validator(mode="after")
    def _check_llm_endpoint(self) -> "Settings":
        """A custom endpoint without a key fails at the first model call, deep inside an agent run. Catch it at startup instead."""
        if self.base_url and not self.openai_api_key:
            raise ValueError(
                "base_url is set but openai_api_key is not. "
                "Either set openai_api_key or unset base_url."
            )
        return self
    

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the validated runtime configuration."""
    try:
        return Settings()
    except Exception as exc:
        raise RuntimeError(f"Invalid backend configuration: {exc}") from exc
