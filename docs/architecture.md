# DataMesh Warden — Architecture Blueprint

This document is the canonical architectural reference for the project. It
mirrors the plan agreed on with the team and drives the phased build order
below. Implementation should follow this spec; if reality diverges, update
this file in the same PR.

## 1. System architecture & data flow

```
                                  ┌───────────────────────────────────────────┐
                                  │  EVENT SOURCES (async, push-based)        │
                                  │  • BigQuery audit-log Pub/Sub topic        │
                                  │  • Cloud SQL insights alerts (Eventarc)    │
                                  │  • Cloud Scheduler synthetic drift probes  │
                                  └───────────────────────┬────────────────────┘
                                                          │ CloudEvent (JSON)
                                                          ▼
┌──────────────────────────────────────────────────────────────────────────────────────┐
│  Cloud Run Service: warden-orchestrator  (FastAPI + asyncio, min-instances=1)        │
│  ┌────────────────────────────────────────────────────────────────────────────────┐  │
│  │  POST /events/ingest  →  IncidentIngestor                                     │  │
│  │     1. Validate CloudEvent → build IncidentState (Pydantic v2)                │  │
│  │     2. Firestore: incidents/{incident_id} = IncidentState.model_dump()        │  │
│  │     3. Enqueue asyncio.Task → WardenOrchestrator.run(incident_id)             │  │
│  │     4. Return 202 Accepted (does NOT block on reasoning)                      │  │
│  └────────────────────────────────────────────────────────────────────────────────┘  │
│                                                                                      │
│  ┌────────────────────────────────────────────────────────────────────────────────┐  │
│  │  WardenOrchestrator  (Gemini 3 Pro — gemini-3.1-pro-preview)                   │  │
│  │  • google-genai async client, function-calling harness                        │  │
│  │  • Registered tools: investigate_incident_logs,                               │  │
│  │                     generate_and_test_patch,                                  │  │
│  │                     verify_governance_policy                                  │  │
│  │  • Multi-turn loop: max 8 turns, per-turn timeout 90s, global 240s            │  │
│  │  • Every model turn & tool call → AgentStepLog (Firestore subcollection)      │  │
│  └───────┬────────────────┬──────────────────────────┬─────────────────────────┬─┘  │
│          │ tool_call      │ tool_call                │ tool_call               │    │
│          ▼                ▼                          ▼                         │    │
│  ┌───────────────┐  ┌──────────────────────┐  ┌────────────────────────┐       │    │
│  │ Sub-Agent 1   │  │ Sub-Agent 2          │  │ Sub-Agent 3            │       │    │
│  │ Gemma-on-CR   │  │ Gemini 3 Flash       │  │ Gemini 3 Flash + rules │       │    │
│  │ (log triage)  │  │ (SQL patch + sandbox)│  │ (governance policy)    │       │    │
│  │               │  │  → BQ dry-run        │  │  → IAM/DLP/tag check   │       │    │
│  │ returns       │  │  → clone-to-sandbox  │  │  returns               │       │    │
│  │ DiagnosticFdg │  │  returns SQLPatch    │  │  GovernanceAudit       │       │    │
│  └───────────────┘  └──────────────────────┘  └────────────────────────┘       │    │
└──────────────────────────────────────────────────────────────────────────────────────┘
                                                          │
                                                          │  Firestore writes (streamed)
                                                          ▼
                        ┌───────────────────────────────────────────────────────┐
                        │  Firestore  (source of truth + fan-out bus)           │
                        │  /incidents/{id}                     ← IncidentState  │
                        │  /incidents/{id}/steps/{n}           ← AgentStepLog   │
                        │  /incidents/{id}/findings/{fid}      ← DiagnosticFdg  │
                        │  /incidents/{id}/patches/{pid}       ← SQLPatchPayld  │
                        │  /incidents/{id}/audits/{aid}        ← GovernanceAud  │
                        └────────────────────────────┬──────────────────────────┘
                                                     │ on_snapshot listener
                                                     ▼
                        ┌───────────────────────────────────────────────────────┐
                        │  Streamlit "Incident War Room"  (Cloud Run, port 8080)│
                        │  • Sidebar: preset incident triggers (POST /events)   │
                        │  • Live timeline of AgentStepLog (spinners + JSON)    │
                        │  • Visual DDL diff (before / after)                   │
                        │  • Governance verdict badge                           │
                        │  • [Approve & Execute] / [Reject]                     │
                        └────────────────────────────┬──────────────────────────┘
                                                     │ approve → POST /incidents/{id}/execute
                                                     ▼
                        ┌───────────────────────────────────────────────────────┐
                        │  RemediationExecutor  (same Cloud Run service)        │
                        │  • Re-validates GovernanceAudit.verdict == "PASS"     │
                        │  • Runs SQLPatchPayload.production_sql via BQ client  │
                        │  • Writes final AgentStepLog(status="EXECUTED")       │
                        │  • Updates IncidentState.status = "RESOLVED"          │
                        └───────────────────────────────────────────────────────┘
```

Key async guarantees:

- Ingestion endpoint returns in <150 ms; orchestration runs as a detached
  `asyncio.Task`.
- Streamlit never calls Gemini directly — it only reads Firestore, decoupling
  UI latency from model latency.
- Firestore acts as both persistence *and* pub/sub bus (via `on_snapshot`),
  removing the need for a separate broker.

## 2. State & memory schema

Firestore document tree:

```
incidents/{incident_id}                          ← IncidentState
    steps/{step_id_zero_padded}                  ← AgentStepLog       (append-only)
    findings/{finding_id}                        ← DiagnosticFinding  (one per Sub-Agent 1 call)
    patches/{patch_id}                           ← SQLPatchPayload    (one per Sub-Agent 2 call)
    audits/{audit_id}                            ← GovernanceAudit    (one per Sub-Agent 3 call)
```

Pydantic models (`app/models/state.py`), all with
`model_config = ConfigDict(extra="forbid")`:

### `IncidentState`

| Field | Type |
|---|---|
| `incident_id` | `str` (ULID) |
| `source` | `Literal["bigquery_audit","cloudsql_alert","synthetic_probe","manual_demo"]` |
| `resource_uri` | `str` |
| `severity` | `Literal["P1","P2","P3"]` |
| `raw_event` | `dict[str, Any]` |
| `status` | `Literal["INGESTED","DIAGNOSING","PATCHING","AWAITING_APPROVAL","EXECUTING","RESOLVED","REJECTED","FAILED"]` |
| `active_step_id` | `str \| None` |
| `summary` | `str \| None` |
| `created_at` / `updated_at` | `datetime` |
| `orchestrator_model` | `str` |
| `turn_count` | `int` |
| `error` | `str \| None` |

### `AgentStepLog`

| Field | Type |
|---|---|
| `step_id` | `str` (zero-padded, e.g. `"0001"`) |
| `parent_incident_id` | `str` |
| `kind` | `Literal["MODEL_TURN","TOOL_CALL","TOOL_RESULT","USER_DECISION","EXECUTION"]` |
| `actor` | `Literal["orchestrator","sub_agent_1_gemma","sub_agent_2_flash","sub_agent_3_flash","human","executor"]` |
| `tool_name` | `str \| None` |
| `tool_args` | `dict[str, Any] \| None` |
| `tool_result_ref` | `str \| None` |
| `content_markdown` | `str \| None` |
| `latency_ms` | `int \| None` |
| `status` | `Literal["RUNNING","OK","ERROR","TIMEOUT"]` |
| `error_detail` | `str \| None` |
| `started_at` / `finished_at` | `datetime` / `datetime \| None` |

### `DiagnosticFinding`

| Field | Type |
|---|---|
| `finding_id` | `str` (ULID) |
| `hypothesis` | `str` |
| `evidence` | `list[EvidenceItem]` |
| `drift_type` | `Literal["SCHEMA_DRIFT","DATA_QUALITY","BROKEN_JOB","PERFORMANCE_DEGRADATION","PERMISSION","UNKNOWN"]` |
| `affected_columns` | `list[str]` |
| `confidence` | `float` (0.0–1.0) |
| `triage_model` | `str` |

`EvidenceItem`: `{ source: str, log_line: str, timestamp: datetime, severity: Literal["INFO","WARN","ERROR"] }`

### `SQLPatchPayload`

| Field | Type |
|---|---|
| `patch_id` | `str` (ULID) |
| `linked_finding_id` | `str` |
| `patch_kind` | `Literal["DDL","DML","BOTH"]` |
| `sandbox_sql` / `production_sql` | `str` |
| `before_schema` / `after_schema` | `list[ColumnSpec]` |
| `dry_run_bytes_processed` | `int` |
| `sandbox_execution_ms` | `int` |
| `sandbox_row_delta` | `int` |
| `validation_status` | `Literal["SANDBOX_PASS","SANDBOX_FAIL","DRY_RUN_ONLY"]` |
| `validation_errors` | `list[str]` |
| `patcher_model` | `str` |

`ColumnSpec`: `{ name: str, type: str, mode: Literal["NULLABLE","REQUIRED","REPEATED"], description: str | None }`

### `GovernanceAudit`

| Field | Type |
|---|---|
| `audit_id` | `str` (ULID) |
| `linked_patch_id` | `str` |
| `verdict` | `Literal["PASS","BLOCK","WARN"]` |
| `policy_checks` | `list[PolicyCheck]` |
| `pii_columns_touched` | `list[str]` |
| `requires_human_approval` | `bool` |
| `rationale` | `str` |

`PolicyCheck`: `{ policy_id: str, description: str, result: Literal["PASS","FAIL","WARN"], detail: str }`

### Persistence contract (`app/persistence/base.py`)

```python
class StateManager(Protocol):
    async def create_incident(self, state: IncidentState) -> None: ...
    async def update_incident(self, incident_id: str, **patch) -> IncidentState: ...
    async def get_incident(self, incident_id: str) -> IncidentState: ...
    async def append_step(self, incident_id: str, log: AgentStepLog) -> None: ...
    async def write_finding(self, incident_id: str, finding: DiagnosticFinding) -> None: ...
    async def write_patch(self, incident_id: str, patch: SQLPatchPayload) -> None: ...
    async def get_patch(self, incident_id: str, patch_id: str) -> SQLPatchPayload: ...
    async def write_audit(self, incident_id: str, audit: GovernanceAudit) -> None: ...
    async def stream_steps(self, incident_id: str) -> AsyncIterator[AgentStepLog]: ...
    async def list_findings(self, incident_id: str) -> list[DiagnosticFinding]: ...
    async def list_patches(self, incident_id: str) -> list[SQLPatchPayload]: ...
    async def list_audits(self, incident_id: str) -> list[GovernanceAudit]: ...
    async def list_steps(self, incident_id: str) -> list[AgentStepLog]: ...
```

Two implementations: `FirestoreStateManager` (prod) and `InMemoryStateManager`
(offline dev, backed by `asyncio.Queue` for `stream_steps`). The
`list_findings`/`list_patches`/`list_audits`/`list_steps` methods were added
in Phase 4 to give the HTTP surface (below) a backend-agnostic way to read
an incident's full sub-resource history; on Firestore they're ordered by
document id (`"__name__"`), which sorts chronologically for free since
finding/patch/audit ids are ULIDs.

## 3. Sub-agent tool interfaces

All async, defined in `app/agents/tools/`, registered via
`app/agents/tool_registry.py`.

### `investigate_incident_logs`

```python
async def investigate_incident_logs(
    incident_id: str,
    resource_uri: str,
    lookback_minutes: int = 60,
    max_log_lines: int = 500,
) -> dict:
    """Triage raw Cloud Logging / BigQuery INFORMATION_SCHEMA entries for the
    referenced resource and produce a structured root-cause hypothesis.

    Delegates to a Gemma-2 model hosted on a separate Cloud Run service
    (URL from env WARDEN_GEMMA_ENDPOINT). Gemma is cheap and good at
    high-volume log summarization; the orchestrator (Gemini 3 Pro) then
    reasons over the compact JSON returned here.

    Returns:
        {
          "finding_id": "01J...",
          "hypothesis": "Column `email` dropped by upstream DAG at 14:03Z",
          "drift_type": "SCHEMA_DRIFT",
          "affected_columns": ["email"],
          "confidence": 0.86,
          "evidence_count": 7,
          "firestore_path": "incidents/{id}/findings/{finding_id}"
        }
    """
```

Writes a full `DiagnosticFinding` to Firestore before returning. On timeout
(>30s) returns `{"error": "TIMEOUT", "finding_id": null, ...}`.

### `generate_and_test_patch`

```python
async def generate_and_test_patch(
    incident_id: str,
    finding_id: str,
    target_resource_uri: str,
    drift_summary: str,
    allow_destructive: bool = False,
) -> dict:
    """Generate a candidate SQL/DDL patch with Gemini 2.5 Flash, then
    validate it in an isolated BigQuery sandbox by (a) cloning the target
    table via `CREATE TABLE ... CLONE`, (b) running the patch against the
    clone, and (c) executing a BigQuery dry-run on the production-rewritten
    statement to capture bytes-processed.

    Returns:
        {
          "patch_id": "01J...",
          "patch_kind": "DDL",
          "validation_status": "SANDBOX_PASS",
          "dry_run_bytes_processed": 128394,
          "sandbox_execution_ms": 812,
          "sandbox_row_delta": 0,
          "before_schema_hash": "sha256:...",
          "after_schema_hash": "sha256:...",
          "firestore_path": "incidents/{id}/patches/{patch_id}"
        }
    """
```

Sandbox dataset convention: `warden_sandbox_{incident_id_lower}`, 24h
expiration. Rejects DROP/TRUNCATE unless `allow_destructive=True`. Retries
once on transient `ServiceUnavailable`.

### `verify_governance_policy`

```python
async def verify_governance_policy(
    incident_id: str,
    patch_id: str,
    dataset_id: str,
) -> dict:
    """Run rule-based + LLM-augmented governance checks on a proposed patch:
      • Blocks DROP COLUMN on tables tagged `pii=true`.
      • Requires `data_steward` IAM role bound if `sensitivity=high`.
      • Flags DML touching >10% of rows without a WHERE clause.
      • Cross-checks column names against a Data Catalog / DLP taxonomy.

    Deterministic rules run first (~200ms); Gemini 2.5 Flash is invoked only
    to write a plain-English rationale when a rule is WARN.

    Returns:
        {
          "audit_id": "01J...",
          "verdict": "PASS" | "WARN" | "BLOCK",
          "requires_human_approval": true,
          "policy_checks_passed": 6,
          "policy_checks_failed": 0,
          "pii_columns_touched": [],
          "rationale": "No PII columns are affected; ...",
          "firestore_path": "incidents/{id}/audits/{audit_id}"
        }
    """
```

`verdict == "BLOCK"` short-circuits the orchestrator — it must not propose
execution.

## 4. Agent harness & execution loop

`app/agents/orchestrator.py` — `WardenOrchestrator.run(incident_id)`. Never
raises; failures are captured as `IncidentState.status = "FAILED"` + `error`.

Loop:

1. Load `IncidentState`; set `status=DIAGNOSING`.
2. Build initial conversation with `SYSTEM_PROMPT` + `raw_event`.
3. For each turn (max `WARDEN_MAX_TURNS`, default 8):
   - Call Gemini with `tools=TOOLS`, automatic function calling disabled,
     wrapped in `asyncio.wait_for(WARDEN_TURN_TIMEOUT_S)`.
   - Persist reasoning to `AgentStepLog`.
   - If no function calls → treat as final summary, set
     `status=AWAITING_APPROVAL`, break.
   - Else, for each function call: append `TOOL_CALL` step, invoke
     `TOOL_IMPL[name](**args)` under `asyncio.wait_for(WARDEN_TOOL_TIMEOUT_S)`,
     append `TOOL_RESULT` step, feed `FunctionResponse` back into contents.
   - If any tool result has `verdict == "BLOCK"` → `status=REJECTED`, break.
4. If turn budget exhausted → `status=FAILED`, `error="turn_budget_exhausted"`.
5. Whole run wrapped in `asyncio.wait_for(WARDEN_INCIDENT_TIMEOUT_S)`.

Retry policy (`app/agents/retry.py`, `tenacity`-based): retries on
`ServiceUnavailable`, `DeadlineExceeded`, `ResourceExhausted`,
`aiohttp.ClientError`; exponential backoff base=1s, max=8s, 3 attempts; each
retry logs an `AgentStepLog(status="ERROR")`.

Timeout matrix:

| Boundary | Value |
|---|---|
| Ingest → 202 response | 150ms hard |
| Per model turn | 90s |
| Per tool call | 60s |
| Whole incident | 240s |
| Human approval window | none (persisted state, no timeout) |

Non-blocking guarantees: single global async `google.genai.Client`; sync
BigQuery client wrapped with `asyncio.to_thread`; Firestore via
`AsyncClient`; Streamlit is a fully separate process that only reads
Firestore.

### HTTP surface (`app/api/*`, Phase 4)

FastAPI app (`app.api.main:app`), included router in `app/api/routes.py`:

| Route | Behavior |
|---|---|
| `POST /events/ingest` | Body is a simplified event payload (`source`, `resource_uri`, `severity`, `raw_event` -- not a full CNCF CloudEvents envelope, since every source here is either a synthetic probe or a demo trigger). Creates the `IncidentState`, fires `WardenOrchestrator.run()` as a tracked background `asyncio.Task` (see `app/api/deps.py::run_in_background`), returns `202` immediately with `{incident_id, status}`. |
| `GET /incidents/{id}` | Full `IncidentState`. `404` if unknown. |
| `GET /incidents/{id}/steps` \| `/findings` \| `/patches` \| `/audits` | List the corresponding sub-resource via the `StateManager.list_*` methods. `404` if the incident itself is unknown (each returns `[]`, not `404`, once the incident exists but has no items yet). |
| `POST /incidents/{id}/execute` | Body optionally `{"patch_id": "..."}` (defaults to the incident's most recently written patch). Delegates to `app.agents.executor.execute_incident`; `409` if the incident isn't `AWAITING_APPROVAL`, has no patch, has no governance audit, or the audit verdict is `BLOCK`. |
| `POST /incidents/{id}/reject` | Body optionally `{"reason": "..."}`. Delegates to `execute.reject_incident`; same `409` precondition as execute. |
| `GET /status` | Cloud Run liveness/readiness probe. Deliberately not `/healthz` -- see the Phase 6 note. |

`app/agents/executor.py` (`RemediationExecutor`) is the code actually
reached by `execute`: it re-validates governance fresh from the
`StateManager` (never trusting client-supplied state beyond an optional
`patch_id`), runs the patch's `production_sql` via a local/cloud backend
pair mirroring Phase 2's pattern (`LocalHeuristicExecutorBackend` /
`BigQueryExecutorBackend`, selected by `Settings.warden_mode`), logs an
`EXECUTION` step, and sets `IncidentState.status = "RESOLVED"` (or
`"FAILED"` if the execution itself throws).

Domain exceptions (`IncidentNotFoundError`, `PatchNotFoundError`,
`IncidentNotAwaitingApprovalError`, `NoPatchAvailableError`,
`NoGovernanceAuditError`, `GovernanceBlockError`) are mapped to HTTP status
codes by exception handlers registered in `app/api/main.py`, rather than
per-route `try`/`except`, so every route body only has to describe its
happy path.

## 5. Streamlit UI layout & demo flow

Layout: sidebar with preset incident triggers + custom event JSON; main area
with four tabs — Timeline, Diagnosis, Patch Diff, Governance; a decision
footer below the tabs with Approve/Reject buttons gated on
`status == AWAITING_APPROVAL` and `verdict != BLOCK`.

Data flow (see Phase 5 implementation note below for why this differs from
the Firestore-listener design originally sketched here): the UI never
talks to Firestore/BigQuery/Gemini directly. It only calls the HTTP API
(`app/api/*`) via `ui/api_client.py`, and `streamlit-autorefresh` redraws
every ~1.5s while the incident is in an in-flight status
(`INGESTED`/`DIAGNOSING`/`PATCHING`/`EXECUTING`), re-fetching the incident
+ steps/findings/patches/audits on each rerun.

4-minute demo script:

| Time | Action | Narration |
|---|---|---|
| 0:00–0:20 | Open dashboard, dark theme reveal | "This is our async agent fleet's war room." |
| 0:20–0:40 | Click "Schema drift" preset | "One click fires a CloudEvent into Cloud Run." |
| 0:40–1:30 | Timeline streams; Diagnosis tab populates | "Gemma triages logs; Gemini 3 Pro orchestrates." |
| 1:30–2:30 | Patch Diff tab: column diff + sandbox stats | "Flash writes a DDL patch, validated in a sandbox." |
| 2:30–3:15 | Governance tab: verdict PASS, buttons enable | "Every patch is audited against org policies." |
| 3:15–3:45 | Click Approve → EXECUTING → RESOLVED | "Human-in-the-loop, single click." |
| 3:45–4:00 | Show Firestore console incident tree | "Full auditable trace." |

## 6. Phased file-by-file build order

- **Phase 0** — repo scaffolding: `pyproject.toml`, `README.md`,
  `.env.example`, `.streamlit/config.toml`, `.gitignore`, `Makefile`. *(done)*
- **Phase 1** — domain models & persistence: `app/models/*`,
  `app/persistence/*`, `app/config.py`, `app/logging.py`. *(done)*
- **Phase 2** — tools & sub-agents: `app/agents/tools/*`,
  `app/agents/tool_registry.py`, `app/agents/bq_sandbox.py`, `app/agents/prompts.py`,
  `app/agents/genai_client.py`. *(done -- see note below on local vs. cloud
  implementations)*
- **Phase 3** — orchestrator harness: `app/agents/retry.py`,
  `app/agents/orchestrator.py`. *(done -- `app/agents/executor.py` deferred
  to Phase 4, since executing an approved patch is really part of the HTTP
  surface's `/incidents/{id}/execute` handler; see note below.)*
- **Phase 4** — HTTP surface: `app/api/*`, `Dockerfile`. *(done -- also
  added `app/agents/executor.py`, deferred from Phase 3; see note below.)*
- **Phase 5** — Streamlit UI: `ui/*`, `Dockerfile.ui`. *(done -- see note
  below on the HTTP-polling data flow, a deliberate deviation from this
  doc's original Firestore-listener sketch.)*
- **Phase 6** — deployment & demo assets: `deploy/*`, `scripts/*`,
  `docs/demo_script.md`. *(done -- see note below on the private-API /
  public-UI security model and the deliberately-deferred Eventarc trigger.)*
- **Phase 7 (optional)** — local Gemma fallback, e2e tests, CI workflow.

Each phase is independently runnable/testable before moving to the next.

### Phase 2 implementation note: local vs. cloud backends

Every external dependency in Phase 2 (Gemma, Gemini, BigQuery, Data
Catalog) is behind a small `Protocol` with two implementations, mirroring
the `StateManager` / `InMemoryStateManager` pattern from Phase 1:

| Tool | Zero-resource local fallback | Real implementation (needs setup) |
|---|---|---|
| `investigate_incident_logs` | `LocalHeuristicTriageBackend` (reads `IncidentState.raw_event` scenario fields) | `GemmaHttpTriageBackend` (needs `WARDEN_GEMMA_ENDPOINT`, i.e. a deployed Gemma Cloud Run service) |
| `generate_and_test_patch` | `LocalHeuristicPatchGenerator` + `LocalHeuristicSandboxExecutor` | `GeminiPatchGenerator` (needs `GEMINI_API_KEY` or Vertex) + `BigQuerySandboxExecutor` (needs a real GCP project + BigQuery dataset) |
| `verify_governance_policy` | `LocalHeuristicMetadataProvider` + `TemplatedRationaleGenerator` | `BigQueryMetadataProvider` (needs labelled BigQuery dataset) + `GeminiRationaleGenerator` |

Selection is automatic based on `Settings` (`warden_mode`, `gemini_api_key`
/ `warden_use_vertex`, `warden_gemma_endpoint`) via a `get_*()` factory
function per tool -- no code changes needed to switch from local to cloud
once credentials/resources exist. This is what let Phase 2 be built and
fully unit-tested without creating any new GCP resources.

### Phase 3 implementation note: no new GCP resources needed either

`WardenOrchestrator` takes an optional `genai_client` constructor argument
(same shape as `genai.Client()` -- specifically the `.aio.models` surface).
When omitted it lazily resolves the real client via
`app.agents.genai_client.get_genai_client()` (only reachable once a real
Gemini call actually happens). Tests inject a small fake client scripted
with canned `types.GenerateContentResponse` objects (or exceptions, to
exercise retry/timeout paths), so the full multi-turn loop -- tool
dispatch, per-tool actor attribution, the governance `BLOCK` short-circuit,
turn-budget exhaustion, retries, and timeouts -- is exercised deterministically
in `tests/test_orchestrator_loop.py` with zero network calls and zero GCP
setup. A real `GEMINI_API_KEY` (or Vertex project) is only required to
actually run `WardenOrchestrator.run()` against live Gemini, same as noted
for Phase 2's cloud backends.

`app/agents/executor.py` (running an *approved* patch's `production_sql` for
real) was intentionally deferred to Phase 4: it needs to be re-entered from
`POST /incidents/{id}/execute`, and the exact lookup of "the patch + audit
this approval refers to" is naturally an HTTP-request-shaped concern rather
than an orchestrator-loop concern. It will reuse
`app.agents.bq_sandbox.run_sandbox_statement` for the real BigQuery path and
a `LocalHeuristicExecutor` fallback, mirroring the Phase 2 pattern.

### Phase 4 implementation note: no new GCP resources needed here either

`app/api/routes.py::ingest_event` depends on `get_orchestrator()`
(`app/api/deps.py`), a one-line factory FastAPI resolves per-request via
`Depends`. Tests override it with `app.dependency_overrides[get_orchestrator]`
to inject a stub whose `run()` never calls Gemini, so the full
ingest -> background-task -> state-transition flow is tested over real ASGI
(`httpx.AsyncClient` + `ASGITransport`, no running server) without any
credentials. `app/api/deps.py::drain_background_tasks` lets tests await the
fire-and-forget orchestrator task deterministically instead of
sleeping/polling for it.

`execute`/`reject` are tested the same way, seeding `InMemoryStateManager`
directly with an `AWAITING_APPROVAL` incident + patch + audit fixture.
`LocalHeuristicExecutorBackend` (the default outside `WARDEN_MODE=cloud`)
simulates a successful run with no BigQuery access, so `POST
.../execute`'s happy path is fully exercised offline too.

The `Dockerfile` was verified with a real local `docker build` +
`docker run` (hitting `/status` and `/events/ingest` against the
container) -- no GCP project needed for that either, since `WARDEN_MODE`
defaults to `local`. The ingested incident correctly finished `FAILED`
with a `GenAIConfigurationError` message (no `GEMINI_API_KEY` in that
throwaway container), which is exactly the graceful-failure behavior
Phase 3 designed for.

### Phase 5 implementation note: HTTP polling instead of Firestore listeners

Section 5 above originally sketched the UI attaching Firestore
`on_snapshot` listeners directly. That design assumed `WARDEN_MODE=cloud`
was already set up; it would have made the UI unusable (and untestable)
in `local` mode, and would have coupled `ui/*` to `google-cloud-firestore`
credentials. Since Phase 4 already built a complete read/write HTTP
surface over `IncidentState` and its subcollections, the UI instead talks
*only* to that API (`ui/api_client.py`, a small synchronous `httpx.Client`
wrapper -- synchronous because Streamlit's script-rerun model doesn't run
its own asyncio loop) and relies on `streamlit-autorefresh` for polling
instead of push updates. This keeps the UI backend-agnostic: it behaves
identically whether the API underneath is running `WARDEN_MODE=local` or
`WARDEN_MODE=cloud`, and it needs zero GCP credentials of its own -- the
same "defer GCP resources as long as possible" principle applied in every
prior phase.

`ui/streamlit_app.py` is a thin entrypoint; rendering is split into
`ui/views.py` (Streamlit-calling functions for the sidebar, header, four
tabs, and decision footer) and `ui/formatting.py` (pure, `streamlit`-free
helpers -- status/verdict badges, icons, and the `can_execute` gating
logic -- so the business logic has plain, fast unit tests independent of
any Streamlit runtime). `ui/presets.py` holds the three sidebar one-click
demo incidents, using the same `raw_event.scenario` convention the Phase 2
local heuristics understand, so the whole demo flow (ingest → diagnose →
patch → govern → approve → resolve) works end-to-end with `WARDEN_MODE=local`
and no credentials at all.

Testing uses three layers, all offline:

- `tests/test_ui_formatting.py` -- plain unit tests of the pure helpers.
- `tests/test_ui_api_client.py` -- `ui/api_client.py` against
  `httpx.MockTransport`, covering success, HTTP error, and
  connection-error paths.
- `tests/test_ui_app_smoke.py` -- Streamlit's own `AppTest` harness driving
  the *real* `ui/streamlit_app.py` script against a real `uvicorn` server
  started in a background thread within the test process (needed because
  `ui/api_client.py` uses a blocking `httpx.Client`, not an ASGI
  transport). The background orchestrator is stubbed via the same
  `get_orchestrator` dependency-override seam `tests/test_api.py` uses, so
  this exercises the full click-preset → poll-until-awaiting-approval →
  click-approve → resolved flow with no Gemini credentials and no GCP
  resources -- just a loopback socket.

`Dockerfile.ui` builds a separate, independently-deployable image (its own
Cloud Run service in Phase 6) that only needs `WARDEN_API_BASE_URL`
pointing at the API service; it was verified with a real local
`docker build` + `docker run`, confirming the container serves Streamlit's
`/_stcore/health` endpoint with no GCP project configured.

### Cloud mode validated against a real GCP project

`WARDEN_MODE=cloud` was exercised end-to-end against a real GCP project
(Firestore Native database, a labelled BigQuery dataset, Vertex AI for
Gemini) running the API locally. Two real findings came out of that:

1. **Sandbox dataset location mismatch.** `ensure_sandbox_dataset`
   (`app/agents/bq_sandbox.py`) created the per-incident sandbox dataset
   with no explicit `location`, which defaults to the `US` multi-region.
   If the target table's actual dataset lives in a specific region (e.g.
   `us-central1`), BigQuery can't run a `CREATE TABLE ... CLONE` across
   that location boundary and fails with a misleading "Dataset ... was
   not found in location" error -- nothing in the message mentions
   location at all. Fixed by adding `get_dataset_location()` and always
   creating the sandbox dataset in the *source* table's actual location,
   so this works regardless of which region a user's real data happens to
   live in.
2. **Tests must never read a developer's `.env`.** `Settings` loads
   `env_file=".env"` by default, so once a real `.env` was configured for
   `WARDEN_MODE=cloud` locally, the whole `pytest` suite silently started
   asserting against/attempting real GCP calls instead of the
   `InMemoryStateManager`/local-heuristic defaults it's supposed to run
   against. Fixed with a session-scoped autouse fixture in
   `tests/conftest.py` that disables `Settings.model_config["env_file"]`
   for the whole test session, so `get_settings()` only ever sees actual
   process environment variables (which individual tests still set via
   `monkeypatch.setenv`, exactly as before) plus hardcoded defaults --
   never whatever a developer happens to have in `.env`.

One more thing worth knowing if you set this up yourself: preview model
names (e.g. `gemini-3.1-pro-preview`) aren't necessarily enabled for
every project/region on Vertex AI yet, even though they work fine via the
AI Studio API key path. `WARDEN_ORCHESTRATOR_MODEL`/`WARDEN_PATCHER_MODEL`
/`WARDEN_GOVERNANCE_MODEL` may need to point at a GA model (`gemini-2.5-pro`,
`gemini-2.5-flash`) instead, depending on what's actually available in
your Vertex AI project -- there's no code change needed, just an `.env`
value, and it's worth a quick smoke test (`client.models.generate_content`)
before assuming a given model name works.

### Phase 6 implementation note: private API, public UI, and a deferred Eventarc trigger

Deploying two Cloud Run services (`Dockerfile` / `Dockerfile.ui`, already
built and verified in Phases 4-5) surfaced three decisions worth
recording:

**1. The API is private; the UI is public.** `warden-api` is deployed
with `--no-allow-unauthenticated` -- nobody can call `/events/ingest` (and
spend Gemini/BigQuery quota) directly from the internet. `warden-ui` is
public so it's demo-friendly, but its own Cloud Run service account has
*no* project-level data roles at all (consistent with the Phase 5
principle that the UI never touches Firestore/BigQuery/Gemini directly)
-- it's granted only `roles/run.invoker` on the `warden-api` service.
`ui/api_client.py::fetch_cloud_run_id_token` fetches a Google-signed
identity token from the metadata server (only when `K_SERVICE` is set,
i.e. actually running on Cloud Run, or `WARDEN_API_USE_ID_TOKEN=true` is
set explicitly) and attaches it as a `Bearer` token on every API call,
which Cloud Run's IAM layer validates automatically. Local dev is
unaffected -- `WardenApiClient`'s default `id_token_provider` is a no-op
outside Cloud Run.

**2. `--no-cpu-throttling` is required, not optional.** `/events/ingest`
returns `202` immediately and continues the orchestrator loop as a
fire-and-forget `asyncio.create_task` (`app/api/deps.py::run_in_background`).
Cloud Run's default "CPU only allocated during request processing" mode
would throttle that background task the moment the HTTP response is
sent, since from Cloud Run's perspective the request is already done.
Both services are deployed with `--no-cpu-throttling` (Cloud Run's
documented, supported pattern for exactly this "background work after
response" case) so the orchestrator's multi-turn loop actually gets to
run to completion.

**3. Eventarc was deliberately deferred.** The original architecture
sketch (top of this doc) shows BigQuery audit log / Cloud SQL insight
alerts arriving via Eventarc and automatically calling `/events/ingest`.
Wiring that up for real requires a genuine schema-change or
data-quality-alert source to trigger against, which is disproportionate
setup/fragility for a demo that already has a fully working manual
trigger path (the UI's presets, or a direct `curl`/`POST`). If you want
to add it later: create a Cloud Logging sink that filters for the
relevant BigQuery audit log entries (e.g. `protoPayload.methodName=
"google.cloud.bigquery.v2.TableService.PatchTable"`) into a Pub/Sub
topic, then an Eventarc trigger on that topic invoking `warden-api`'s
`/events/ingest` (as an authenticated Cloud Run push subscriber, same
`roles/run.invoker` pattern used for the UI above) with a small Cloud
Function or the API itself translating the Pub/Sub envelope into an
`IncidentIngestRequest`.

**4. `/healthz` is a reserved path -- use something else.** While
validating this deploy against a real project, `GET /healthz` on the
deployed `warden-api` consistently returned a generic Google-branded
404 page (not our app's own JSON 404), while every other path (`/`,
`/docs`, `/openapi.json`, `/events/ingest`) reached the container
correctly -- reproduced with the service both private and fully public,
ruling out IAM entirely. Google's edge (GFE) appears to intercept the
exact literal path `/healthz` on `*.run.app` domains for its own
purposes and never forwards it to the container. The fix was simply to
rename the liveness endpoint to `/status` (`app/api/main.py`) -- if you
add your own health check route, avoid the literal name `/healthz` on
Cloud Run.

**5. Two more real bugs, found by actually clicking through the deployed
demo in `WARDEN_MODE=cloud`:**

- The sidebar presets (`ui/presets.py`) hardcoded `bq://warden-demo...`
  as the resource URI -- `warden-demo` was always a placeholder project
  name, harmless in `WARDEN_MODE=local` (the heuristic backends never
  call BigQuery for real) but a real, confusing `404 ... Project
  warden-demo is not found` once the sub-agent tools started making real
  BigQuery calls against it. Fixed by building the URI from the actual
  configured `GOOGLE_CLOUD_PROJECT` at render time (`build_presets`);
  `scripts/deploy.ps1` now also sets `GOOGLE_CLOUD_PROJECT` on the
  `warden-ui` service so it can do this in production.
- Worse, when the model gave up after that tool error (an ordinary final
  text turn with no further tool calls), `WardenOrchestrator._run_loop`
  unconditionally moved the incident to `AWAITING_APPROVAL` -- with no
  patch and no governance audit ever produced. The UI's Approve button
  rendered as clickable (`ui/formatting.py::can_execute` treated "no
  audit at all" as "not BLOCK, so allow it"), and clicking it hit
  `RemediationExecutor`'s `NoPatchAvailableError` with a confusing
  "incident ... has no patch to execute" message -- exactly backwards
  from the intended design, where the executor's checks are supposed to
  be an unreachable defense-in-depth backstop, not the only thing
  standing between a human and an empty approval. Fixed on both sides:
  `WardenOrchestrator._validated_finish_status` now requires an actual
  patch *and* a non-`BLOCK` governance audit for it before allowing
  `AWAITING_APPROVAL` (`FAILED` with a clear `orchestrator_finished_
  without_patch` / `..._without_governance_audit` error otherwise), and
  `can_execute` now treats "no audit at all" as not executable rather
  than vacuously fine.

`scripts/deploy.ps1` automates all of the above end-to-end (idempotent:
safe to re-run) -- Artifact Registry repo, both service accounts + IAM
bindings, `deploy/cloudbuild.yaml` build+push, then both `gcloud run
deploy` calls and the invoker binding. `scripts/teardown.ps1` reverses
just the Cloud Run/service-account/Artifact-Registry pieces it created,
deliberately leaving Firestore/BigQuery/Vertex AI alone since those were
set up manually and aren't this script's to delete. Firestore, the
BigQuery dataset, and Vertex AI enablement are assumed to already exist
per the GCP setup walkthrough; this phase only adds the Cloud
Run/Build/Artifact Registry layer on top.

### Cost note: everything scales to zero except Artifact Registry storage

Cloud Run (`minScale=0` on both services, confirmed via `gcloud run
services describe`), Firestore, BigQuery, and Vertex AI are all
pay-per-use with no idle cost -- `--no-cpu-throttling` only affects CPU
allocation *while an instance is handling a request*, it does not
prevent scale-to-zero. The one resource that silently accumulates cost
over time is Artifact Registry: every `make deploy` pushes new image
versions and never deleted the old ones, so repeated iteration (e.g. a
day of debugging a broken deploy) can leave a dozen+ stale image
versions behind. Fixed by adding
`deploy/artifact_registry_cleanup_policy.json` (keep the 3 most recent
versions per service, delete everything else) and applying it
idempotently in `scripts/deploy.ps1` on every deploy -- so this never
needs manual cleanup again, even if the repository is recreated after a
`-RemoveArtifactRegistry` teardown.

### Post-Phase-6 addition: a fourth scenario, "slow copy job" (performance degradation)

Added a fourth demo scenario alongside schema drift / data quality /
broken job: a copy/ETL job that's taking far longer than its baseline
because it full-scans its source table. New `drift_type`,
`"PERFORMANCE_DEGRADATION"` (`app/models/enums.py`); new
`raw_event.scenario == "slow_copy"` handled by
`LocalHeuristicTriageBackend._performance_degradation_finding`
(`app/agents/tools/investigate.py`), with fields `table`, `job_name`,
`filter_column`, `duration_minutes`, `baseline_minutes`. New sidebar
preset in `ui/presets.py`.

Worth calling out: **BigQuery has no traditional row-level/secondary
indexes** the way Postgres/MySQL do. The equivalent lever for "this
query/job full-scans and is slow" is clustering (or partitioning for
very large tables) --
`ALTER TABLE ... SET OPTIONS (clustering_fields = [...])`. Two places
were updated so nobody (human or model) reaches for a nonsensical
`CREATE INDEX` statement:

- `SYSTEM_PROMPT` (`app/agents/prompts.py`) now explicitly teaches the
  orchestrator model this BigQuery fact, so in `WARDEN_MODE=cloud` the
  real `GeminiPatchGenerator` reliably proposes valid clustering DDL
  instead of hallucinating index syntax that would fail in the sandbox.
- `LocalHeuristicPatchGenerator` (`app/agents/tools/patch.py`) gained a
  narrow pattern match (`_CLUSTER_HINT_PATTERN`, tied to the local
  triage backend's exact phrasing) so the fully-offline path proposes
  the same kind of fix deterministically, without needing Gemini at all.

This scenario is also the first one that's naturally idempotent:
`SET OPTIONS (clustering_fields = [...])` is a metadata-only change, and
re-applying the same clustering fields is a harmless no-op rather than
an error (unlike "Schema drift"'s `ADD COLUMN`, which needs
`scripts/reset-demo-data.ps1` between replays -- see
`docs/demo_script.md`).
