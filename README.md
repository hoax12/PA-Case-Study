# Prior-authorization adjudication with a human in the loop

A prior-authorization packet arrives as images. This system transcribes it,
extracts the clinical facts, routes the request to the guideline that governs it,
evaluates every clause of that guideline against the packet, and returns one of
two outcomes:

- **Provisional affirmation**: every criterion is met, each citing the clause and
  page it was checked against. Reversible, and never final.
- **Refer to human**: anything else, with the blocking clauses named and, for
  each criterion that failed on missing information, what to request and from
  whom (e.g. "ask the ordering provider for a documented BMI value").

There is no third outcome. `Outcome` is an enum with two members, `decide()` is a
pure function over evaluated criteria, and a denial can enter the record only
through a person posting one to `/api/jobs/{id}/decision`, stored with their name
against it. A 500-iteration fuzz test asserts the outcome set stays closed.

The model transcribes, extracts, and locates evidence. It never returns a
coverage status: routing, criterion evaluation, and the decision are ordinary
code you can read, test, and diff.

## Design

**Start with [docs/SUBMISSION.md](docs/SUBMISSION.md)**: the plain-English
walkthrough of the core idea, the decisions and their trade-offs, and the
assumptions, written for a reader with no prior context.

**[docs/DESIGN.md](docs/DESIGN.md)** is the technical deep dive: the knowledge
representation, the temporal engine, the evaluation methodology and its results,
the deployment and feedback plan, and the limitations.

Measured over eight synthetic packets against Claude Opus 5:

| metric | result |
|---|---|
| False-affirmation rate (primary) | 0/8 |
| Outcome accuracy | 8/8 |
| Per-criterion status accuracy | 82/82 |
| Evidence traceability | 78/78 |
| Cost / latency | $0.79–$0.86, 64–78 s per packet |

Full report: [data/eval/report.md](data/eval/report.md).

## Run it

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"

# Live mode. Either the environment, or a .env at the repo root (gitignored).
$env:ANTHROPIC_API_KEY = "sk-ant-..."

python -m pytest -q                        # 97 tests, no network
python scripts\make_synthetic_packets.py   # 8 evaluation packets
python scripts\evaluate.py --cached        # metrics, replayed from cache
python -m uvicorn app.main:app --port 8000
```

Without a key the app runs in a clearly labelled fixture mode for offline
rehearsal. Fixture output is not a model result and must never be shown as one.

## Layout

| path | what it holds |
|---|---|
| `app/registry.py` | policy loading, routing, clause lookup, BM25 search |
| `app/adjudication.py` | predicate evaluation and `decide()`: the auditable core |
| `app/temporal.py` | ambiguous-date resolution and window arithmetic |
| `app/pipeline.py` | the sequence, end to end |
| `app/providers_claude.py` | the three bounded model calls |
| `knowledge/policies/*.yaml` | the guideline, as data |
| `knowledge/catalog/pa_catalog.yaml` | the payer's PA index: which services need PA at all |
| `scripts/extract_policy_draft.py` | draft a new payer's YAML for review; `--diff` for revisions |
| `scripts/evaluate.py` | the metrics |
| `tests/` | `test_adjudication.py` and `test_registry.py` are the ones that matter |

The files in `docs/legacy/` predate this build and describe a removed Google
ADK / Gemini / FAISS prototype. They are kept only as a record of what was
replaced; `DESIGN.md` supersedes all of them.

## Limitations

Adult primary bariatric surgery is delegated to InterQual and Medicare Advantage
to CMS NCD 100.1; neither criteria set is held here, so both always refer.
Confidence is an uncalibrated legibility signal, not a probability. The
evaluation set is synthetic and small. There is no authentication, audit
retention, or PHI control; this is a prototype, not a deployable service.
