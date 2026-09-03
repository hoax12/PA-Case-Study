# Architecture

## Purpose and scope

ClarityPA Intake Copilot converts patient-supplied document images into
reviewable structured evidence and grounds that evidence against a versioned
bariatric guideline. It retrieves policy passages and creates a preliminary
three-valued criteria matrix, but it does not make an approval or denial
decision.

The core design principle is bounded automation: OCR and entity extraction may
organize evidence, but ambiguous handwriting and missing data remain visible to
a clinician.

## System context

```text
Clinician browser
  │ upload images / review / correct
  ▼
FastAPI application
  ├─ validates and stores uploads
  ├─ creates and reads review jobs
  ├─ streams workflow events over SSE
  └─ persists corrections
  │
  ▼
Google ADK Runner
  ├─ ocr_patient_documents
  ├─ extract_patient_entities
  └─ ground_bariatric_guidelines
  │
  ├──────────────► Gemini provider or labeled fixture provider
  │
  ├──────────────► data/adk_sessions.db
  │                 ADK runtime/session state
  │
  └──────────────► data/review.db
                    jobs, OCR, extraction, grounding, events, corrections

Local knowledge layer
  ├─ knowledge/source/BariatricSurgery.pdf
  ├─ sentence-transformers/all-MiniLM-L6-v2
  └─ knowledge/index/*.faiss + cited chunk metadata
```

The two SQLite databases have deliberately different ownership:

- `adk_sessions.db` stores ADK runtime memory and invocation history.
- `review.db` stores the application-domain record that the UI must be able to
  recover independently of a model conversation.

## End-to-end sequence

1. The browser accepts up to eight PNG, JPEG, or WebP images. Reopening the file
   chooser adds files rather than replacing the existing selection.
2. `POST /api/jobs` creates a UUID job directory, streams each upload to disk,
   enforces the configured per-file size limit, verifies the image with Pillow,
   and rejects images larger than 50 million pixels.
3. The application creates a `queued` job and starts the orchestrator as an
   application background task.
4. The orchestrator marks the job `running`, creates an ADK SQLite session, and
   sends the exact job ID and validated upload paths to the ADK agent.
5. The ADK agent must call `ocr_patient_documents` exactly once with the full
   document list.
6. The OCR tool confines every path to the current job upload directory and
   invokes the configured provider. The complete OCR object is saved in
   `temp:ocr_result` and in the review database.
7. Only after OCR succeeds, the agent calls `extract_patient_entities`. The NER
   tool reads `temp:ocr_result`; it does not reread the source files.
8. The NER result is schema-validated, normalized, confidence-gated against its
   OCR evidence, and persisted.
9. The agent calls `ground_bariatric_guidelines`. It reads the extraction from
   state, retrieves relevant policy chunks from local FAISS, and emits a cited
   `met / not_met / unknown` criteria matrix. Unsupported conclusions are
   downgraded to `unknown`.
10. ADK events are reduced to safe operational summaries and appended to the
   application event log. The UI receives them through Server-Sent Events.
11. A complete job becomes `ready`. The clinician can edit a value and persist
    an append-only correction without destroying the original extraction.

## Component responsibilities

| Component | Responsibility | Explicitly does not do |
|---|---|---|
| `app/main.py` | HTTP API, upload validation, static UI, SSE | Clinical reasoning |
| `app/agent.py` | Declares the bounded ADK agent and required tool order | Arbitrary tool selection |
| `app/tools.py` | Path confinement, tool state handoff, persistence | Provider-specific prompting |
| `app/providers.py` | Gemini and fixture OCR/NER implementations | Job or HTTP lifecycle |
| `app/knowledge.py` | PDF segmentation, local embeddings, FAISS retrieval, citation validation | Authorization decisions |
| `app/orchestrator.py` | ADK session, event mirroring, job state, capacity recovery | Silent failure masking |
| `app/models.py` | Pydantic contracts and confidence bands | Database or UI behavior |
| `app/repository.py` | SQLite jobs, events, extractions, corrections | ADK session ownership |
| `app/static/*` | Upload, progress, evidence table, corrections, reset | Policy decisions |

## Agent and tool boundaries

The root agent has only three registered tools. Its instruction requires the
following order:

```text
ocr_patient_documents(job_id, complete_image_paths)
                    │
                    ▼
extract_patient_entities(job_id)
                    │
                    ▼
ground_bariatric_guidelines(job_id)
```

The second and third tools deliberately have no document-path argument. They
consume typed state from the preceding stage. This prevents accidental
bypassing of OCR, reduces prompt duplication, and makes the workflow invariant
testable.

All tools return compact operational summaries to the agent. Full transcripts,
entity payloads, retrieved passages, and the matrix remain in state and
application persistence rather than being echoed through every agent message.

## Knowledge management and grounding

Only operative policy pages 1–7 are indexed; the bibliography is deliberately
excluded. Chunk metadata retains source hash, policy number, effective date,
page, section, embedding model, and index version. Normalized embeddings are
stored in a FAISS inner-product index, making cosine-style retrieval local after
the embedding model is downloaded.

The retrieved text is treated as evidence, not executable instructions. Gemini
may structure the matrix in live mode, but application validation permits only
citations returned by retrieval and requires literal patient evidence for any
`met` or `not_met` status. Invalid support is downgraded to `unknown`. Fixture or
assessment-provider failure returns a deterministic all-unknown matrix.

## Provider abstraction

`ClinicalExtractionProvider` defines two async operations:

- `ocr_documents(image_paths) -> OcrBatchResult`
- `extract_entities(ocr) -> ExtractionResult`

The live provider uses the configured Gemini endpoint. OCR sends one image at a
time with a transcription-only prompt. NER sends the typed OCR envelope and
requests an `ExtractionResult` structured response. Both use temperature zero.

The fixture provider is deterministic and recognizes only the supplied sample
filenames. It exists for offline UI rehearsal and contract testing. Its values
and confidence scores must never be presented as measured model performance.

## Prompt-injection boundary

Every model prompt states that uploaded document content is untrusted data, not
instructions. OCR is instructed to preserve source wording and avoid inference.
NER is instructed to extract only supported facts and never infer sensitive or
clinical conclusions. File paths are constructed by the application and
validated again inside the OCR tool.

This is defense in depth, not a complete production security control. A real
deployment still requires model-input governance, authentication, authorization,
audit review, and adversarial testing.

## Confidence and evidence flow

Confidence represents extraction support and legibility, not clinical truth.

```text
OCR transcript line
  ├─ source type
  ├─ heuristic confidence
  └─ uncertainty markers / alternatives
          │
          ▼
NER entity with evidence_text + document_id
          │
          ▼
entity confidence capped by matching OCR block confidence
          │
          ▼
high >= 0.85 | review 0.65–0.849 | low < 0.65
```

When the model assigns an entity more confidence than its source evidence, the
normalizer lowers the entity score and records a note. Every entity below 0.85
becomes `needs_review`. `human_review_required` is forced to `true` for every
extraction in this phase.

OCR confidence is currently an uncalibrated heuristic derived from document
type, line context, and visible uncertainty markers. Production scores require a
labeled calibration dataset.

## Resilience and failure behavior

Job states are `queued`, `running`, `ready`, and `error`. Errors are persisted
and emitted to the live event feed.

The ADK SDK already retries retryable requests. If the ADK planner still returns
a Gemini `503` capacity error, the orchestrator records an explicit `recovery`
event and executes the same bounded OCR/NER/grounding functions directly. It reuses saved
OCR when available, avoiding unnecessary retranscription. This recovery is
limited to capacity-style 503 responses; authentication, validation, schema, and
other failures are not treated as successful.

The recovery path uses the same live provider and data contracts. It is not a
fixture fallback and does not conceal the service condition.

## UI state and recovery

The browser receives tool and workflow events through `/events`. Completed data
is always re-read from the job endpoint, so the event stream is observability,
not the source of truth.

After creation, the URL becomes `/?job=<job-id>`. Reloading that URL restores the
persisted documents, OCR, extraction, grounding matrix, and status. Reset/New intake closes the
event stream, clears transient UI state and selected files, removes the job query
parameter, restores the numbered progress steps, and unlocks upload controls. It
does not delete the persisted job from SQLite.

## Current deployment shape

This is a single-process local prototype:

- FastAPI background tasks are not durable across process restarts.
- Uploaded files and SQLite databases live under `data/`.
- There is no authentication, user isolation, work queue, object storage, or
  distributed event broker.

Production should move jobs to a durable queue, store encrypted objects and
domain records in managed services, enforce per-user authorization, and replace
the fixed demo reviewer identity.
