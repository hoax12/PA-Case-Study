# Demo and presentation runbook

## Goal

Demonstrate that an agent can turn mixed-quality patient documents into
structured, source-linked, human-reviewable evidence while keeping the clinical
decision boundary explicit.

Target duration: 10–15 minutes. The recommended script below is approximately 12
minutes.

## Preflight checklist

Complete this before the presentation:

1. Activate the virtual environment and run the tests.
2. Confirm `.env` contains the intended provider and model settings.
3. Confirm `/api/config` reports `provider_mode: gemini` for a live demo.
4. Confirm `/api/knowledge/status` reports `ready: true`.
5. Open the UI and verify the provider badge.
6. Keep the two supplied images in an easy-to-select folder.
7. Run one complete packet in advance and keep its `?job=<id>` URL available.
8. Check the browser at the presentation resolution; the table should have no
   horizontal overflow and the Verify column should be visible.
9. Keep fixture mode as an explicitly labeled rehearsal fallback.
10. Never display the `.env` file or API key during screen sharing.
11. Avoid real patient data; use only the approved case-study samples.

For the knowledge status check, also confirm `runtime_ready: true`, the expected
policy/effective date, and a non-empty chunk count. A present PDF without a ready
embedding runtime is not sufficient for a reliable live demo.

Commands:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\build_guideline_index.py
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Recommended 12-minute narrative

### 1. Problem and safety boundary: 1 minute

“Prior-authorization packets arrive as heterogeneous images. The first problem
is not policy reasoning; it is converting those documents into trustworthy,
reviewable evidence, then retrieving the exact policy evidence relevant to the
packet. The prototype deliberately stops before a coverage decision.”

Point to:

- the three bounded tools;
- the human-in-the-loop label;
- the explicit confidence legend.

### 2. Architecture and orchestration: 2 minutes

Explain:

- FastAPI validates uploads and creates a durable review job;
- Google ADK enforces OCR before NER before guideline grounding;
- typed ADK state passes OCR, extraction, then grounding between tools;
- local embeddings and FAISS retrieve page-cited clauses from Policy 008;
- ADK session memory and the clinical review record use separate SQLite stores;
- the live event feed exposes tool calls without dumping full documents.

Senior-level rationale: deterministic application code owns safety invariants,
validation, and persistence. The LLM coordinates bounded tools but does not own
the domain record.

### 3. Live upload and tool trace: 3 minutes

1. Select `med2.webp`.
2. Reopen the chooser and add `Prior-Authorization-Form.jpg` to show additive
   selection.
3. Optionally remove and re-add a file to demonstrate upload control.
4. Start extraction.
5. Narrate Upload → OCR → Clinical NER → Guideline RAG → Human review.
6. Point out the live tool-call and tool-result events.

Do not overpromise latency. OCR processes documents individually and live model
capacity can vary.

### 4. Evidence and guideline review: 3 minutes

Show:

- source image tabs;
- OCR transcript and its confidence signal;
- the concise entity table;
- field value, confidence, evidence, and document source in one row;
- amber/red treatment for uncertain handwriting;
- missing PA fields and review reasons.

Correct one uncertain value and click Verify. Explain that the correction is
append-only and the original extraction remains available in persistence.

Then show the criteria matrix and retrieved passages. The supplied infant note
and blank PA form are intentionally insufficient for bariatric eligibility, so
the correct outcome is mostly/all `Unknown`, not a fabricated denial. Point to a
page citation and explain that the matrix is preliminary decision support.

Open “View retrieved policy passages” and connect one matrix row to its source
page and section. This demonstrates the full claim-to-source chain rather than
presenting RAG as a generic PDF chat experience.

### 5. Evaluation and tradeoffs: 2 minutes

State clearly:

- OCR: CER/WER, checkbox accuracy, blank-field false positives, ambiguity recall;
- NER: per-field precision/recall, value accuracy, evidence grounding;
- retrieval: recall@k, MRR/nDCG, page/section citation accuracy, stale-index tests;
- criteria: status accuracy, unsupported-conclusion rate, unknown-state recall;
- confidence: reliability and false-green rate;
- workflow: sequence compliance, completion rate, latency, recovery rate;
- human factors: correction rate and median review time.

Explain why entity confidence is capped by source legibility: downstream
certainty cannot exceed the evidence it depends on.

### 6. Boundary and next phase: 1 minute

The current build implements local guideline retrieval and a preliminary matrix:

1. Policy pages, effective date, source hash, and embedding version are retained.
2. FAISS retrieves exact chunks with page/section citations.
3. Criteria use three values: met, not met, unknown.
4. Every result stays behind human review; no autonomous denial.

The next production step is converting a payer-approved subset into versioned,
deterministic predicates and validating it with clinical/payer reviewers.

## Failure handling during the demo

### Gemini returns 503/high demand

- The UI should display the capacity/recovery event.
- The orchestrator attempts the same bounded tools directly.
- If the live provider remains unavailable, explain that 503 is provider
  capacity, not a quota 429 or invalid key.
- Switch to fixture mode only after explicitly telling the audience it is a
  deterministic rehearsal, not a live-model result.
- Alternatively open the precomputed live job URL and continue the review demo.

### Job is left running after a server restart

FastAPI background tasks are in-process. A process restart can leave a persisted
job marked running. Start a new intake; do not imply that the old task is still
executing. Durable queue recovery is production work.

### Reset button is not obvious

Once a job starts or a persisted job is restored, Reset intake appears in the
header and New intake appears next to job status. Both clear the browser intake
state and URL; neither deletes the persisted database record.

### Live extraction fails

Use the visible error state as a reliability discussion:

- provider failure remains an error;
- the UI does not manufacture fields;
- the uploaded record and event history remain inspectable;
- fixture mode is labeled rather than silently substituted.

## Likely evaluator questions

### Why use an agent for a fixed three-step flow?

The three-tool flow keeps orchestration constrained while exposing ADK state,
tools, observability, and a meaningful retrieval stage. Deterministic code still
enforces required ordering, citation validation, and safety gates.

### Why not use one multimodal prompt for everything?

Separating OCR from NER creates inspectable intermediate evidence, independent
evaluation targets, reusable transcripts, and a clear confidence dependency.
It also makes human disagreement traceable to transcription versus extraction.

### Is confidence a probability?

No. It is currently an uncalibrated review signal. The demo uses it to prioritize
verification, and production requires calibration on labeled data.

### Why two databases?

ADK memory and the domain audit record have different lifecycles and consumers.
The UI should not depend on private ADK session schema, and clinical corrections
should not disappear when a conversation changes.

### How do you handle prompt injection in documents?

Prompts label documents and retrieved policy text as untrusted data; the agent
has only three tools; the OCR
tool validates paths inside the active job; and NER sees the typed OCR envelope.
Production still requires adversarial evaluation and broader governance.

### Why no approval or denial?

Extraction confidence is not medical-necessity evidence. Policy reasoning needs
versioned guidelines, traceable clauses, explicit unknown states, and a separate
human outcome gate.

## Three-day delivery framing

### Day 1: implemented foundation

- typed OCR and extraction contracts;
- ADK agent and tools;
- SQLite persistence;
- upload, SSE, confidence, correction UI;
- deterministic fixture and contract tests.

### Day 2: implemented guideline phase

- parse and version the bariatric guideline;
- build citation-preserving retrieval;
- implement a reviewed subset of structured criteria;
- add unknown/missing-evidence handling.

### Day 3: evaluation and presentation

- golden cases and error slices;
- calibration analysis;
- end-to-end rehearsal and failure-path rehearsal;
- visual polish and final presentation.
