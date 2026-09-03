"""Shared singletons for the pipeline and the HTTP layer."""

from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.registry import CriteriaRegistry
from app.repository import ReviewRepository


@lru_cache(maxsize=1)
def get_repository() -> ReviewRepository:
    repository = ReviewRepository(get_settings().resolved_app_db_path)
    repository.initialize()
    return repository


@lru_cache(maxsize=1)
def get_registry() -> CriteriaRegistry:
    return CriteriaRegistry(get_settings().policies_dir_path)
