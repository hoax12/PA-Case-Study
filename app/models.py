"""Typed contracts shared by the OCR, NER, persistence, and UI layers."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class SourceType(StrEnum):
    PRINTED = "printed"
    HANDWRITTEN = "handwritten"
    CHECKBOX = "checkbox"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class VerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    NEEDS_REVIEW = "needs_review"
    VERIFIED = "verified"
    CORRECTED = "corrected"


class CriterionStatus(StrEnum):
    MET = "met"
    NOT_MET = "not_met"
    UNKNOWN = "unknown"


class BoundingBox(BaseModel):
    """Normalized source coordinates where (0, 0) is the top-left corner."""

    x1: float = Field(ge=0, le=1)
    y1: float = Field(ge=0, le=1)
    x2: float = Field(ge=0, le=1)
    y2: float = Field(ge=0, le=1)

    @field_validator("x2")
    @classmethod
    def x2_after_x1(cls, value: float, info: Any) -> float:
        if "x1" in info.data and value < info.data["x1"]:
            raise ValueError("x2 must be greater than or equal to x1")
        return value

    @field_validator("y2")
    @classmethod
    def y2_after_y1(cls, value: float, info: Any) -> float:
        if "y1" in info.data and value < info.data["y1"]:
            raise ValueError("y2 must be greater than or equal to y1")
        return value


class OcrBlock(BaseModel):
    block_id: str
    document_id: str
    page: int = Field(default=1, ge=1)
    text: str
    confidence: float = Field(ge=0, le=1)
    source_type: SourceType = SourceType.UNKNOWN
    bbox: BoundingBox | None = None
    alternatives: list[str] = Field(default_factory=list)


class OcrDocument(BaseModel):
    document_id: str
    file_name: str
    document_type: str
    raw_text: str
    overall_confidence: float = Field(ge=0, le=1)
    blocks: list[OcrBlock] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OcrBatchResult(BaseModel):
    documents: list[OcrDocument]
    average_confidence: float = Field(ge=0, le=1)
    provider: str
    warnings: list[str] = Field(default_factory=list)


class ExtractedEntity(BaseModel):
    entity_id: str
    section: Literal[
        "patient",
        "provider",
        "request",
        "clinical",
        "history",
        "assessment",
        "other",
    ]
    field_name: str
    display_name: str
    value: str | None = None
    normalized_value: str | None = None
    data_type: Literal[
        "text", "date", "number", "boolean", "identifier", "measurement"
    ] = "text"
    unit: str | None = None
    confidence: float = Field(ge=0, le=1)
    evidence_text: str
    document_id: str
    bbox: BoundingBox | None = None
    alternatives: list[str] = Field(default_factory=list)
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    notes: str | None = None


class ExtractionResult(BaseModel):
    entities: list[ExtractedEntity]
    missing_fields: list[str] = Field(default_factory=list)
    overall_confidence: float = Field(ge=0, le=1)
    human_review_required: bool = True
    review_reasons: list[str] = Field(default_factory=list)
    provider: str


class CriterionEvidence(BaseModel):
    """Patient evidence bearing on one criterion.

    Deliberately has no met/not_met field: the model reports what the chart says,
    and `app.adjudication` decides what that means.
    """

    criterion_id: str
    found: bool
    value: str | None = None
    value_date: str | None = None
    evidence_text: str = ""
    document_id: str = ""
    page: int | None = None


class ClauseSearchRequest(BaseModel):
    query: str = Field(min_length=3, max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=20)


class CorrectionRequest(BaseModel):
    entity_id: str
    corrected_value: str
    reviewer: str = Field(default="Clinical reviewer", min_length=1, max_length=120)
    verified: bool = True


class ReviewerDecisionRequest(BaseModel):
    """A clinician's conclusion, per criterion or for the whole request.

    `final_outcome` is free text because a licensed reviewer may record a denial.
    The system's own `Outcome` type has no such member; a non-affirmation can only
    enter the record through this endpoint, attributed to a person.
    """

    criterion_id: str | None = None
    system_status: str | None = None
    reviewer_status: Literal["met", "not_met", "unknown"] | None = None
    final_outcome: str | None = Field(default=None, max_length=120)
    reason_code: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=2000)
    reviewer: str = Field(default="Clinical reviewer", min_length=1, max_length=120)


class JobEvent(BaseModel):
    id: int
    job_id: str
    event_type: str
    stage: str
    payload: dict[str, Any]
    created_at: str


def confidence_band(confidence: float) -> Literal["high", "medium", "low"]:
    if confidence >= 0.85:
        return "high"
    if confidence >= 0.65:
        return "medium"
    return "low"
