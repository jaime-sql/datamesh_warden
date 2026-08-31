# DataMesh Warden

An asynchronous agent fleet that diagnoses data-pipeline incidents, tests
SQL/DDL patches in a BigQuery sandbox, and waits for a human before any
production write. Reasoning and diffs stream to a Streamlit **Incident War
Room** for one-click Approve or Reject.

Built for the All Things Agentic Hackathon (**Fortified Enterprise Fleet**).

- **Hosted War Room:** https://warden-ui-3ec2w24tiq-uc.a.run.app
- **Architecture diagram:** [`docs/architecture.png`](docs/architecture.png)
- **Companion pipeline:** [datamesh_pipeline](https://github.com/jaime-sql/datamesh_pipeline)
  — hourly Neon Postgres → BigQuery Cloud Run Job (`pg-to-bq-sync`)

See [`docs/architecture.md`](docs/architecture.md) for the full blueprint
(data flow, state schema, tool specs, orchestrator design, UI plan).

## Tech stack

Hackathon requirements this repo is built to satisfy:

| Requirement | What Warden uses |
|---|---|
| Gemini 3.5 or newer (Gemini API or Vertex AI) | **Gemini 3.5 Flash** (`gemini-3.5-flash`) for patch generation and governance, via Vertex AI (`global`) |
| Google agent framework | **Google GenAI SDK** (`google-genai`) |
| Google Cloud infrastructure | Cloud Run (`warden-api`, `warden-ui`), Firestore, BigQuery |
| Bonus | **Gemma 2** (`gemma-2-9b-it`) for log triage |

Other pieces:

- **Orchestrator:** Gemini 3.1 Pro (`gemini-3.1-pro-preview`)
- **Runtime:** Python 3.11+, Pydantic v2, FastAPI
- **State/memory:** Firestore, with an `InMemoryStateManager` fallback for offline local dev
- **Data plane:** BigQuery sandbox clones for auto-patch; Neon Postgres is the live ELT source (diagnose-only — the sandbox does not write to Postgres)
- **Frontend:** Dark-themed Streamlit War Room

## Prerequisites

| Tool | Required for |
|---|---|
| Python 3.11+ | Everything |
| Docker | Building/running Cloud Run container images |
| Git | Version control |
| `gcloud` CLI | Cloud deploys, ADC auth, enabling GCP APIs |
| Java 8+ | Firestore emulator (optional, local dev only) |

Local development (`WARDEN_MODE=local`) does **not** require `gcloud` auth or
a live GCP project — it runs entirely against an in-memory state manager and
stubbed/mocked tools.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
```

Edit `.env` and fill in values as needed. Leave `WARDEN_MODE=local` to develop
without any GCP credentials.

`pyproject.toml` (not a separate `requirements.txt`) is the source of truth
for dependencies. If a fresh `pip install` ever resolves a newer, broken
transitive dependency, restore the exact known-working versions instead:

```powershell
pip install -r requirements-lock.txt
pip install -e . --no-deps
```

## Running locally

```powershell
# Terminal 1
make run-api

# Terminal 2
make run-ui
```

(If you don't have `make` on Windows, run the two commands inside the
`Makefile` targets directly, or install `make` via `choco install make` /
use it from a Git Bash / WSL shell.)

## Testing

```powershell
make test
make lint
make typecheck
```

## HTTP API

Once `make run-api` is running, the FastAPI service (`app/api/main.py`,
interactive docs at `/docs`) exposes:

| Route | Purpose |
|---|---|
| `POST /events/ingest` | Open a new incident and kick off the orchestrator in the background. Returns `202` immediately. |
| `GET /incidents/{id}` | Full incident state. |
| `GET /incidents/{id}/steps` \| `/findings` \| `/patches` \| `/audits` | Everything the orchestrator/sub-agents produced for that incident. |
| `POST /incidents/{id}/execute` | Human approval: re-validates governance and runs the patch for real. |
| `POST /incidents/{id}/reject` | Human rejection. |
| `GET /status` | Liveness/readiness probe. |

Example (local mode, no GCP needed):

```powershell
curl -X POST http://localhost:8080/events/ingest `
  -H "Content-Type: application/json" `
  -d '{"source":"manual_demo","resource_uri":"bq://proj.ds.orders","severity":"P1","raw_event":{"scenario":"schema_drift","table":"orders","dropped_column":"email"}}'
```

## Streamlit UI

Once both `make run-api` and `make run-ui` are running, open
`http://localhost:8501` for the Incident War Room:

- **Sidebar** — a view switcher (War Room / Incident History), one-click
  preset incidents (schema drift, data quality anomaly, broken pipeline
  job, slow copy job), a "🔌 Check pipeline health" button that checks a
  *real* external pipeline (see below) and opens a real incident if it
  actually failed, a custom event form, and a box to load an existing
  incident by ID.
- **Incident History view** — every incident ever ingested, regardless of
  outcome, with status-count metrics (resolved / rejected / failed / in
  progress) and a table you can open any row from.
- **Timeline / Diagnosis / Patch Diff / Governance tabs** (War Room) —
  everything the orchestrator and sub-agents produced for the selected
  incident.
- **Approve & execute / Reject** — shown once the incident reaches
  `AWAITING_APPROVAL`; Approve is disabled if the latest governance
  verdict is `BLOCK`.

### Real pipeline integration

Alongside the four sidebar presets, Warden can also open a real incident
from a genuine external pipeline:
[datamesh_pipeline](https://github.com/jaime-sql/datamesh_pipeline) runs
a Cloud Run Job + Cloud Scheduler ELT copy from Neon Postgres into
BigQuery hourly. Clicking "🔌 Check pipeline health" (or `POST
/pipelines/{job_name}/check`) checks that job's latest execution and, if
it actually failed, opens a real incident with the genuine error pulled
from Cloud Logging -- same orchestrator loop, same War Room, real
evidence. Auto-patch and Approve apply only to `bq://` resources (sandbox
clone first). A `postgres://` failure is diagnosed and left for a human.
See `docs/architecture.md`'s "Post-Phase-6 addition: connecting Warden to
a real external pipeline" for the full design.

The UI never talks to Firestore/BigQuery/Gemini directly -- it only calls
the HTTP API above (`ui/api_client.py`), polling via
`streamlit-autorefresh` while an incident is in flight. That means it
works identically against `WARDEN_MODE=local` or `WARDEN_MODE=cloud`, and
needs no GCP credentials of its own; point `WARDEN_API_BASE_URL` at
whichever API instance you want to drive (defaults to
`http://localhost:8080`).

## Running in Docker

```powershell
make docker-build
make docker-run
```

Builds and runs the API service container (`Dockerfile`) locally. Defaults
to `WARDEN_MODE=local`, so no GCP project or credentials are required --
pass `-e` flags (or `--env-file .env`) to run it against real Gemini/BigQuery/Firestore.

```powershell
make docker-build-ui
make docker-run-ui
```

Builds and runs the Streamlit UI container (`Dockerfile.ui`) separately, so
it can scale/deploy independently of the API. It points at
`http://host.docker.internal:8080` by default -- override
`WARDEN_API_BASE_URL` if the API container is exposed elsewhere.

## Deploying to Cloud Run

Requires `gcloud` authenticated (`gcloud auth login`), a GCP project with
billing enabled, and Firestore/a BigQuery dataset/Vertex AI already set up
(see `docs/architecture.md`'s GCP setup walkthrough -- this deploy step
only adds the Cloud Run/Build/Artifact Registry layer on top).

```powershell
make deploy
# or directly, with explicit params:
./scripts/deploy.ps1 -ProjectId <your-project> -Region us-central1
```

This builds + pushes both images via `deploy/cloudbuild.yaml`, then deploys
two Cloud Run services:

- **`warden-api`** -- private (`--no-allow-unauthenticated`); only
  `warden-ui`'s service account can call it.
- **`warden-ui`** -- public; has no GCP data permissions of its own, and
  authenticates its calls to `warden-api` with a Cloud Run identity token
  (see `ui/api_client.py::fetch_cloud_run_id_token`).

See `docs/architecture.md`'s Phase 6 note for why both services need
`--no-cpu-throttling`, and why the Eventarc auto-trigger from the original
sketch was deliberately deferred in favor of the UI's manual/preset
triggers. Tear everything this script created back down with:

```powershell
make teardown
```

Walk through a live demo with `docs/demo_script.md`.

## Project layout

```
app/            # Orchestrator, sub-agent tools, models, persistence, API
ui/             # Streamlit "Incident War Room" dashboard
tests/          # pytest suite
deploy/         # Cloud Build config
docs/           # Architecture blueprint, demo script
scripts/        # deploy.ps1 / teardown.ps1
```
