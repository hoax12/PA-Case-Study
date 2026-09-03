# Engineering decisions, status, and limitations

## Implementation status

| Capability | Status | Notes |
|---|---|---|
| Image upload and validation | Implemented | PNG/JPEG/WebP, max 8, size and pixel checks |
| OCR tool | Implemented | Gemini vision plus deterministic fixture |
| Clinical NER tool | Implemented | Pydantic structured output and evidence links |
| Google ADK orchestration | Implemented | Three bounded tools and SQLite sessions |
| Live tool observability | Implemented | Persisted events streamed through SSE |
| Confidence visualization | Implemented | High/review/low bands |
| Clinician correction | Implemented | Append-only correction overlay |
| Additive upload and file removal | Implemented | Repeated selection retains prior files |
| Reset/New intake | Implemented | Clears UI state; does not delete records |
| Refresh-safe job URL | Implemented | `?job=<id>` restores persisted job |
| Capacity recovery | Implemented | Explicit 503-only bounded recovery |
| Offline rehearsal | Implemented | Clearly labeled fixture provider |
| Patient PDF/multi-page ingestion | Not implemented | Patient input is image-only |
| Guideline PDF ingestion | Implemented | Operative pages 1–7, cited chunks, source hash |
| Guideline retrieval | Implemented | Local MiniLM embeddings and FAISS |
| Preliminary criteria matrix | Implemented | met/not_met/unknown with citation validation |
| Deterministic medical-necessity rules | Not implemented | Requires payer-reviewed predicates |
| Approval/denial | Intentionally excluded | Human/policy gate required |
| Authentication and RBAC | Not implemented | Required for production |

## Decision 1: split OCR and NER

Why:

- creates an inspectable intermediate transcript;
- permits separate OCR and extraction evaluation;
- makes source evidence reusable;
- allows confidence to propagate from legibility to entities;
- localizes failure analysis.

Tradeoff: two model stages increase latency and cost compared with one
multimodal prompt.

## Decision 2: use a bounded ADK agent

The agent has exactly three tools and a required order. This demonstrates tool
orchestration, memory, and observability without giving the model authority over
the clinical outcome.

Tradeoff: a deterministic workflow could call the functions directly with less
planner latency. The retrieval stage now shows where ADK adds coordination and
observability, while deterministic application checks remain necessary.

## Decision 3: use typed Pydantic contracts

OCR and extraction share explicit models across provider, tools, persistence,
tests, and UI. Structured NER output is validated before it becomes review data.

Tradeoff: schema evolution requires versioning and migrations in production.
The prototype stores JSON without an explicit schema-version field.

## Decision 4: require evidence for every entity

Each entity includes `evidence_text` and `document_id`. This makes a value
reviewable and supports future image overlays.

Tradeoff: evidence is currently a text match, not a geometric source span.
Bounding boxes are modeled but not populated or rendered.

## Decision 5: cap confidence by OCR evidence

Downstream NER confidence cannot exceed the confidence of the matching OCR block
or source document. This prevents a confident extraction model from visually
overriding poor handwriting.

Tradeoff: OCR confidence is heuristic and can cap a correct entity too low.
Calibration and learned/validated confidence are future work.

## Decision 6: force human review

Every extraction sets `human_review_required = true`; values below 0.85 are
`needs_review`. The UI supports correction but no automated decision.

Why: the supplied packet has insufficient bariatric evidence, and retrieved
guidelines are decision support rather than authority to adjudicate.

## Decision 7: separate ADK memory from domain persistence

ADK `DatabaseSessionService` owns agent runtime history. `ReviewRepository` owns
jobs, OCR, extraction, events, and corrections.

Why: the UI and audit record should not depend on a framework-private session
schema or conversational lifetime.

## Decision 8: stream persisted events with SSE

SSE matches the server-to-browser, one-way progress pattern and is simpler than a
WebSocket for the prototype. Persisting events means reconnects can continue
from `Last-Event-ID`.

Tradeoff: polling SQLite every 250 ms is acceptable locally but not the desired
production event architecture.

## Decision 9: provide a labeled fixture provider

The fixture makes UI work and presentation rehearsal deterministic during
network outages. It is selected explicitly or automatically only when no key is
configured and is visibly labeled.

Tradeoff: fixture outputs can be mistaken for evaluation results unless the
presenter follows the labeling rules.

## Decision 10: retain persisted jobs across UI reset

Reset is a browser workflow action, not data deletion. The job URL is removed and
the interface returns to upload, while SQLite retains the job and corrections.

Why: deletion has different audit and authorization semantics and should be an
explicit backend capability, not a side effect of a convenience control.

## Decision 11: use synchronous Gemini calls in worker threads

The provider calls the synchronous SDK through `asyncio.to_thread`. In this
environment, the comparable async vision transport showed repeated capacity
failures while the sync path completed during earlier verification.

Tradeoff: this is an operational workaround, not a universal recommendation.
Re-test after SDK/model upgrades and add explicit request timeouts.

## Decision 12: recover only qualifying capacity failures

When the ADK planner exhausts SDK retries with a 503/high-demand error, the
orchestrator records a recovery event and runs the same bounded tools. It can
resume from persisted OCR.

Tradeoff: if the underlying OCR/NER model shares the same capacity issue, the
recovery can still fail. Authentication or validation errors intentionally do
not trigger this path.

## Decision 13: local embeddings plus FAISS

The policy index uses `sentence-transformers/all-MiniLM-L6-v2` and normalized
FAISS inner-product search. This keeps retrieval fast and local during the demo,
avoids using Gemini quota for every embedding, and makes index rebuilding
explicit.

Tradeoff: a small general-purpose embedding model is not clinically calibrated.
Production selection should compare it with a managed/vector-database design on
a labeled policy-query set, under update, scale, access-control, and audit needs.

## Decision 14: page-cited, three-valued grounding

The index stores page/section metadata and the criteria matrix permits only
`met`, `not_met`, or `unknown`. Any unsupported patient evidence or citation is
downgraded to `unknown`; no decision label exists in the data contract.

Why: missing evidence must not be treated as negative evidence, and a reviewer
needs to trace every policy claim back to the source version.

## Current model configuration

The configurable default is `gemini-3.5-flash-lite` for both ADK planning and
extraction. It was selected after the configured account rejected 2.5 Flash-Lite
for generation and recommended 3.5 Flash-Lite. Live model capacity can still
return 503 across multiple model families; model switching is not a substitute
for retry, monitoring, and a presentation fallback.

## Security and privacy limitations

The prototype lacks:

- authentication, authorization, tenancy, and session ownership;
- encrypted managed storage and formal key management;
- consent, retention, deletion, and legal-hold workflows;
- production audit immutability;
- PHI-safe telemetry/redaction policy;
- deployment hardening, rate limiting, CSRF controls, and malware scanning;
- a verified Google Cloud healthcare/BAA deployment configuration.

Use only approved case-study data locally. Do not treat the application as ready
for real patient information.

## Reliability limitations

- Background work is tied to the FastAPI process.
- There is no durable queue, idempotency key, lease, cancellation, or resume after
  process death.
- Provider timeouts and retry budgets are mostly SDK defaults.
- Repeated jobs can duplicate model work.
- SQLite and local disk constrain concurrency and horizontal scaling.
- An interrupted job can remain marked running.
- The local embedding model must be downloaded before an offline demo.
- The index watches source hash/model identity but has no automated policy-update feed.

## Extraction limitations

- Image-only input; no PDF rasterization or multi-page document model.
- No orientation correction, deskew, denoise, or crop pipeline.
- No populated OCR bounding boxes or image overlays.
- Handwriting confidence is heuristic and uncalibrated.
- Blank-form and field semantics rely partly on prompting.
- Cross-document conflict resolution is not implemented.
- Date normalization remains conservative but is not fully formalized.

## UI limitations

- Reviewer identity is fixed as “Clinical reviewer.”
- No search, sort, filters, keyboard review queue, or bulk verification.
- Evidence is truncated in the compact table and expanded only through hover
  title/OCR transcript.
- Mobile hides the evidence column to retain concise editing.
- There is no explicit backend delete action.

## Implemented guideline boundary and next step

The guideline tool is intentionally not an unstructured “ask the PDF” prompt.
The implemented boundary:

1. Parse, segment, and version the bariatric guideline.
2. Retain section hierarchy, page, effective date, and exact clause text.
3. Retrieve criteria with citations.
4. Structure a preliminary matrix as met, not met, or unknown.
5. Validate citations and patient evidence; downgrade unsupported claims.
6. Map missing/ambiguous extraction to unknown, never silently false.
7. Produce a cited rationale for clinician review.
8. Keep denial and external submission behind an authorized human gate.

This preserves the current principle: models organize and retrieve evidence;
deterministic contracts and humans own consequential decisions.

The next phase is to translate a payer-approved subset into typed, testable
predicates, add policy-update governance and a labeled retrieval/criteria test
set, and validate workflow outcomes with authorized clinical reviewers.
