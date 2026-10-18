<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/brand/abide-logo-dark.svg">
  <img alt="Abide" src="docs/brand/abide-logo-light.svg" width="420">
</picture>

### The security checkpoint between your AI agents and your business

**Hand off the task. Approve it once.** Anything you didn't ask for is stopped, even when a document tells the agent to do it.

[![CI](https://github.com/sankalp2515/Abide/actions/workflows/ci.yml/badge.svg)](https://github.com/sankalp2515/Abide/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-1D5E46)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11-1D5E46)](packages/core/pyproject.toml)
[![Live on Cloud Run](https://img.shields.io/badge/live-Cloud%20Run-4285F4?logo=googlecloud&logoColor=white)](https://abide-1041805455955.us-central1.run.app)
[![Gemini](https://img.shields.io/badge/Gemini%203.5%20Flash-Vertex%20AI-8E75B2?logo=googlegemini&logoColor=white)](docs/ARCHITECTURE.md)
[![Google ADK](https://img.shields.io/badge/agents-Google%20ADK-4285F4)](docs/INTEGRATE.md)

[**Try it live**](https://abide-1041805455955.us-central1.run.app/#/tasks?task=new&preset=attack) ·
[Evaluation](https://abide-1041805455955.us-central1.run.app/#/evaluation) ·
[Quick start](#quick-start) ·
[Bring your own agent](docs/INTEGRATE.md) ·
[Colab](examples/colab/abide_quickstart.ipynb) ·
[Architecture](docs/ARCHITECTURE.md)

<img src="docs/hero.gif" alt="Live: a hijacked Gemini agent tries to open another customer's order; Abide stops it and traces the instruction to the customer's ticket" width="860">

<sub>Recorded on the live deployment: real Gemini agents on Google ADK, one hijacked by a line hidden in a support ticket.</sub>

</div>

---

## Why Abide

AI agents now do real work: refunds, invoices, emails, records. But **an agent obeys whatever it reads**. One hidden line in a ticket, PDF or invoice can make it pay, send or export something nobody authorised.

Teams have two options today, and both fail:

- **Detect suspicious content.** Content filters catch obvious attacks, but polite, ordinary-looking instructions get through.
- **Approve every step.** People click "yes" out of habit, and the work stops at every step.

**Abide isn't another agent.** It's the checkpoint between agents and the business, and it asks a different question. A filter asks *"does this look dangerous?"*; Abide asks *"did the person authorise this action?"*

<div align="center">

| Same 28 attacks · same agents | Attacks that executed something unauthorised | Normal tasks finished |
|---|:---:|:---:|
| No protection | 28 / 28 | 26 / 26 |
| Approve every step | 28 / 28 | 22 / 26 |
| Detection only (content filter) | 12 / 28 | 26 / 26 |
| **Abide** | **0 / 28** | **26 / 26** |

</div>

<sub>64 scenarios (28 attack, 26 normal, 10 ambiguous), intended decisions marked before the run, outcomes counted from the sandbox's execution log. Our own suite, so zero failures is not proof of safety. [Method and limits →](docs/EVAL.md)</sub>

## How it works

<div align="center"><img src="docs/architecture.png" alt="Abide architecture" width="900"></div>

1. **Say it once.** The employee types the task in plain words. **Gemini 3.5 Flash on Vertex AI** turns it into a work order: *MAY read order 4521 · MAY refund it up to ₹2,000 · MAY email that customer · NEVER anything else.* Every line must quote the employee's words, or it is dropped.
2. **Approve once.** The employee confirms the order, which is bound to its hash. From then on the agents work without pop-ups.
3. **Every tool call is checked.** The action gate runs **fixed rules, not the model**: recipient, record, amount (across the whole task) and unknown fields, all against the confirmed order. Each call gets one of three answers:
   - **Ran**: inside the order.
   - **Stopped**: the instruction came from a document. Abide traces it to that document.
   - **Waits**: a genuinely new action. It needs one question, bound to that exact action, single use, 15-minute expiry.
4. **Everything leaves proof.** Every read is screened by **Model Armor** and Abide's filter and labelled untrusted. Handoffs between agents are HMAC-sealed, so the label can't be stripped. Every decision is a hash-chained receipt. Gemini explains each stop in plain words and writes the final receipt; every sentence cites a record.

<table>
<tr>
<td width="50%"><img src="docs/snapshots/1-work-order.png" alt="Work order"><br><sub><b>The employee's words become the work order</b></sub></td>
<td width="50%"><img src="docs/snapshots/2-stopped-traced.png" alt="Stopped and traced"><br><sub><b>Stopped, traced to the customer's ticket, explained by Gemini</b></sub></td>
</tr>
<tr>
<td><img src="docs/snapshots/3-evaluation.png" alt="Evaluation"><br><sub><b>Evaluation: detection 12/28 vs Abide 0/28</b></sub></td>
<td><img src="docs/snapshots/4-activity.png" alt="Activity"><br><sub><b>Activity: every decision and its source, with BigQuery insights</b></sub></td>
</tr>
</table>

## Quick start

### 1 · Try it live (no sign-up)

Open **[the one-click attack demo](https://abide-1041805455955.us-central1.run.app/#/tasks?task=new&preset=attack)**. A private 7-day trial key is created for you. Real Gemini agents work in a sandbox support desk, and one of them is hijacked on purpose. Confirm the work order and watch the Checkpoint.

### 2 · Run it locally (Docker)

```bash
cp .env.example .env               # optional: GEMINI_API_KEY for Gemini work orders and receipts
docker compose up --build -d       # gateway + PostgreSQL; first start loads the classifier
```

Open <http://localhost:8000/#/delegate> and sign in with `local-project-key`. API docs are at `/docs`. Without Google credentials, word rules draft the order and scripted stand-in agents run; everything else is identical. For Vertex AI and Model Armor, see [Deploying](docs/DEPLOY_CLOUD_RUN.md).

### 3 · Watch three agents work through Abide

```bash
docker compose --profile agents run --rm agents
```

The Intake, Accountant and Notifier team runs with an **agent key**, which can act but never approve:

- Its attempt to confirm its own work order is refused (403).
- A poisoned invoice tries three things; all are stopped and traced to the invoice.
- The real invoice is paid.
- One question (copying the finance team) waits for you in **Needs you**.

## Bring your own agent

One Python file, any framework. Your agents run on your side and ask Abide before each tool call.

```python
from abide import DelegateClient, ToolGate            # one file: GET /v1/sdk/abide.py

client = DelegateClient("https://abide-1041805455955.us-central1.run.app", AGENT_KEY)
card = await client.start("Pay invoice INV-1042 up to ₹15,000 and email Priya.", workspace="my-business")
# the person confirms the work order in the browser; the agent key cannot
pay = ToolGate(client, card["workflow_id"], "accountant").wrap(pay_invoice)
await pay("INV-1042", 12400)          # Ran
await pay("INV-7781", 92000)          # Stopped: not in the work order, traced to the invoice
```

| Framework | How |
|---|---|
| **Google ADK** | `RemoteAdkGuard` as `before_tool_callback` / `after_tool_callback` |
| LangChain / LangGraph | `ToolGate.wrap` around each tool |
| OpenAI Agents SDK | `ToolGate.wrap` on function tools |
| MCP servers | gate each tool handler |
| Anything else | REST: `POST /v1/delegations`, `/confirm`, `/check`, `/executed`, `GET /report` |

Your business is one policy (tools and their fields, contacts, records, confidential files, company rules), added once by a person. Guide: [docs/INTEGRATE.md](docs/INTEGRATE.md) · [Colab, about 2 minutes](examples/colab/abide_quickstart.ipynb).

## Evidence

| Metric (Abide) | Result | Notes |
|---|---|---|
| Unauthorised action rate | **0 / 28** | 95% CI 0–12% |
| Legitimate task success | **26 / 26** | 95% CI 87–100% |
| Safe recovery | **26 / 28** | attacked tasks still finished their real work, nothing unsafe |
| False blocking | **0 / 126** | authorised calls in normal tasks |
| Handoff provenance | **190 / 190** | labels preserved; 256 / 256 tampering probes caught |
| Questions per normal task | **0.38** | vs 1.62 when a person approves every step |
| Runtime overhead | **+166 ms** | median per workflow (p95 +430 ms), no model calls |
| Live, hijacked Gemini agents | **0 / 6** | unprotected: 4 / 5 attacks got through |

Every number is reproducible: `python -m intentseal_eval.delegate`. The live dashboard is at [/#/evaluation](https://abide-1041805455955.us-central1.run.app/#/evaluation).

## Built on Google Cloud

| Service | What it does in Abide |
|---|---|
| **Gemini 3.5 Flash · Vertex AI** | Drafts the work order from the employee's words, explains every stop, writes the cited receipt |
| **Google ADK** | The live agent team (Reader → Resolver); `before/after_tool_callback` integration |
| **Model Armor** | Screens everything agents read for prompt injection |
| **Cloud Run** | The checkpoint service (one container, autoscaled, `us-central1`) |
| **Cloud SQL · PostgreSQL 16** | Work orders, hash-chained receipts, approvals, keys |
| **BigQuery** | Every receipt streamed to `abide_audit`; insights for the security team |
| **Secret Manager · IAM** | API keys, handoff secret, database URL; least-privilege service account |
| **Cloud Build · Artifact Registry** | One-command deploy ([`scripts/deploy_cloud_run.ps1`](scripts/deploy_cloud_run.ps1)) |

## Security model, plainly

**Abide guarantees**
- A tool call outside the confirmed work order does not run.
- An agent key can't confirm, widen or approve.
- Approvals cover one exact action, once.
- Untrusted labels survive handoffs.
- The receipt chain shows any edit.

**Not yet**
- Receipts are hash-chained, not KMS-signed.
- The evaluation is our own suite.
- The OpenAI Agents SDK and MCP adapters are tested against stand-in interfaces.

Report a vulnerability: [SECURITY.md](SECURITY.md).

## Repository

| Path | Contents |
|---|---|
| [`packages/core/intentseal_core/delegate/`](packages/core/intentseal_core/delegate) | Work-order model, compiler and grounding check, action gate, approvals, handoff seal, receipts, ADK connector |
| [`services/gateway/`](services/gateway) | FastAPI service: Delegate API, live Gemini agent team, sandbox businesses, Console hosting |
| [`apps/console/`](apps/console) | React Console: Tasks, Needs you, Activity, Connect, Evaluation |
| [`packages/sdk/`](packages/sdk) | One-file client and framework adapters |
| [`examples/`](examples) | Three-agent team (Docker), Colab quickstart, terminal demo |
| [`eval/`](eval) | Evaluation harness and published reports |
| [`deploy/`](deploy) | Cloud Build and Cloud Run |

<details>
<summary><b>Development</b></summary>

```bash
python -m venv .venv && . .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -c requirements.lock -e "packages/core[engine,classifiers,ocr,postgres,anthropic]" \
  -e packages/sdk -e services/gateway -e eval "google-adk==2.12.0" openai respx pytest pytest-asyncio ruff
pytest -q && ruff check .
cd apps/console && npm ci && npx tsc --noEmit && npm test && npm run build
```
</details>

**Docs:** [Integrate](docs/INTEGRATE.md) · [Architecture](docs/ARCHITECTURE.md) · [Evaluation](docs/EVAL.md) · [Testing guide](docs/TESTING.md) · [Walkthrough](docs/WALKTHROUGH.md) · [Deploy](docs/DEPLOY_CLOUD_RUN.md)

<div align="center"><sub>Apache-2.0 · Built for the Google Cloud AI Builder Cup 2026 · Future of Work</sub></div>
