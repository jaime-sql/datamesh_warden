"""Sub-Agent 3: deterministic governance rules + optional LLM rationale.

All four policy checks are pure-Python/text-based and need no external
resources by default:

  1. Blocks DROP COLUMN on tables tagged pii=true.
  2. Requires a bound data_steward role for high-sensitivity tables.
  3. Flags DML without a WHERE clause as potentially touching all rows.
  4. Cross-checks touched column names against a simple PII naming
     taxonomy (a stand-in for a real Data Catalog / DLP lookup).

`LocalHeuristicMetadataProvider` supplies benign defaults (no PII, low
sensitivity) so these checks run purely off the SQL text in local/demo
mode. `BigQueryMetadataProvider` reads real dataset labels and is only
selected in cloud mode, once you've actually labelled a BigQuery dataset
with `pii`, `sensitivity`, and `data_steward_bound` -- a step we have not
taken yet.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Protocol

from pydantic import BaseModel

from app.agents.bq_sandbox import get_bigquery_client
from app.agents.genai_client import get_genai_client, is_genai_configured
from app.config import get_settings
from app.models.enums import GovernanceVerdict, PatchKind, Sensitivity
from app.models.ids import new_id
from app.models.state import GovernanceAudit, PolicyCheck
from app.persistence.factory import get_state_manager

_PII_NAME_PATTERN = re.compile(
    r"\b(email|ssn|social_security|phone|address|date_of_birth|dob|"
    r"credit_card|password|passport|national_id)\w*\b",
    re.IGNORECASE,
)


def _mentions_pii_like_column(sql: str) -> list[str]:
    return sorted({m.group(0).lower() for m in _PII_NAME_PATTERN.finditer(sql)})


class TableMetadata(BaseModel):
    pii_tagged: bool = False
    sensitivity: Sensitivity = "low"
    data_steward_bound: bool = False


class MetadataProvider(Protocol):
    async def get_table_metadata(self, dataset_id: str) -> TableMetadata: ...


class LocalHeuristicMetadataProvider:
    """Zero-resource fallback: assumes benign defaults, so the rules below
    only fire based on signals present in the SQL text itself."""

    async def get_table_metadata(self, dataset_id: str) -> TableMetadata:
        return TableMetadata()


class BigQueryMetadataProvider:
    """Real implementation: reads `pii`, `sensitivity`, and
    `data_steward_bound` labels off a BigQuery dataset. Requires
    WARDEN_MODE=cloud and a dataset that has actually been labelled this
    way -- see docs/architecture.md section 3."""

    async def get_table_metadata(self, dataset_id: str) -> TableMetadata:
        client = get_bigquery_client()

        def _get() -> TableMetadata:
            dataset = client.get_dataset(dataset_id)
            labels = dataset.labels or {}
            sensitivity: Sensitivity = "high" if labels.get("sensitivity") == "high" else "low"
            return TableMetadata(
                pii_tagged=labels.get("pii", "false").lower() == "true",
                sensitivity=sensitivity,
                data_steward_bound=labels.get("data_steward_bound", "false").lower() == "true",
            )

        return await asyncio.to_thread(_get)


def get_metadata_provider() -> MetadataProvider:
    settings = get_settings()
    if settings.warden_mode == "cloud":
        return BigQueryMetadataProvider()
    return LocalHeuristicMetadataProvider()


class RationaleGenerator(Protocol):
    async def explain(
        self, policy_checks: list[PolicyCheck], verdict: GovernanceVerdict
    ) -> str: ...


class TemplatedRationaleGenerator:
    """Zero-resource fallback: a plain-English summary built directly from
    the policy check results, no LLM call involved."""

    async def explain(self, policy_checks: list[PolicyCheck], verdict: GovernanceVerdict) -> str:
        if verdict == "PASS":
            return "All governance checks passed; no PII or broad-impact statements were detected."
        lines = [
            f"- [{c.result}] {c.description}: {c.detail}"
            for c in policy_checks
            if c.result != "PASS"
        ]
        return f"Verdict {verdict}:\n" + "\n".join(lines)


class GeminiRationaleGenerator:
    """Real implementation: asks Gemini (WARDEN_GOVERNANCE_MODEL) to write
    a plain-English rationale. Requires GEMINI_API_KEY or Vertex AI."""

    async def explain(self, policy_checks: list[PolicyCheck], verdict: GovernanceVerdict) -> str:
        settings = get_settings()
        client = get_genai_client()
        checks_text = "\n".join(
            f"- [{c.result}] {c.description}: {c.detail}" for c in policy_checks
        )
        prompt = (
            "Summarize in 2-3 plain-English sentences why a data-platform "
            f"governance verdict of {verdict} was reached, given these checks:\n{checks_text}"
        )
        response = await client.aio.models.generate_content(
            model=settings.warden_governance_model,
            contents=prompt,
        )
        return (response.text or "").strip()


def get_rationale_generator() -> RationaleGenerator:
    return GeminiRationaleGenerator() if is_genai_configured() else TemplatedRationaleGenerator()


async def _evaluate_policy_checks(
    sql: str, patch_kind: PatchKind, metadata: TableMetadata
) -> list[PolicyCheck]:
    checks: list[PolicyCheck] = []

    drops_column = bool(re.search(r"DROP\s+COLUMN", sql, re.IGNORECASE))
    if drops_column and metadata.pii_tagged:
        checks.append(
            PolicyCheck(
                policy_id="pii-drop-guard",
                description="Blocks DROP COLUMN on tables tagged pii=true",
                result="FAIL",
                detail="Statement drops a column on a table tagged as containing PII.",
            )
        )
    else:
        checks.append(
            PolicyCheck(
                policy_id="pii-drop-guard",
                description="Blocks DROP COLUMN on tables tagged pii=true",
                result="PASS",
                detail="No PII-tagged column drop detected.",
            )
        )

    if metadata.sensitivity == "high" and not metadata.data_steward_bound:
        checks.append(
            PolicyCheck(
                policy_id="data-steward-binding",
                description="Requires a bound data_steward role for high-sensitivity tables",
                result="WARN",
                detail=(
                    "Table is marked high-sensitivity but no data_steward role binding was found."
                ),
            )
        )
    else:
        checks.append(
            PolicyCheck(
                policy_id="data-steward-binding",
                description="Requires a bound data_steward role for high-sensitivity tables",
                result="PASS",
                detail="Sensitivity is not high, or a data_steward role is bound.",
            )
        )

    is_dml = patch_kind in ("DML", "BOTH")
    has_where = bool(re.search(r"\bWHERE\b", sql, re.IGNORECASE))
    if is_dml and not has_where:
        checks.append(
            PolicyCheck(
                policy_id="broad-dml-guard",
                description="Flags DML without a WHERE clause as potentially touching all rows",
                result="WARN",
                detail="DML statement has no WHERE clause; it may affect every row in the table.",
            )
        )
    else:
        checks.append(
            PolicyCheck(
                policy_id="broad-dml-guard",
                description="Flags DML without a WHERE clause as potentially touching all rows",
                result="PASS",
                detail="Statement is not unscoped DML.",
            )
        )

    pii_like_columns = _mentions_pii_like_column(sql)
    if pii_like_columns and not metadata.pii_tagged:
        checks.append(
            PolicyCheck(
                policy_id="pii-taxonomy-crosscheck",
                description="Cross-checks touched column names against a PII naming taxonomy",
                result="WARN",
                detail=(
                    f"Column name(s) suggest PII ({', '.join(pii_like_columns)}); "
                    "confirm classification with Data Catalog before merging."
                ),
            )
        )
    else:
        checks.append(
            PolicyCheck(
                policy_id="pii-taxonomy-crosscheck",
                description="Cross-checks touched column names against a PII naming taxonomy",
                result="PASS",
                detail=(
                    "No PII-like column names detected, or the table is already correctly tagged."
                ),
            )
        )

    return checks


def _verdict_from_checks(checks: list[PolicyCheck]) -> GovernanceVerdict:
    if any(c.result == "FAIL" for c in checks):
        return "BLOCK"
    if any(c.result == "WARN" for c in checks):
        return "WARN"
    return "PASS"


async def verify_governance_policy(
    incident_id: str,
    patch_id: str,
    dataset_id: str,
) -> dict[str, Any]:
    """Run rule-based + LLM-augmented governance checks on a proposed patch.

    Deterministic rules run first (fast, no external calls). Gemini is
    invoked only to write a plain-English rationale, and only if
    configured -- otherwise a templated rationale is used instead.

    Args:
        incident_id: Firestore incident doc id.
        patch_id: The SQLPatchPayload being audited.
        dataset_id: The BigQuery dataset the patch targets (used to fetch
            labels / policy tags).

    Returns:
        A dict with audit_id, verdict, requires_human_approval,
        policy_checks_passed, policy_checks_failed, pii_columns_touched,
        rationale, and firestore_path.
    """
    state_manager = get_state_manager()
    patch = await state_manager.get_patch(incident_id, patch_id)

    metadata_provider = get_metadata_provider()
    metadata = await metadata_provider.get_table_metadata(dataset_id)

    sql = patch.production_sql
    checks = await _evaluate_policy_checks(sql, patch.patch_kind, metadata)
    verdict = _verdict_from_checks(checks)

    rationale_generator = get_rationale_generator()
    rationale = await rationale_generator.explain(checks, verdict)

    pii_columns_touched = _mentions_pii_like_column(sql) if metadata.pii_tagged else []

    audit = GovernanceAudit(
        audit_id=new_id(),
        linked_patch_id=patch_id,
        verdict=verdict,
        policy_checks=checks,
        pii_columns_touched=pii_columns_touched,
        requires_human_approval=True,
        rationale=rationale,
    )
    await state_manager.write_audit(incident_id, audit)

    return {
        "audit_id": audit.audit_id,
        "verdict": audit.verdict,
        "requires_human_approval": audit.requires_human_approval,
        "policy_checks_passed": sum(1 for c in checks if c.result == "PASS"),
        "policy_checks_failed": sum(1 for c in checks if c.result == "FAIL"),
        "pii_columns_touched": audit.pii_columns_touched,
        "rationale": audit.rationale,
        "firestore_path": f"incidents/{incident_id}/audits/{audit.audit_id}",
    }
