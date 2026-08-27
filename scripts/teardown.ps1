<#
.SYNOPSIS
    Deletes everything scripts/deploy.ps1 created.

.DESCRIPTION
    Removes the two Cloud Run services, the two runtime service accounts,
    and (optionally) the Artifact Registry repository. Deliberately does
    NOT touch Firestore, the BigQuery dataset, or any IAM bindings on your
    own user account -- those were created manually per
    docs/architecture.md's GCP setup walkthrough and aren't
    scripts/deploy.ps1's to clean up.

.PARAMETER ProjectId
    GCP project ID. Defaults to the active `gcloud config` project.

.PARAMETER Region
    Region the services were deployed to.

.PARAMETER RemoveArtifactRegistry
    Also delete the Artifact Registry repository (and every image tag in
    it). Off by default since it's harmless/cheap to leave.

.EXAMPLE
    ./scripts/teardown.ps1 -ProjectId dataengineering-505822
#>
param(
    [string]$ProjectId = (gcloud config get-value project 2>$null),
    [string]$Region = "us-central1",
    [string]$Repository = "datamesh-warden",
    [switch]$RemoveArtifactRegistry
)

$ErrorActionPreference = "Continue"

if ([string]::IsNullOrWhiteSpace($ProjectId)) {
    throw "No project set. Pass -ProjectId or run 'gcloud config set project <id>'."
}

Write-Host "==> Deleting Cloud Run services" -ForegroundColor Cyan
gcloud run services delete warden-ui --region=$Region --project=$ProjectId --quiet
gcloud run services delete warden-api --region=$Region --project=$ProjectId --quiet

Write-Host "==> Deleting runtime service accounts" -ForegroundColor Cyan
gcloud iam service-accounts delete "warden-api-run@$ProjectId.iam.gserviceaccount.com" --project=$ProjectId --quiet
gcloud iam service-accounts delete "warden-ui-run@$ProjectId.iam.gserviceaccount.com" --project=$ProjectId --quiet

if ($RemoveArtifactRegistry) {
    Write-Host "==> Deleting Artifact Registry repository" -ForegroundColor Cyan
    gcloud artifacts repositories delete $Repository --location=$Region --project=$ProjectId --quiet
} else {
    Write-Host "==> Leaving Artifact Registry repository in place (pass -RemoveArtifactRegistry to delete it too)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "==> Teardown complete. Firestore/BigQuery/Vertex AI resources were left untouched." -ForegroundColor Green
