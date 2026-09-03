"""FastAPI application serving the review UI and streamed orchestration events."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader

from app.config import get_settings
from app.models import (
    ClauseSearchRequest,
    CorrectionRequest,
    ReviewerDecisionRequest,
)
from app.pipeline import PriorAuthPipeline
from app.repository import ReviewRepository
from app.tools import get_registry


settings = get_settings()
repository = ReviewRepository(settings.resolved_app_db_path)
repository.initialize()
pipeline = PriorAuthPipeline(repository, settings)
registry = get_registry()
background_tasks: set[asyncio.Task[None]] = set()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info(
        "Loaded %d policy file(s) from %s",
        len(registry.policies),
        settings.policies_dir_path,
    )
    yield


app = FastAPI(
    title="Prior Authorization Intake Copilot",
    version="0.1.0",
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(settings.static_dir / "index.html")


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "provider_mode": settings.provider_mode}


@app.get("/api/config")
async def public_config() -> dict[str, object]:
    return {
        "provider_mode": settings.provider_mode,
        "llm_model": settings.llm_model,
        "max_upload_mb": settings.max_upload_mb,
        "accepted_types": ["PNG", "JPEG", "WebP", "PDF"],
        "knowledge_base": registry.status(),
        "fixture_notice": (
            "Fixture mode is for offline rehearsal only. Set ANTHROPIC_API_KEY "
            "for live OCR, extraction, and criterion evidence."
            if settings.provider_mode == "fixture"
            else None
        ),
    }


@app.get("/api/knowledge/status")
async def knowledge_status() -> dict[str, object]:
    return registry.status()


@app.post("/api/knowledge/search")
async def search_clauses(request: ClauseSearchRequest) -> dict[str, object]:
    """Lexical clause lookup. Returns clauses, not pages."""

    clauses = registry.search(request.query, request.top_k or 5)
    return {
        "query": request.query,
        "clauses": [
            {
                "criterion_id": clause.id,
                "page": clause.page,
                "text": clause.text,
                "predicate_type": clause.predicate.type,
            }
            for clause in clauses
        ],
        "human_review_required": True,
    }


@app.post("/api/jobs", status_code=202)
async def create_job(
    files: Annotated[list[UploadFile], File(description="Patient document images")],
) -> dict[str, object]:
    if not files:
        raise HTTPException(status_code=400, detail="Upload at least one image")
    if len(files) > 8:
        raise HTTPException(status_code=400, detail="A maximum of 8 images is supported")

    job_id = str(uuid.uuid4())
    job_dir = settings.uploads_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    saved_files: list[dict[str, object]] = []
    saved_paths: list[Path] = []
    try:
        for index, upload in enumerate(files, start=1):
            safe_name = Path(upload.filename or f"document-{index}").name
            suffix = Path(safe_name).suffix.lower()
            if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".pdf"}:
                raise HTTPException(
                    status_code=415,
                    detail=f"Unsupported file type for {safe_name}",
                )
            destination = job_dir / f"{index:02d}-{safe_name}"
            size = await _save_upload(upload, destination)
            _verify_upload(destination)
            document_id = f"document-{index}"
            saved_paths.append(destination)
            saved_files.append(
                {
                    "document_id": document_id,
                    "original_name": safe_name,
                    "stored_name": destination.name,
                    "content_type": upload.content_type,
                    "size": size,
                    "url": f"/api/jobs/{job_id}/documents/{document_id}",
                }
            )
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    finally:
        for upload in files:
            await upload.close()

    repository.create_job(job_id, settings.provider_mode, saved_files)
    repository.append_event(
        job_id,
        "stage",
        "upload",
        {
            "status": "completed",
            "message": f"Received {len(saved_files)} document(s).",
        },
    )
    task = asyncio.create_task(pipeline.run_job(job_id, saved_paths))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return {
        "job_id": job_id,
        "status": "queued",
        "provider_mode": settings.provider_mode,
        "files": saved_files,
    }


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, object]:
    job = repository.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/api/jobs/{job_id}/documents/{document_id}")
async def get_document(job_id: str, document_id: str) -> FileResponse:
    job = repository.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    file_entry = next(
        (item for item in job["files"] if item["document_id"] == document_id),
        None,
    )
    if file_entry is None:
        raise HTTPException(status_code=404, detail="Document not found")
    path = (settings.uploads_dir / job_id / file_entry["stored_name"]).resolve()
    if not path.is_relative_to((settings.uploads_dir / job_id).resolve()):
        raise HTTPException(status_code=400, detail="Invalid document path")
    return FileResponse(path, filename=file_entry["original_name"])


@app.get("/api/jobs/{job_id}/events")
async def stream_events(job_id: str, request: Request) -> StreamingResponse:
    if repository.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        after_id = int(request.headers.get("last-event-id", "0"))
    except ValueError:
        after_id = 0

    async def generate():
        nonlocal after_id
        idle_ticks = 0
        while True:
            if await request.is_disconnected():
                return
            events = repository.list_events_after(job_id, after_id)
            for event in events:
                after_id = event["id"]
                yield (
                    f"id: {event['id']}\n"
                    f"event: workflow\n"
                    f"data: {json.dumps(event)}\n\n"
                )
            job = repository.get_job(job_id)
            if job and job["status"] in {"ready", "error"} and not events:
                return
            idle_ticks += 1
            if idle_ticks % 40 == 0:
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.25)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/jobs/{job_id}/corrections")
async def add_correction(
    job_id: str, correction: CorrectionRequest
) -> dict[str, object]:
    try:
        updated = repository.add_correction(job_id, correction)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    repository.append_event(
        job_id,
        "reviewer_action",
        "review",
        {
            "entity_id": correction.entity_id,
            "reviewer": correction.reviewer,
            "verified": correction.verified,
            "message": "Reviewer correction saved.",
        },
    )
    return updated


@app.post("/api/jobs/{job_id}/decision")
async def add_reviewer_decision(
    job_id: str, decision: ReviewerDecisionRequest
) -> dict[str, object]:
    """Record a reviewer's agreement, override, or final outcome."""

    try:
        updated = repository.add_reviewer_decision(job_id, decision)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    repository.append_event(
        job_id,
        "reviewer_action",
        "decision",
        {
            "criterion_id": decision.criterion_id,
            "reviewer_status": decision.reviewer_status,
            "final_outcome": decision.final_outcome,
            "reviewer": decision.reviewer,
            "message": "Reviewer decision recorded.",
        },
    )
    return updated


async def _save_upload(upload: UploadFile, destination: Path) -> int:
    max_bytes = settings.max_upload_mb * 1024 * 1024
    size = 0
    with destination.open("wb") as output:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"{upload.filename} exceeds {settings.max_upload_mb} MB",
                )
            output.write(chunk)
    if size == 0:
        raise HTTPException(status_code=400, detail=f"{upload.filename} is empty")
    return size


def _verify_upload(path: Path) -> None:
    if path.suffix.lower() == ".pdf":
        _verify_pdf(path)
    else:
        _verify_image(path)


def _verify_pdf(path: Path) -> None:
    try:
        reader = PdfReader(path)
        if not reader.pages:
            raise HTTPException(status_code=415, detail=f"Empty PDF: {path.name}")
        if len(reader.pages) > 40:
            raise HTTPException(
                status_code=413, detail=f"{path.name} exceeds 40 pages"
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=415, detail=f"Invalid PDF: {path.name}"
        ) from exc


def _verify_image(path: Path) -> None:
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            width, height = image.size
            if width * height > 50_000_000:
                raise HTTPException(status_code=413, detail="Image dimensions are too large")
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(status_code=415, detail=f"Invalid image: {path.name}") from exc
