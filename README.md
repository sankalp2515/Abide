# IntentSeal Delegate

**Hand off the task. Approve it once.**

IntentSeal Delegate makes it safe to hand everyday office work to a team of AI agents. The employee's own request
becomes the permission: Gemini turns it into a short card of what the agents may and may not do, every agent tool
call is checked against that card, and the employee gets a receipt of what actually happened.

Built for the Google Cloud AI Builder Cup 2026, track **Future of Work & Enterprise Productivity**.

![Architecture](docs/architecture.svg)

## The problem

Companies want agents to read a supplier's quote, draft the reply, issue a refund or update an order. Today they have
two bad choices: approve every step (people click "yes" without reading, and the work stops at every step) or grant
broad tool permissions up front (one hidden line in a PDF or support ticket can tell an agent to send the customer list
outside the company). Neither is tied to what the employee actually asked for.

## How it works

| Step | What happens |
|---|---|
| **Ask** | The employee types the task in their words, for example *"Read the paint supplier's quote and write a reply. Don't send it yet."* Gemini drafts a permission card. A grounding validator keeps a line only if it quotes the employee's words and every name, file, order or amount resolves from the company's trusted directory. |
| **Confirm once** | The employee confirms the card (bound to its hash). The agents then work without approval pop-ups. Every tool call is checked: who receives it, which file or record, how much, which fields. A call outside the card is stopped; if it traces back to a document the agents read, it is stopped without asking anyone. A company rule such as "confirm before an email leaves" asks one question for one exact action. |
| **Prove** | Each step, stop and approval becomes a numbered, hash-chained receipt. Gemini writes the summary, and every sentence must cite receipts; a sentence the receipts do not support is dropped. |

Two layers work together: the IntentSeal content filter removes instructions hidden in what the agents read, and the
card stops actions that were never asked for, even when the filter misses.

## Results (local evaluation, two businesses)

28 attacks across nine attack types and 26 normal tasks, in a sandbox office (Lotus Interiors) and a sandbox support
desk (Acme Home). Outcomes come from the sandbox's execution log, not from what the agents say.

| Setup | Attacks that executed something unauthorised | Normal tasks finished | Questions per task |
|---|---|---|---|
| No protection | 28 of 28 | 26 of 26 | 0.0 |
| Ask before every step | 28 of 28 | 22 of 26 | 1.62 |
| **IntentSeal Delegate** | **0 of 28** | **26 of 26** | **0.38** |
| Delegate with the content filter off | 0 of 28 | 26 of 26 | 0.38 |

The agents in this evaluation are scripted to obey every instruction they read (a worst case), the scenarios are our
own, and the confidence intervals are wide. Method and limits: [docs/EVAL.md](docs/EVAL.md).

## Quick start (Docker)

Requirements: Docker with Compose v2, about 4 GB of free memory.

```bash
cp .env.example .env        # optional: add GEMINI_API_KEY so Gemini drafts cards and writes receipts
docker compose up --build -d
docker compose ps           # wait until both services are "healthy" (the first start loads the classifier)
```

Open <http://localhost:8000/#/delegate> and sign in with `local-project-key` (the local employee key). API reference:
<http://localhost:8000/docs>. Stop with `docker compose down` (add `-v` to delete the data volumes).

Without a Gemini key, the word rules draft the card and a template writes the receipt; everything else works the same.

| Service | Image | Purpose |
|---|---|---|
| `gateway` | built from `Dockerfile` | API, Delegate service, Console (served at `/`), content filter, Gemini agents (Google ADK). Non-root, all capabilities dropped. |
| `postgres` | `postgres:16.4-alpine` | All evidence: cards, receipts, approvals, decisions. Not published outside the Compose network. |

## Integrate

**Google ADK** — two callbacks on any agent:

```python
from intentseal_core.delegate.adk import AdkGuard

guard = AdkGuard(service, workflow_id, agent_name="reader")
agent = LlmAgent(..., before_tool_callback=guard.before_tool, after_tool_callback=guard.after_tool)
```

**Any framework** — REST: `POST /v1/delegations` (from the employee's words), `POST /v1/delegations/{id}/confirm`,
`POST /v1/delegations/{id}/check` before each tool call, `POST /v1/delegations/{id}/executed` after it, and
`GET /v1/delegations/{id}/report` for the receipt.

**A new business** — one policy file: tools and the fields each may carry, contacts, records, business verbs (for
example "refund" is a purchase), amount currency and company rules. The office and the support desk differ only in
[`policies/delegate/workspace.yaml`](policies/delegate/workspace.yaml) and
[`policies/delegate/desk.yaml`](policies/delegate/desk.yaml).

## Repository layout

| Path | Contents |
|---|---|
| `packages/core/intentseal_core/delegate/` | Contract model, compiler and validator, gate, approvals, handoff seal, receipts, service, ADK connector |
| `packages/core/intentseal_core/` | IntentSeal content filter engine (rules, classifier, judge router), store (SQLite or PostgreSQL) |
| `services/gateway/` | FastAPI gateway: Delegate API, live sandbox demo, Gemini agent team, Console hosting |
| `apps/console/` | React Console: landing page, Delegate page, decisions, replay, review |
| `policies/delegate/` | Business policies for the two sandboxes |
| `eval/` | Evaluation harness and the published report |
| `examples/delegate_demo/` | Terminal demo of the office scenario in each setup |
| `deploy/`, `scripts/deploy_cloud_run.ps1` | Cloud Build and Cloud Run deployment |
| `docs/` | Architecture, evaluation, testing guide, walkthrough, deployment, design notes |

## Development

```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -c requirements.lock -e "packages/core[engine,classifiers,ocr,postgres]" -e packages/sdk -e services/gateway -e eval "google-adk==2.12.0" pytest ruff
pytest -q                                            # Python tests
ruff check .
cd apps/console && npm ci && npx tsc --noEmit && node --test tests/*.test.cjs && npm run build
python -m intentseal_eval.delegate                   # re-run the evaluation
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — components, request flow, data model, security model
- [Evaluation](docs/EVAL.md) — results, method and limits
- [Testing guide](docs/TESTING.md) — every claim and how to check it by hand
- [Walkthrough](docs/WALKTHROUGH.md) — the full user journey with screenshots
- [Deploying to Cloud Run](docs/DEPLOY_CLOUD_RUN.md)
- [Engineering review](docs/ENGINEERING_REVIEW.md) and [design notes](docs/DESIGN_NOTES.md)

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Security reports: [SECURITY.md](SECURITY.md).
