# A Prior-Authorization Copilot That Cannot Say No

**Case study submission: AI Engineer.**
This document explains the whole project in plain language: what it does, how
it works, the decisions I made, the trade-offs behind them, and the assumptions
I worked under. It assumes no prior knowledge of the project. Technical depth
lives in [DESIGN.md](DESIGN.md); the code is in this repository and runs.

---

## 1. The problem, in one paragraph

When a doctor wants to perform certain procedures, the patient's insurance plan
often requires **prior authorization (PA)**: the doctor submits a request plus
the patient's medical records, and someone at the health plan checks the
request against the plan's written guidelines before the procedure happens.
This checking is slow, manual, and repetitive: a reviewer reads a stack of
scanned documents, finds the guideline that applies, and walks through its
requirements one by one. The task: build an AI system that safely speeds this
up.

## 2. The core idea

> **The AI reads. The code decides. A human owns everything else.**

Most AI-for-PA designs hand the documents and the guideline to a large language
model and ask, "should this be approved?" I deliberately did not do that,
because a model's answer cannot be audited, can change between runs, and, in
the worst case, could wrongly deny care. News coverage of AI-driven bulk denials
(the AMA has written about this) shows exactly how that goes wrong.

Instead, this system splits the work by who is trustworthy at what:

- **The AI model** does what models are good at: reading messy scanned
  documents. It transcribes them, pulls out facts ("BMI: 36.2", "surgery date:
  2019-06-14"), and finds the exact sentence in the chart that supports each
  fact. It is never asked whether anything should be approved, and the data
  structure it must fill in has **no field where a verdict could even go**.
- **Ordinary, readable code** makes the decision. Each guideline requirement
  is stored as a small machine-checkable rule, and a plain function evaluates
  the extracted facts against those rules. Same inputs, same output, every
  time. You can read it, test it, and diff it when it changes.
- **A human reviewer** handles everything that is not a clear-cut yes. And
  only a human can ever say no.

The system has exactly **two possible outcomes**:

1. **Provisional affirmation**: every requirement is met, and every claim
   cites both the guideline clause it checked and the sentence in the
   patient's chart that proves it.
2. **Refer to a human**: anything else. The referral names which
   requirements are blocking, and for each piece of *missing* information it
   says what to request and from whom ("ask the ordering provider for a
   documented BMI value").

There is no third outcome from the automated path. This is a structural
property, not a prompt instruction: the outcome type in the code has exactly
these two values, the decision is computed by a deterministic function over the
evaluated criteria, and a test asserts the type stays closed. The automated
pipeline therefore has no way to *represent* a denial. That is a claim about
the design of one path, not a claim that the whole system is unbreakable,
which is why a denial enters the record through exactly one intended channel
(a person posting it through the review screen, stored with their name on it),
and why production would still need authentication and audit controls around
that channel.

## 3. How it works, end to end

```
 A provider uploads the PA request + patient chart (images / PDF)
        │
        ▼
 ┌─────────────────────────────────────────────────────────────┐
 │ 1. TRANSCRIBE   AI reads every page (handwriting, forms)    │  ← model
 │ 2. EXTRACT      AI pulls typed facts with exact quotes      │  ← model
 │ 3. ROUTE        Code finds the guideline that governs this  │  ← code
 │ 4. LOCATE       AI finds chart evidence for each criterion  │  ← model
 │ 5. EVALUATE     Code checks each rule against the evidence  │  ← code
 │ 6. DECIDE       Code computes the outcome                   │  ← code
 └─────────────────────────────────────────────────────────────┘
        │
        ├── every requirement met, on cited evidence
        │        → PROVISIONAL AFFIRMATION (reversible, never final)
        │
        └── anything else
                 → REFER TO HUMAN, with:
                    • the blocking clauses, quoted verbatim with page numbers
                    • what is missing and whom to ask for it
                    → reviewer agrees or overrides, per criterion and overall
                    → every override is recorded and becomes a test case
```

Steps 3, 5, and 6 contain **no AI at all**. The model reads; it never judges.

Two safety rails run through every step:

- **Missing evidence never counts against the patient.** If the chart doesn't
  mention something, the status is "unknown," which routes to a human. A gap
  can slow a request down; it can never sink it.
- **Every quote must be verifiable.** If the model cites a sentence that
  cannot be found in the transcribed documents, that evidence is discarded and
  the criterion becomes "unknown." This gate targets fabricated evidence: a
  hallucinated quote never reaches the decision.

To be precise about what protects against what: quote verification defends
against fabricated evidence; treating every uploaded document as untrusted
input, plus output schemas that have no field for a verdict, *reduces* the
prompt-injection surface; and deterministic evaluation means the model cannot
directly generate the adjudication outcome at all. These are layered risk
reductions with different strengths: the determinism is structural, the
injection defenses are hardening that still warrants adversarial testing
(listed in future scope), not a proof.

## 4. One request, two endings: a worked example

A simple TORe (a revision of a previous weight-loss surgery) request:

```
INPUT
Procedure:            TORe
Line of business:     Commercial
BMI:                  38.2
Primary surgery:      RYGB on 2024-05-10
Order date:           2026-09-01
Diet consultation:    completed
...and the rest of the chart
```

What each stage does with it:

```
1. TRANSCRIBE   "Current BMI 38.2"                          ← model reads
                "Prior RYGB 05/10/2024"

2. EXTRACT      bmi = 38.2                                   ← model, with quotes
                primary_surgery_date = 2024-05-10
                primary_procedure = RYGB

3. ROUTE        Commercial + TORe → MGB-008, TORe criteria   ← code

4. LOCATE       TORe.5 evidence: "Prior RYGB 05/10/2024"     ← model, per criterion

5. EVALUATE     TORe.4  BMI 38.2, clause requires >= 35        → MET
                TORe.5  surgery 2024-05-10 (2 years earlier)   → MET
                        against order date 2026-09-01;
                        the clause requires 1y
                ...                                          ← code
                16 of 16 MET

6. DECIDE       every criterion met, on cited evidence       ← code
                → PROVISIONAL AFFIRMATION
```

Now change **one field**. The surgery date is handwritten as `09/03/25`
instead:

```
5. EVALUATE     TORe.5: '09/03/25' can be read as
                  2025-09-03  (11 months earlier)  → not enough
                  2025-03-09  (1y 5m earlier)      → enough
                The readings disagree about this window        → UNKNOWN

6. DECIDE       → REFER TO HUMAN

   The reviewer sees, alongside the clause quoted from page 4:
   "To resolve before re-submission: ask the ordering provider to
    confirm the primary surgery date written '09/03/25'; it can be
    read more than one way."
```

Same packet, one ambiguous handwritten date, and the system's answer changes
from "affirm, with citations" to "a human should look, and here is exactly
what to ask for." It never guessed.

## 5. How guidelines are stored, and why not a vector database

A payer guideline looks like prose, but it is really a numbered list of
checkable requirements. So I store it as exactly that: one entry per numbered
clause, each carrying its **verbatim text**, its **page number**, and a small
machine-readable rule:

```yaml
- id: MGB-008.TORe.5
  page: 4
  text: "The primary surgery was performed at least one year ago"
  predicate:
    type: temporal        # a date checked against a time window
    anchor: primary_surgery_date
    op: ">="
    duration: P1Y         # one year
    relative_to: order_date
```

Six rule types (number thresholds, date windows, fixed choices, documented
yes/no facts, required attestations, and "this policy delegates elsewhere")
cover the entire 44-criterion bariatric surgery policy, because they describe
*how a clause decides*, not what it is about. An ejection-fraction threshold in
a cardiology policy is the same rule type as a BMI threshold here.

**I built the first version of this project the other way**, with the standard
RAG recipe: chop the PDF into ~300-word chunks, embed them, search by
similarity.
It worked, and I replaced it, for three reasons:

1. **A chunk is not a clause.** A citation could say "somewhere on page 3,"
   but the reviewer needs *"criterion 5, and here is its exact sentence."*
2. **A cross-reference is invisible to retrieval.** This policy says things
   like "for Medicare Advantage, apply CMS NCD 100.1 instead." Retrieval
   returns that as text a model may gloss over; here it is a first-class rule
   that forces a referral with a named reason.
3. **At 44 clauses, similarity search solves a problem I don't have.** Keyword
   search over clause text is simpler and has no embedding model to version.

And one enforcement that keeps the store honest: a test re-reads the source
PDF and asserts that **all 44 stored clause texts appear verbatim on the page
they cite**. If anyone paraphrases a clause or a page number drifts, the build
fails. A citation that doesn't match the source is worse than no citation.

## 6. How this scales beyond one policy

The fair challenge: "You encoded one policy by hand. Payers have hundreds."
My answer is that the *authoring* is automatable, but the *accountability* is
not, so the design makes human review small, targeted, and shrinking, rather
than pretending to remove it. Three tiers, all implemented:

**Breadth first: the payer's own index.** Payers publish a catalog of which
services need PA at all. I loaded MGB's ("Prior Authorization, Notification,
and Referral Guidelines," 38 services curated as data). When no detailed
policy matches a request, the system answers from the catalog: *"the PA guide
(p. 12) lists this as not requiring prior authorization"*, or *"PA is required,
but the governing policy is not yet authored, referring to a reviewer."*
Every service gets an informed answer on day one, without pretending to have
criteria it doesn't hold.

**Depth on demand.** Detailed policies are added in request-volume order, not
all upfront. Each catalog-informed miss is logged with the service name, so
the production traffic itself writes the authoring backlog. Adding a policy is
zero code: a script sends the payer PDF to the model, which drafts the YAML,
and automated verification then checks the draft for schema validity, verbatim
source fidelity (every clause text must appear on the page it cites), and
obvious inconsistencies such as duplicate criterion ids. Verification failures
are flagged so review attention concentrates where the model was unreliable.
The flags narrow attention; they do not replace approval. **Every drafted
policy receives full human review before activation**, because the automated
checks establish source-text correctness, not predicate-semantic correctness:
a clause can be copied perfectly while its machine rule encodes the wrong
meaning, say a one-year window anchored to the order date when the clause
means the surgery date. Only a person can confirm the rule says what the
clause means. Activation is the reviewer renaming the file; until then the
registry refuses to load it, so a machine-drafted rule cannot decide coverage
unreviewed.

**Updates re-review only the change.** When a payer revises a policy, a diff
tool compares the new draft to the live file clause by clause. A revision that
touches 2 of 44 clauses costs review of 2 clauses. A stored hash of the source
PDF flags staleness automatically.

The one-line version: **the human never leaves the loop; the loop just gets
cheap.**

## 7. The decisions and their trade-offs

**Decision 1: a fixed pipeline, not an AI agent.**
My first build used an agent framework where a planner model chose which tool
to call next. But the steps here always run in the same order. A planner that
must be told the order isn't planning; it is extra cost, extra latency, and an
extra way to fail. I replaced it with a plain function. *Trade-off:* less
flexibility. If a criterion comes back unknown, an agent could go hunt for the
evidence with follow-up searches. That is real future work; ordering six fixed
steps is not.

**Decision 2: three separate model calls, not one big prompt.**
Transcription, fact extraction, and evidence-finding fail differently and need
to be measured separately. Splitting them means a reviewer's disagreement can
be traced to "the OCR misread the page" versus "the extraction picked the
wrong value." *Trade-off:* more calls, more latency (~70 seconds per packet)
than a single mega-prompt. For an overnight-or-faster process, auditability
wins.

**Decision 3: hand-authored rules over automatic retrieval.**
Writing 44 rules took longer than pointing an embedder at a PDF. In exchange,
every decision cites an exact clause and page, a failure reason is one sentence
a reviewer can verify in seconds, and a paraphrase fails the build. For a
system whose entire value is making the reviewer faster, the citation must be
exact, so authoring cost is the right side to pay on. The drafting script
then wins most of that cost back.

**Decision 4: treat the two error types as incomparable.**
A **false affirmation** (auto-approving what a reviewer would have caught) is
a safety failure nobody would ever look at again. A **false referral** (making
a human check something clear-cut) costs minutes. So the primary metric is the
false-affirmation rate with a target of zero, and every ambiguity resolves
toward referral. *Trade-off:* coverage. Example: a surgery date written
"09/03/25" could be September 3 or March 9. If the two readings disagree about
whether a time window is satisfied, the system refuses to guess and refers,
telling the reviewer to confirm the date, even though guessing would often be
right.

**Decision 5: the feedback loop tunes reading, never deciding.**
Reviewer overrides are recorded per criterion and feed four things, in
increasing order of caution: threshold tuning, a growing regression-test set
(every override becomes a permanent test case), corrections to the policy
rules, and eventually fine-tuning the extraction step. The decision function
itself is never learned; it stays deterministic, reviewed, and diffable.

**Decision 6: build the working prototype anyway.**
A block diagram was acceptable for this case study, but several of my design
claims ("the quote-verification gate catches hallucinated evidence," "the
automated path cannot represent a denial") are only credible when executable.
The evaluation run also caught two real bugs that a paper design would have
shipped (see below), which I take as evidence the prototype was worth building.

## 8. What I measured

I built eight synthetic test packets with per-criterion ground truth (real
patient data wasn't available, correctly so; the two supplied sample images are
kept as the out-of-domain case). One packet deserves affirmation; the other
seven each contain one specific flaw: a surgery too recent, a missing BMI, an
ambiguous date, a delegated plan type, and so on. The ground truth is itself
tested: a test suite asserts each expected answer is consistent with the
decision function before any metric is computed against it.

Results against Claude Opus 5, running the exact production pipeline:

| Metric | Result |
|---|---|
| **False affirmations (primary)** | **0 of 7 non-affirmable cases** |
| Eligible affirmations achieved | 1 of 1 |
| Overall routing accuracy | 8 of 8 |
| Per-criterion status accuracy | 82 of 82 |
| Evidence quotes verified against transcripts | 78 of 78 |
| Cost / latency per packet | ~$0.80 / ~70 s |

To be clear about what these numbers mean: a set of eight synthetic packets
demonstrates that the evaluation *method* works, per-criterion, with tested
ground truth. It does not establish production safety. That would take
hundreds of real, adjudicated packets, which is why the harness is built to
rerun cheaply (model outputs are cached per case and stage) as the set grows.

How each case behaved, at all three levels (input, criterion, routing):

| Test case | Input / perturbation | Criterion affected | Criterion result | Expected | Actual |
|---|---|---|---|---|---|
| Complete packet | all 16 TORe requirements documented | all 16 | MET | affirm | affirm |
| Missing BMI | no height, weight, or BMI anywhere | TORe.4 (BMI threshold) | UNKNOWN | refer | refer |
| Surgery too recent | primary surgery 8 months before order | TORe.5 (1-year window) | NOT_MET | refer | refer |
| Ambiguous date | surgery date written "09/03/25"; readings disagree on the window | TORe.5 | UNKNOWN | refer | refer |
| Recent tobacco use | quit date one day inside the 6-week window before surgery | TORe.10 (tobacco lookback) | NOT_MET | refer | refer |
| Delegated policy (commercial) | primary RYGB, which MGB-008 delegates to InterQual | external reference | UNKNOWN | refer | refer |
| Delegated policy (Medicare) | same chart under a Medicare Advantage plan, delegated to CMS NCD 100.1 | external reference | UNKNOWN | refer | refer |
| Out of domain | the two sample images from the case study (a pediatric note, a blank pharmacy form) | none routed | n/a | refer | refer |

In every referral the criterion-level statuses above are what the reviewer
sees, with the clause quoted and, where information is missing, what to
request. One more gate is exercised in unit tests rather than a packet:
evidence whose quote cannot be found in the transcript is discarded and the
criterion degrades to UNKNOWN, which also refers.

The honest details: the *first* live run got 7 of 16 criteria wrong on the
affirmable packet. Both causes were design defects, not model noise: a
negatively-phrased clause ("does **not** have a substance use disorder")
whose yes/no answer inverted, and a correct quote spanning two lines that my
verification gate was too strict to accept. Both fixes are in the code and
both failures were *conservative* (they wrongly referred, never wrongly
approved). That is the safety asymmetry working as designed, but also proof that
measuring only safety would have hidden them. 107 automated tests now pass,
including a fuzz test that hammers the decision function with 500 random
inputs and asserts the outcome set stays closed.

## 9. Assumptions I made

1. **Wrongly approving is far worse than wrongly referring.** The entire
   design leans on this asymmetry. If the business goal were maximum
   automation instead, this would be the wrong architecture.
2. **Guidelines are numbered and checkable.** True of this policy and most
   medical-necessity documents I inspected; a purely narrative guideline would
   need the reviewer-heavy path.
3. **Synthetic data can stand in for real packets, structurally.** My
   packets are rendered text, not real scans. Real handwriting is the largest
   untested risk, which is exactly why transcription is isolated as its own
   measurable step.
4. **The adjudicator's clock and time zone are trustworthy**, since date
   windows are computed against it.
5. **A confidence number without calibration data is a label, not a
   probability.** Mine orders criteria sensibly but is hand-set; I report it
   as a band and would fit it to reviewer-agreement data (a few hundred
   packets) before trusting a threshold chosen from it.
6. **No PHI in this prototype.** No authentication, encryption at rest, or
   audit retention; listed as production work, not silently assumed away.

## 10. Production readiness and future scope

What separates this prototype from a deployable service, by area:

- **PHI and security.** Authentication, role-based access control, encryption
  at rest and in transit, tenant isolation, immutable audit logging, a data
  retention policy, and a BAA-covered deployment. None of this exists in the
  prototype, deliberately and visibly.
- **Reliable infrastructure.** A durable job queue with idempotency keys and
  lease timeouts (today a restart leaves a job marked running), encrypted
  object storage for uploaded documents, and Postgres in place of SQLite.
- **Policy lifecycle.** Versioning with effective and termination dates,
  scheduled update detection against source hashes, clause-level diffing on
  revision (built), an approval gate before any version can decide (built as
  the rename convention; production wants a workflow), and rollback.
- **Model and evaluation.** Real scanned and handwritten packets, since
  rendered text is the evaluation's biggest gap; confidence calibrated on
  reviewer agreement instead of hand-set constants; adversarial testing of the
  prompt-injection defenses; and a second criteria policy authored from the
  catalog backlog to measure the marginal-policy cost for real.
- **Monitoring.** p50/p95 latency and cost per packet, routing misses by
  service (the authoring backlog), per-criterion disagreement rates to
  localize regressions, evidence-verification failure rate as the
  hallucination canary, and sampled human audits of provisional affirmations,
  because the error that matters most is on the path nobody otherwise reads.
- **Where an agent would earn its keep.** A targeted evidence-search loop for
  UNKNOWN criteria ("find any tobacco mention anywhere in the packet") before
  giving up; a real search problem, unlike ordering six fixed steps.

## 11. Reading and running the rest

| Where | What |
|---|---|
| [DESIGN.md](DESIGN.md) | full technical design: representation, temporal engine, evaluation, deployment |
| [README.md](../README.md) | run instructions (venv, tests, evaluation, server) |
| `app/adjudication.py` | the decision function, the part that must be readable |
| `knowledge/policies/mgb_008_bariatric.yaml` | the guideline, as data |
| `data/eval/report.md` | the full evaluation report |

Everything runs offline in a labelled fixture mode without an API key;
with a key, the full pipeline runs live. `python -m pytest -q` → 107 passed.
