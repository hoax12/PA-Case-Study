# ClarityPA Intake Copilot

ClarityPA is a human-in-the-loop Google ADK prototype for prior-authorization
intake and preliminary policy grounding. It turns patient document images into
an OCR transcript, typed source-linked entities, and a cited bariatric-criteria
matrix that a clinician can verify.

The current build performs three bounded operations:

1. OCR uploaded patient document images.
2. Extract patient, provider, request, and clinical entities from that OCR.
3. Retrieve page-cited bariatric policy passages and build a conservative
   `met / not_met / unknown` review matrix.

It does **not** determine medical necessity, submit a request, or approve/deny
authorization. The matrix is decision support and always requires human review.

## Current capabilities

- Google ADK agent with exactly three function tools:
  `ocr_patient_documents`, `extract_patient_entities`, then
  `ground_bariatric_guidelines`.
- SQLite-backed ADK sessions and a separate SQLite clinical review record.
- Live Gemini vision OCR and schema-constrained NER through a configurable model;
  current default: `gemini-3.5-flash-lite`.
- Deterministic, visibly labeled fixture mode for offline rehearsal.
- FastAPI upload/job API and Server-Sent Events for live tool observability.
- Healthcare-oriented review UI with:
  - additive multi-file upload, removal, Clear, and Reset/New intake;
  - refresh-safe `?job=<id>` URLs;
  - source image tabs and OCR transcript;
  - concise editable evidence table;
  - green, amber, and red confidence bands;
  - source evidence, missing fields, and review reasons;
  - persisted clinician corrections.
- Cross-stage confidence gate: an entity cannot be more confident than its
  supporting OCR evidence.
- Explicit recovery path for ADK planner 503/capacity failures.
- Local sentence-transformer embeddings and FAISS retrieval over versioned
  operative pages of Bariatric Surgery Policy 008.
- Page/section citations, retrieval scores, and a preliminary criteria matrix;
  missing patient facts remain `unknown`.
- Automated tests for data contracts, uncertainty, confidence gating,
  PDF scope, FAISS retrieval, conservative grounding, persistence, and the
  complete fixture API workflow.

## Documentation

| Document | Use it for |
|---|---|
| [Architecture](docs/ARCHITECTURE.md) | Components, sequence, tools, memory, confidence, recovery |
| [Knowledge base](docs/KNOWLEDGE_BASE.md) | PDF scope, FAISS build, facet retrieval, citations, update governance |
| [API and data model](docs/API_AND_DATA_MODEL.md) | Endpoints, Pydantic contracts, SSE, SQLite, configuration |
| [Evaluation plan](docs/EVALUATION_PLAN.md) | OCR/NER metrics, calibration, test slices, acceptance criteria |
| [Demo runbook](docs/DEMO_RUNBOOK.md) | 10–15 minute presentation, preflight, failure handling, Q&A |
| [Operations](docs/OPERATIONS.md) | Installation, modes, tests, troubleshooting, data locations |
| [Decisions and limitations](docs/DECISIONS_AND_LIMITATIONS.md) | Senior-level rationale, tradeoffs, status, next-phase boundary |

## Architecture at a glance

```text
Browser upload
    │
    ▼
FastAPI validation ────────────────────► Review SQLite
    │                                    jobs / events / corrections
    ▼
Google ADK Runner ─────────────────────► ADK SQLite sessions
    │
    ├── ocr_patient_documents
    │       Gemini vision or labeled fixture
    │       saves temp:ocr_result
    │
    ├── extract_patient_entities
            reads temp:ocr_result
            emits typed evidence + confidence
    │
    └── ground_bariatric_guidelines
            reads temp:extraction_result
            searches local FAISS index + emits cited criteria
    │
    ▼
SSE event stream ──────────────────────► Reviewer UI
```

## RAG pipeline at a glance

```text
BariatricSurgery.pdf
        │ pypdf · operative pages 1–7
        ▼
Page/section-aware chunks ──► MiniLM embeddings ──► FAISS IndexFlatIP
                                                        │
Patient extraction ──► facet queries ──► FAISS candidates
                                                        │
                         lexical subsection rerank ◄────┘
                                      │
                                      ▼
                         page-cited policy passages
                                      │
                    Gemini matrix or conservative fallback
                                      │
                                      ▼
                 citation/evidence validation ──► reviewer UI
```

The supplied policy currently produces 17 chunks. Grounding retrieves up to
eight passages across pathway, BMI/age, SADI-S, TORe, preparation, and exclusion
facets. A `met` or `not_met` status is retained only when both literal patient
evidence and an allowed retrieved citation survive application validation.

## Quick start

Python 3.11 or newer is required.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Add a real key only to the private `.env` file:

```dotenv
GOOGLE_API_KEY=your-google-api-key
EXTRACTION_PROVIDER=gemini
```

Build the local guideline index once after installation:

```powershell
.\.venv\Scripts\python.exe scripts\build_guideline_index.py
```

Confirm the source, index, and local embedding runtime before a demo:

```powershell
curl.exe -sS http://127.0.0.1:8000/api/knowledge/status
```

Expected readiness fields are `"ready": true` and `"runtime_ready": true`.

Start the application:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). FastAPI's local API schema
is at [http://127.0.0.1:8000/api/docs](http://127.0.0.1:8000/api/docs).

Without a key, `EXTRACTION_PROVIDER=auto` selects the clearly labeled fixture
mode. Fixture mode recognizes only `med2.webp` and
`Prior-Authorization-Form.jpg` and is not a live-model evaluation.

The supplied note and blank form are intentionally not a complete bariatric
packet. Their expected grounding result is an all-`unknown` criteria matrix,
which demonstrates safe missing-evidence behavior rather than eligibility.

## Test

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --check app\static\app.js
```

## Safety boundary

- Uploaded text is treated as untrusted data, never model instructions.
- Tool paths are confined to the active job upload directory.
- OCR preserves uncertain text instead of repairing clinical facts.
- NER must cite an OCR span and source document.
- Guideline criteria may cite only retrieved page/section citations.
- Missing or ambiguous patient evidence maps to `unknown`, never silently false.
- Missing values stay missing; ambiguity remains visible.
- All extractions require human review in this phase.
- A confidence score is an uncalibrated review signal, not clinical correctness.
- Reset clears browser state but does not silently delete the persisted record.

## Repository map

```text
app/
  agent.py          ADK agent declaration
  tools.py          bounded OCR/NER/grounding tools and state handoff
  knowledge.py      PDF chunking, embeddings, FAISS, cited matrix validation
  providers.py      Gemini and fixture providers
  orchestrator.py   ADK runner, events, job lifecycle, capacity recovery
  models.py         typed OCR/extraction/correction contracts
  repository.py     SQLite domain persistence
  main.py           FastAPI application and upload validation
  static/            browser UI
docs/               engineering and presentation documentation
tests/              deterministic contract and workflow tests
knowledge/source/   versioned source guideline PDF
knowledge/index/    generated local FAISS index; ignored by Git
data/               runtime databases and uploads; ignored by Git
```

## Prototype warning

This repository is not production-ready for real PHI. It lacks authentication,
authorization, managed encrypted storage, a formal retention policy, durable job
execution, production audit controls, and a verified healthcare deployment
configuration. Use only approved case-study data in the local prototype.

Google ADK references used by the implementation:
[function tools](https://adk.dev/tools-custom/function-tools/),
[events](https://adk.dev/events/), and
[sessions](https://adk.dev/sessions/session/).
