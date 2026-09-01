# Bariatric guideline knowledge base

## Purpose and boundary

The knowledge layer grounds extracted patient evidence against Mass General
Brigham Health Plan Bariatric Surgery Policy 008, effective July 1, 2026. It is
a retrieval and review aid, not a medical-necessity or authorization engine.

The output contract has no approval or denial value. Every result has
`overall_status: requires_human_review`, and criteria use only `met`, `not_met`,
or `unknown`.

## Source governance

- Source: `knowledge/source/BariatricSurgery.pdf`.
- Indexed scope: operative policy pages 1–7.
- Excluded scope: bibliography/reference pages 8–11.
- Metadata: title, policy number, effective date, source SHA-256, embedding
  model, build timestamp, page, section, chunk ID, and index version.
- Generated artifacts: `knowledge/index/bariatric_guideline.faiss` and
  `knowledge/index/bariatric_guideline.json`.

The generated index is ignored by Git because it is reproducible from the
versioned source. The source hash or embedding-model change invalidates the old
index and triggers a rebuild on access.

## Build and runtime

Build explicitly after dependency installation:

```powershell
.\.venv\Scripts\python.exe scripts\build_guideline_index.py --force
```

The build command is the only step allowed to download the sentence-transformer
model. Application runtime loads the cached model with `local_files_only=True`.
The FastAPI lifespan prewarms the model and FAISS index on the startup thread to
avoid first-request PyTorch initialization stalls on Windows.

Verify readiness:

```powershell
curl.exe -sS http://127.0.0.1:8000/api/knowledge/status
```

Both `ready` and `runtime_ready` should be `true` before a presentation.

## Indexing design

1. `pypdf` extracts page text.
2. Repeated headers, footers, page numbers, and the contents table are removed.
3. Known policy headings and procedure subsections are retained as metadata.
4. Text is split into at most 300-word chunks with 45-word overlap.
5. `sentence-transformers/all-MiniLM-L6-v2` produces normalized embeddings.
6. FAISS `IndexFlatIP` stores them; with normalized vectors, inner product acts
   as cosine similarity.

The small local model and exact FAISS index were chosen for a fast, reproducible
three-day prototype. They are not assumed to be the best production retrieval
stack.

## Retrieval design

`search()` performs FAISS candidate retrieval, applies the configured minimum
score, and adds a compact lexical subsection rerank. This helps an exact `TORe`
query rank the TORe criteria section above a version-history paragraph that also
mentions TORe.

Grounding uses facet retrieval rather than one overloaded query. It retrieves
coverage for:

- payer/pathway applicability;
- age and BMI/adolescent requirements;
- SADI-S revision;
- TORe revision;
- preoperative preparation;
- exclusions and required setting.

One leading candidate per facet is retained before runner-up passages fill the
configured `RAG_TOP_K` limit. This improves policy coverage while every passage
still originates in the FAISS candidate set.

## Criteria construction and validation

Live Gemini mode may structure the retrieved evidence into a criteria matrix.
Fixture mode and assessment-provider failures use a deterministic conservative
matrix.

Application validation enforces:

- only citation strings from retrieved passages are accepted;
- `met` or `not_met` requires literal patient evidence;
- unsupported status or citation is downgraded to `unknown`;
- at most eight material criteria are retained;
- no approval, denial, or medical-necessity conclusion is emitted.

The conservative matrix selects citations by criterion vocabulary rather than
passage position. A criterion may show two relevant citations where one clause
does not cover the full concept.

The supplied handwritten pediatric note and blank PA form do not contain a
bariatric request, plan, BMI, or surgery history. An all-unknown matrix is
therefore the expected safe result, not a workflow failure.

## API examples

Direct retrieval:

```http
POST /api/knowledge/search
Content-Type: application/json

{"query":"TORe revisional procedure requirements","top_k":3}
```

The response contains page/section citations and
`human_review_required: true`. The full workflow persists grounding under the
job's `grounding` property and emits a `grounding` tool call/result through SSE.

## Update procedure

When a policy changes:

1. Confirm source authority, effective date, and applicable line of business.
2. Replace the versioned PDF in `knowledge/source/`.
3. Review page-scope and heading parsers; do not assume the old layout.
4. Rebuild the index with `--force`.
5. Confirm source hash, index version, chunk count, and page citations.
6. Run the retrieval and criteria golden set.
7. Obtain payer/clinical review before promoting deterministic predicates.

Production needs a policy registry, effective/termination dates, scheduled
update detection, rollback, tenant/plan access controls, immutable lineage, and
an approved release workflow.

## Evaluation

Report retrieval and criteria quality independently:

- recall@k, precision@k, MRR, and nDCG;
- page/section citation accuracy;
- material-criterion coverage;
- `met / not_met / unknown` accuracy;
- unsupported-conclusion rate;
- unknown-state recall when critical evidence is absent;
- stale-index and source-change detection.

Compare the local embedding model with BM25 and stronger embedding alternatives
on a labeled policy-query set before choosing a production design.
