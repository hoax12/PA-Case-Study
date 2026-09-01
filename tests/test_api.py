import io
import time

from fastapi.testclient import TestClient
from PIL import Image

from app.main import app, knowledge_base, settings
from app.models import (
    CriterionAssessment,
    CriterionStatus,
    GuidelineGroundingResult,
    RetrievedGuidelinePassage,
)


def png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(output, format="PNG")
    return output.getvalue()


def test_fixture_api_runs_ocr_ner_and_correction(monkeypatch) -> None:
    # Keep this contract test deterministic even when a developer has added a
    # live Gemini key to the local .env file.
    monkeypatch.setattr(settings, "extraction_provider", "fixture")
    monkeypatch.setattr(knowledge_base, "prewarm", lambda: None)

    async def fake_grounding(job_id, state):
        result = GuidelineGroundingResult(
            query="bariatric requirements",
            passages=[
                RetrievedGuidelinePassage(
                    chunk_id="policy-008-p4-c1",
                    document_title="Mass General Brigham Health Plan - Bariatric Surgery",
                    policy_number="008",
                    page=4,
                    section="Commercial and Qualified Health Plans",
                    text="Sample grounded policy passage.",
                    score=0.8,
                    citation="Bariatric Surgery Policy 008, p. 4, Commercial and Qualified Health Plans",
                )
            ],
            criteria=[
                CriterionAssessment(
                    criterion_id="bmi",
                    criterion="BMI threshold",
                    status=CriterionStatus.UNKNOWN,
                    rationale="BMI was not found in the packet.",
                    confidence=0.2,
                    guideline_citations=[
                        "Bariatric Surgery Policy 008, p. 4, Commercial and Qualified Health Plans"
                    ],
                )
            ],
            summary="Human review is required; no authorization decision was made.",
            knowledge_base="Mass General Brigham Health Plan - Bariatric Surgery",
            policy_number="008",
            effective_date="July 1, 2026",
            index_version="test-index",
            embedding_model="test-embedding",
        )
        state["temp:guideline_grounding"] = result.model_dump(mode="json")
        from app.main import repository

        repository.save_grounding(job_id, result)
        return result

    monkeypatch.setattr("app.orchestrator.run_guideline_grounding", fake_grounding)
    with TestClient(app) as client:
        response = client.post(
            "/api/jobs",
            files=[
                ("files", ("med2.webp", png_bytes(), "image/webp")),
                (
                    "files",
                    ("Prior-Authorization-Form.jpg", png_bytes(), "image/jpeg"),
                ),
            ],
        )
        assert response.status_code == 202
        job_id = response.json()["job_id"]

        job = None
        for _ in range(50):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] in {"ready", "error"}:
                break
            time.sleep(0.05)

        assert job is not None
        assert job["status"] == "ready"
        assert job["ocr"]["documents"]
        assert job["extraction"]["entities"]
        assert job["grounding"]["criteria"][0]["status"] == "unknown"

        correction = client.post(
            f"/api/jobs/{job_id}/corrections",
            json={
                "entity_id": "clinical-weight",
                "corrected_value": "19 lb",
                "reviewer": "Demo clinician",
                "verified": True,
            },
        )
        assert correction.status_code == 200
        corrected_entity = next(
            entity
            for entity in correction.json()["extraction"]["entities"]
            if entity["entity_id"] == "clinical-weight"
        )
        assert corrected_entity["verification_status"] == "corrected"
