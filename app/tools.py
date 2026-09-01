"""Google ADK function tools for OCR and clinical NER."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, MutableMapping

from google.adk.tools import ToolContext

from app.config import get_settings
from app.knowledge import BariatricGuidelineKnowledgeBase
from app.models import ExtractionResult, GuidelineGroundingResult, OcrBatchResult
from app.providers import ClinicalExtractionProvider, build_provider
from app.repository import ReviewRepository


@lru_cache(maxsize=1)
def get_repository() -> ReviewRepository:
    repository = ReviewRepository(get_settings().resolved_app_db_path)
    repository.initialize()
    return repository


@lru_cache(maxsize=1)
def get_provider() -> ClinicalExtractionProvider:
    return build_provider(get_settings())


@lru_cache(maxsize=1)
def get_guideline_knowledge_base() -> BariatricGuidelineKnowledgeBase:
    return BariatricGuidelineKnowledgeBase(get_settings())


def validate_upload_paths(job_id: str, image_paths: list[str]) -> list[Path]:
    """Resolve tool inputs while preventing access outside the job upload directory."""

    settings = get_settings()
    job_dir = (settings.uploads_dir / job_id).resolve()
    allowed_suffixes = {".png", ".jpg", ".jpeg", ".webp"}
    validated: list[Path] = []
    for raw_path in image_paths:
        candidate = Path(raw_path).resolve()
        if not candidate.is_relative_to(job_dir):
            raise ValueError("Document path is outside the active job directory")
        if candidate.suffix.lower() not in allowed_suffixes:
            raise ValueError(f"Unsupported document type: {candidate.suffix}")
        if not candidate.is_file():
            raise FileNotFoundError(f"Uploaded document not found: {candidate.name}")
        validated.append(candidate)
    if not validated:
        raise ValueError("At least one image is required")
    return validated


async def run_ocr(
    job_id: str,
    image_paths: list[str],
    state: MutableMapping[str, Any],
    provider: ClinicalExtractionProvider | None = None,
) -> OcrBatchResult:
    provider = provider or get_provider()
    result = await provider.ocr_documents(validate_upload_paths(job_id, image_paths))
    state["temp:ocr_result"] = result.model_dump(mode="json")
    state["last_ocr_summary"] = {
        "job_id": job_id,
        "document_count": len(result.documents),
        "average_confidence": result.average_confidence,
        "provider": result.provider,
    }
    get_repository().save_ocr(job_id, result)
    return result


async def run_ner(
    job_id: str,
    state: MutableMapping[str, Any],
    provider: ClinicalExtractionProvider | None = None,
) -> ExtractionResult:
    provider = provider or get_provider()
    raw_ocr = state.get("temp:ocr_result")
    if not raw_ocr:
        raise ValueError("OCR must complete before clinical entity extraction")
    ocr = OcrBatchResult.model_validate(raw_ocr)
    result = await provider.extract_entities(ocr)
    state["temp:extraction_result"] = result.model_dump(mode="json")
    state["last_extraction_summary"] = {
        "job_id": job_id,
        "entity_count": len(result.entities),
        "overall_confidence": result.overall_confidence,
        "needs_review": sum(
            1 for entity in result.entities if entity.confidence < 0.85
        ),
        "provider": result.provider,
    }
    get_repository().save_extraction(job_id, result)
    return result


async def run_guideline_grounding(
    job_id: str,
    state: MutableMapping[str, Any],
    knowledge_base: BariatricGuidelineKnowledgeBase | None = None,
) -> GuidelineGroundingResult:
    raw_extraction = state.get("temp:extraction_result")
    if not raw_extraction:
        raise ValueError("Entity extraction must complete before guideline grounding")
    extraction = ExtractionResult.model_validate(raw_extraction)
    knowledge_base = knowledge_base or get_guideline_knowledge_base()
    result = await knowledge_base.ground(extraction)
    state["temp:guideline_grounding"] = result.model_dump(mode="json")
    state["last_grounding_summary"] = {
        "job_id": job_id,
        "passage_count": len(result.passages),
        "criteria_count": len(result.criteria),
        "unknown_count": sum(
            1 for criterion in result.criteria if criterion.status == "unknown"
        ),
        "index_version": result.index_version,
    }
    get_repository().save_grounding(job_id, result)
    return result


async def ocr_patient_documents(
    job_id: str, image_paths: list[str], tool_context: ToolContext
) -> dict[str, Any]:
    """Transcribe uploaded patient images before any entity extraction.

    Args:
        job_id: The active intake job identifier supplied by the application.
        image_paths: Absolute paths for all uploaded PNG, JPEG, or WebP images in
            the active job directory. Process the complete list in one call.

    Returns:
        A status dictionary containing the OCR provider, document count, confidence,
        and warnings. The full OCR result is passed to the NER tool through ADK
        invocation state and persisted for the reviewer UI.
    """

    result = await run_ocr(job_id, image_paths, tool_context.state)
    return {
        "status": "success",
        "job_id": job_id,
        "provider": result.provider,
        "document_count": len(result.documents),
        "average_confidence": round(result.average_confidence, 4),
        "warnings": result.warnings,
        "next_action": "Call extract_patient_entities exactly once.",
    }


async def extract_patient_entities(
    job_id: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Extract patient and request key-value entities from the completed OCR result.

    Args:
        job_id: The same active intake job identifier used by the OCR tool.

    Returns:
        A status dictionary containing entity counts, confidence, missing fields,
        and human-review reasons. This tool never determines medical necessity.
    """

    result = await run_ner(job_id, tool_context.state)
    return {
        "status": "success",
        "job_id": job_id,
        "provider": result.provider,
        "entity_count": len(result.entities),
        "overall_confidence": round(result.overall_confidence, 4),
        "needs_review_count": sum(
            1 for entity in result.entities if entity.confidence < 0.85
        ),
        "missing_fields": result.missing_fields,
        "human_review_required": result.human_review_required,
        "review_reasons": result.review_reasons,
    }


async def ground_bariatric_guidelines(
    job_id: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Retrieve bariatric policy clauses and build a review-only criteria matrix.

    Args:
        job_id: The same active intake job used for OCR and entity extraction.

    Returns:
        A status summary containing retrieved passage and criteria counts. Full
        page-cited passages and the met/not_met/unknown matrix are persisted for
        the reviewer UI. This tool never approves or denies authorization.
    """

    result = await run_guideline_grounding(job_id, tool_context.state)
    return {
        "status": "success",
        "job_id": job_id,
        "knowledge_base": result.knowledge_base,
        "policy_number": result.policy_number,
        "passage_count": len(result.passages),
        "criteria_count": len(result.criteria),
        "unknown_count": sum(
            1 for criterion in result.criteria if criterion.status == "unknown"
        ),
        "overall_status": result.overall_status,
        "index_version": result.index_version,
        "human_review_required": True,
    }
