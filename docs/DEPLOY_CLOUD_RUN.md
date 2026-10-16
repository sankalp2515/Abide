# Deploying IntentSeal Delegate to Cloud Run

One Cloud Run service runs the gateway API, the Console (served at `/`) and the live demo. Cloud Build builds the image,
so nothing heavy runs on your machine. Gemini (Vertex AI) and Model Armor are called with the service's own identity:
there is no model API key to store or rotate.

| Google Cloud service | Use |
|---|---|
| Cloud Run | The service (scales to zero) |
| Cloud Build, Artifact Registry | Build and store the image |
| Vertex AI (Gemini) | Draft the permission card, write the receipt, run the live agent team |
| Model Armor | Second screen on everything agents read (prompt injection, jailbreak, malicious links) |
| Secret Manager | The API keys line and the handoff-seal key |
| IAM | A dedicated runtime service account: Vertex AI user, Model Armor user, two secrets, nothing else |

## Before you start

1. A Google Cloud project for IntentSeal with billing enabled, and a budget with alerts on it.
2. `gcloud auth login` with an account that owns that project.

## Deploy

```powershell
.\scripts\deploy_cloud_run.ps1 -Project <your-intentseal-project> -Region asia-southeast1
```

Every command names the project explicitly; your active gcloud project is never read or changed. The script enables
the APIs, creates the Artifact Registry repository, the Model Armor template, the runtime service account and its roles,
and the two secrets (it asks you to paste the API keys line; the seal key is generated), then runs
`gcloud builds submit --config deploy/cloudbuild.yaml`. It prints the service URL and the Console's read-only key.

Settings the deployment turns on (`deploy/cloudbuild.yaml`):

| Variable | Value | Why |
|---|---|---|
| `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_QUOTA_PROJECT` | the project | Gemini on Vertex AI, billed to this project |
| `INTENTSEAL_MODEL_ARMOR_TEMPLATE` | `projects/<p>/locations/us-central1/templates/intentseal-delegate` | Model Armor screen |
| `INTENTSEAL_TRIAL_KEYS` | `1` | Visitors get a private 7-day key without contacting anyone ([INTEGRATE.md](INTEGRATE.md)) |
| `INTENTSEAL_TRUSTED_PROXIES` | `*` | Real client addresses behind Cloud Run's front end (per-address limits) |

## Cost control

- The router's daily token cap for Vertex AI (`config/providers.yaml`, `budget.daily_tokens`) stops model calls for
  the rest of the day once reached; cards then come from the word rules and receipts from the template.
- `--min-instances=0`: no cost while idle. A Cloud Billing budget with alerts is the outer guard (alerts do not stop
  spending by themselves).

## Known limits of the prototype deployment

- One instance (`--max-instances=1`): the live demo keeps its sandboxes in memory.
- SQLite on the instance's disk: evidence resets when the instance is replaced. The store already runs on PostgreSQL
  (`docker compose`); on Google Cloud, set `INTENTSEAL_DATABASE_URL` to a Cloud SQL for PostgreSQL instance for durable
  evidence across instances.
