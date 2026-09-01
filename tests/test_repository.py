from pathlib import Path

from app.models import (
    CorrectionRequest,
    ExtractedEntity,
    ExtractionResult,
    VerificationStatus,
)
from app.repository import ReviewRepository


def test_reviewer_correction_overlays_extracted_value(tmp_path: Path) -> None:
    repository = ReviewRepository(tmp_path / "review.db")
    repository.initialize()
    repository.create_job("job-1", "fixture", [])
    repository.save_extraction(
        "job-1",
        ExtractionResult(
            entities=[
                ExtractedEntity(
                    entity_id="weight",
                    section="clinical",
                    field_name="weight",
                    display_name="Weight",
                    value="19 lbs",
                    normalized_value="19 lbs",
                    data_type="measurement",
                    unit="lb",
                    confidence=0.68,
                    evidence_text="weighs 19 lbs",
                    document_id="document-1",
                    verification_status=VerificationStatus.NEEDS_REVIEW,
                )
            ],
            overall_confidence=0.68,
            provider="fixture",
        ),
    )

    updated = repository.add_correction(
        "job-1",
        CorrectionRequest(
            entity_id="weight",
            corrected_value="19 lb",
            reviewer="Dr. Reviewer",
        ),
    )

    entity = updated["extraction"]["entities"][0]
    assert entity["value"] == "19 lb"
    assert entity["original_value"] == "19 lbs"
    assert entity["verification_status"] == "corrected"
