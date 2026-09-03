# Evaluation plan

## Evaluation objective

The system should reduce manual intake effort without hiding uncertainty or
creating unsupported clinical facts. Evaluation must therefore cover more than
whether a model produced plausible text. It must measure transcription,
field/value extraction, evidence grounding, confidence calibration, workflow
correctness, and the clinician-review experience.

Fixture outputs and handcrafted confidence values are demo artifacts. They are
not model evaluation results.

## Test corpus design

Create a de-identified, access-controlled corpus with document-level provenance
and explicit permission for model evaluation. Split at patient or packet level,
not page level, to prevent near-duplicate leakage.

Recommended slices:

- printed forms: blank, partially completed, fully completed;
- handwriting: clear, cursive, overlapping labels, abbreviations, mixed ink;
- scan quality: clean, blur, skew, shadow, compression, low contrast;
- language: English, Spanish labels, mixed-language notes;
- document structure: clinical notes, PA forms, medication history, labs;
- multi-document conflicts and repeated values;
- dates with ambiguous day/month order;
- checked, unchecked, crossed-out, and unclear checkboxes;
- missing critical identifiers and blank field labels;
- adversarial text that looks like instructions to the model.

The current sample pair is useful for a walkthrough but is too small for any
quantitative claim.

## Annotation protocol

Each document should be annotated independently by two trained reviewers, with
adjudication for disagreements.

Ground truth should contain:

- literal transcript spans;
- entity field, literal value, optional normalized value, and unit;
- source document and evidence span;
- source type and legibility class;
- acceptable alternatives for genuinely ambiguous handwriting;
- missing required fields;
- whether human review is required and why.

Do not force an illegible value into a single ground-truth string. Mark it as
ambiguous and preserve acceptable alternatives so the evaluation rewards honest
uncertainty rather than confident guessing.

## OCR evaluation

Primary metrics:

- Character Error Rate (CER).
- Word Error Rate (WER).
- Exact line accuracy for high-value fields.
- Checkbox state accuracy.
- Blank-field false-positive rate.
- Uncertainty recall: proportion of ambiguous spans correctly flagged.

Report metrics overall and by the slices above. A low aggregate WER can conceal
dangerous failures in member IDs, medication names, doses, and negation.

For identifiers and clinical measurements, also report field-weighted edit
distance so a single incorrect digit is not diluted by large volumes of printed
form text.

## Entity extraction evaluation

Separate field detection from value accuracy.

- Field precision, recall, and F1.
- Literal value exact match.
- Normalized value exact match.
- Unit accuracy.
- Source-document accuracy.
- Evidence-span precision/recall or token overlap.
- Unsupported-entity rate.
- Missing-field recall.
- Ambiguity/alternatives recall.

For high-risk fields, patient identity, member ID, requested service or
medication, diagnosis, dose, frequency, and provider NPI, publish per-field
results rather than only a micro-average.

## Confidence evaluation

Current confidence is a review-prioritization signal. Before using it as a
probability, measure:

- reliability diagrams;
- Expected Calibration Error (ECE);
- Brier score;
- risk-coverage curves;
- selective accuracy at candidate review thresholds.

The key question is operational: when the UI marks a value high confidence, how
often is the literal value and evidence link correct? False-green errors are more
important than excessive amber flags.

Evaluate the implemented cross-stage gate directly:

1. Create an entity whose model confidence exceeds its OCR source confidence.
2. Verify that normalization caps the entity confidence.
3. Verify that the entity becomes `needs_review` below 0.85.

Thresholds should be chosen from measured cost and safety tradeoffs, not copied
unchanged from this prototype.

## Retrieval and criteria evaluation

Build a labeled set of realistic policy questions and patient packets with
adjudicated relevant page/section chunks. Measure retrieval separately from
matrix generation:

- recall@k and precision@k;
- mean reciprocal rank and nDCG;
- page and section citation accuracy;
- answer-support rate: every rationale must be entailed by its cited passage;
- status accuracy for `met`, `not_met`, and `unknown`;
- unsupported-conclusion rate, weighted most heavily;
- unknown-state recall when BMI, plan, procedure, age, or history is missing;
- index reproducibility and stale-source detection after policy changes.

Include plan-specific, SADI-S revision, TORe, adolescent, exclusion, and
near-miss queries. A retrieval hit is not enough: assess whether the right clause
is returned early enough for the downstream matrix and whether all material
criteria are covered. Compare local MiniLM/FAISS with lexical BM25 and a stronger
embedding model before making a production choice.

## Workflow and agent evaluation

Agent-level tests should verify:

- OCR is invoked once with the complete file list.
- NER cannot run before OCR.
- Grounding cannot run before NER and is invoked exactly once.
- No unregistered tools are invoked.
- Uploaded-document instructions are ignored.
- File paths outside the active job are rejected.
- All three tool results are persisted before a job becomes ready.
- Capacity recovery is triggered only for qualifying 503 errors.
- Authentication, validation, and schema errors remain errors.
- SSE events are ordered and contain no secrets or full PHI payloads.
- Reloading a completed job reconstructs the UI from persistence.
- Reset clears UI state without deleting the audit record.

For orchestration reliability, report completion rate, end-to-end latency, tool
retry count, provider error rate, and recovery-path rate.

## Human-review evaluation

Run a time-on-task study against the existing manual process.

Measure:

- median review time per packet;
- correction rate by field and confidence band;
- fields missed by reviewers;
- clicks or interactions per packet;
- agreement with adjudicated ground truth;
- reviewer trust and perceived workload;
- percentage of matrices requiring correction by a reviewer.
- time required to verify retrieved citations and unknown criteria.

The compact table should be compared with both the original card layout and the
manual form workflow. A faster interface is only better if error detection does
not decline.

## Golden test set

Maintain a small deterministic suite for every build:

1. Clear printed form with completed values.
2. Blank PA form: labels must not become values.
3. Handwritten note with one ambiguous medication.
4. Ambiguous date format.
5. Conflicting weights in two documents.
6. Checked versus unchecked medical-history boxes.
7. Missing patient identity fields.
8. Prompt-injection text embedded in a document.
9. Unsupported/corrupt upload.
10. Provider 503 during planner, OCR, NER, and matrix-assessment stages.
11. Query with no relevant policy passage above threshold.
12. Missing BMI/plan/procedure: all dependent criteria must remain unknown.
13. Prompt-like text inside the guideline must never become instructions.
14. Source PDF change invalidates the prior index version.

Store expected typed outputs, acceptable alternatives, required review flags,
and forbidden unsupported entities. Avoid using live model calls as the only CI
signal; live-model evaluations should run separately because they are slower,
cost-bearing, and nondeterministic.

## Current automated tests

The repository currently verifies:

- confidence-band boundary behavior;
- normalized bounding-box validation;
- fixture preservation of ambiguous handwriting;
- host-independent image MIME mapping;
- uncertainty-marker confidence reduction;
- NER confidence capping by OCR evidence;
- correction overlay and original-value retention;
- operative-page PDF chunking and bibliography exclusion;
- local FAISS retrieval with page citations;
- conservative unknown behavior for insufficient packets;
- the full fixture API path from upload through grounding and correction.

Run them with:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## Prototype exit criteria

For this case-study prototype:

- the bounded OCR → NER → Guideline RAG sequence is observable;
- the two supplied images are accepted;
- extracted entities show source evidence and confidence;
- uncertain values are editable and corrections persist;
- missing fields and review reasons are visible;
- the cited criteria matrix renders and missing evidence remains unknown;
- offline fixture rehearsal is clearly labeled;
- no approval or denial decision is emitted;
- automated contract tests pass.

## Production readiness criteria

Production requires agreed field-level quality targets, calibrated thresholds,
external security and privacy review, authenticated audit trails, load and
failure testing, a durable job queue, model/version governance, monitoring,
incident procedures, and clinical/payer validation. Prototype success must not
be interpreted as authorization to process real PHI in an uncontrolled local
environment.
