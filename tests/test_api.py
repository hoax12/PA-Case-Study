"""End-to-end contract test over the HTTP surface, in fixture mode."""

import io
import time

from fastapi.testclient import TestClient
from PIL import Image

from app.main import app, settings


def png_bytes() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(output, format="PNG")
    return output.getvalue()


def run_fixture_job(client: TestClient) -> dict:
    response = client.post(
        "/api/jobs",
        files=[
            ("files", ("med2.webp", png_bytes(), "image/webp")),
            ("files", ("Prior-Authorization-Form.jpg", png_bytes(), "image/jpeg")),
        ],
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    for _ in range(60):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"ready", "error"}:
            return job
        time.sleep(0.05)
    raise AssertionError("The job never reached a terminal state")


def test_provided_sample_packet_is_referred_with_a_stated_reason(monkeypatch) -> None:
    """The supplied images are a pediatric note and a blank pharmacy form.

    They are not a bariatric request, so the only correct behaviour is to refer
    with 'no applicable guideline' rather than to score them against a policy
    that does not govern them.
    """

    monkeypatch.setattr(settings, "extraction_provider", "fixture")
    with TestClient(app) as client:
        job = run_fixture_job(client)

    assert job["status"] == "ready"
    assert job["ocr"]["documents"]
    assert job["extraction"]["entities"]

    adjudication = job["adjudication"]
    assert adjudication["outcome"] == "refer_to_human"
    assert adjudication["confidence"] == 0
    assert adjudication["policy_id"] is None
    assert adjudication["refer_reasons"]
    assert "no coverage determination" in adjudication["rationale"].casefold()


def test_reviewer_can_correct_a_value_and_record_a_decision(monkeypatch) -> None:
    monkeypatch.setattr(settings, "extraction_provider", "fixture")
    with TestClient(app) as client:
        job = run_fixture_job(client)
        job_id = job["id"]

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
        corrected = next(
            entity
            for entity in correction.json()["extraction"]["entities"]
            if entity["entity_id"] == "clinical-weight"
        )
        assert corrected["verification_status"] == "corrected"
        assert corrected["original_value"] == "19 lbs"

        # A denial can only enter the record through a person.
        decision = client.post(
            f"/api/jobs/{job_id}/decision",
            json={
                "final_outcome": "denied",
                "reason_code": "not-medically-necessary",
                "note": "Wrong policy; packet is unrelated to bariatric surgery.",
                "reviewer": "Dr. Reviewer",
            },
        )
        assert decision.status_code == 200
        recorded = decision.json()["reviewer_decisions"][-1]
        assert recorded["final_outcome"] == "denied"
        assert recorded["reviewer"] == "Dr. Reviewer"
        # The system's own adjudication is untouched by the human's conclusion.
        assert decision.json()["adjudication"]["outcome"] == "refer_to_human"


def test_reviewer_can_agree_and_override_one_criterion(monkeypatch) -> None:
    """Per-clause feedback is the signal the monitoring plan is built on.

    An override of an affirmed criterion is the false-affirmation proxy in
    production, where no ground truth exists, so the system status and the
    reviewer's status must both survive the round trip and stay distinguishable.
    """

    monkeypatch.setattr(settings, "extraction_provider", "fixture")
    with TestClient(app) as client:
        job_id = run_fixture_job(client)["id"]

        agreed = client.post(
            f"/api/jobs/{job_id}/decision",
            json={
                "criterion_id": "MGB-008.TORe.5",
                "system_status": "met",
                "reviewer_status": "met",
                "reviewer": "Dr. Reviewer",
            },
        )
        assert agreed.status_code == 200

        overridden = client.post(
            f"/api/jobs/{job_id}/decision",
            json={
                "criterion_id": "MGB-008.TORe.10",
                "system_status": "met",
                "reviewer_status": "not_met",
                "reviewer": "Dr. Reviewer",
            },
        )
        assert overridden.status_code == 200

        decisions = overridden.json()["reviewer_decisions"]
        by_criterion = {item["criterion_id"]: item for item in decisions}
        assert by_criterion["MGB-008.TORe.5"]["reviewer_status"] == "met"
        assert by_criterion["MGB-008.TORe.10"]["system_status"] == "met"
        assert by_criterion["MGB-008.TORe.10"]["reviewer_status"] == "not_met"
        # Clause feedback carries no final outcome; that is a separate act.
        assert by_criterion["MGB-008.TORe.10"]["final_outcome"] is None


def test_adjudication_payload_has_the_fields_the_ui_renders(monkeypatch) -> None:
    """The review panel is driven entirely by this payload.

    A renamed field would not fail any Python test; it would silently render an
    empty criteria matrix in the demo, which is the one thing that must not happen
    in front of an audience.
    """

    monkeypatch.setattr(settings, "extraction_provider", "fixture")
    with TestClient(app) as client:
        adjudication = run_fixture_job(client)["adjudication"]

    for field in (
        "outcome",
        "confidence",
        "threshold",
        "rationale",
        "criteria",
        "refer_reasons",
        "policy_id",
        "policy_effective_date",
        "pathway_id",
    ):
        assert field in adjudication, f"the UI reads adjudication.{field}"


def test_clause_search_returns_clauses_not_pages() -> None:
    with TestClient(app) as client:
        response = client.post(
            "/api/knowledge/search",
            json={"query": "primary surgery at least one year ago", "top_k": 3},
        )
        assert response.status_code == 200
        clauses = response.json()["clauses"]
        assert clauses
        assert all(item["criterion_id"].startswith("MGB-008.") for item in clauses)
        assert all(item["page"] for item in clauses)


def test_knowledge_status_reports_policy_provenance() -> None:
    with TestClient(app) as client:
        status = client.get("/api/knowledge/status").json()
        policy = status["policies"][0]
        assert policy["policy_id"] == "MGB-008"
        assert policy["criteria_count"] == 44
        assert policy["source_present"] is True
