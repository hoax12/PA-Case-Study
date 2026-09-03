from pathlib import Path

import pytest

from app.models import ExtractedEntity, ExtractionResult, OcrBatchResult, OcrBlock, OcrDocument
from app.providers import (
    FixtureClinicalProvider,
    _image_mime_type,
    _normalize_extraction,
    best_evidence_match,
    token_overlap,
)
from app.providers_claude import OcrLine, OcrTranscript, _document_from_lines


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


def test_ocr_confidence_comes_from_reported_legibility() -> None:
    document = _document_from_lines(
        "document-1",
        "note.webp",
        OcrTranscript(
            document_type="clinical note",
            lines=[
                OcrLine(page=1, text="DATE 07/04/14", legibility="clear"),
                OcrLine(
                    page=1,
                    text="TREATMENT Albendazole",
                    legibility="partial",
                    alternatives=["Mebendazole"],
                ),
                OcrLine(page=1, text="RTC ????", legibility="illegible"),
            ],
        ),
    )
    clear, partial, illegible = document.blocks
    assert clear.confidence > partial.confidence > illegible.confidence
    assert partial.alternatives == ["Mebendazole"]
    assert document.warnings


def test_evidence_match_accepts_quotes_and_rejects_inventions() -> None:
    blocks = [
        OcrBlock(
            block_id="line-1",
            document_id="document-1",
            text="Weight 214 lbs, height 68 in, BMI 32.5",
            confidence=0.95,
        )
    ]
    # A verbatim quote survives differing punctuation and line wrapping.
    assert best_evidence_match("BMI 32.5", blocks) is not None
    # An invented span does not.
    assert best_evidence_match("BMI 41.0 recorded 2026-08-20", blocks) is None
    assert token_overlap("BMI 32.5", blocks[0].text) == 1.0


def test_evidence_match_spans_a_label_and_its_value() -> None:
    """A form label and its value are two transcript lines but one quote.

    The evaluation caught this: "Primary surgery date:\\n2019-06-14" matched
    neither line alone, so a correctly cited date was rejected as untraceable and
    the criterion degraded to UNKNOWN. The window stops at two lines - three would
    let a span assemble itself from scattered text.
    """

    blocks = [
        OcrBlock(
            block_id=f"line-{index}",
            document_id="document-1",
            text=text,
            confidence=0.95,
        )
        for index, text in enumerate(
            ["Primary surgery date:", "2019-06-14", "Surgeon: Reyes", "Facility: MWS"],
            start=1,
        )
    ]
    match = best_evidence_match("Primary surgery date:\n2019-06-14", blocks)
    assert match is not None
    # Attribution goes to the line carrying the value, not the label.
    assert match.text == "2019-06-14"
    # Three scattered lines still do not assemble into a quote.
    assert best_evidence_match("Primary surgery date: Reyes MWS", blocks) is None


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
        provider="claude",
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
        provider="claude",
    )
    normalized = _normalize_extraction(extraction, ocr)
    assert normalized.entities[0].confidence == 0.55
    assert normalized.entities[0].verification_status == "needs_review"


def test_untraceable_evidence_is_penalized() -> None:
    ocr = OcrBatchResult(
        documents=[
            OcrDocument(
                document_id="document-1",
                file_name="note.webp",
                document_type="clinical note",
                raw_text="WT 19 lbs",
                overall_confidence=0.95,
                blocks=[
                    OcrBlock(
                        block_id="line-1",
                        document_id="document-1",
                        text="WT 19 lbs",
                        confidence=0.95,
                    )
                ],
            )
        ],
        average_confidence=0.95,
        provider="claude",
    )
    extraction = ExtractionResult(
        entities=[
            ExtractedEntity(
                entity_id="bmi",
                section="clinical",
                field_name="bmi",
                display_name="BMI",
                value="41",
                confidence=0.99,
                evidence_text="BMI was calculated as 41 on admission",
                document_id="document-1",
            )
        ],
        overall_confidence=0.99,
        provider="claude",
    )
    normalized = _normalize_extraction(extraction, ocr)
    assert normalized.entities[0].confidence <= 0.5
    assert "not found" in (normalized.entities[0].notes or "").lower()
