"""Build or refresh the local bariatric guideline FAISS index."""

from __future__ import annotations

import argparse
import json

from app.config import get_settings
from app.knowledge import (
    BariatricGuidelineKnowledgeBase,
    SentenceTransformerEmbeddingModel,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the page-cited bariatric guideline FAISS index."
    )
    parser.add_argument(
        "--force", action="store_true", help="Rebuild even when the source is unchanged."
    )
    args = parser.parse_args()
    settings = get_settings()
    # Index building is the one explicit network-enabled step. Runtime lookup
    # loads the cached model with local_files_only=True for predictable demos.
    embedder = SentenceTransformerEmbeddingModel(
        settings.embedding_model, local_files_only=False
    )
    knowledge_base = BariatricGuidelineKnowledgeBase(settings, embedder=embedder)
    status = knowledge_base.build(force=args.force)
    print(json.dumps(status, indent=2))


if __name__ == "__main__":
    main()
