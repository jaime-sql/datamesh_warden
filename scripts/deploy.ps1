<#
.SYNOPSIS
    Builds both service images via Cloud Build and deploys them to Cloud Run.

.DESCRIPTION
    Idempotent end-to-end deploy for DataMesh Warden (see docs/architecture.md
    Phase 6):
      1. Enables Cloud Run / Cloud Build / Artifact Registry APIs.
      2. Creates the Artifact Registry Docker repository if it doesn't exist.
      3. Creates two least-privilege runtime service accounts if they don't
         exist: one for the API, one for the UI.
      4. Submits deploy/cloudbuild.yaml to build + push both images.
      5. Deploys `warden-api` as a PRIVATE Cloud Run service
         (--no-allow-unauthenticated) -- it is never directly reachable from
         the internet.
      6. Deploys `warden-ui` as a PUBLIC Cloud Run service, pointed at the
         API's URL, with `--no-cpu-throttling` so its background reruns
         behave predictably.
      7. Grants the UI's service account permission to invoke the API
         service (Cloud Run service-to-service auth via ID tokens --
         see ui/api_client.py's `fetch_cloud_run_id_token`).

    Firestore, the BigQuery dataset, and Vertex AI are assumed to already
    exist (see docs/architecture.md's "Cloud mode validated against a real
    GCP project" note) -- this script only touches Cloud Run/Build/Artifact
    Registry resources.

.PARAMETER ProjectId
    GCP project ID. Defaults to the active `gcloud config` project.

.PARAMETER Region
    Region for Artifact Registry + both Cloud Run services. Must match
    (or be compatible with) the region of your Firestore database and
    BigQuery datasets.

.PARAMETER Tag
    Image tag for this deploy. Defaults to the current git short SHA (falls
    back to a timestamp if not in a git repo).

.EXAMPLE
    ./scripts/deploy.ps1 -ProjectId dataengineering-505822 -Region us-central1
#>
param(
    [string]$ProjectId = (gcloud config get-value project 2>$null),
    [string]$Region = "us-central1",
    [string]$Repository = "datamesh-warden",
    [string]$Tag = $(try { git rev-parse --short HEAD 2>$null } catch { "" }),
    [string]$OrchestratorModel = "gemini-2.5-pro"
)

# "Continue" (not "Stop"): gcloud routinely writes informational/expected
# messages to stderr (e.g. a NOT_FOUND from an existence check we expect
# to fail the first time), which PowerShell would otherwise treat as a
# terminating NativeCommandError under "Stop". Critical steps check
# $LASTEXITCODE explicitly instead.
$ErrorActionPreference = "Continue"

function Assert-LastExitCode([string]$step) {
    if ($LASTEXITCODE -ne 0) {
        throw "Step failed: $step (exit code $LASTEXITCODE)"
    }
}

if ([string]::IsNullOrWhiteSpace($ProjectId)) {
    throw "No project set. Pass -ProjectId or run 'gcloud config set project <id>'."
}
if ([string]::IsNullOrWhiteSpace($Tag)) {
    $Tag = Get-Date -Format "yyyyMMddHHmmss"
}

$ApiServiceAccount = "warden-api-run@$ProjectId.iam.gserviceaccount.com"
$UiServiceAccount = "warden-ui-run@$ProjectId.iam.gserviceaccount.com"
$ApiImage = "$Region-docker.pkg.dev/$ProjectId/$Repository/warden-api:$Tag"
$UiImage = "$Region-docker.pkg.dev/$ProjectId/$Repository/warden-ui:$Tag"

Write-Host "==> Project: $ProjectId | Region: $Region | Tag: $Tag" -ForegroundColor Cyan

Write-Host "==> Enabling required APIs" -ForegroundColor Cyan
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com `
    --project=$ProjectId
Assert-LastExitCode "enable APIs"

Write-Host "==> Ensuring Artifact Registry repository exists" -ForegroundColor Cyan
$repoExists = gcloud artifacts repositories describe $Repository --location=$Region --project=$ProjectId 2>$null
if (-not $repoExists) {
    gcloud artifacts repositories create $Repository `
        --repository-format=docker --location=$Region --project=$ProjectId `
        --description="DataMesh Warden service images"
    Assert-LastExitCode "create Artifact Registry repository"
}

Write-Host "==> Ensuring runtime service accounts exist" -ForegroundColor Cyan
foreach ($sa in @(
    @{ Name = "warden-api-run"; Display = "DataMesh Warden API (Cloud Run)" },
    @{ Name = "warden-ui-run"; Display = "DataMesh Warden UI (Cloud Run)" }
)) {
    $exists = gcloud iam service-accounts describe "$($sa.Name)@$ProjectId.iam.gserviceaccount.com" --project=$ProjectId 2>$null
    if (-not $exists) {
        gcloud iam service-accounts create $sa.Name --display-name=$sa.Display --project=$ProjectId
        Assert-LastExitCode "create service account $($sa.Name)"
    }
}

Write-Host "==> Granting the API service account least-privilege data access" -ForegroundColor Cyan
foreach ($role in @("roles/datastore.user", "roles/bigquery.admin", "roles/aiplatform.user")) {
    gcloud projects add-iam-policy-binding $ProjectId `
        --member="serviceAccount:$ApiServiceAccount" --role=$role --condition=None | Out-Null
    Assert-LastExitCode "grant $role to API service account"
}
# The UI service account intentionally gets NO project-level data roles --
# it never touches Firestore/BigQuery/Gemini directly (see
# docs/architecture.md's Phase 5 note). It only gets run.invoker on the
# API service specifically, granted after the API service exists below.

Write-Host "==> Submitting Cloud Build (building + pushing both images)" -ForegroundColor Cyan
gcloud builds submit `
    --config=deploy/cloudbuild.yaml `
    --substitutions="_REGION=$Region,_REPOSITORY=$Repository,_TAG=$Tag" `
    --project=$ProjectId `
    .
Assert-LastExitCode "Cloud Build"

Write-Host "==> Deploying warden-api (private -- no unauthenticated access)" -ForegroundColor Cyan
gcloud run deploy warden-api `
    --image=$ApiImage `
    --region=$Region `
    --project=$ProjectId `
    --service-account=$ApiServiceAccount `
    --no-allow-unauthenticated `
    --no-cpu-throttling `
    --min-instances=0 `
    --max-instances=2 `
    --memory=512Mi `
    --set-env-vars="WARDEN_MODE=cloud,GOOGLE_CLOUD_PROJECT=$ProjectId,GOOGLE_CLOUD_LOCATION=$Region,WARDEN_USE_VERTEX=true,WARDEN_ORCHESTRATOR_MODEL=$OrchestratorModel"
Assert-LastExitCode "deploy warden-api"

$ApiUrl = gcloud run services describe warden-api --region=$Region --project=$ProjectId --format="value(status.url)"
Write-Host "    warden-api URL: $ApiUrl"

Write-Host "==> Deploying warden-ui (public)" -ForegroundColor Cyan
gcloud run deploy warden-ui `
    --image=$UiImage `
    --region=$Region `
    --project=$ProjectId `
    --service-account=$UiServiceAccount `
    --allow-unauthenticated `
    --no-cpu-throttling `
    --min-instances=0 `
    --max-instances=2 `
    --memory=512Mi `
    --set-env-vars="WARDEN_API_BASE_URL=$ApiUrl"
Assert-LastExitCode "deploy warden-ui"

$UiUrl = gcloud run services describe warden-ui --region=$Region --project=$ProjectId --format="value(status.url)"

Write-Host "==> Granting warden-ui permission to invoke warden-api" -ForegroundColor Cyan
gcloud run services add-iam-policy-binding warden-api `
    --region=$Region --project=$ProjectId `
    --member="serviceAccount:$UiServiceAccount" --role="roles/run.invoker" | Out-Null
Assert-LastExitCode "grant run.invoker to UI service account"

Write-Host ""
Write-Host "==> Deploy complete" -ForegroundColor Green
Write-Host "    API (private): $ApiUrl"
Write-Host "    UI  (public):  $UiUrl"
Write-Host ""
Write-Host "Open the UI URL in a browser to use the war room."
Write-Host "To call the API directly for debugging, attach your own identity token:"
Write-Host "  `$token = gcloud auth print-identity-token"
Write-Host "  curl -H `"Authorization: Bearer `$token`" $ApiUrl/status"
