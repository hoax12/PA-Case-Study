"""Application configuration and filesystem boundaries."""

from __future__ import annotations

import os
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

    google_api_key: str | None = Field(default=None, alias="GOOGLE_API_KEY")
    google_genai_use_enterprise: bool = Field(
        default=False, alias="GOOGLE_GENAI_USE_ENTERPRISE"
    )
    adk_model: str = Field(default="gemini-3.5-flash-lite", alias="ADK_MODEL")
    extraction_model: str = Field(
        default="gemini-3.5-flash-lite", alias="EXTRACTION_MODEL"
    )
    extraction_provider: Literal["auto", "gemini", "fixture"] = Field(
        default="auto", alias="EXTRACTION_PROVIDER"
    )
    app_db_path: Path = Field(default=Path("data/review.db"), alias="APP_DB_PATH")
    adk_db_path: Path = Field(
        default=Path("data/adk_sessions.db"), alias="ADK_DB_PATH"
    )
    max_upload_mb: int = Field(default=12, alias="MAX_UPLOAD_MB", ge=1, le=50)
    guideline_pdf_path: Path = Field(
        default=Path("knowledge/source/BariatricSurgery.pdf"),
        alias="GUIDELINE_PDF_PATH",
    )
    guideline_index_dir: Path = Field(
        default=Path("knowledge/index"), alias="GUIDELINE_INDEX_DIR"
    )
    embedding_model: str = Field(
        default="sentence-transformers/all-MiniLM-L6-v2",
        alias="EMBEDDING_MODEL",
    )
    rag_top_k: int = Field(default=8, alias="RAG_TOP_K", ge=1, le=12)
    rag_min_score: float = Field(
        default=0.22, alias="RAG_MIN_SCORE", ge=-1, le=1
    )

    @computed_field
    @property
    def provider_mode(self) -> Literal["gemini", "fixture"]:
        if self.extraction_provider == "fixture":
            return "fixture"
        if self.extraction_provider == "gemini":
            return "gemini"
        return "gemini" if self.google_api_key else "fixture"

    @property
    def resolved_app_db_path(self) -> Path:
        return self._resolve_project_path(self.app_db_path)

    @property
    def resolved_adk_db_path(self) -> Path:
        return self._resolve_project_path(self.adk_db_path)

    @property
    def uploads_dir(self) -> Path:
        return PROJECT_ROOT / "data" / "uploads"

    @property
    def static_dir(self) -> Path:
        return PROJECT_ROOT / "app" / "static"

    @property
    def resolved_guideline_pdf_path(self) -> Path:
        return self._resolve_project_path(self.guideline_pdf_path)

    @property
    def resolved_guideline_index_dir(self) -> Path:
        return self._resolve_project_path(self.guideline_index_dir)

    @staticmethod
    def _resolve_project_path(path: Path) -> Path:
        return path if path.is_absolute() else PROJECT_ROOT / path

    def ensure_directories(self) -> None:
        self.resolved_app_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.resolved_adk_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.resolved_guideline_index_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    # Pydantic reads .env values into the Settings object, while ADK's built-in
    # Gemini model resolves credentials from the process environment. Bridge
    # the two without ever logging the secret.
    if settings.google_api_key:
        os.environ["GOOGLE_API_KEY"] = settings.google_api_key
    settings.ensure_directories()
    return settings
