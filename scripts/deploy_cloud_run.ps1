# Abide: one-time setup and deploy to Cloud Run. Run from the repository root in PowerShell.
#
#   .\scripts\deploy_cloud_run.ps1 -Project my-intentseal-project [-Region us-central1]
#
# Every command names -Project explicitly: the machine's active gcloud project is never read or changed.
# What it does (each step is idempotent):
#   1. enables Cloud Run, Cloud Build, Artifact Registry, Secret Manager, Vertex AI and Model Armor
#   2. creates the Artifact Registry repository "intentseal" and the Model Armor template
#   3. creates a runtime service account with only what it needs (Vertex AI user, Model Armor user, two secrets)
#   4. creates the two secrets if missing (the API keys line is pasted, never written to disk; the seal key is random)
#   5. lets Cloud Build build, push and deploy (deploy/cloudbuild.yaml)
param(
  [Parameter(Mandatory = $true)][string]$Project,
  [string]$Region = "us-central1",
  [string]$ArmorLocation = "us-central1",
  [string]$PublicKey = ""
)
$ErrorActionPreference = "Stop"
if (-not $PublicKey) { $PublicKey = "pub-" + [guid]::NewGuid().ToString("N").Substring(0, 16) }
$P = "--project=$Project"

gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com `
  secretmanager.googleapis.com aiplatform.googleapis.com modelarmor.googleapis.com $P

if (-not (gcloud artifacts repositories list --location=$Region --filter="name~intentseal" --format="value(name)" $P)) {
  gcloud artifacts repositories create intentseal --repository-format=docker --location=$Region `
    --description="Abide images" $P
}

# Model Armor: prompt injection / jailbreak and malicious links hold content; sensitive data is recorded.
$token = gcloud auth print-access-token
$armor = "https://modelarmor.$ArmorLocation.rep.googleapis.com/v1/projects/$Project/locations/$ArmorLocation/templates"
$headers = @{ Authorization = "Bearer $token"; "x-goog-user-project" = $Project }
try { Invoke-RestMethod -Uri "$armor/intentseal-delegate" -Headers $headers | Out-Null }
catch {
  $body = '{"filterConfig":{"piAndJailbreakFilterSettings":{"filterEnforcement":"ENABLED","confidenceLevel":"MEDIUM_AND_ABOVE"},' +
          '"maliciousUriFilterSettings":{"filterEnforcement":"ENABLED"},"sdpSettings":{"basicConfig":{"filterEnforcement":"ENABLED"}}}}'
  Invoke-RestMethod -Method Post -Uri "$armor`?templateId=intentseal-delegate" -Headers $headers `
    -ContentType "application/json" -Body $body | Out-Null
}

# A runtime identity with only what the service needs.
$sa = "intentseal-run@$Project.iam.gserviceaccount.com"
if (-not (gcloud iam service-accounts list --filter="email=$sa" --format="value(email)" $P)) {
  gcloud iam service-accounts create intentseal-run --display-name="Abide runtime" $P
}
foreach ($role in "roles/aiplatform.user", "roles/modelarmor.user") {
  gcloud projects add-iam-policy-binding $Project --member="serviceAccount:$sa" --role=$role --condition=None | Out-Null
}

# Newer projects run Cloud Build as the default compute account: let it build, push and deploy as $sa.
$num = gcloud projects describe $Project --format="value(projectNumber)"
$builder = "serviceAccount:$num-compute@developer.gserviceaccount.com"
foreach ($role in "roles/cloudbuild.builds.builder", "roles/run.admin") {
  gcloud projects add-iam-policy-binding $Project --member=$builder --role=$role --condition=None | Out-Null
}
gcloud iam service-accounts add-iam-policy-binding $sa --member=$builder --role=roles/iam.serviceAccountUser $P | Out-Null

function Ensure-Secret([string]$Name, [scriptblock]$Value) {
  if (-not (gcloud secrets list --filter="name~$Name" --format="value(name)" $P)) {
    (& $Value) | gcloud secrets create $Name --data-file=- --replication-policy=automatic $P
  }
  gcloud secrets add-iam-policy-binding $Name --member="serviceAccount:$sa" `
    --role="roles/secretmanager.secretAccessor" $P | Out-Null
}
Ensure-Secret "intentseal-api-keys" {
  $v = Read-Host -Prompt "INTENTSEAL_API_KEYS (e.g. employee:<random>:project,reviewer:<random>:admin)" -AsSecureString
  [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($v))
}
Ensure-Secret "intentseal-delegate-secret" {       # the handoff-seal key: random, generated here, never shown
  $bytes = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  [Convert]::ToBase64String($bytes)
}

# Cloud SQL (PostgreSQL): cards, receipts, workspaces and trial keys survive restarts and are shared by instances.
gcloud services enable sqladmin.googleapis.com $P
if (-not (gcloud sql instances list --filter="name=abide-db" --format="value(name)" $P)) {
  gcloud sql instances create abide-db --database-version=POSTGRES_16 --edition=ENTERPRISE --tier=db-f1-micro `
    --region=$Region --storage-type=HDD --storage-size=10 $P
  gcloud sql databases create abide --instance=abide-db $P
  $bytes = New-Object byte[] 24; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $pw = -join ($bytes | ForEach-Object { $_.ToString("x2") })
  gcloud sql users create abide --instance=abide-db --password=$pw $P
  Ensure-Secret "intentseal-database-url" { "postgresql://abide:$pw@/abide?host=/cloudsql/${Project}:${Region}:abide-db" }
}
gcloud projects add-iam-policy-binding $Project --member="serviceAccount:$sa" --role=roles/cloudsql.client `
  --condition=None | Out-Null

# BigQuery: every receipt streamed to abide_audit.receipts for analysis (the Console's insights panel).
gcloud services enable bigquery.googleapis.com $P
if (-not (bq --project_id=$Project ls --datasets --format=json | Select-String '"abide_audit"')) {
  bq --project_id=$Project mk --dataset --location=US --description="Abide receipts for analysis" "${Project}:abide_audit"
  bq --project_id=$Project mk --table --time_partitioning_field=created --time_partitioning_type=DAY `
    --clustering_fields=principal,decision "${Project}:abide_audit.receipts" `
    "workflow_id:STRING,seq:INT64,rid:STRING,kind:STRING,agent:STRING,tool:STRING,decision:STRING,reason:STRING,ruling:STRING,traced_to:STRING,explanation:STRING,workspace:STRING,principal:STRING,created:TIMESTAMP,hash:STRING,data:STRING"
}
foreach ($role in "roles/bigquery.dataEditor", "roles/bigquery.jobUser") {
  gcloud projects add-iam-policy-binding $Project --member="serviceAccount:$sa" --role=$role --condition=None | Out-Null
}

gcloud builds submit --config deploy/cloudbuild.yaml $P `
  --substitutions="_REGION=$Region,_PUBLIC_KEY=$PublicKey,_SA=$sa,_ARMOR_LOCATION=$ArmorLocation"
gcloud run services describe abide --region=$Region --format="value(status.url)" $P
Write-Host "Console read-only key (also baked into the Console build): $PublicKey"
