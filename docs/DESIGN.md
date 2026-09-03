# Prior-authorization adjudication with a human in the loop

**Design note for the Innovaccer AI Engineer case study.**
Deep-dive areas: **Knowledge Management** and **Evaluation**.

> **Document status.** This file describes the system as it stands today. The
> files in `docs/legacy/` were written for the earlier build, which used Google
> ADK, Gemini, and a FAISS/MiniLM chunk index. All three are gone. Those
> documents are kept for history but are **no longer accurate**; read this one.
> `REBUILD_SPEC.md` is the implementation brief this rebuild followed.

---

## 1. What the system does

A provider submits a PA request plus the patient's chart, as images or PDFs. The
system produces one of two outcomes, with a confidence signal and a rationale in
which every claim is traceable to both a guideline clause and a patient-record
span:

- **Provisional affirmation**: every criterion in the governing policy is met on
  cited evidence, above a confidence threshold. No person needs to look at it.
- **Refer to human clinical review**: anything else. A referral is not a bare
  hand-off: it names the blocking clauses, and for every criterion that failed on
  *missing* information (as opposed to contradicting evidence) it states what to
  request and from whom. For example: "ask the ordering provider for a
  documented BMI value", or "confirm the primary surgery date written
  '09/03/25'; it can be read more than one way". The reviewer starts with a
  work order, not a puzzle.

It cannot produce a denial. Not "is instructed not to"; it cannot. See §7.

```
upload (png / jpg / webp / pdf)
   │  main.py          validation, job record, SSE event stream
   ▼
run_job()              pipeline.py: deterministic, seven steps
   1  ocr              Claude vision      → OcrBatchResult      ← model
   2  extract          Claude structured  → ExtractionResult    ← model
   3  route            registry.route()   → RouteResult | NoRouteReason
   4  locate evidence  Claude structured  → [CriterionEvidence] ← model
   5  evaluate         evaluate_criterion() per predicate
   6  decide           decide()           → Adjudication
   7  persist          review.db
   ▼
reviewer UI: outcome, criteria matrix, clause ↔ evidence, agree / override
   ▼
reviewer_decisions table  ← the only place a non-affirmation can exist
```

Steps 3, 5 and 6 contain no model call. The model reads documents; it never
decides.

---

## 2. How guidelines are ingested, and how the representation generalizes

*(Presentation question 1. Deep-dive area: Knowledge Management.)*

### The representation

A payer guideline is not prose to be retrieved. It is a set of numbered criteria,
each of which is a predicate over patient facts. `knowledge/policies/*.yaml`
stores exactly that, with one node per numbered clause:

```yaml
- id: MGB-008.TORe.5
  page: 4
  text: "The primary surgery was performed at least one year ago"
  predicate:
    type: temporal
    anchor: primary_surgery_date
    op: ">="
    duration: P1Y
    relative_to: order_date
```

Six predicate types cover the whole of MGB-008 and, as far as we tested,
generalize across payers because they describe *how a clause decides*, not what it
is about:

| type | decides by | example |
|---|---|---|
| `threshold` | a number against a value | BMI ≥ 35 |
| `temporal` | an event date against a window | primary surgery ≥ 1 year before the order |
| `enum` | membership in a fixed set | primary procedure was RYGB |
| `boolean` | a documented yes/no fact | dietary consultation completed |
| `attestation` | a statement someone must make | not pregnant, no plan for 18 months |
| `external_ref` | the policy delegates elsewhere | InterQual, NCD 100.1, MassHealth |

What varies between payers is the data: clause text, page, thresholds, durations,
plan pathways. What does not vary is the evaluator. Adding a payer is authoring a
YAML file, not writing code.

The current file holds **44 criteria** across three self-contained criteria sets
(TORe 16, SADI-S revisional 14, SADI-S second-stage 14), 5 plan pathways, 3
exclusions, and 2 cross-references.

### Ingestion

`scripts/extract_policy_draft.py` sends a payer PDF to Claude with the `Policy`
schema as the structured-output format and writes `_draft_<name>.yaml`. The
registry **refuses to load any file beginning with an underscore**, so a
machine-drafted policy cannot decide coverage until a person renames it.

Automated verification then checks the draft three ways: it is schema-valid,
every clause text appears verbatim on the page it cites (the script re-reads
the source PDF), and there are no obvious inconsistencies such as duplicate
criterion ids. Failures are reported so review attention concentrates where
the model was unreliable.

Those flags narrow attention; they do not replace approval. **Every drafted
policy is reviewed in full before activation**, because the automated checks
establish source-text correctness, not predicate-semantic correctness: a clause
can be copied perfectly while its predicate encodes the wrong meaning, say a
one-year window anchored to `order_date` when the clause means the surgery
date. No verifier catches that; a person reading the clause beside its
predicate does.

This is the honest division of labour: the model does the tedious transcription
into a schema; a human owns the artifact that decides coverage.

### Why this instead of chunk retrieval

The previous build embedded 300-word sliding windows into FAISS. Three problems,
all of which this replaces:

1. **A window is not a clause.** SADI-S criteria 1–4 and 7–14 landed in different
   chunks. A citation could say "page 3, SADI-S revisional criteria" but not
   *which* requirement failed. The case study asks for "the specific clause a
   decision hinges on, not just a relevant page."
2. **Precision over recall.** The corpus is 44 clauses. BM25 over clause text
   beats dense retrieval at this size, and it has no embedding model to version,
   no index to rebuild, and no silent staleness when the model changes.
3. **Cross-references were invisible.** "See Medicare Advantage criteria above",
   "If Medicare Advantage criteria are not met, then MassHealth criteria are
   applied", InterQual, NCD 100.1, LCD L35022. These are now first-class
   `external_ref` nodes that force a referral with a named reason, instead of text
   that retrieval could return and a model could gloss over.

### Guarding the citation

`tests/test_registry.py::test_every_clause_appears_verbatim_on_its_cited_page`
re-reads the source PDF and asserts that all 44 clause strings appear, after
whitespace and typography folding, on the page each one claims. A paraphrase, a
typo, or a renumbered page fails the build. A citation that does not match the
source is worse than no citation, so it is enforced rather than trusted.

### Scaling to many policies

The scaling question is not "must someone author a YAML per policy?" It is
"how small can the human review per policy get?" That cost exists in every
architecture (a retrieval system still needs per-policy evaluation before it
touches real requests); this design makes it small, targeted, and shrinking.
Three tiers, all implemented:

**Breadth first: the payer's own PA index as a catalog.** The payer publishes
an index of *which services need PA at all* (for MGB, the Prior Authorization,
Notification, and Referral Guidelines). `knowledge/catalog/pa_catalog.yaml`
holds it as data (38 services, yes/no/varies, page-cited), and routing consults
it whenever no criteria policy matches. So every cataloged service gets an
informed answer on day one: *"the PA guide (p. 12) lists 'Nuclear Stress Tests'
as not requiring prior authorization"*, or *"'Spinal Surgery' requires prior
authorization, but the governing medical policy is not yet authored, so a
reviewer must adjudicate it."* An entry can never adjudicate (only an authored
policy holds criteria), but a referral that names the payer's own index beats
"nothing found." The catalog entry for a held policy defers to real routing.

**Depth on demand.** Criteria policies are authored in request-volume order,
not upfront: every catalog-informed miss carries a `catalog_service` field, so
production telemetry *is* the authoring backlog. Authoring payer N+1 is
`extract_policy_draft.py` (model drafts the YAML, verifier flags every clause
it cannot find verbatim on its cited page, a human reviews only the flags and
renames the file): zero code, because the six predicate types are closed over
*how clauses decide*, not what they are about.

**Updates re-review only the change.** When a payer revises a policy,
`extract_policy_draft.py --diff` compares the new draft against the live file
clause by clause and reports added, removed, and changed (text / page /
predicate) criteria, so a revision to 2 of 44 clauses costs review of 2, not
44. Combined with `source_sha256` staleness detection, the update loop is:
hash mismatch → redraft → clause diff → review the delta → rename.

At hundreds of policies the flat directory becomes a database-backed registry
with effective/termination dates, per-tenant scoping, and an approval workflow
UI for clinical policy staff, but the unit of knowledge (a versioned,
human-approved set of typed predicates with verbatim citations) does not change
shape. The human never leaves the loop; the loop just gets cheap.

### Updates and versioning

Each policy carries `effective_date` and `source_sha256`; `/api/knowledge/status`
reports whether the recorded hash still matches the PDF on disk. Production needs
more: a policy registry with effective/termination dates, scheduled update
detection, per-tenant access control, rollback, and an approval gate before a new
version can decide anything. But the shape here (versioned data + verification
against source + human approval) is the one that scales to it.

---

## 3. One temporal criterion, end to end

*(Presentation question 2.)*

Take **MGB-008.TORe.5**: *"The primary surgery was performed at least one year
ago."*

**Where each date comes from.** The anchor (`primary_surgery_date`) is extracted
from the chart by the evidence step, which must return a verbatim span and the
document it came from. The reference (`order_date`) comes from the PA request via
the extraction step. `decision_date` is the clock.

**How the window is evaluated.** `shift(anchor, "P1Y") <= order_date`. Month
arithmetic clamps to real calendar days, so 31 January + 1 month is 28 February,
not an error.

**What happens when a date is ambiguous.** `parse_date` returns *every* plausible
reading rather than picking one. `09/03/25` is 3 September 2025 or 9 March 2025.
Against a 2026-09-01 order date, the first is two days short of a year (NOT_MET)
and the second clears it comfortably (MET). Because the readings disagree, the
criterion is **UNKNOWN**, and the reason names both:

> `'09/03/25' can be read as 2025-09-03 (11 months earlier), 2025-03-09 (1y 5m
> earlier). The readings disagree about this window, so the date must be
> confirmed.`

When every reading agrees, the status is returned normally; ambiguity only blocks
a decision when it would change one.

**What happens when a date is missing.** If the anchor is absent, UNKNOWN. If the
*reference* is absent (no order date in the packet), also UNKNOWN, with
"The window is anchored to the order date, which is not present in the packet."
An ambiguous order date is treated as no order date, because an ambiguous anchor
would silently shift every window hung off it.

**A second window, anchored differently.** MGB-008.TORe.10 (tobacco) counts back
from the **planned surgery date**, not the order date. The `tore_tobacco_recent`
case has a quit date of 2026-08-26 against surgery on 2026-10-06: six weeks lands
on 10-07, one day past. NOT_MET. Two temporal criteria in one policy with two
different anchors is exactly the trap the case study describes, and the anchor is
data in the YAML rather than an assumption in code.

---

## 4. Model architecture, and why

*(Presentation question 3.)*

**A deterministic pipeline with bounded model calls.** Not one big prompt, and not
an agent.

- **Not a single call.** OCR, extraction, and evidence location have different
  failure modes and need separate evaluation. Splitting them creates an
  inspectable transcript, lets entity confidence be capped by the legibility of
  the line it came from, and makes a reviewer's disagreement attributable to
  transcription or to extraction.
- **Not an agent.** The earlier build used a Google ADK planner to call three
  tools in a fixed order. A planner that must be told the order is not planning;
  it is latency, cost, and a second failure mode in exchange for nothing. It also
  produced three copies of the pipeline (planner path, 503-recovery path, fixture
  path) that could drift apart. One function now serves all three.
- **Where agency would earn its place:** when a criterion comes back UNKNOWN, a
  loop that issues targeted follow-up queries ("find any tobacco mention anywhere
  in the packet") before giving up. That is a real search problem. Ordering three
  fixed steps is not. This is future work, not in the build.

**Which model does what.** One provider, `claude-opus-5` by default, overridable
per deployment with `LLM_MODEL` (`claude-haiku-4-5` is the cheap setting):

| step | why a model | what it may return |
|---|---|---|
| OCR | handwriting, checkboxes, layout | lines + legibility + alternatives |
| extraction | free text → typed entities | entities with verbatim spans |
| evidence location | 44 clauses × a messy chart | values and spans, **per criterion** |
| routing, evaluation, decision | n/a | *no model runs here* |

The evidence step is prompted to find values, and its output schema
(`CriterionEvidence`) has fields for `found`, `value`, `value_date`,
`evidence_text`, `document_id`, and no field for whether the criterion is
satisfied. The model is not asked to judge, and the schema gives it nowhere to
record a judgement if it tried.

---

## 5. Deep dive: evaluation

*(Presentation question 4, part two. Deep-dive area: Evaluation.)*

### The metric, and why it is asymmetric

The two error types are not comparable:

- A **false affirmation** (the system auto-approves something a reviewer would
  not) is a coverage error the payer owns, potentially a regulatory event, and by
  construction nobody looked at it.
- A **false referral** (the system refers something a reviewer would have
  approved) costs reviewer minutes.

So the primary metric is **false-affirmation rate (FAR)**, and the target is zero.
The secondary metric is **coverage**: the share of genuinely clear-cut requests
that get auto-cleared. Reporting coverage without FAR is meaningless, and
optimizing coverage before FAR is the failure mode the AMA article describes.

`scripts/evaluate.py` (WP6, next) reports, per run: the outcome confusion matrix;
FAR; coverage at τ ∈ {0.5, 0.7, 0.9} as a risk/coverage curve; per-criterion
status accuracy with every mismatch listed; the share of MET/NOT_MET whose
evidence span validates against the transcript; a confidence-band reliability
table; and cost and latency per packet from `response.usage`.

**No numbers are reported here yet.** The harness needs live model calls, and no
`ANTHROPIC_API_KEY` was available in this environment. Publishing a metric that
has not been computed would be exactly the kind of claim this system is built to
prevent, so this section states the design and the numbers follow the first run.

### The evaluation set

`scripts/make_synthetic_packets.py` renders eight packets with per-criterion
ground truth. The two images supplied with the case study are a pediatric Spanish
clinic note and a blank pharmacy PA form: neither is a bariatric request, so
neither can exercise a bariatric policy. They are kept as the out-of-domain case;
the rest are synthetic because a set with no affirmable case cannot measure false
affirmations at all.

| case | perturbation | expected |
|---|---|---|
| `tore_affirm` | none: complete packet | **affirm**, 16/16 met |
| `tore_temporal_fail` | primary surgery 8 months ago | refer: TORe.5 not met |
| `tore_bmi_missing` | no height, weight, or BMI | refer: TORe.4 unknown |
| `tore_tobacco_recent` | quit date 6 days inside the window | refer: TORe.10 not met |
| `tore_ambiguous_date` | surgery date written `09/03/25` | refer: TORe.5 unknown |
| `primary_rygb_delegated` | primary RYGB | refer: InterQual |
| `medicare_tore` | same chart, Medicare Advantage | refer: NCD 100.1 |
| `out_of_domain` | the two supplied images | refer: no applicable guideline |

Packets are gitignored because `make_synthetic_packets.py` regenerates them
deterministically.

### Ground truth is itself tested

`tests/test_golden_cases.py` asserts, without a network, that each `expected.json`
routes where it claims, names only clauses that exist in the registry, lists
documents that are present, and, the important one, that feeding its stated
criteria matrix to `decide()` returns its stated outcome. Ground truth that
disagrees with the decision function would make every metric computed against it
wrong. This caught a real routing bug during the build: the Medicare Advantage
pathway matched the alias `TORe` but not the string `Transoral outlet reduction
(TORe)`, so a Medicare request silently fell through to "no route" instead of
"delegated to CMS".

### Measured results

`scripts/evaluate.py` runs the production pipeline over all eight packets against
Claude and writes `data/eval/report.md`. Only the provider is wrapped, so routing,
criterion evaluation, and the decision run exactly as they do in the app. Model
outputs are cached per case and stage, so re-running the metrics never re-bills
the model.

| metric | result |
|---|---|
| **False affirmations** | **0 of 7 non-affirmable cases** |
| Eligible affirmations achieved | 1 of 1 |
| Outcome accuracy | 8/8 |
| Per-criterion status accuracy | 82/82 |
| Evidence traceability | 78/78 spans matched the transcript |
| Cost / latency, cold | $0.79–$0.86 and 64–78 s per packet |

Coverage is 1/1 at τ = 0.5 and 0.7 and 0/1 at τ = 0.9, and FAR is 0 at every τ.
The cliff is structural rather than empirical: a satisfied boolean or attestation
criterion scores a hand-set 0.8 and packet confidence is the minimum across
criteria, so any τ above 0.8 refers every packet resting on documented
attestations. On this set τ buys no safety and 0.9 costs all the coverage, which
is the honest version of the previous paragraph's point that τ is an assertion,
not a measurement.

### What the error analysis found

The first live run was 7/16 wrong on the affirmable packet, in two clusters that
turned out to be real defects rather than model noise.

**Negatively-phrased clauses inverted their own answer.** `MGB-008.TORe.7` reads
"The member does not have a substance use disorder or has been in recovery for at
least one year". The model was asked the clause text, answered about the fact
("no substance use disorder documented" → `no`), and the boolean predicate scored
that NOT_MET. The clause text is the citation and must stay verbatim, so the fix
separates the two jobs: `evidence_questions` in the policy YAML gives each
predicate field a retrieval question with a well-defined polarity, keyed by field
because the same concept is cited by clauses in three criteria sets. The reviewer
still sees the policy's own words. `tests/test_registry.py` now requires every
boolean and attestation clause to have one.

**A quote spanning a label and its value failed the traceability gate.** OCR puts
`Primary surgery date:` and `2019-06-14` on separate lines; the model quoted both,
and `best_evidence_match` compared against single lines, so a correctly cited date
was rejected as ungrounded and degraded to UNKNOWN. The gate now also tests
adjacent line pairs, attributing the match to the line carrying the value. The
window stops at two; a wider one would let a span assemble itself out of
scattered text, which is the thing the gate exists to catch.

Both failures were conservative: every one of them referred a packet that should
have been affirmed. That is the asymmetry working as designed (the defects cost
coverage and never cost safety), but it is also why coverage needs its own metric.
An evaluation that only watched FAR would have scored this run a clean zero and
learned nothing.

### The hardest trade-off

**Authoring cost versus citation precision.** Writing 44 predicates by hand took
longer than pointing an embedder at the PDF, and it does not generalize for free
to the next payer; someone must review each drafted policy. In exchange, a
decision cites `MGB-008.TORe.5` with its verbatim text and page, the failure
reason is a sentence a reviewer can check in seconds, and a paraphrased clause
fails the build. For a system whose entire value is making a reviewer faster, the
citation has to be exact, so the authoring cost is the right side to pay on. With
more time the leverage is in halving the review effort (better drafting prompts,
clause-level diffing when a policy is revised), not in going back to retrieval.

**What I would do differently with more data.** The confidence numbers are
hand-set constants (0.9 threshold, 0.85 temporal, 0.8 boolean). They order
criteria sensibly but they are not probabilities, and τ = 0.7 is therefore an
arbitrary line. With a few hundred adjudicated packets I would fit confidence on
observed reviewer agreement per predicate type and pick τ from the measured
risk/coverage curve rather than by assertion.

---

## 6. Deployment, monitoring, and the feedback loop

*(Presentation question 5.)*

**Deployment.** The prototype is a single FastAPI process with SQLite and
in-process background tasks. Production replaces those three: a durable queue with
idempotency keys and lease timeouts (a restart currently leaves a job marked
`running`), encrypted object storage for documents, and Postgres for the domain
record. The registry is read-only data and ships with the image.

**Monitoring.** The metric that matters cannot be measured directly in production
(nobody reviews the affirmations), so it is monitored by proxy:

- **reviewer overrides of an affirmation**: the closest live signal to FAR. Any
  non-zero rate is an incident, not a trend.
- coverage, and its drift after a policy update or a model change;
- per-criterion disagreement rate, which localizes a regression to a clause;
- evidence-validation failure rate, which is the hallucination canary;
- routing misses (`NoRouteReason` by line of business and procedure), which say
  which policy to author next;
- p50/p95 latency and cost per packet.

Sampled audit closes the gap: a fixed fraction of affirmations goes to a reviewer
anyway, purely to measure FAR on traffic nobody would otherwise see.

**Feedback.** `reviewer_decisions` records, per criterion, what the system said,
what the reviewer said, the final outcome, a reason code, and who decided. That
feeds four things, in increasing order of caution:

1. **Threshold tuning.** Disagreement by confidence band moves τ. Cheap, reversible.
2. **A regression set.** Every override becomes a golden case, so a fixed bug
   stays fixed. This is where most of the value is.
3. **Policy corrections.** A criterion with a high disagreement rate usually means
   the predicate is wrong, not that the model is; the fix is a YAML edit and a
   re-review, tracked like any other policy change.
4. **Extraction fine-tuning.** Enough corrected spans could train the evidence
   step. Only the *reading* step, never the deciding step.

**The decision function is never learned.** It stays deterministic, reviewed, and
diffable. Feedback tunes what the system reads and how confident it is; it never
tunes what the system is allowed to conclude.

---

## 7. How the no-denial guarantee is mechanical

*(Presentation question 6, the one the whole design is organized around.)*

Five independent layers, none of which is a prompt instruction:

1. **The type cannot express it.** `Outcome` has two members. There is no denial
   value to return, so no code path, model error, or injected instruction can
   produce one. `test_outcome_type_cannot_express_a_denial` asserts the member set.
2. **The decision is not generated.** `decide()` is a pure function of the
   criteria matrix: no I/O, no model call, no clock read except the one passed
   in. Identical inputs give identical outputs, always.
3. **The model has nowhere to put a verdict.** `CriterionEvidence`, the evidence
   step's output schema, has `found`/`value`/`evidence_text` and no status field.
4. **Absence never counts against the member.** Missing, ambiguous, untraceable,
   or delegated evidence is UNKNOWN, and any UNKNOWN forces a referral. A gap in
   the chart can only ever slow a request down, never deny it.
5. **Exclusions refer, they do not deny.** A request hitting an exclusion clause,
   the one case that most looks like an automatic no, is referred with the clause
   attached, because non-coverage is a determination only a licensed reviewer may
   make.

A 500-iteration fuzz over random criteria matrices asserts that the outcome set
stays closed and that an affirmation is unreachable unless every criterion is met.
A denial enters the record in exactly one way: a person posts it to
`/api/jobs/{id}/decision`, and it is stored with their name against it.

### Against the AMA failure modes

| failure mode | what prevents it here |
|---|---|
| opaque bulk denials | the system cannot deny; only affirm or refer |
| no clinician oversight | every non-affirmation reaches a person, with the blocking clauses named |
| error propagation | evidence must be quotable from the transcript; unsupported claims degrade to UNKNOWN rather than flowing downstream |

---

## 8. Prompt-injection boundary

Uploaded documents are untrusted input on every hop. Every system prompt states
that document content is data and never instructions. Beyond the instruction:
tool paths are confined to the job's own upload directory; the evidence step's
output schema cannot express a decision; and any evidence span that is not
quotable from the transcript is discarded, so text injected into a document cannot
become a fact. Retrieved policy text is treated the same way. This is defence in
depth, not a complete control; production still needs adversarial evaluation,
authentication, and audit review.

---

## 9. Limitations

Stated plainly, because a partial solution that is honest about its edges is worth
more than one that is not.

- **The evaluation set is eight packets.** The numbers in §5 are real, from live
  model calls on the production pipeline, but a set this small demonstrates the
  method rather than establishing production safety. That takes hundreds of
  real adjudicated packets.
- **Adult primary bariatric surgery is not adjudicable.** MGB-008 delegates it to
  InterQual, which is licensed and not in the document. Those requests always
  refer. The most common bariatric request is therefore out of scope by the
  policy's own design, and the system says so rather than approximating.
- **Confidence is not calibrated.** It is a legibility × traceability signal with
  hand-set constants, reported as a band. It is not a probability.
- **Evaluation data is synthetic.** Rendered text, not scans: no skew, no noise,
  no cursive. Real handwriting is the largest untested risk in the OCR step.
- **One criteria policy, one specialty.** The PA catalog gives every listed
  service an informed routing answer, but only MGB-008 has authored, verified
  criteria; every other PA-required service refers with "policy not yet
  authored." The drafting, verification, and diff tooling for the next policy
  exists; the next policy itself does not.
- **Not production-ready for PHI.** No authentication, authorization, tenancy,
  encrypted storage, retention policy, audit immutability, or BAA-covered
  deployment. In-process background work does not survive a restart.
- **Bounding boxes are modelled but not populated**, so evidence highlights are
  textual rather than visual.

---

## 10. Running it

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"

# Live mode. Either the environment, or a .env at the repo root (gitignored).
$env:ANTHROPIC_API_KEY = "sk-ant-..."

.\.venv\Scripts\python.exe -m pytest -q                          # 107 tests, no network
.\.venv\Scripts\python.exe scripts\make_synthetic_packets.py     # 8 golden packets
.\.venv\Scripts\python.exe scripts\evaluate.py                   # live: ~$6.50, ~9 min
.\.venv\Scripts\python.exe scripts\evaluate.py --cached          # replay: free
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

The key is never committed: `.env` is the first line of `.gitignore`, and nothing
in the codebase writes it or logs it.

Without a key the app runs in a clearly labelled fixture mode for offline
rehearsal. Fixture output is not a model result and must never be presented as one.

| path | what it holds |
|---|---|
| `app/registry.py` | policy loading, routing, clause lookup, BM25 |
| `app/adjudication.py` | predicate evaluation and `decide()`: the auditable core |
| `app/temporal.py` | date resolution and window arithmetic |
| `app/pipeline.py` | the seven steps |
| `app/providers_claude.py` | the three model calls |
| `knowledge/policies/*.yaml` | the guideline, as data |
| `scripts/extract_policy_draft.py` | draft a new payer's YAML for review |
| `scripts/make_synthetic_packets.py` | the evaluation set |
| `scripts/evaluate.py` | the metrics; writes `data/eval/report.md` |
| `tests/` | 107 tests; `test_adjudication.py` and `test_registry.py` are the ones that matter |
