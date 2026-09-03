"""Provider protocol, the offline fixture, and shared normalization helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from app.config import Settings, get_settings
from app.models import (
    ExtractedEntity,
    ExtractionResult,
    OcrBatchResult,
    OcrBlock,
    OcrDocument,
    SourceType,
    VerificationStatus,
)


class ClinicalExtractionProvider(Protocol):
    name: str

    async def ocr_documents(self, image_paths: list[Path]) -> OcrBatchResult: ...

    async def extract_entities(self, ocr: OcrBatchResult) -> ExtractionResult: ...


class FixtureClinicalProvider:
    """Honest, labeled offline fixture for rehearsing the supplied case."""

    name = "fixture"

    async def ocr_documents(self, image_paths: list[Path]) -> OcrBatchResult:
        documents: list[OcrDocument] = []
        for index, path in enumerate(image_paths, start=1):
            document_id = f"document-{index}"
            normalized_name = path.name.lower()
            if normalized_name.endswith("med2.webp"):
                documents.append(self._clinical_note(document_id, path.name))
            elif any(
                normalized_name.endswith(suffix)
                for suffix in (
                    "prior-authorization-form.jpg",
                    "prior-authorization-form.jpeg",
                    "prior-authorization-form.png",
                )
            ):
                documents.append(self._blank_prior_auth_form(document_id, path.name))
            else:
                documents.append(
                    OcrDocument(
                        document_id=document_id,
                        file_name=path.name,
                        document_type="unsupported fixture upload",
                        raw_text="",
                        overall_confidence=0,
                        warnings=[
                            "Fixture mode recognizes only med2.webp and "
                            "Prior-Authorization-Form.jpg. Set ANTHROPIC_API_KEY "
                            "for real OCR."
                        ],
                    )
                )
        average = (
            sum(document.overall_confidence for document in documents) / len(documents)
            if documents
            else 0
        )
        return OcrBatchResult(
            documents=documents,
            average_confidence=average,
            provider=self.name,
            warnings=[
                "Offline fixture mode is active. Confidence values are illustrative "
                "and must not be presented as a validated model evaluation."
            ],
        )

    async def extract_entities(self, ocr: OcrBatchResult) -> ExtractionResult:
        note = next(
            (
                document
                for document in ocr.documents
                if document.document_type == "handwritten clinical note"
            ),
            None,
        )
        if note is None:
            return ExtractionResult(
                entities=[],
                missing_fields=_required_intake_fields(),
                overall_confidence=0,
                human_review_required=True,
                review_reasons=["No supported clinical-note fixture was recognized."],
                provider=self.name,
            )

        def entity(
            entity_id: str,
            section: str,
            field_name: str,
            display_name: str,
            value: str,
            confidence: float,
            evidence: str,
            data_type: str = "text",
            unit: str | None = None,
            alternatives: list[str] | None = None,
        ) -> ExtractedEntity:
            return ExtractedEntity(
                entity_id=entity_id,
                section=section,
                field_name=field_name,
                display_name=display_name,
                value=value,
                normalized_value=value,
                data_type=data_type,
                unit=unit,
                confidence=confidence,
                evidence_text=evidence,
                document_id=note.document_id,
                alternatives=alternatives or [],
                verification_status=(
                    VerificationStatus.UNVERIFIED
                    if confidence >= 0.85
                    else VerificationStatus.NEEDS_REVIEW
                ),
                notes="Offline fixture extraction; verify against the source image.",
            )

        entities = [
            entity(
                "patient-sex",
                "patient",
                "sex",
                "Sex",
                "Female",
                0.94,
                "FEMALE (circled)",
            ),
            entity(
                "clinical-encounter-date",
                "clinical",
                "encounter_date",
                "Encounter date",
                "02/04/14",
                0.55,
                "DATE 02/04/14",
                data_type="date",
                alternatives=["February 4, 2014", "April 2, 2014"],
            ),
            entity(
                "patient-age",
                "patient",
                "age",
                "Age",
                "1 year old",
                0.62,
                "The patient is 1 yr old",
            ),
            entity(
                "clinical-weight",
                "clinical",
                "weight",
                "Weight",
                "19 lbs",
                0.68,
                "weighs 19 lbs",
                data_type="measurement",
                unit="lb",
                alternatives=["19 lb", "handwriting requires verification"],
            ),
            entity(
                "clinical-temperature",
                "clinical",
                "temperature",
                "Temperature",
                "97 °F",
                0.72,
                "TEMP 97",
                data_type="measurement",
                unit="°F",
            ),
            entity(
                "clinical-pulse",
                "clinical",
                "pulse",
                "Pulse",
                "130 bpm",
                0.82,
                "PULSE 130",
                data_type="measurement",
                unit="bpm",
            ),
            entity(
                "clinical-complaint",
                "clinical",
                "complaint",
                "Complaint",
                "parasitos, vitaminas",
                0.66,
                "COMPLAINT parasitos, vitaminas",
                alternatives=["parasites / vitamins"],
            ),
            entity(
                "history-pertinent",
                "history",
                "pertinent_history",
                "Pertinent history",
                "Not eating well; no diarrhea; no nausea/vomiting",
                0.58,
                "not eating well / no diarrhea / no N/V",
            ),
            entity(
                "assessment-impression",
                "assessment",
                "impression",
                "Impression",
                "Low weight",
                0.74,
                "IMPRESSION Low Weight",
            ),
            entity(
                "assessment-treatment",
                "assessment",
                "treatment",
                "Treatment",
                "Mebendazole; infant formula with iron; return in one month",
                0.42,
                "Mebendazole ... Infant formula with iron ... RTC month",
                alternatives=[
                    "Medication name is difficult to read",
                    "Return-to-clinic interval is uncertain",
                ],
            ),
        ]
        return ExtractionResult(
            entities=entities,
            missing_fields=_required_intake_fields(),
            overall_confidence=sum(item.confidence for item in entities) / len(entities),
            human_review_required=True,
            review_reasons=[
                "The prior-authorization request form is blank.",
                "Several handwritten values are ambiguous.",
                "Requested medication or service and member identifiers are missing.",
            ],
            provider=self.name,
        )

    @staticmethod
    def _clinical_note(document_id: str, file_name: str) -> OcrDocument:
        blocks = [
            OcrBlock(
                block_id="note-header",
                document_id=document_id,
                text="DATE 02/04/14; FEMALE; WT 19 lbs; TEMP 97; PULSE 130",
                confidence=0.69,
                source_type=SourceType.MIXED,
            ),
            OcrBlock(
                block_id="note-history",
                document_id=document_id,
                text="The patient is 1 yr old and weighs 19 lbs. not eating well; no diarrhea; no N/V.",
                confidence=0.58,
                source_type=SourceType.HANDWRITTEN,
                alternatives=["Some history wording is illegible."],
            ),
            OcrBlock(
                block_id="note-assessment",
                document_id=document_id,
                text="IMPRESSION: Low Weight",
                confidence=0.74,
                source_type=SourceType.HANDWRITTEN,
            ),
            OcrBlock(
                block_id="note-treatment",
                document_id=document_id,
                text="Mebendazole ... Infant formula with iron ... RTC month",
                confidence=0.42,
                source_type=SourceType.HANDWRITTEN,
                alternatives=["Medication and return interval require verification."],
            ),
        ]
        return OcrDocument(
            document_id=document_id,
            file_name=file_name,
            document_type="handwritten clinical note",
            raw_text="\n".join(block.text for block in blocks),
            overall_confidence=0.61,
            blocks=blocks,
            warnings=["Handwritten values were intentionally preserved as uncertain."],
        )

    @staticmethod
    def _blank_prior_auth_form(document_id: str, file_name: str) -> OcrDocument:
        raw_text = """PRIOR AUTHORIZATION FORM
Patient Information | Prescriber Information
Contact Person | Date Faxed
Patient Name | Prescriber Name and Specialty
Date of Birth | NPI
Member ID | Office Phone
Group Number | Office Fax
Requested Medication and Diagnosis
Medication | Strength | Frequency | Quantity
Diagnosis | Expected Length of Therapy
Medical History and Rationale for Prior Authorization Request
List all medications tried and failed, including dose, duration, and outcome
Medication | Date | Reason for Failure"""
        return OcrDocument(
            document_id=document_id,
            file_name=file_name,
            document_type="blank prior authorization form",
            raw_text=raw_text,
            overall_confidence=0.96,
            blocks=[
                OcrBlock(
                    block_id="form-printed-labels",
                    document_id=document_id,
                    text=raw_text,
                    confidence=0.96,
                    source_type=SourceType.PRINTED,
                )
            ],
            warnings=["No completed field values were detected on the blank form."],
        )


def _required_intake_fields() -> list[str]:
    return [
        "Patient name",
        "Date of birth",
        "Member ID",
        "Payer / line of business",
        "Prescriber name and NPI",
        "Requested medication or service",
        "Diagnosis",
        "Clinical rationale",
    ]


def _image_mime_type(path: Path) -> str:
    """Return an allowlisted MIME type independent of the host OS registry."""
    mime_types = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }
    try:
        return mime_types[path.suffix.lower()]
    except KeyError as exc:
        raise ValueError(f"Unsupported image extension: {path.suffix}") from exc


def _normalize_extraction(
    result: ExtractionResult, ocr: OcrBatchResult | None = None
) -> ExtractionResult:
    documents = {document.document_id: document for document in ocr.documents} if ocr else {}
    seen: dict[str, int] = {}
    for entity in result.entities:
        base = re.sub(r"[^a-z0-9]+", "-", entity.entity_id.lower()).strip("-")
        base = base or re.sub(
            r"[^a-z0-9]+", "-", entity.field_name.lower()
        ).strip("-")
        seen[base] = seen.get(base, 0) + 1
        entity.entity_id = base if seen[base] == 1 else f"{base}-{seen[base]}"
        document = documents.get(entity.document_id)
        if document:
            best = best_evidence_match(entity.evidence_text, document.blocks)
            if best is None:
                # The span does not appear in the transcript. Treat the entity as
                # untraceable rather than trusting a paraphrase.
                entity.confidence = min(entity.confidence, 0.5)
                _add_note(entity, "Evidence span was not found in the OCR transcript.")
            elif entity.confidence > best.confidence:
                entity.confidence = best.confidence
                _add_note(entity, "Confidence capped by supporting OCR legibility.")
        if entity.confidence < 0.85:
            entity.verification_status = VerificationStatus.NEEDS_REVIEW
    if result.entities:
        result.overall_confidence = sum(
            entity.confidence for entity in result.entities
        ) / len(result.entities)
    result.human_review_required = True
    return result


def _add_note(entity: ExtractedEntity, note: str) -> None:
    entity.notes = f"{entity.notes} {note}" if entity.notes else note


def evidence_tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.casefold())


def token_overlap(span: str, candidate: str) -> float:
    """Fraction of the evidence span's tokens present in a transcript line."""

    span_tokens = evidence_tokens(span)
    if not span_tokens:
        return 0.0
    candidate_tokens = set(evidence_tokens(candidate))
    matched = sum(1 for token in span_tokens if token in candidate_tokens)
    return matched / len(span_tokens)


def best_evidence_match(
    span: str, blocks: list[OcrBlock], threshold: float = 0.8
) -> OcrBlock | None:
    """Return the transcript line an evidence span was copied from, if any.

    Token overlap rather than substring: OCR line breaks and whitespace differ from
    what a model echoes back, but a genuine quote still shares its words. A
    paraphrase or an invented span does not clear the threshold.

    A form label and its value land on separate transcript lines, and a model
    quoting the pair returns both ("Primary surgery date:\\n2019-06-14"). Adjacent
    pairs are therefore candidates too; the match is attributed to the line that
    carries the value. Windows stop at two - a wider one would let a span assemble
    itself from scattered text, which is the thing this gate exists to catch.
    """

    best: OcrBlock | None = None
    best_score = threshold
    for index, block in enumerate(blocks):
        candidates = [(block.text, block)]
        if index + 1 < len(blocks):
            following = blocks[index + 1]
            candidates.append((f"{block.text} {following.text}", following))
        for text, attributed in candidates:
            score = token_overlap(span, text)
            if score >= best_score:
                best, best_score = attributed, score
    return best


def build_provider(settings: Settings | None = None) -> ClinicalExtractionProvider:
    settings = settings or get_settings()
    if settings.provider_mode == "claude":
        from app.providers_claude import ClaudeClinicalProvider

        return ClaudeClinicalProvider(settings)
    return FixtureClinicalProvider()
