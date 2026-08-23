"""Tests for Sub-Agent 2's SQL generation + sandbox validation logic.

Only the zero-resource local fallbacks (`LocalHeuristicPatchGenerator`,
`LocalHeuristicSandboxExecutor`) and the pure-text helpers in
`app/agents/bq_sandbox.py` are exercised here. `BigQuerySandboxExecutor`
requires a real GCP project and a BigQuery dataset to clone, which haven't
been set up yet -- see docs/architecture.md section 3 for what that would
take when we get there.
"""

from __future__ import annotations

import pytest

from app.agents.bq_sandbox import (
    InvalidResourceUriError,
    contains_destructive_statement,
    parse_added_columns,
    parse_resource_uri,
    rewrite_sql_for_dataset,
    sandbox_dataset_id,
)
from app.agents.tools import patch as patch_module
from app.agents.tools.patch import (
    GeneratedSQL,
    LocalHeuristicPatchGenerator,
    LocalHeuristicSandboxExecutor,
    generate_and_test_patch,
)
from app.models import IncidentState, new_id
from app.persistence import InMemoryStateManager
from app.persistence.factory import get_state_manager


def test_parse_resource_uri_accepts_valid_uri() -> None:
    ref = parse_resource_uri("bq://my-project.my_dataset.my_table")
    assert ref.project == "my-project"
    assert ref.dataset == "my_dataset"
    assert ref.table == "my_table"
    assert ref.fully_qualified == "my-project.my_dataset.my_table"


def test_parse_resource_uri_rejects_malformed_uri() -> None:
    with pytest.raises(InvalidResourceUriError):
        parse_resource_uri("not-a-valid-uri")


def test_sandbox_dataset_id_is_deterministic_and_safe() -> None:
    dataset_id = sandbox_dataset_id("01ABCXYZ")
    assert dataset_id == "warden_sandbox_01abcxyz"


def test_contains_destructive_statement_detects_drop_and_truncate() -> None:
    assert contains_destructive_statement("DROP TABLE proj.ds.t")
    assert contains_destructive_statement("ALTER TABLE proj.ds.t DROP COLUMN email")
    assert contains_destructive_statement("TRUNCATE TABLE proj.ds.t")
    assert not contains_destructive_statement("ALTER TABLE proj.ds.t ADD COLUMN email STRING")


def test_rewrite_sql_for_dataset_points_at_sandbox() -> None:
    ref = parse_resource_uri("bq://proj.orders_ds.orders")
    sql = "ALTER TABLE `proj.orders_ds.orders` ADD COLUMN email STRING"
    rewritten = rewrite_sql_for_dataset(sql, ref, "warden_sandbox_abc123")
    assert "proj.warden_sandbox_abc123.orders" in rewritten
    assert "orders_ds" not in rewritten


def test_parse_added_columns_extracts_ddl_additions() -> None:
    columns = parse_added_columns("ALTER TABLE t ADD COLUMN email STRING")
    assert len(columns) == 1
    assert columns[0].name == "email"
    assert columns[0].type == "STRING"


async def test_local_heuristic_patch_generator_extracts_backticked_column() -> None:
    generator = LocalHeuristicPatchGenerator()
    generated = await generator.generate(
        target_resource_uri="bq://proj.ds.orders",
        drift_summary="Column `email` appears to have been dropped from `orders`.",
        allow_destructive=False,
    )
    assert generated.patch_kind == "DDL"
    assert "email" in generated.production_sql
    assert "ADD COLUMN" in generated.production_sql


async def test_local_heuristic_sandbox_executor_produces_schema_diff() -> None:
    executor = LocalHeuristicSandboxExecutor()
    validation = await executor.validate(
        incident_id=new_id(),
        target_resource_uri="bq://proj.ds.orders",
        production_sql="ALTER TABLE `proj.ds.orders` ADD COLUMN `email` STRING",
    )
    assert validation.validation_status == "SANDBOX_PASS"
    assert len(validation.after_schema) == len(validation.before_schema) + 1
    assert validation.after_schema[-1].name == "email"


async def test_generate_and_test_patch_tool_end_to_end_local_mode() -> None:
    state_manager = get_state_manager()
    assert isinstance(state_manager, InMemoryStateManager)

    incident = IncidentState(
        incident_id=new_id(),
        source="manual_demo",
        resource_uri="bq://proj.ds.orders",
        severity="P1",
        raw_event={"scenario": "schema_drift", "table": "orders", "dropped_column": "email"},
        orchestrator_model="gemini-3.1-pro-preview",
    )
    await state_manager.create_incident(incident)

    result = await generate_and_test_patch(
        incident_id=incident.incident_id,
        finding_id=new_id(),
        target_resource_uri=incident.resource_uri,
        drift_summary="Column `email` appears to have been dropped from `orders`.",
    )

    assert result["validation_status"] == "SANDBOX_PASS"
    assert result["patch_id"]

    patches = state_manager.list_patches(incident.incident_id)
    assert len(patches) == 1
    assert patches[0].patch_id == result["patch_id"]


async def test_generate_and_test_patch_rejects_destructive_sql_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _DestructiveGenerator:
        async def generate(
            self, target_resource_uri: str, drift_summary: str, allow_destructive: bool
        ) -> GeneratedSQL:
            return GeneratedSQL(
                patch_kind="DDL",
                production_sql="DROP TABLE `proj.ds.orders`",
                patcher_model="test-fixture",
            )

    monkeypatch.setattr(patch_module, "get_patch_generator", lambda: _DestructiveGenerator())

    state_manager = get_state_manager()
    assert isinstance(state_manager, InMemoryStateManager)

    incident = IncidentState(
        incident_id=new_id(),
        source="manual_demo",
        resource_uri="bq://proj.ds.orders",
        severity="P1",
        raw_event={},
        orchestrator_model="gemini-3.1-pro-preview",
    )
    await state_manager.create_incident(incident)

    result = await generate_and_test_patch(
        incident_id=incident.incident_id,
        finding_id=new_id(),
        target_resource_uri=incident.resource_uri,
        drift_summary="drop everything",
    )

    assert result["validation_status"] == "SANDBOX_FAIL"
    patches = state_manager.list_patches(incident.incident_id)
    assert "Destructive statement rejected" in patches[-1].validation_errors[0]
