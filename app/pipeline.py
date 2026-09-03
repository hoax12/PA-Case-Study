"""The deterministic adjudication pipeline.

A prior-authorization review is a fixed sequence, so it is written as one. Model
calls are bounded functions inside this sequence - they transcribe, extract, and
locate evidence. Routing, criterion evaluation, and the outcome are computed by
application code that cannot emit a denial.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any, Callable

from app.adjudication import (
    Adjudication,
    DecisionContext,
    decide,
    evaluate_criterion,
    parse_date,
)
from app.config import Settings, get_settings
from app.models import CriterionEvidence, ExtractionResult, OcrBatchResult
from app.providers import ClinicalExtractionProvider, build_provider
from app.registry import CriteriaRegistry, RouteResult
from app.repository import ReviewRepository


logger = logging.getLogger(__name__)

Emit = Callable[[str, str, dict[str, Any]], None]


class PriorAuthPipeline:
    def __init__(
        self,
        repository: ReviewRepository,
        settings: Settings | None = None,
        provider: ClinicalExtractionProvider | None = None,
        registry: CriteriaRegistry | None = None,
    ):
        self.repository = repository
        self.settings = settings or get_settings()
        self._provider = provider
        self._registry = registry

    @property
    def provider(self) -> ClinicalExtractionProvider:
        if self._provider is None:
            self._provider = build_provider(self.settings)
        return self._provider

    @property
    def registry(self) -> CriteriaRegistry:
        if self._registry is None:
            from app.tools import get_registry

            self._registry = get_registry()
        return self._registry

    async def run_job(self, job_id: str, image_paths: list[Path]) -> None:
        def emit(event_type: str, stage: str, payload: dict[str, Any]) -> None:
            self.repository.append_event(job_id, event_type, stage, payload)

        self.repository.update_job(job_id, status="running", error=None)
        emit(
            "stage",
            "intake",
            {
                "status": "completed",
                "message": f"Validated {len(image_paths)} uploaded document(s).",
            },
        )
        if self.provider.name == "fixture":
            emit(
                "mode",
                "workflow",
                {
                    "status": "notice",
                    "message": (
                        "Offline fixture mode is active. Set ANTHROPIC_API_KEY and "
                        "EXTRACTION_PROVIDER=claude for live model calls."
                    ),
                },
            )
        try:
            ocr = await self._run_ocr(job_id, image_paths, emit)
            extraction = await self._run_extraction(job_id, ocr, emit)
            await self._run_adjudication(job_id, ocr, extraction, emit)
            self.repository.update_job(job_id, status="ready")
            emit(
                "stage",
                "review",
                {
                    "status": "completed",
                    "message": "The packet is ready for clinician review.",
                },
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            logger.exception("Job %s failed", job_id)
            self.repository.update_job(job_id, status="error", error=message)
            emit(
                "error",
                "workflow",
                {
                    "status": "error",
                    "message": message,
                    "action": "Check provider configuration and retry the intake.",
                },
            )

    async def _run_ocr(
        self, job_id: str, image_paths: list[Path], emit: Emit
    ) -> OcrBatchResult:
        emit(
            "tool_call",
            "ocr",
            {
                "tool": "ocr_patient_documents",
                "arguments": {
                    "job_id": job_id,
                    "files": [path.name for path in image_paths],
                },
            },
        )
        result = await self.provider.ocr_documents(
            validate_upload_paths(job_id, [str(path) for path in image_paths])
        )
        self.repository.save_ocr(job_id, result)
        emit(
            "tool_result",
            "ocr",
            {
                "tool": "ocr_patient_documents",
                "status": "success",
                "document_count": len(result.documents),
                "average_confidence": round(result.average_confidence, 4),
                "provider": result.provider,
            },
        )
        return result

    async def _run_extraction(
        self, job_id: str, ocr: OcrBatchResult, emit: Emit
    ) -> ExtractionResult:
        emit(
            "tool_call",
            "ner",
            {
                "tool": "extract_patient_entities",
                "arguments": {"job_id": job_id, "source": "ocr_result"},
            },
        )
        result = await self.provider.extract_entities(ocr)
        self.repository.save_extraction(job_id, result)
        emit(
            "tool_result",
            "ner",
            {
                "tool": "extract_patient_entities",
                "status": "success",
                "entity_count": len(result.entities),
                "overall_confidence": round(result.overall_confidence, 4),
                "needs_review_count": sum(
                    1 for entity in result.entities if entity.confidence < 0.85
                ),
                "provider": result.provider,
            },
        )
        return result

    async def _run_adjudication(
        self,
        job_id: str,
        ocr: OcrBatchResult,
        extraction: ExtractionResult,
        emit: Emit,
    ) -> Adjudication:
        registry = self.registry
        decision_date = date.today()

        # 1. Route. Which policy, plan pathway, and criteria set govern this request?
        line_of_business = entity_value(extraction, "line_of_business", "payer", "plan")
        procedure = entity_value(
            extraction, "requested_procedure", "procedure", "requested_service", "cpt"
        )
        routed = registry.route(line_of_business, procedure)
        route = routed if isinstance(routed, RouteResult) else None
        no_route_reason = None if route else routed.reason  # type: ignore[union-attr]
        emit(
            "tool_result",
            "routing",
            {
                "tool": "route_request",
                "status": "success",
                "line_of_business": line_of_business,
                "procedure": procedure,
                "policy_id": route.policy_id if route else None,
                "pathway": route.pathway_id if route else None,
                "criteria_ref": route.criteria_ref if route else None,
                "message": no_route_reason
                or f"Routed to {route.policy_id}/{route.criteria_ref}.",  # type: ignore[union-attr]
            },
        )

        # 2. Locate evidence for each governing clause. The model reads; it does
        #    not judge.
        criteria = registry.criteria_for(route) if route else []
        evidence_by_id: dict[str, CriterionEvidence] = {}
        if criteria and any(
            item.predicate.type != "external_ref" for item in criteria
        ):
            emit(
                "tool_call",
                "evidence",
                {
                    "tool": "locate_criterion_evidence",
                    "arguments": {
                        "job_id": job_id,
                        "criteria_count": len(criteria),
                    },
                },
            )
            found = await self.provider.assess_criteria(extraction, ocr, criteria)
            evidence_by_id = {item.criterion_id: item for item in found}
            emit(
                "tool_result",
                "evidence",
                {
                    "tool": "locate_criterion_evidence",
                    "status": "success",
                    "criteria_count": len(criteria),
                    "evidence_found": sum(1 for item in found if item.found),
                },
            )

        # 3. Evaluate each predicate, then decide. Both are deterministic.
        context = DecisionContext(
            order_date=entity_date(extraction, "order_date", "request_date"),
            planned_surgery_date=entity_date(
                extraction, "planned_surgery_date", "surgery_date", "scheduled_date"
            ),
            decision_date=decision_date,
        )
        results = [
            evaluate_criterion(
                criterion, evidence_by_id.get(criterion.id), context, ocr
            )
            for criterion in criteria
        ]
        adjudication = decide(
            results,
            route,
            decision_date=decision_date,
            threshold=self.settings.auto_affirm_threshold,
            no_route_reason=no_route_reason,
        )
        self.repository.save_adjudication(job_id, adjudication)
        emit(
            "tool_result",
            "decision",
            {
                "tool": "decide",
                "status": "success",
                "outcome": adjudication.outcome.value,
                "confidence": round(adjudication.confidence, 4),
                "met": sum(1 for item in results if item.status == "met"),
                "not_met": sum(1 for item in results if item.status == "not_met"),
                "unknown": sum(1 for item in results if item.status == "unknown"),
                "message": adjudication.rationale.split("\n")[0],
            },
        )
        return adjudication


def entity_value(extraction: ExtractionResult, *field_names: str) -> str | None:
    """Read the first populated entity whose field name matches one of the names."""

    for name in field_names:
        for entity in extraction.entities:
            haystack = f"{entity.field_name} {entity.display_name}".casefold()
            if name.casefold() in haystack and entity.value:
                return entity.value
    return None


def entity_date(extraction: ExtractionResult, *field_names: str) -> date | None:
    """Resolve a context date, refusing anything with more than one reading."""

    raw = entity_value(extraction, *field_names)
    resolved = parse_date(raw)
    if resolved is None or resolved.ambiguous:
        # An ambiguous anchor would silently shift every window hung off it.
        return None
    return resolved.candidates[0]


def validate_upload_paths(job_id: str, image_paths: list[str]) -> list[Path]:
    """Resolve inputs while preventing access outside the job upload directory."""

    settings = get_settings()
    job_dir = (settings.uploads_dir / job_id).resolve()
    allowed_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".pdf"}
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
        raise ValueError("At least one document is required")
    return validated
