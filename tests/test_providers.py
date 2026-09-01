from pathlib import Path

import pytest

from app.models import ExtractedEntity, ExtractionResult, OcrBatchResult, OcrBlock, OcrDocument
from app.providers import (
    FixtureClinicalProvider,
    _document_from_transcript,
    _image_mime_type,
    _normalize_extraction,
)


@pytest.mark.asyncio
async def test_fixture_provider_preserves_uncertain_handwriting(tmp_path: Path) -> None:
    note = tmp_path / "med2.webp"
    form = tmp_path / "Prior-Authorization-Form.jpg"
    note.touch()
    form.touch()

    provider = FixtureClinicalProvider()
    ocr = await provider.ocr_documents([note, form])
    extraction = await provider.extract_entities(ocr)

    treatment = next(
        entity for entity in extraction.entities if entity.field_name == "treatment"
    )
    assert treatment.confidence < 0.5
    assert treatment.alternatives
    assert extraction.human_review_required is True
    assert "Member ID" in extraction.missing_fields
    assert ocr.provider == "fixture"


def test_image_mime_type_is_not_host_registry_dependent(tmp_path: Path) -> None:
    assert _image_mime_type(tmp_path / "handwritten-note.webp") == "image/webp"
    assert _image_mime_type(tmp_path / "form.jpg") == "image/jpeg"


def test_transcript_envelope_flags_visual_uncertainty() -> None:
    document = _document_from_transcript(
        "document-1", "note.webp", "DATE 07/04/14\nTREATMENT Albendazole [?]"
    )
    assert document.blocks[1].confidence < document.blocks[0].confidence
    assert document.warnings


def test_ner_confidence_is_capped_by_supporting_ocr() -> None:
    ocr = OcrBatchResult(
        documents=[
            OcrDocument(
                document_id="document-1",
                file_name="note.webp",
                document_type="clinical note",
                raw_text="WT 19 lbs",
                overall_confidence=0.6,
                blocks=[
                    OcrBlock(
                        block_id="line-1",
                        document_id="document-1",
                        text="WT 19 lbs",
                        confidence=0.55,
                    )
                ],
            )
        ],
        average_confidence=0.6,
        provider="gemini",
    )
    extraction = ExtractionResult(
        entities=[
            ExtractedEntity(
                entity_id="weight",
                section="clinical",
                field_name="weight",
                display_name="Weight",
                value="19",
                confidence=0.98,
                evidence_text="19 lbs",
                document_id="document-1",
            )
        ],
        overall_confidence=0.98,
        provider="gemini",
    )
    normalized = _normalize_extraction(extraction, ocr)
    assert normalized.entities[0].confidence == 0.55
    assert normalized.entities[0].verification_status == "needs_review"
