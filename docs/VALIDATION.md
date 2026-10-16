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
| Gemini drafts the permission card (role `contract`) | done; verified live with an AI Studio key |
| Gemini writes the work receipt (role `receipt`) | done; verified live |
| Google ADK connector (`before_tool_callback`, `after_tool_callback`) | done; runtime test with a stand-in model |
| Live Gemini agent team run | wired; needs Vertex AI or a billed key (free tier: 20 requests per model per day) |
| Cloud Run deployment (Cloud Build, Artifact Registry, Secret Manager) | files ready; needs a dedicated Google Cloud project |
| Cloud SQL for PostgreSQL on Cloud Run | next; the store already runs on PostgreSQL |
