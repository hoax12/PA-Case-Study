# API and data model

## HTTP endpoints

The FastAPI interactive schema is available locally at `/api/docs`.

| Method | Path | Purpose | Main responses |
|---|---|---|---|
| `GET` | `/api/health` | Process health and provider mode | `200` |
| `GET` | `/api/config` | Non-secret runtime configuration for the UI | `200` |
| `GET` | `/api/knowledge/status` | Source/index readiness and version metadata | `200` |
| `POST` | `/api/knowledge/search` | Retrieve cited passages for a text query | `200`, `422` |
| `POST` | `/api/jobs` | Validate uploads and enqueue extraction | `202`, `400`, `413`, `415` |
| `GET` | `/api/jobs/{job_id}` | Read the persisted job and reviewer overlays | `200`, `404` |
| `GET` | `/api/jobs/{job_id}/documents/{document_id}` | Preview a stored source image | `200`, `400`, `404` |
| `GET` | `/api/jobs/{job_id}/events` | Stream workflow events using SSE | `200`, `404` |
| `POST` | `/api/jobs/{job_id}/corrections` | Save a clinician correction | `200`, `404` |

## Create a job

`POST /api/jobs` accepts multipart form fields named `files`.

Constraints:

- One to eight files.
- `.png`, `.jpg`, `.jpeg`, or `.webp` extension.
- Maximum size is `MAX_UPLOAD_MB` per file; default 12 MB.
- The content must decode as an image.
- Image dimensions must not exceed 50 million pixels.

Example:

```powershell
curl.exe -X POST `
  -F "files=@C:\path\to\note.webp;type=image/webp" `
  -F "files=@C:\path\to\form.jpg;type=image/jpeg" `
  http://127.0.0.1:8000/api/jobs
```

The `202` response contains a job ID, initial status, provider mode, and safe
document metadata. Processing continues in the background.

## Job lifecycle

| Status | Meaning |
|---|---|
| `queued` | Upload is valid and the background task was created. |
| `running` | ADK/provider processing is active. |
| `ready` | OCR, extraction, and guideline grounding were persisted. |
| `error` | A terminal workflow exception was persisted. |

The job payload contains:

- file metadata and document preview URLs;
- provider mode and ADK session ID;
- complete typed OCR result when available;
- complete typed extraction result when available;
- complete grounded passages and criteria matrix when available;
- error text when terminal;
- append-only corrections overlaid onto returned entities;
- UTC creation and update timestamps.

## OCR contracts

### `OcrBlock`

| Field | Type | Meaning |
|---|---|---|
| `block_id` | string | Stable identifier inside a document |
| `document_id` | string | Source document reference |
| `page` | integer >= 1 | Page number; currently always 1 for images |
| `text` | string | Literal OCR span |
| `confidence` | number 0–1 | Uncalibrated legibility signal |
| `source_type` | enum | printed, handwritten, checkbox, mixed, unknown |
| `bbox` | optional normalized box | Reserved for source overlays |
| `alternatives` | string list | Visible uncertainty candidates or notes |

### `OcrDocument`

Contains document ID, stored filename, detected document type, raw transcript,
overall confidence, blocks, and warnings.

### `OcrBatchResult`

Contains all documents, average confidence, provider name, and batch warnings.

## Extraction contracts

### `ExtractedEntity`

| Field | Type | Meaning |
|---|---|---|
| `entity_id` | string | Normalized unique identifier |
| `section` | enum | patient, provider, request, clinical, history, assessment, other |
| `field_name` | string | Machine-oriented field name |
| `display_name` | string | Reviewer-facing label |
| `value` | optional string | Literal extracted value |
| `normalized_value` | optional string | Normalized representation when supported |
| `data_type` | enum | text, date, number, boolean, identifier, measurement |
| `unit` | optional string | Measurement unit |
| `confidence` | number 0–1 | Evidence/legibility support after capping |
| `evidence_text` | string | Short OCR source span |
| `document_id` | string | Source document reference |
| `bbox` | optional normalized box | Reserved for overlays |
| `alternatives` | string list | Ambiguity candidates |
| `verification_status` | enum | unverified, needs_review, verified, corrected |
| `notes` | optional string | Normalization and review context |

Entity IDs are lowercased, non-alphanumeric runs become hyphens, and duplicates
receive a numeric suffix.

### `ExtractionResult`

Contains entities, missing fields, overall confidence, review reasons, provider,
and the `human_review_required` gate. In the current phase the gate is always
true.

## Corrections

Correction request:

```json
{
  "entity_id": "clinical-weight",
  "corrected_value": "19 lb",
  "reviewer": "Clinical reviewer",
  "verified": true
}
```

Corrections are append-only. The original extraction JSON is retained. When a
job is read, the latest correction per entity is overlaid and the response adds:

- `original_value`;
- corrected `value` and `normalized_value`;
- `corrected` or `needs_review` verification status;
- `reviewed_by`.

This is sufficient for the prototype UI but is not a full clinical audit model.
Production records should include authenticated actor identity, reason, version,
before/after hashes, and immutable audit retention.

## Guideline grounding contracts

`RetrievedGuidelinePassage` carries chunk ID, policy metadata, page, section,
text, similarity score, and a reviewer-facing citation. `CriterionAssessment`
contains a criterion, `met / not_met / unknown` status, literal patient evidence,
retrieved citations, rationale, and assessment confidence.

`GuidelineGroundingResult` stores the query, passages, criteria, policy/effective
date, source-index version, embedding/assessment models, warnings, and the fixed
overall status `requires_human_review`. It is not an authorization result.

`POST /api/knowledge/search` accepts:

```json
{"query": "TORe revisional procedure requirements", "top_k": 5}
```

The response includes cited passages and `human_review_required: true`.

## Server-Sent Events

All SSE messages use event name `workflow`. The `data` object contains event ID,
job ID, type, stage, payload, and timestamp.

Implemented event types:

- `stage`: upload, intake, and review lifecycle updates;
- `tool_call`: sanitized tool name and arguments;
- `tool_result`: counts, confidence summaries, and provider;
- `agent_message`: final operational agent text;
- `mode`: explicit fixture-mode notice;
- `recovery`: ADK capacity recovery notice;
- `reviewer_action`: correction saved;
- `error`: terminal workflow failure.

The endpoint honors `Last-Event-ID`, polls persisted events in order, emits a
keep-alive comment periodically, and closes after a terminal job has no more
events. No API key or full document body is included in an event payload.

## SQLite schema

### Application review database

`jobs`

- job status, provider mode, ADK session ID;
- file metadata JSON;
- OCR, extraction, and grounding JSON;
- error and timestamps.

`job_events`

- ordered event ID;
- job foreign key;
- event type, stage, payload JSON, timestamp.

`corrections`

- job and entity identifiers;
- original and corrected values;
- reviewer, verified flag, timestamp.

SQLite uses WAL mode and foreign keys.

### ADK database

`DatabaseSessionService` owns the schema in `adk_sessions.db`. The application
does not depend on its internal table layout; it interacts through the ADK
service abstraction.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `GOOGLE_API_KEY` | unset | Enables live Gemini mode |
| `GOOGLE_GENAI_USE_ENTERPRISE` | `FALSE` | Selects configured Google transport mode |
| `ADK_MODEL` | `gemini-3.5-flash-lite` | ADK orchestration model |
| `EXTRACTION_MODEL` | `gemini-3.5-flash-lite` | OCR and NER model |
| `EXTRACTION_PROVIDER` | `auto` | auto, gemini, or fixture |
| `APP_DB_PATH` | `data/review.db` | Application persistence |
| `ADK_DB_PATH` | `data/adk_sessions.db` | ADK persistence |
| `MAX_UPLOAD_MB` | `12` | Per-file upload limit |
| `GUIDELINE_PDF_PATH` | `knowledge/source/BariatricSurgery.pdf` | Versioned source policy |
| `GUIDELINE_INDEX_DIR` | `knowledge/index` | Generated FAISS and chunk metadata |
| `EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | Local embedding model |
| `RAG_TOP_K` | `8` | Maximum grounded passage count |
| `RAG_MIN_SCORE` | `0.22` | Minimum cosine-style retrieval score |

`auto` chooses Gemini when a key is present and the labeled fixture otherwise.
The API key is bridged to the process environment for ADK but is never returned
by `/api/config`.
