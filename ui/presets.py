"""Canned incident payloads for the sidebar's one-click demo triggers.

Each preset's `raw_event` matches the `scenario` convention the Phase 2
zero-resource local heuristics understand (see
`app/agents/tools/investigate.py`'s `LocalHeuristicTriageBackend`), so
clicking a preset produces a sensible diagnosis end-to-end even with
`WARDEN_MODE=local` and no Gemini/BigQuery credentials configured.

`resource_uri` is built from a real project ID at render time (see
`build_presets`) rather than hardcoded, because in `WARDEN_MODE=cloud`
the sub-agent tools make real BigQuery calls against it -- a fake
placeholder project (the original `warden-demo`) causes a real, very
confusing `404 ... Project warden-demo is not found` failure partway
through diagnosis (see docs/architecture.md's Phase 6 note).
"""

from __future__ import annotations

from typing import Any, TypedDict

# Only used as a label when no real GCP project is configured (pure
# WARDEN_MODE=local dev, where the local heuristic backends never
# actually call BigQuery, so the exact project name is cosmetic).
DEFAULT_PROJECT = "warden-demo"


class IncidentPreset(TypedDict):
    label: str
    icon: str
    source: str
    resource_uri: str
    severity: str
    raw_event: dict[str, Any]


def build_presets(
    *, project: str = DEFAULT_PROJECT, dataset: str = "sales", table: str = "orders"
) -> list[IncidentPreset]:
    resource_uri = f"bq://{project}.{dataset}.{table}"
    return [
        {
            "label": "Schema drift",
            "icon": "🧬",
            "source": "bigquery_audit",
            "resource_uri": resource_uri,
            "severity": "P1",
            "raw_event": {
                "scenario": "schema_drift",
                "table": table,
                "dropped_column": "email",
            },
        },
        {
            "label": "Data quality anomaly",
            "icon": "📉",
            "source": "synthetic_probe",
            "resource_uri": resource_uri,
            "severity": "P2",
            "raw_event": {
                "scenario": "data_quality",
                "table": table,
                "column": "total",
                "null_rate": 0.42,
            },
        },
        {
            "label": "Broken pipeline job",
            "icon": "💥",
            "source": "cloudsql_alert",
            "resource_uri": resource_uri,
            "severity": "P1",
            "raw_event": {
                "scenario": "broken_job",
                "job_name": "nightly_etl",
                "error_message": "OOM killed at 03:12Z",
            },
        },
    ]
