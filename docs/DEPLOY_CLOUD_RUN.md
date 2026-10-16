# Deploying IntentSeal Delegate to Cloud Run

One Cloud Run service runs the gateway API, the Console (served at `/`) and the live demo. The image is built by
Cloud Build, so nothing heavy runs on your machine.

## Before you start

1. A Google Cloud project for IntentSeal with billing enabled (Cloud Run and Vertex AI need it; a demo costs cents).
   Use a project dedicated to IntentSeal.
2. `gcloud auth login` with an account that owns that project.

## Deploy

```powershell
.\scripts\deploy_cloud_run.ps1 -Project <your-intentseal-project> -Region asia-southeast1 -UseVertex
```

The script enables the APIs, creates the Artifact Registry repository and two secrets (it asks you to paste the
values: the API keys line, and a Gemini API key unless you use Vertex AI), grants the runtime service account access,
then runs `gcloud builds submit --config deploy/cloudbuild.yaml`. It prints the service URL and the Console's
read-only key.

## Gemini: API key or Vertex AI

- `-UseVertex`: the Gemini agents (Google ADK) call Gemini on Vertex AI with the service's own identity (no key).
- Without it: they use `GEMINI_API_KEY` from Secret Manager. The contract drafter and receipt writer use the router's
  `gemini` provider (the key) in both cases.

## Known limits of the prototype deployment

- One instance (`--max-instances=1`): the live demo keeps its sandboxes in memory.
- SQLite on the instance's disk: evidence resets when the instance is replaced. The store already runs on PostgreSQL
  (`docker compose`); on Google Cloud, set `INTENTSEAL_DATABASE_URL` to a Cloud SQL for PostgreSQL instance for durable
  evidence across instances.
