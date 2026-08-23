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
| `drift_type` | `Literal["SCHEMA_DRIFT","DATA_QUALITY","BROKEN_JOB","PERMISSION","UNKNOWN"]` |
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
    async def write_audit(self, incident_id: str, audit: GovernanceAudit) -> None: ...
    async def stream_steps(self, incident_id: str) -> AsyncIterator[AgentStepLog]: ...
```

Two implementations: `FirestoreStateManager` (prod) and `InMemoryStateManager`
(offline dev, backed by `asyncio.Queue` for `stream_steps`).

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

## 5. Streamlit UI layout & demo flow

Layout: sidebar with preset incident triggers + custom event JSON; main area
with four tabs — Timeline, Diagnosis, Patch Diff, Governance; sticky footer
with Approve/Reject buttons gated on `status == AWAITING_APPROVAL` and
`verdict != BLOCK`.

Data flow: a background thread attaches Firestore `on_snapshot` listeners
(`incidents/{id}`, `.../steps`, `.../findings`, `.../patches`, `.../audits`)
and pushes updates into `st.session_state`; `streamlit-autorefresh` redraws
every ~1s.

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
- **Phase 4** — HTTP surface: `app/api/*`, `Dockerfile`.
- **Phase 5** — Streamlit UI: `ui/*`, `Dockerfile.ui`.
- **Phase 6** — deployment & demo assets: `deploy/*`, `scripts/*`,
  `docs/demo_script.md`.
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
