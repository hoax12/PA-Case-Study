"""Application configuration and filesystem boundaries."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    llm_model: str = Field(default="claude-opus-5", alias="LLM_MODEL")
    extraction_provider: Literal["auto", "claude", "fixture"] = Field(
        default="auto", alias="EXTRACTION_PROVIDER"
    )
    app_db_path: Path = Field(default=Path("data/review.db"), alias="APP_DB_PATH")
    max_upload_mb: int = Field(default=12, alias="MAX_UPLOAD_MB", ge=1, le=50)
    policies_dir: Path = Field(
        default=Path("knowledge/policies"), alias="POLICIES_DIR"
    )
    auto_affirm_threshold: float = Field(
        default=0.7, alias="AUTO_AFFIRM_THRESHOLD", ge=0, le=1
    )

    @computed_field
    @property
    def provider_mode(self) -> Literal["claude", "fixture"]:
        if self.extraction_provider == "fixture":
            return "fixture"
        if self.extraction_provider == "claude":
            return "claude"
        return "claude" if self.anthropic_api_key else "fixture"

    @property
    def resolved_app_db_path(self) -> Path:
        return self._resolve_project_path(self.app_db_path)

    @property
    def uploads_dir(self) -> Path:
        return PROJECT_ROOT / "data" / "uploads"

    @property
    def static_dir(self) -> Path:
        return PROJECT_ROOT / "app" / "static"

    @property
    def policies_dir_path(self) -> Path:
        return self._resolve_project_path(self.policies_dir)

    @staticmethod
    def _resolve_project_path(path: Path) -> Path:
        return path if path.is_absolute() else PROJECT_ROOT / path

    def ensure_directories(self) -> None:
        self.resolved_app_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
