# Local operations and troubleshooting

## Prerequisites

- Windows PowerShell or an equivalent shell.
- Python 3.11 or newer.
- A Gemini API key for live mode.
- The approved sample PNG, JPEG, or WebP files.

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\.venv\Scripts\python.exe scripts\build_guideline_index.py
```

Edit `.env` and place the real API key only there. `.env` is ignored by Git.
Never place a real key in `.env.example`, source code, screenshots, logs, or
presentation material.

## Provider modes

### Live Gemini

```dotenv
GOOGLE_API_KEY=your-google-api-key
ADK_MODEL=gemini-3.5-flash-lite
EXTRACTION_MODEL=gemini-3.5-flash-lite
EXTRACTION_PROVIDER=gemini
```

`auto` behaves the same as `gemini` when a key is present.

Model availability is account- and time-dependent. Treat the model names as
configuration, not hard-coded business logic. Validate them against the account
before the presentation.

### Offline fixture

```dotenv
EXTRACTION_PROVIDER=fixture
```

The fixture recognizes `med2.webp` and `Prior-Authorization-Form.jpg`. The UI
shows a rehearsal notice. Do not describe fixture output as a live model result.

## Start and stop

Start:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/`.

Stop with `Ctrl+C` in the server terminal. In-process background jobs do not
survive a server restart.

## Health and configuration checks

```powershell
curl.exe -sS http://127.0.0.1:8000/api/health
curl.exe -sS http://127.0.0.1:8000/api/config
curl.exe -sS http://127.0.0.1:8000/api/knowledge/status
```

`/api/config` is intentionally non-secret. Confirm provider and model names
without inspecting or displaying `.env`.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --check app\static\app.js
```

The automated suite uses fixture mode for the end-to-end API contract, even when
a developer has configured a live key.

## Data locations

| Path | Contents |
|---|---|
| `data/review.db` | jobs, OCR/extraction/grounding JSON, events, corrections |
| `data/adk_sessions.db` | ADK session persistence |
| `data/uploads/<job-id>/` | uploaded source images |
| `knowledge/source/BariatricSurgery.pdf` | versioned guideline source |
| `knowledge/index/` | generated FAISS index and page-cited chunk metadata |

These paths are ignored by Git. They may still contain sensitive information on
disk. Use only approved case-study files in the prototype and apply an explicit
retention/deletion procedure in any shared environment.

The Reset/New intake UI action clears browser state but does not delete these
records.

## Common failures

### `429 RESOURCE_EXHAUSTED`

This indicates a project quota or rate-limit condition. Check requests per
minute, tokens per minute, daily limits, billing tier, and project association.
Use exponential backoff and reduce request volume.

### `503 UNAVAILABLE` or high demand

This indicates temporary provider capacity rather than a quota limit. The SDK
retries, and the orchestrator has a bounded recovery path when the ADK planner
fails. Persistent 503s can still make the job terminal. Rehearse with a saved
live job URL or visibly labeled fixture mode.

### Model `404 NOT_FOUND`

The model may be listed generally but unavailable to new users or to the current
account. Query the account model list or follow the replacement named by the API,
then update both `ADK_MODEL` and `EXTRACTION_MODEL` and restart the process.

### Job remains `running`

The process may have restarted while its background task was active. Create a
new intake. A production implementation should use a durable queue, heartbeat,
lease timeout, and startup reconciliation.

### Upload button does not accept a file

Check:

- extension is PNG/JPEG/WebP;
- file is not empty or corrupt;
- file is within `MAX_UPLOAD_MB`;
- image is below the pixel limit;
- no more than eight documents are selected.

Selection is additive. Use the × button on a file chip or Clear before starting.

### Knowledge index is not ready

Run `.\.venv\Scripts\python.exe scripts\build_guideline_index.py --force` and
check `/api/knowledge/status`. The first build downloads the configured local
embedding model; later searches run from local model/index files. Rebuild after
changing the PDF or `EMBEDDING_MODEL`.

### Reset button visibility

The top-header Reset intake control is intentionally hidden before any job is
active. It becomes visible after upload begins or when a persisted `?job=` URL is
restored. A second New intake control is always present inside an active
workspace. Reset clears selection, progress, results, events, and the URL query,
then unlocks upload.

### Job URL does not restore

Ensure the server is using the same `APP_DB_PATH` that created the job and that
the job still exists. An invalid ID is removed from the URL and reported through
the UI toast.

## Logging and observability

The prototype currently provides:

- Uvicorn request and exception logs;
- persisted domain events;
- live SSE rendering;
- job status and error text.

Before production, add structured logs, request/job correlation IDs, model
latency and error metrics, retry counters, PHI-safe redaction, alerting, and an
audited access policy. Never log API keys or full medical documents by default.

## Dependency and model upgrades

After changing Google ADK, `google-genai`, or model versions:

1. Run the complete automated test suite.
2. Verify tool declarations and structured outputs.
3. Test both printed and handwritten samples.
4. Re-test sync worker-thread transport versus the async SDK path.
5. Exercise planner, OCR, NER, and criteria-assessment capacity failures.
6. Rebuild the index and verify page citations after changing the source PDF.
7. Confirm event payloads and UI restore behavior.
8. Re-run the golden evaluation set before changing production defaults.
