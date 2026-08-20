# DataMesh Warden

An asynchronous, event-driven agent fleet that detects BigQuery/Cloud SQL schema
drift, data quality anomalies, and broken pipeline jobs; orchestrates three
specialized sub-agents to diagnose and test safe SQL/DDL patches in a sandbox;
and streams the reasoning and visual diffs to a Streamlit **Incident War Room**
dashboard for one-click human-in-the-loop approval.

See [`docs/architecture.md`](docs/architecture.md) for the full architectural
blueprint (data flow, state schema, tool specs, orchestrator design, UI plan,
and file-by-file build order).

## Tech stack

- **Runtime:** Python 3.11+, strict typing, Pydantic v2
- **AI orchestration:** `google-genai` (Gemini 3 Pro for master reasoning)
- **Sub-agents:** Gemma on Cloud Run (log triage) + Gemini 3 Flash (patch
  generation & sandbox validation)
- **State/memory:** Firestore, with an `InMemoryStateManager` fallback for
  offline local dev
- **Data target:** BigQuery (dry-run + sandbox table cloning)
- **Frontend:** Dark-themed Streamlit dashboard

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

## Deploying to Cloud Run

Requires `gcloud` authenticated (`gcloud auth login` +
`gcloud auth application-default login`), a GCP project with billing enabled,
and the following APIs enabled:

```
run.googleapis.com
firestore.googleapis.com
bigquery.googleapis.com
aiplatform.googleapis.com
eventarc.googleapis.com
cloudbuild.googleapis.com
artifactregistry.googleapis.com
```

See `deploy/cloudbuild.yaml` and `deploy/cloudrun-*.yaml` (added in Phase 6).

## Project layout

```
app/            # Orchestrator, sub-agent tools, models, persistence, API
ui/             # Streamlit "Incident War Room" dashboard
tests/          # pytest suite
deploy/         # Cloud Build / Cloud Run / Eventarc configs
docs/           # Architecture blueprint, demo script
scripts/        # Dev helper scripts
```
