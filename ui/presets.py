"""Canned incident payloads for the sidebar's one-click demo triggers.

Each preset's `raw_event` matches the `scenario` convention the Phase 2
zero-resource local heuristics understand (see
`app/agents/tools/investigate.py`'s `LocalHeuristicTriageBackend`), so
clicking a preset produces a sensible diagnosis end-to-end even with
`WARDEN_MODE=local` and no Gemini/BigQuery credentials configured.
"""

from __future__ import annotations

from typing import Any, TypedDict


class IncidentPreset(TypedDict):
    label: str
    icon: str
    source: str
    resource_uri: str
    severity: str
    raw_event: dict[str, Any]


PRESETS: list[IncidentPreset] = [
    {
        "label": "Schema drift",
        "icon": "🧬",
        "source": "bigquery_audit",
        "resource_uri": "bq://warden-demo.sales.orders",
        "severity": "P1",
        "raw_event": {
            "scenario": "schema_drift",
            "table": "orders",
            "dropped_column": "email",
        },
    },
    {
        "label": "Data quality anomaly",
        "icon": "📉",
        "source": "synthetic_probe",
        "resource_uri": "bq://warden-demo.sales.orders",
        "severity": "P2",
        "raw_event": {
            "scenario": "data_quality",
            "table": "orders",
            "column": "total",
            "null_rate": 0.42,
        },
    },
    {
        "label": "Broken pipeline job",
        "icon": "💥",
        "source": "cloudsql_alert",
        "resource_uri": "bq://warden-demo.sales.orders",
        "severity": "P1",
        "raw_event": {
            "scenario": "broken_job",
            "job_name": "nightly_etl",
            "error_message": "OOM killed at 03:12Z",
        },
    },
]
