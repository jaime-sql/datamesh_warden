"""Literal type aliases shared across the Pydantic state models.

Kept separate from state.py so tool/orchestrator modules can import just the
vocabulary they need without pulling in the full model definitions.
"""

from __future__ import annotations

from typing import Literal

IncidentSource = Literal[
    "bigquery_audit",
    "cloudsql_alert",
    "synthetic_probe",
    "manual_demo",
]

Severity = Literal["P1", "P2", "P3"]

IncidentStatus = Literal[
    "INGESTED",
    "DIAGNOSING",
    "PATCHING",
    "AWAITING_APPROVAL",
    "EXECUTING",
    "RESOLVED",
    "REJECTED",
    "FAILED",
]

StepKind = Literal[
    "MODEL_TURN",
    "TOOL_CALL",
    "TOOL_RESULT",
    "USER_DECISION",
    "EXECUTION",
]

StepActor = Literal[
    "orchestrator",
    "sub_agent_1_gemma",
    "sub_agent_2_flash",
    "sub_agent_3_flash",
    "human",
    "executor",
]

StepStatus = Literal["RUNNING", "OK", "ERROR", "TIMEOUT"]

DriftType = Literal[
    "SCHEMA_DRIFT",
    "DATA_QUALITY",
    "BROKEN_JOB",
    "PERFORMANCE_DEGRADATION",
    "PERMISSION",
    "UNKNOWN",
]

EvidenceSeverity = Literal["INFO", "WARN", "ERROR"]

PatchKind = Literal["DDL", "DML", "BOTH"]

ColumnMode = Literal["NULLABLE", "REQUIRED", "REPEATED"]

ValidationStatus = Literal["SANDBOX_PASS", "SANDBOX_FAIL", "DRY_RUN_ONLY"]

GovernanceVerdict = Literal["PASS", "BLOCK", "WARN"]

PolicyCheckResult = Literal["PASS", "FAIL", "WARN"]

Sensitivity = Literal["low", "high"]
