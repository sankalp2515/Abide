# IntentSeal Delegate: one-time setup and deploy to Cloud Run. Run from the repository root in PowerShell.
#
#   .\scripts\deploy_cloud_run.ps1 -Project my-intentseal-project [-Region asia-southeast1]
#
# What it does (each step is idempotent):
#   1. points gcloud at the project you name (it never uses the currently active project silently)
#   2. enables Cloud Run, Cloud Build, Artifact Registry, Secret Manager (and Vertex AI)
#   3. creates the Artifact Registry repository "intentseal"
#   4. creates the two secrets if missing, asking you to paste values (nothing is written to disk)
#   5. lets Cloud Build build, push and deploy (deploy/cloudbuild.yaml)
param(
  [Parameter(Mandatory = $true)][string]$Project,
  [string]$Region = "asia-southeast1",
  [string]$PublicKey = "",
  [switch]$UseVertex
)
$ErrorActionPreference = "Stop"
if (-not $PublicKey) { $PublicKey = "pub-" + [guid]::NewGuid().ToString("N").Substring(0, 16) }

gcloud config set project $Project | Out-Null
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com `
  secretmanager.googleapis.com aiplatform.googleapis.com

$repo = gcloud artifacts repositories list --location=$Region --filter="name~intentseal" --format="value(name)"
if (-not $repo) {
  gcloud artifacts repositories create intentseal --repository-format=docker --location=$Region `
    --description="IntentSeal Delegate images"
}

function Ensure-Secret([string]$Name, [string]$Prompt) {
  $exists = gcloud secrets list --filter="name~$Name" --format="value(name)"
  if (-not $exists) {
    $value = Read-Host -Prompt $Prompt -AsSecureString
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($value))
    $plain | gcloud secrets create $Name --data-file=- --replication-policy=automatic
  }
}
Ensure-Secret "intentseal-api-keys" "INTENTSEAL_API_KEYS (e.g. employee:<random>:project,reviewer:<random>:admin)"
Ensure-Secret "intentseal-gemini-key" "GEMINI_API_KEY (Gemini API key; any value if you use Vertex AI)"
# The handoff-seal key: random, generated here, never shown.
if (-not (gcloud secrets list --filter="name~intentseal-delegate-secret" --format="value(name)")) {
  $bytes = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  [Convert]::ToBase64String($bytes) | gcloud secrets create intentseal-delegate-secret --data-file=- --replication-policy=automatic
}

# Cloud Run's runtime service account reads the secrets (and calls Vertex AI when -UseVertex is set).
$number = gcloud projects describe $Project --format="value(projectNumber)"
$sa = "$number-compute@developer.gserviceaccount.com"
foreach ($s in "intentseal-api-keys", "intentseal-gemini-key", "intentseal-delegate-secret") {
  gcloud secrets add-iam-policy-binding $s --member="serviceAccount:$sa" --role="roles/secretmanager.secretAccessor" | Out-Null
}
if ($UseVertex) {
  gcloud projects add-iam-policy-binding $Project --member="serviceAccount:$sa" --role="roles/aiplatform.user" | Out-Null
}

$vertex = if ($UseVertex) { "true" } else { "false" }
gcloud builds submit --config deploy/cloudbuild.yaml `
  --substitutions="_REGION=$Region,_PUBLIC_KEY=$PublicKey,_USE_VERTEX=$vertex"
gcloud run services describe intentseal-delegate --region=$Region --format="value(status.url)"
Write-Host "Console read-only key (also baked into the Console build): $PublicKey"
