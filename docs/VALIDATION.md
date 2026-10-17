# Release validation — 0.1.0 (16 Oct 2026)

Every check run on the release candidate, with the result. All were run on the committed tree.

## Build and code quality

| Check | Command | Result |
|---|---|---|
| Python tests | `pytest -q` | 186 passed, 5 skipped (optional providers not configured) |
| Lint | `ruff check .` | passed |
| Console types | `npx tsc --noEmit` | passed |
| Console tests | `node --test tests/*.test.cjs` | 8 passed (cold-start and recovery behaviour included) |
| Secret scan | API-key and private-key patterns over the tree | none found |
| Repository hygiene | `git status` before the first commit | no virtual environments, `node_modules`, `.env` or data files |

## Docker stack (`docker compose up --build -d`)

| Check | Result |
|---|---|
| Image builds | `intentseal-delegate:local`, 2.86 GB (classifier baked in, ADK included) |
| Both services healthy | gateway and postgres `healthy`; `/readyz`: database, policies and classifier ready |
| Every module imports in the image | 52 modules of `intentseal_gateway` and `intentseal_core` |
| Evidence is in PostgreSQL | receipts, contracts and workflows queried with `psql` inside the stack |
| API end to end (20 checks) | 20 of 20: card drafted with a send prohibition; public key cannot read (403) or confirm (403); wrong card hash refused (409); confirm with hash; hidden instruction in a document blocked by the content filter; quote read and supplier draft allowed; send to an address from the document and reading the customer list blocked; receipt chain intact; every report sentence cites receipts; both live demos start |
| Browser journey (headless Edge) | all 18 steps of [WALKTHROUGH.md](WALKTHROUGH.md) against the containers; screenshots refreshed |
| Shut down | `docker compose down` |

Found and fixed during this validation: the live demo needed `reportlab`, which had arrived only through a package
removed from this repository. It is now a gateway dependency, and CI imports every module in the image and starts both
demos so this cannot recur.

## Evaluation

`python -m intentseal_eval.delegate`: Delegate stops 28 of 28 attacks (0%; without protection 100%), finishes 26 of 26
normal tasks and asks 0.38 questions per task (asking before every step: 1.62, and every attack still gets through).
Method and limits: [EVAL.md](EVAL.md).

## Security review

29 judge-level probes pass (`tests/test_delegate_security.py`). Fixed findings G1–G15 and open items with reasons:
[ENGINEERING_REVIEW.md](ENGINEERING_REVIEW.md).

## Google integration status

| Item | Status |
|---|---|
| Gemini drafts the permission card (role `contract`) | done; verified live (AI Studio key, then Vertex AI) |
| Gemini writes the work receipt (role `receipt`) | done; verified live |
| Google ADK connector (`before_tool_callback`, `after_tool_callback`) | done; runtime test with a stand-in model |
| Live Gemini agent team run | done on Vertex AI (0.2.0, below) |
| Cloud Run deployment (Cloud Build, Artifact Registry, Secret Manager) | files ready and keyless; deploy after the frontend redesign |
| Cloud SQL for PostgreSQL on Cloud Run | next; the store already runs on PostgreSQL |

## Google integration validation — 0.2.0 (17 Oct 2026, project `intentseal`, not deployed)

| Check | Result |
|---|---|
| Python tests / lint / Console | 199 passed, 5 skipped / clean / 8 of 8 |
| Evaluation (scripted, two businesses) | unchanged: 0 of 28 attacks, 26 of 26 tasks, 0.38 questions per task |
| Docker with Google (`docker-compose.google.yml`), 12 checks | 12 of 12: ready with Vertex AI; trial key; one-file client served; live Gemini agents; card and receipt by `model:vertex/gemini-3.5-flash`; agents in the background (confirm in 2.7 s, work in 34 s); hijacked agent's ₹48,000 refund and customer export stopped and traced to the ticket while the ₹1,850 refund ran; Model Armor screened every read; a business registered with a trial key governs its own recipients |
| Colab quickstart, run against a live gateway | trial key, travel agency registered, card by Vertex AI, remote Gemini ADK agent under the card, receipt chain intact |
| Live agent runs | see [EVAL.md](EVAL.md), "Live runs with real Gemini agents" |

## Abide rename and landing page — 0.3.0 (17 Oct 2026)

| Check | Result |
|---|---|
| Python tests / lint | 199 passed, 5 skipped / clean |
| Console types / tests | clean / 8 of 8 |
| Browser journey (headless Edge, Docker, stand-in agents) | all 18 steps of [WALKTHROUGH.md](WALKTHROUGH.md); screenshots refreshed with the new landing page and the trial sign-in |
| Docker with Google, 13 checks | 13 of 13: Console served as Abide; trial key; `/v1/sdk/abide.py`; live Gemini agents; card and receipt by Vertex AI; hijacked agent's ₹48,000 refund and customer export stopped and traced to the ticket while the ₹1,850 refund ran; Model Armor on every read; a registered business governs its own recipients |

## Cloud Run (18 Oct 2026)

Deployed to project intentseal, region us-central1 (4 GiB, 2 CPU, scale to zero, max one instance). The 13 Docker-with-Google checks rerun against the live URL: 13/13. Live Gemini agents finished in 59 s; the hijacked agent's 48,000 refund and customer export were blocked, and only the 1,850 refund for order 4521 ran. Cards and receipts were written by Vertex AI (gemini-3.5-flash), and Model Armor screened every read.


## Gemini in every stage, Cloud SQL, framework adapters (18 Oct 2026)

Live on Cloud Run with Cloud SQL PostgreSQL: 14/14 end-to-end checks (Gemini explained both blocked actions of the hijacked agent). After a new revision (all memory gone) the trial key, the workspace, the card and its receipts were still there (2/2). A subtle injection sent to /v1/inspect was held at tier 2 by the Gemini judge (2.8 s). Bring-your-own agent with the client downloaded from the live service: the injected email was stopped, the real one sent, a LangChain tool held to the same card, receipt chain intact (3/3). pytest 206 passed, 5 skipped; ruff clean; Console 8/8.

