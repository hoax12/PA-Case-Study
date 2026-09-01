from pathlib import Path
import re

import numpy as np

from app.config import PROJECT_ROOT, Settings
from app.knowledge import (
    BariatricGuidelineKnowledgeBase,
    conservative_criteria_matrix,
    extract_guideline_chunks,
)
from app.models import ExtractedEntity, ExtractionResult


class KeywordEmbedder:
    name = "test-keyword-embedding"

    def encode(self, texts: list[str]) -> np.ndarray:
        vectors = []
        for text in texts:
            lowered = text.casefold()
            vectors.append(
                [
                    1.0 if re.search(r"\btore\b", lowered) else 0.0,
                    1.0 if "sadi-s" in lowered else 0.0,
                    1.0 if "adolescent" in lowered else 0.0,
                    1.0 if "exclusion" in lowered else 0.0,
                    0.1,
                ]
            )
        return np.asarray(vectors, dtype="float32")


def test_pdf_chunking_uses_only_operational_pages() -> None:
    pdf_path = PROJECT_ROOT / "knowledge" / "source" / "BariatricSurgery.pdf"
    chunks = extract_guideline_chunks(pdf_path)

    assert chunks
    assert max(chunk["page"] for chunk in chunks) <= 7
    assert any("TORe" in chunk["text"] for chunk in chunks)
    assert not any(chunk["section"] == "References" for chunk in chunks)


def test_faiss_search_returns_page_cited_policy_passage(tmp_path: Path) -> None:
    settings = Settings(
        GUIDELINE_PDF_PATH=PROJECT_ROOT
        / "knowledge"
        / "source"
        / "BariatricSurgery.pdf",
        GUIDELINE_INDEX_DIR=tmp_path / "index",
        EMBEDDING_MODEL="test-keyword-embedding",
        EXTRACTION_PROVIDER="fixture",
        APP_DB_PATH=tmp_path / "review.db",
        ADK_DB_PATH=tmp_path / "adk.db",
    )
    knowledge_base = BariatricGuidelineKnowledgeBase(
        settings=settings, embedder=KeywordEmbedder()
    )
    status = knowledge_base.build(force=True)
    passages = knowledge_base.search("TORe endoscopic revision", top_k=3)

    assert status["ready"] is True
    assert passages
    assert "TORe" in passages[0].text
    assert passages[0].citation.startswith("Bariatric Surgery Policy 008, p.")


def test_conservative_matrix_does_not_infer_missing_eligibility() -> None:
    extraction = ExtractionResult(
        entities=[
            ExtractedEntity(
                entity_id="weight",
                section="clinical",
                field_name="weight",
                display_name="Weight",
                value="19 lb",
                confidence=0.7,
                evidence_text="weight 19 lb",
                document_id="document-1",
            )
        ],
        overall_confidence=0.7,
        provider="fixture",
    )

    criteria, summary = conservative_criteria_matrix(extraction, [])

    assert criteria
    assert {criterion.status.value for criterion in criteria} == {"unknown"}
    assert "no approval or denial" in summary.lower()
