"""Runs the ADK event loop and mirrors safe event summaries into the UI feed."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from app.agent import root_agent
from app.config import Settings, get_settings
from app.providers import FixtureClinicalProvider, GeminiClinicalProvider
from app.repository import ReviewRepository
from app.tools import run_guideline_grounding, run_ner, run_ocr


APP_NAME = "prior_auth_intake"
USER_ID = "clinical-reviewer-demo"


class PriorAuthOrchestrator:
    def __init__(
        self,
        repository: ReviewRepository,
        settings: Settings | None = None,
    ):
        self.repository = repository
        self.settings = settings or get_settings()

    async def run_job(self, job_id: str, image_paths: list[Path]) -> None:
        self.repository.update_job(job_id, status="running", error=None)
        self.repository.append_event(
            job_id,
            "stage",
            "intake",
            {
                "status": "completed",
                "message": f"Validated {len(image_paths)} uploaded document(s).",
            },
        )
        try:
            if self.settings.provider_mode == "gemini":
                await self._run_adk(job_id, image_paths)
            else:
                await self._run_fixture(job_id, image_paths)
            job = self.repository.get_job(job_id)
            if (
                not job
                or not job.get("ocr")
                or not job.get("extraction")
                or not job.get("grounding")
            ):
                raise RuntimeError(
                    "The workflow finished without OCR, extraction, and grounding outputs"
                )
            self.repository.update_job(job_id, status="ready")
            self.repository.append_event(
                job_id,
                "stage",
                "review",
                {
                    "status": "completed",
                    "message": (
                        "Extraction and guideline grounding are ready for clinician "
                        "verification."
                    ),
                },
            )
        except Exception as exc:
            self.repository.update_job(job_id, status="error", error=str(exc))
            self.repository.append_event(
                job_id,
                "error",
                "workflow",
                {
                    "status": "error",
                    "message": str(exc),
                    "action": "Check provider configuration and retry the intake.",
                },
            )

    async def _run_adk(self, job_id: str, image_paths: list[Path]) -> None:
        session_service = self._session_service()
        await session_service.create_session(
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=job_id,
            state={"job_id": job_id, "workflow": "ocr_then_ner_then_grounding"},
        )
        self.repository.set_session_id(job_id, job_id)
        runner = Runner(
            agent=root_agent,
            app_name=APP_NAME,
            session_service=session_service,
        )
        path_list = [str(path.resolve()) for path in image_paths]
        new_message = types.Content(
            role="user",
            parts=[
                types.Part.from_text(
                    text=(
                        f"Process intake job {job_id}. Use this exact complete image_paths "
                        f"list: {path_list}. Run OCR first, NER second, and guideline "
                        "grounding third."
                    )
                )
            ],
        )
        try:
            async for event in runner.run_async(
                user_id=USER_ID,
                session_id=job_id,
                new_message=new_message,
            ):
                self._record_adk_event(job_id, event)
        except Exception as exc:
            if not self._is_transient_model_capacity_error(exc):
                raise
            await self._run_capacity_recovery(job_id, image_paths)

    async def _run_capacity_recovery(
        self, job_id: str, image_paths: list[Path]
    ) -> None:
        """Finish bounded tools when the ADK planner endpoint is temporarily busy.

        This path is deliberately limited to Gemini 503/capacity errors. It does
        not mask authentication, validation, or extraction failures. The same
        live Gemini OCR/NER providers and persistence contracts remain in use,
        and the recovery is made explicit in the observable event feed.
        """

        provider = GeminiClinicalProvider(self.settings)
        job = self.repository.get_job(job_id) or {}
        state: dict[str, Any] = {}
        if job.get("ocr"):
            state["temp:ocr_result"] = job["ocr"]
        if job.get("extraction"):
            state["temp:extraction_result"] = job["extraction"]
        if job.get("grounding"):
            state["temp:guideline_grounding"] = job["grounding"]

        self.repository.append_event(
            job_id,
            "recovery",
            "workflow",
            {
                "status": "notice",
                "message": (
                    "ADK planner capacity was temporarily unavailable. "
                    "Continuing the same bounded live tools without re-uploading."
                ),
            },
        )

        if not state.get("temp:ocr_result"):
            self.repository.append_event(
                job_id,
                "tool_call",
                "ocr",
                {
                    "tool": "ocr_patient_documents",
                    "arguments": {
                        "job_id": job_id,
                        "files": [path.name for path in image_paths],
                    },
                    "recovery": "adk_capacity",
                },
            )
            ocr = await run_ocr(
                job_id,
                [str(path) for path in image_paths],
                state,
                provider=provider,
            )
            self.repository.append_event(
                job_id,
                "tool_result",
                "ocr",
                {
                    "tool": "ocr_patient_documents",
                    "status": "success",
                    "document_count": len(ocr.documents),
                    "average_confidence": ocr.average_confidence,
                    "provider": ocr.provider,
                    "recovery": "adk_capacity",
                },
            )

        if not state.get("temp:extraction_result"):
            self.repository.append_event(
                job_id,
                "tool_call",
                "ner",
                {
                    "tool": "extract_patient_entities",
                    "arguments": {"job_id": job_id, "source": "persisted_ocr"},
                    "recovery": "adk_capacity",
                },
            )
            extraction = await run_ner(job_id, state, provider=provider)
            self.repository.append_event(
                job_id,
                "tool_result",
                "ner",
                {
                    "tool": "extract_patient_entities",
                    "status": "success",
                    "entity_count": len(extraction.entities),
                    "overall_confidence": extraction.overall_confidence,
                    "needs_review_count": sum(
                        1 for entity in extraction.entities if entity.confidence < 0.85
                    ),
                    "provider": extraction.provider,
                    "recovery": "adk_capacity",
                },
            )

        if not state.get("temp:guideline_grounding"):
            self.repository.append_event(
                job_id,
                "tool_call",
                "grounding",
                {
                    "tool": "ground_bariatric_guidelines",
                    "arguments": {
                        "job_id": job_id,
                        "source": "persisted_extraction",
                    },
                    "recovery": "adk_capacity",
                },
            )
            grounding = await run_guideline_grounding(job_id, state)
            self.repository.append_event(
                job_id,
                "tool_result",
                "grounding",
                {
                    "tool": "ground_bariatric_guidelines",
                    "status": "success",
                    "passage_count": len(grounding.passages),
                    "criteria_count": len(grounding.criteria),
                    "unknown_count": sum(
                        1
                        for criterion in grounding.criteria
                        if criterion.status == "unknown"
                    ),
                    "index_version": grounding.index_version,
                    "recovery": "adk_capacity",
                },
            )

    @staticmethod
    def _is_transient_model_capacity_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return "503" in message and any(
            marker in message
            for marker in ("unavailable", "high demand", "capacity", "overloaded")
        )

    async def _run_fixture(self, job_id: str, image_paths: list[Path]) -> None:
        provider = FixtureClinicalProvider()
        state: dict[str, Any] = {}
        session_service = self._session_service()
        await session_service.create_session(
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=job_id,
            state={
                "job_id": job_id,
                "workflow": "ocr_then_ner_then_grounding",
                "mode": "fixture",
            },
        )
        self.repository.set_session_id(job_id, job_id)
        self.repository.append_event(
            job_id,
            "mode",
            "workflow",
            {
                "status": "notice",
                "message": (
                    "Offline fixture mode is active. Add GOOGLE_API_KEY and set "
                    "EXTRACTION_PROVIDER=gemini for live model calls."
                ),
            },
        )
        self.repository.append_event(
            job_id,
            "tool_call",
            "ocr",
            {
                "tool": "ocr_patient_documents",
                "arguments": {
                    "job_id": job_id,
                    "files": [path.name for path in image_paths],
                },
            },
        )
        await asyncio.sleep(0.35)
        ocr = await run_ocr(
            job_id, [str(path) for path in image_paths], state, provider=provider
        )
        self.repository.append_event(
            job_id,
            "tool_result",
            "ocr",
            {
                "tool": "ocr_patient_documents",
                "status": "success",
                "document_count": len(ocr.documents),
                "average_confidence": ocr.average_confidence,
                "provider": ocr.provider,
            },
        )
        await asyncio.sleep(0.25)
        self.repository.append_event(
            job_id,
            "tool_call",
            "ner",
            {
                "tool": "extract_patient_entities",
                "arguments": {"job_id": job_id, "source": "temp:ocr_result"},
            },
        )
        await asyncio.sleep(0.35)
        extraction = await run_ner(job_id, state, provider=provider)
        self.repository.append_event(
            job_id,
            "tool_result",
            "ner",
            {
                "tool": "extract_patient_entities",
                "status": "success",
                "entity_count": len(extraction.entities),
                "overall_confidence": extraction.overall_confidence,
                "needs_review_count": sum(
                    1 for entity in extraction.entities if entity.confidence < 0.85
                ),
                "provider": extraction.provider,
            },
        )
        await asyncio.sleep(0.25)
        self.repository.append_event(
            job_id,
            "tool_call",
            "grounding",
            {
                "tool": "ground_bariatric_guidelines",
                "arguments": {
                    "job_id": job_id,
                    "source": "temp:extraction_result",
                },
            },
        )
        grounding = await run_guideline_grounding(job_id, state)
        self.repository.append_event(
            job_id,
            "tool_result",
            "grounding",
            {
                "tool": "ground_bariatric_guidelines",
                "status": "success",
                "passage_count": len(grounding.passages),
                "criteria_count": len(grounding.criteria),
                "unknown_count": sum(
                    1
                    for criterion in grounding.criteria
                    if criterion.status == "unknown"
                ),
                "index_version": grounding.index_version,
            },
        )

    def _session_service(self) -> DatabaseSessionService:
        database_url = (
            "sqlite+aiosqlite:///"
            + self.settings.resolved_adk_db_path.resolve().as_posix()
        )
        return DatabaseSessionService(db_url=database_url)

    def _record_adk_event(self, job_id: str, event: Any) -> None:
        calls = event.get_function_calls()
        if calls:
            for call in calls:
                self.repository.append_event(
                    job_id,
                    "tool_call",
                    self._tool_stage(call.name),
                    {
                        "tool": call.name,
                        "arguments": self._sanitize_arguments(call.args),
                        "invocation_id": event.invocation_id,
                    },
                )
            return

        responses = event.get_function_responses()
        if responses:
            for response in responses:
                payload = dict(response.response or {})
                payload["tool"] = response.name
                payload["invocation_id"] = event.invocation_id
                self.repository.append_event(
                    job_id,
                    "tool_result",
                    self._tool_stage(response.name),
                    payload,
                )
            return

        if event.is_final_response() and event.content and event.content.parts:
            text = "".join(
                part.text for part in event.content.parts if getattr(part, "text", None)
            )
            if text:
                self.repository.append_event(
                    job_id,
                    "agent_message",
                    "workflow",
                    {
                        "message": text,
                        "invocation_id": event.invocation_id,
                    },
                )

    @staticmethod
    def _sanitize_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
        sanitized = dict(arguments or {})
        if "image_paths" in sanitized:
            sanitized["files"] = [
                Path(path).name for path in sanitized.pop("image_paths")
            ]
        return sanitized

    @staticmethod
    def _tool_stage(tool_name: str) -> str:
        if tool_name == "ocr_patient_documents":
            return "ocr"
        if tool_name == "extract_patient_entities":
            return "ner"
        if tool_name == "ground_bariatric_guidelines":
            return "grounding"
        return "workflow"
