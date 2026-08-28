"""BigQuery sandbox helpers: URI parsing, table cloning, schema
introspection, and dry-run cost estimation.

The pure-text helpers (`parse_resource_uri`, `rewrite_sql_for_dataset`,
`contains_destructive_statement`, `parse_added_columns`,
`sandbox_dataset_id`) need no BigQuery access at all and are exercised
directly in tests. The `async def` helpers that actually talk to BigQuery
(`ensure_sandbox_dataset`, `clone_table_to_sandbox`, `get_table_schema`,
`run_sandbox_statement`, `dry_run_bytes_processed`) require a real
`bigquery.Client` -- i.e. a GCP project, a BigQuery dataset to test
against, and `gcloud auth application-default login` -- none of which have
been set up yet. They're only reachable via `BigQuerySandboxExecutor`
(app/agents/tools/patch.py), which is only selected when
`Settings.warden_mode == "cloud"`.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from functools import lru_cache

from google.cloud import bigquery

from app.config import get_settings
from app.models.state import ColumnSpec

_RESOURCE_URI_PATTERN = re.compile(
    r"^bq://(?P<project>[^./]+)\.(?P<dataset>[^./]+)\.(?P<table>[^./]+)$"
)

_DESTRUCTIVE_PATTERN = re.compile(
    r"\b(DROP\s+TABLE|DROP\s+COLUMN|TRUNCATE\s+TABLE)\b", re.IGNORECASE
)

_ADD_COLUMN_PATTERN = re.compile(
    r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?`?(?P<name>\w+)`?\s+(?P<type>\w+)",
    re.IGNORECASE,
)


class InvalidResourceUriError(ValueError):
    """Raised when a resource_uri isn't in the `bq://project.dataset.table` form."""


@dataclass(frozen=True)
class TableRef:
    project: str
    dataset: str
    table: str

    @property
    def fully_qualified(self) -> str:
        return f"{self.project}.{self.dataset}.{self.table}"


def parse_resource_uri(resource_uri: str) -> TableRef:
    match = _RESOURCE_URI_PATTERN.match(resource_uri)
    if not match:
        if resource_uri.startswith("postgres://"):
            # Post-Phase-6: real incidents from the pg-to-bq-sync pipeline
            # carry a postgres:// resource_uri (see
            # app/agents/pipeline_health.py) -- automated patch generation
            # only understands BigQuery today, so surface a clear,
            # self-explanatory tool error instead of a raw parse failure.
            # The model sees this text verbatim in the next turn and
            # should explain the limitation rather than retry blindly.
            raise InvalidResourceUriError(
                f"generate_and_test_patch does not yet support Postgres resources "
                f"({resource_uri!r}) -- automated patch generation currently only "
                "works against BigQuery (bq://project.dataset.table). Diagnose the "
                "root cause and recommend a manual fix instead of retrying this tool."
            )
        raise InvalidResourceUriError(
            f"Expected 'bq://project.dataset.table', got: {resource_uri!r}"
        )
    return TableRef(project=match["project"], dataset=match["dataset"], table=match["table"])


def sandbox_dataset_id(incident_id: str) -> str:
    settings = get_settings()
    safe_suffix = re.sub(r"[^a-zA-Z0-9_]", "_", incident_id.lower())
    return f"{settings.warden_bq_sandbox_dataset_prefix}{safe_suffix}"


def contains_destructive_statement(sql: str) -> bool:
    return bool(_DESTRUCTIVE_PATTERN.search(sql))


def rewrite_sql_for_dataset(sql: str, source: TableRef, new_dataset: str) -> str:
    """Point a SQL statement at a different dataset, same project/table.

    Purely textual: replaces the `project.dataset.table` (and bare
    `dataset.table`) qualifiers the patch generator is instructed to use.
    """
    rewritten = re.sub(
        rf"{re.escape(source.project)}\.{re.escape(source.dataset)}\.{re.escape(source.table)}",
        f"{source.project}.{new_dataset}.{source.table}",
        sql,
    )
    return re.sub(
        rf"(?<![.\w]){re.escape(source.dataset)}\.{re.escape(source.table)}",
        f"{new_dataset}.{source.table}",
        rewritten,
    )


def parse_added_columns(sql: str) -> list[ColumnSpec]:
    """Best-effort extraction of `ADD COLUMN` clauses from a DDL statement.

    Used by the local (BigQuery-free) sandbox executor to synthesize a
    plausible before/after schema diff for the demo UI.
    """
    return [
        ColumnSpec(name=m["name"], type=m["type"].upper(), mode="NULLABLE")
        for m in _ADD_COLUMN_PATTERN.finditer(sql)
    ]


@lru_cache
def get_bigquery_client() -> bigquery.Client:
    settings = get_settings()
    return bigquery.Client(project=settings.google_cloud_project)


async def get_dataset_location(client: bigquery.Client, project: str, dataset_id: str) -> str:
    """The source dataset's actual region/multi-region (e.g. `us-central1`
    or `US`). BigQuery query jobs can only reference datasets that live in
    the *same* location, so the sandbox dataset must be created there too
    -- otherwise `CREATE TABLE ... CLONE` fails with a confusing
    "Dataset ... was not found in location" error rather than anything
    mentioning a location mismatch."""

    def _get() -> str:
        return client.get_dataset(bigquery.DatasetReference(project, dataset_id)).location or "US"

    return await asyncio.to_thread(_get)


async def ensure_sandbox_dataset(
    client: bigquery.Client,
    project: str,
    dataset_id: str,
    expiration_hours: int,
    *,
    location: str,
) -> None:
    def _ensure() -> None:
        dataset_ref = bigquery.DatasetReference(project, dataset_id)
        dataset = bigquery.Dataset(dataset_ref)
        dataset.location = location
        dataset.default_table_expiration_ms = expiration_hours * 60 * 60 * 1000
        client.create_dataset(dataset, exists_ok=True)

    await asyncio.to_thread(_ensure)


async def clone_table_to_sandbox(
    client: bigquery.Client, source: TableRef, sandbox_dataset: str
) -> str:
    destination = f"{source.project}.{sandbox_dataset}.{source.table}"
    sql = f"CREATE OR REPLACE TABLE `{destination}` CLONE `{source.fully_qualified}`"

    def _clone() -> None:
        client.query(sql).result()

    await asyncio.to_thread(_clone)
    return destination


async def get_table_schema(
    client: bigquery.Client, project: str, dataset: str, table: str
) -> list[ColumnSpec]:
    def _get() -> list[ColumnSpec]:
        bq_table = client.get_table(f"{project}.{dataset}.{table}")
        return [
            ColumnSpec(
                name=field.name,
                type=field.field_type or "STRING",
                mode=field.mode or "NULLABLE",
                description=field.description,
            )
            for field in bq_table.schema
        ]

    return await asyncio.to_thread(_get)


async def run_sandbox_statement(client: bigquery.Client, sql: str) -> tuple[int, int]:
    """Execute `sql` and return `(elapsed_ms, affected_row_count)`."""

    def _run() -> tuple[int, int]:
        start = time.monotonic()
        job = client.query(sql)
        job.result()
        elapsed_ms = int((time.monotonic() - start) * 1000)
        affected = job.num_dml_affected_rows or 0
        return elapsed_ms, affected

    return await asyncio.to_thread(_run)


async def dry_run_bytes_processed(client: bigquery.Client, sql: str) -> int:
    def _dry_run() -> int:
        job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        job = client.query(sql, job_config=job_config)
        return int(job.total_bytes_processed or 0)

    return await asyncio.to_thread(_dry_run)
