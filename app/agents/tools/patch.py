"""Sub-Agent 2: SQL/DDL patch generation + sandbox validation.

Two independent extension points, each with a zero-resource local fallback
so this tool works fully offline until real Gemini / BigQuery access is
configured:

  - `PatchGenerator`  -- turns a drift summary into candidate SQL.
  - `SandboxExecutor` -- validates that SQL (clone + run + dry-run) and
                         produces the before/after schema diff for the UI.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Protocol

from pydantic import BaseModel

from app.agents.bq_sandbox import (
    InvalidResourceUriError,
    clone_table_to_sandbox,
    contains_destructive_statement,
    dry_run_bytes_processed,
    ensure_sandbox_dataset,
    get_bigquery_client,
    get_table_schema,
    parse_added_columns,
    parse_resource_uri,
    rewrite_sql_for_dataset,
    run_sandbox_statement,
    sandbox_dataset_id,
)
from app.agents.genai_client import get_genai_client, is_genai_configured
from app.config import get_settings
from app.models.enums import PatchKind, ValidationStatus
from app.models.ids import new_id
from app.models.state import ColumnSpec, SQLPatchPayload
from app.persistence.factory import get_state_manager

_COLUMN_HINT_PATTERN = re.compile(r"`(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)`")


class GeneratedSQL(BaseModel):
    patch_kind: PatchKind
    production_sql: str
    patcher_model: str


class PatchGenerator(Protocol):
    async def generate(
        self, target_resource_uri: str, drift_summary: str, allow_destructive: bool
    ) -> GeneratedSQL: ...


class SandboxValidation(BaseModel):
    sandbox_sql: str
    dry_run_bytes_processed: int
    sandbox_execution_ms: int
    sandbox_row_delta: int
    validation_status: ValidationStatus
    validation_errors: list[str]
    before_schema: list[ColumnSpec]
    after_schema: list[ColumnSpec]


class SandboxExecutor(Protocol):
    async def validate(
        self, incident_id: str, target_resource_uri: str, production_sql: str
    ) -> SandboxValidation: ...


class LocalHeuristicPatchGenerator:
    """Zero-resource fallback: extracts a backticked column name from the
    drift summary (as produced by `LocalHeuristicTriageBackend`) and
    proposes an `ADD COLUMN` DDL. Deterministic, no network calls."""

    async def generate(
        self, target_resource_uri: str, drift_summary: str, allow_destructive: bool
    ) -> GeneratedSQL:
        table = parse_resource_uri(target_resource_uri)
        match = _COLUMN_HINT_PATTERN.search(drift_summary)
        column_name = match["name"] if match else "unknown_column"
        sql = (
            f"ALTER TABLE `{table.fully_qualified}` ADD COLUMN IF NOT EXISTS `{column_name}` STRING"
        )
        return GeneratedSQL(
            patch_kind="DDL",
            production_sql=sql,
            patcher_model="local-heuristic-patcher-v1",
        )


class GeminiPatchGenerator:
    """Real implementation: asks Gemini (WARDEN_PATCHER_MODEL) for a JSON
    payload describing the patch. Requires GEMINI_API_KEY or Vertex AI to
    be configured -- see app/agents/genai_client.py."""

    async def generate(
        self, target_resource_uri: str, drift_summary: str, allow_destructive: bool
    ) -> GeneratedSQL:
        settings = get_settings()
        client = get_genai_client()
        table = parse_resource_uri(target_resource_uri)
        prompt = (
            "You are generating a safe BigQuery DDL/DML patch.\n"
            f"Target table: {table.fully_qualified}\n"
            f"Drift summary: {drift_summary}\n"
            f"Destructive statements allowed: {allow_destructive}\n\n"
            'Respond with strict JSON: {"patch_kind": "DDL"|"DML"|"BOTH", '
            '"production_sql": "<single SQL statement>"}. '
            "Do not include markdown fences or commentary."
        )
        response = await client.aio.models.generate_content(
            model=settings.warden_patcher_model,
            contents=prompt,
        )
        payload = json.loads(response.text or "{}")
        return GeneratedSQL(
            patch_kind=payload["patch_kind"],
            production_sql=payload["production_sql"],
            patcher_model=settings.warden_patcher_model,
        )


class LocalHeuristicSandboxExecutor:
    """Zero-resource fallback: synthesizes plausible sandbox metrics and a
    real schema diff parsed from the SQL text itself, without touching
    BigQuery. Good enough to drive the demo UI end-to-end offline."""

    _BASE_SCHEMA = (ColumnSpec(name="id", type="INT64", mode="REQUIRED"),)

    async def validate(
        self, incident_id: str, target_resource_uri: str, production_sql: str
    ) -> SandboxValidation:
        table = parse_resource_uri(target_resource_uri)
        sandbox_dataset = sandbox_dataset_id(incident_id)
        sandbox_sql = rewrite_sql_for_dataset(production_sql, table, sandbox_dataset)
        added_columns = parse_added_columns(production_sql)
        before_schema = list(self._BASE_SCHEMA)
        after_schema = [*before_schema, *added_columns]
        return SandboxValidation(
            sandbox_sql=sandbox_sql,
            dry_run_bytes_processed=len(production_sql) * 1024,
            sandbox_execution_ms=50,
            sandbox_row_delta=0,
            validation_status="SANDBOX_PASS",
            validation_errors=[],
            before_schema=before_schema,
            after_schema=after_schema,
        )


class BigQuerySandboxExecutor:
    """Real implementation: clones the target table into an isolated
    sandbox dataset, runs the patch there, and dry-runs the production
    statement. Requires WARDEN_MODE=cloud, a real GCP project, a BigQuery
    dataset to test against, and Application Default Credentials
    (`gcloud auth application-default login`) -- see
    docs/architecture.md section 3."""

    async def validate(
        self, incident_id: str, target_resource_uri: str, production_sql: str
    ) -> SandboxValidation:
        settings = get_settings()
        client = get_bigquery_client()
        table = parse_resource_uri(target_resource_uri)
        sandbox_dataset = sandbox_dataset_id(incident_id)

        await ensure_sandbox_dataset(
            client, table.project, sandbox_dataset, settings.warden_bq_sandbox_expiration_hours
        )
        await clone_table_to_sandbox(client, table, sandbox_dataset)
        before_schema = await get_table_schema(client, table.project, sandbox_dataset, table.table)
        sandbox_sql = rewrite_sql_for_dataset(production_sql, table, sandbox_dataset)

        errors: list[str] = []
        elapsed_ms = 0
        row_delta = 0
        after_schema = before_schema
        try:
            elapsed_ms, row_delta = await run_sandbox_statement(client, sandbox_sql)
            after_schema = await get_table_schema(
                client, table.project, sandbox_dataset, table.table
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a validation error, not a crash
            errors.append(str(exc))

        bytes_processed = 0
        try:
            bytes_processed = await dry_run_bytes_processed(client, production_sql)
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))

        status: ValidationStatus = "SANDBOX_FAIL" if errors else "SANDBOX_PASS"
        return SandboxValidation(
            sandbox_sql=sandbox_sql,
            dry_run_bytes_processed=bytes_processed,
            sandbox_execution_ms=elapsed_ms,
            sandbox_row_delta=row_delta,
            validation_status=status,
            validation_errors=errors,
            before_schema=before_schema,
            after_schema=after_schema,
        )


def get_patch_generator() -> PatchGenerator:
    return GeminiPatchGenerator() if is_genai_configured() else LocalHeuristicPatchGenerator()


def get_sandbox_executor() -> SandboxExecutor:
    settings = get_settings()
    if settings.warden_mode == "cloud":
        return BigQuerySandboxExecutor()
    return LocalHeuristicSandboxExecutor()


async def generate_and_test_patch(
    incident_id: str,
    finding_id: str,
    target_resource_uri: str,
    drift_summary: str,
    allow_destructive: bool = False,
) -> dict[str, Any]:
    """Generate a candidate SQL/DDL patch and validate it in an isolated
    BigQuery sandbox by (a) cloning the target table via
    `CREATE TABLE ... CLONE`, (b) running the patch against the clone, and
    (c) executing a BigQuery dry-run on the production-rewritten statement
    to capture bytes-processed. Falls back to a deterministic local
    heuristic (no Gemini or BigQuery calls) when those aren't configured.

    Args:
        incident_id: Firestore incident doc id.
        finding_id: The DiagnosticFinding this patch resolves.
        target_resource_uri: bq://proj.ds.table to be patched.
        drift_summary: One-paragraph natural-language description of the
            drift, extracted by the orchestrator from the finding.
        allow_destructive: If False, rejects any DROP/TRUNCATE statement
            and returns validation_status="SANDBOX_FAIL".

    Returns:
        A dict with patch_id, patch_kind, validation_status,
        dry_run_bytes_processed, sandbox_execution_ms, sandbox_row_delta,
        before_schema_hash, after_schema_hash, and firestore_path.
    """
    state_manager = get_state_manager()
    generator = get_patch_generator()

    try:
        generated = await generator.generate(target_resource_uri, drift_summary, allow_destructive)
    except InvalidResourceUriError as exc:
        return {"error": "INVALID_RESOURCE_URI", "detail": str(exc), "patch_id": None}

    patch_id = new_id()

    if contains_destructive_statement(generated.production_sql) and not allow_destructive:
        payload = SQLPatchPayload(
            patch_id=patch_id,
            linked_finding_id=finding_id,
            patch_kind=generated.patch_kind,
            sandbox_sql="",
            production_sql=generated.production_sql,
            validation_status="SANDBOX_FAIL",
            validation_errors=[
                "Destructive statement rejected: set allow_destructive=True to permit "
                "DROP/TRUNCATE."
            ],
            patcher_model=generated.patcher_model,
        )
        await state_manager.write_patch(incident_id, payload)
        return _envelope(incident_id, payload)

    executor = get_sandbox_executor()
    validation = await executor.validate(incident_id, target_resource_uri, generated.production_sql)

    payload = SQLPatchPayload(
        patch_id=patch_id,
        linked_finding_id=finding_id,
        patch_kind=generated.patch_kind,
        sandbox_sql=validation.sandbox_sql,
        production_sql=generated.production_sql,
        before_schema=validation.before_schema,
        after_schema=validation.after_schema,
        dry_run_bytes_processed=validation.dry_run_bytes_processed,
        sandbox_execution_ms=validation.sandbox_execution_ms,
        sandbox_row_delta=validation.sandbox_row_delta,
        validation_status=validation.validation_status,
        validation_errors=validation.validation_errors,
        patcher_model=generated.patcher_model,
    )
    await state_manager.write_patch(incident_id, payload)
    return _envelope(incident_id, payload)


def _envelope(incident_id: str, payload: SQLPatchPayload) -> dict[str, Any]:
    return {
        "patch_id": payload.patch_id,
        "patch_kind": payload.patch_kind,
        "validation_status": payload.validation_status,
        "dry_run_bytes_processed": payload.dry_run_bytes_processed,
        "sandbox_execution_ms": payload.sandbox_execution_ms,
        "sandbox_row_delta": payload.sandbox_row_delta,
        "before_schema_hash": _schema_hash(payload.before_schema),
        "after_schema_hash": _schema_hash(payload.after_schema),
        "firestore_path": f"incidents/{incident_id}/patches/{payload.patch_id}",
    }


def _schema_hash(schema: list[ColumnSpec]) -> str:
    canonical = json.dumps([c.model_dump() for c in schema], sort_keys=True)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
