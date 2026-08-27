<#
.SYNOPSIS
    Resets the demo BigQuery table back to its pristine "email column
    missing" state, so the "Schema drift" preset can be replayed.

.DESCRIPTION
    The demo scenarios are NOT idempotent by design: "Schema drift"'s
    premise is "the `email` column was dropped from `orders`", and
    approving its patch really does re-add that column via `ALTER TABLE`.
    Running the same demo again afterwards fails with a genuine (not a
    bug) `Column already exists: email` error -- see docs/architecture.md's
    Phase 6 note. This script drops it again so the demo can be replayed.

    Only touches the `email` column; leaves order_id/customer_id/total
    alone. Safe to run repeatedly (no-ops with a clear error if the
    column is already gone -- BigQuery's `DROP COLUMN IF EXISTS` isn't
    supported, so this just surfaces that error rather than swallowing it).

.PARAMETER ProjectId
    GCP project ID. Defaults to the active `gcloud config` project.

.PARAMETER Dataset
    Dataset containing the demo `orders` table.

.EXAMPLE
    ./scripts/reset-demo-data.ps1 -ProjectId dataengineering-505822
#>
param(
    [string]$ProjectId = (gcloud config get-value project 2>$null),
    [string]$Dataset = "sales",
    [string]$Table = "orders"
)

$ErrorActionPreference = "Continue"

if ([string]::IsNullOrWhiteSpace($ProjectId)) {
    throw "No project set. Pass -ProjectId or run 'gcloud config set project <id>'."
}

Write-Host "==> Dropping 'email' column from $ProjectId.$Dataset.$Table (if present)" -ForegroundColor Cyan
bq query --project_id=$ProjectId --use_legacy_sql=false "ALTER TABLE $Dataset.$Table DROP COLUMN email"

Write-Host "==> Current schema:" -ForegroundColor Cyan
bq show --schema --format=prettyjson "${ProjectId}:${Dataset}.${Table}"
