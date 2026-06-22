from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, JsonConfigSettingsSource, SettingsConfigDict


class MemoGenerationSettings(BaseModel):
    min_messages: int = 5
    min_human_tokens: int = 500


class EmbeddingSettings(BaseModel):
    enabled: bool = True
    provider: str = "google"
    model: str = "gemini-embedding-2"
    dimensions: int = 3072
    # Dimension stored in the vec0 tables and used for KNN queries. When set
    # below `dimensions`, embeddings are Matryoshka-truncated (first N dims +
    # L2 renormalize) before storage/search — the API still returns and the
    # cache still stores full `dimensions` fidelity, so this is fully
    # reversible. None → no truncation (vec tables use `dimensions`).
    index_dimensions: int | None = None
    api_key_env: str = "GEMINI_API_KEY"

    @property
    def effective_index_dimensions(self) -> int:
        """The dimension actually stored in vec tables / used for queries."""
        d = self.index_dimensions or self.dimensions
        try:
            return int(d)
        except (TypeError, ValueError):
            return int(self.dimensions)


class SearchSettings(BaseModel):
    default_mode: str = "hybrid"
    vector_weight: float = 0.7
    bm25_weight: float = 0.3
    chunk_max_tokens: int = 400
    chunk_overlap_tokens: int = 80


class RerankerSettings(BaseModel):
    enabled: bool = False


class DreamerSettings(BaseModel):
    llm_enabled: bool = True
    model: str = "sonnet"
    max_observations_per_call: int = 200


class AutoMemorySettings(BaseModel):
    enabled: bool = True
    exclude_projects: list[str] = Field(default_factory=list)
    sync_volatile: bool = True


class Settings(BaseSettings):
    _config_path: ClassVar[Path | None] = None

    memex_path: str | None = None
    state_dir: str = "~/.memex"
    memo_generation: MemoGenerationSettings = Field(default_factory=MemoGenerationSettings)
    embeddings: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    reranker: RerankerSettings = Field(default_factory=RerankerSettings)
    dreamer: DreamerSettings = Field(default_factory=DreamerSettings)
    auto_memory: AutoMemorySettings = Field(default_factory=AutoMemorySettings)
    # Explicit cwd-substring → project-name overrides. Must be a declared field:
    # with extra="ignore", an undeclared key in config.json is silently dropped
    # from model_dump(), which is what made detect_project()'s mapping check a
    # silent no-op in CLI/pydantic context (it only worked via the hook raw-json
    # fallback) — the root cause of split-brain project detection.
    project_mappings: dict[str, str] = Field(default_factory=dict)

    model_config = SettingsConfigDict(
        env_prefix="MEMEX_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        return (
            init_settings,
            env_settings,
            JsonConfigSettingsSource(
                settings_cls,
                json_file=settings_cls._config_path,
                deep_merge=True,
            ),
            dotenv_settings,
            file_secret_settings,
        )


_settings_cache: Settings | None = None
_settings_cache_path: Path | None = None


def _default_config_path() -> Path:
    return Path("~/.memex/config.json").expanduser()


def get_settings(config_path: Path | None = None) -> Settings:
    global _settings_cache, _settings_cache_path

    resolved_config_path = (config_path or _default_config_path()).expanduser()
    if _settings_cache is not None and _settings_cache_path == resolved_config_path:
        return _settings_cache

    Settings._config_path = resolved_config_path if resolved_config_path.exists() else None
    _settings_cache = Settings()
    _settings_cache_path = resolved_config_path
    return _settings_cache


def reset_settings() -> None:
    global _settings_cache, _settings_cache_path

    _settings_cache = None
    _settings_cache_path = None
    Settings._config_path = None
