from __future__ import annotations

from app.agents.tools.governance import (
    LocalHeuristicMetadataProvider,
    TableMetadata,
    TemplatedRationaleGenerator,
    _evaluate_policy_checks,
    _verdict_from_checks,
    verify_governance_policy,
)
from app.models import IncidentState, SQLPatchPayload, new_id
from app.persistence import InMemoryStateManager
from app.persistence.factory import get_state_manager


async def test_pii_drop_guard_blocks_drop_column_on_pii_tagged_table() -> None:
    metadata = TableMetadata(pii_tagged=True, sensitivity="low", data_steward_bound=True)
    checks = await _evaluate_policy_checks("ALTER TABLE t DROP COLUMN email", "DDL", metadata)

    verdict = _verdict_from_checks(checks)
    assert verdict == "BLOCK"

    pii_check = next(c for c in checks if c.policy_id == "pii-drop-guard")
    assert pii_check.result == "FAIL"


async def test_drop_column_on_non_pii_table_passes() -> None:
    metadata = TableMetadata(pii_tagged=False, sensitivity="low", data_steward_bound=True)
    checks = await _evaluate_policy_checks("ALTER TABLE t DROP COLUMN legacy_flag", "DDL", metadata)
    assert _verdict_from_checks(checks) == "PASS"


async def test_high_sensitivity_without_data_steward_warns() -> None:
    metadata = TableMetadata(pii_tagged=False, sensitivity="high", data_steward_bound=False)
    checks = await _evaluate_policy_checks("ALTER TABLE t ADD COLUMN x STRING", "DDL", metadata)
    assert _verdict_from_checks(checks) == "WARN"


async def test_unscoped_dml_warns() -> None:
    metadata = TableMetadata()
    checks = await _evaluate_policy_checks("UPDATE t SET x = 1", "DML", metadata)

    assert _verdict_from_checks(checks) == "WARN"
    dml_check = next(c for c in checks if c.policy_id == "broad-dml-guard")
    assert dml_check.result == "WARN"


async def test_scoped_dml_passes() -> None:
    metadata = TableMetadata()
    checks = await _evaluate_policy_checks("UPDATE t SET x = 1 WHERE id = 5", "DML", metadata)
    assert _verdict_from_checks(checks) == "PASS"


async def test_pii_like_column_name_warns_when_table_not_tagged() -> None:
    metadata = TableMetadata(pii_tagged=False)
    checks = await _evaluate_policy_checks("ALTER TABLE t ADD COLUMN email STRING", "DDL", metadata)
    assert _verdict_from_checks(checks) == "WARN"


async def test_pii_like_column_name_ok_when_table_already_tagged() -> None:
    metadata = TableMetadata(pii_tagged=True)
    checks = await _evaluate_policy_checks("ALTER TABLE t ADD COLUMN email STRING", "DDL", metadata)
    crosscheck = next(c for c in checks if c.policy_id == "pii-taxonomy-crosscheck")
    assert crosscheck.result == "PASS"


async def test_templated_rationale_generator_summarizes_verdict() -> None:
    metadata = TableMetadata(pii_tagged=True)
    checks = await _evaluate_policy_checks("ALTER TABLE t DROP COLUMN email", "DDL", metadata)
    rationale = await TemplatedRationaleGenerator().explain(checks, "BLOCK")
    assert "BLOCK" in rationale


async def test_local_heuristic_metadata_provider_defaults_are_benign() -> None:
    provider = LocalHeuristicMetadataProvider()
    metadata = await provider.get_table_metadata("proj.ds")
    assert metadata.pii_tagged is False
    assert metadata.sensitivity == "low"


async def test_verify_governance_policy_tool_end_to_end() -> None:
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

    patch = SQLPatchPayload(
        patch_id=new_id(),
        linked_finding_id=new_id(),
        patch_kind="DDL",
        sandbox_sql="ALTER TABLE `proj.warden_sandbox_x.orders` ADD COLUMN email STRING",
        production_sql="ALTER TABLE `proj.ds.orders` ADD COLUMN email STRING",
        validation_status="SANDBOX_PASS",
        patcher_model="local-heuristic-patcher-v1",
    )
    await state_manager.write_patch(incident.incident_id, patch)

    result = await verify_governance_policy(
        incident_id=incident.incident_id,
        patch_id=patch.patch_id,
        dataset_id="proj.ds",
    )

    assert result["verdict"] in ("PASS", "WARN", "BLOCK")
    assert result["audit_id"]

    audits = state_manager.list_audits(incident.incident_id)
    assert len(audits) == 1
    assert audits[0].audit_id == result["audit_id"]
