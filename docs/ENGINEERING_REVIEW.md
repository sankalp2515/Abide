# IntentSeal Delegate: engineering review (16 Oct 2026, before Google integration)

Each role reviewed the system from its angle; every finding below was checked against the code before it was
accepted. "Fixed" items have tests (`tests/test_delegate_*.py`).

## Fixed in this review

| # | Role | Finding | Fix | Test |
|---|---|---|---|---|
| G1 | Security (OWASP A01) | Reading a delegation (state, receipts, report, list, demo state) needed only the public key, which ships in the Console bundle: anyone could read every employee's requests, pasted text and receipts | Reads need a project key and must be the delegating person's (or admin); others get 404, so existence does not leak; list shows only your own | `test_delegations_are_private_to_their_owner` |
| G2 | Security / Product | Confirm accepted no card fingerprint ("blind" confirm) | The API requires the card's hash | `test_scopes_and_errors` |
| G3 | Security | Approvals never expired | 15-minute lifetime (`INTENTSEAL_APPROVAL_TTL_S`); stale ones expire on the next check and cannot be answered | `test_questions_expire` |
| G4 | Systems / Performance | Per-workflow tracing state in memory grew without bound | Bounded LRU (`INTENTSEAL_DELEGATE_CACHE`, 256), rebuilt from the store on demand | `test_tracing_state_is_bounded_and_rebuilt` |
| G5 | Database | Receipt numbering could collide with two writers on one workflow | The primary key decides; the loser re-reads and retries | covered by chain tests |
| G6 | DevOps / Security | Handoff seal key silently random per process when unset | Warning at start; Cloud Run deploy generates and mounts `INTENTSEAL_DELEGATE_SECRET` from Secret Manager | deploy files |
| G7 | SRE / Observability | No structured record of decisions in logs | One JSON audit line per receipt on `intentseal.delegate.audit` (ids, tool, decision, ruling; never content or arguments) | log output |
| G8 | Privacy / Data | Raw third-party and confidential text kept forever | `purge(days)` and `INTENTSEAL_DELEGATE_RETENTION_DAYS`; receipts (evidence) stay, raw text goes | `test_retention_purges_raw_content_but_keeps_receipts` |
| G9 | API design | "Not the delegating person" returned 409 | 403 (`DelegateForbidden`) | `test_wrong_person_gets_403` |
| G10 | Frontend | Console read state with the public key | Uses the signed-in employee's key | walkthrough replay |
| G11 | AI engineering | A judge that blocked a whole PDF for its hidden line cost the legitimate quote | Visible text is re-inspected alone and delivered when clean (any hold rule) | `test_file_held_for_hidden_text_still_gives_its_visible_text` |
| G12 | AI engineering / UX | Slow or exhausted model made the card wait ~10 s | 12 s timeout (8 s originally; raised for Vertex AI, where a full card takes 3–9 s), 2-minute cooldown after a failure | `test_slow_model_is_cut_off_then_skipped` |
| G13 | Architecture | Sandbox demo routes always on | `INTENTSEAL_DELEGATE_DEMO=0` turns them off | `test_demo_can_be_switched_off` |
| G15 | DevOps | The image had never been built; a dependency (reportlab, for the sandbox PDFs) only arrived through a removed package | Docker Compose stack (gateway + PostgreSQL) built and checked end to end; reportlab declared by the gateway; CI imports every module in the image and starts both demos | `.github/workflows/ci.yml` (docker job) |
| G14 | UX / Accessibility (WCAG) | No announcement of progress for screen readers; jargon on screen | `aria-live` status; plain labels; details folded (K1–K11) | walkthrough |

## Open, with the reason

| # | Role | Finding | Why not now | Next step |
|---|---|---|---|---|
| O1 | Security | Agent-runtime calls (`read`, `check`, `handoff`, `executed`) accept any project key, not only the agents of that workflow | Needs per-workflow agent credentials | Issue a short-lived delegation token at confirmation; require it on agent calls |
| O2 | Security | `executed` is reported by the tool runner (trusted) | In production the gateway should execute tools itself (MCP proxy) so it observes execution | MCP proxy mode |
| O3 | Database / Scale | Evidence is in PostgreSQL (Docker); the Cloud Run deployment still uses SQLite on one instance, and demo sandboxes live in memory (`--max-instances=1`) | PostgreSQL store is done and tested | Cloud SQL for PostgreSQL on Cloud Run; sandboxes out of process |
| O4 | AI | Live Gemini agent runs need quota (free key: 20 requests per model per day) | Billing decision | Vertex AI on the project |
| O5 | QA | The browser replay is not in CI | Needs a browser on the runner; the evaluation, tests, lint, Console build and a Docker smoke test are in `.github/workflows/ci.yml` | Add the Playwright replay as a CI job |
| O8 | AI | Hinglish "Customer list mat bhejna" from Gemini can become "don't send anything" | Over-cautious, never unsafe; visible on the card | Prompt and validator tuning with quota |

## Principles check

- **Correctness and security by default**: permissions come only from the employee's words; anything not on the card
  is off; model output is a proposal; reads are private; approvals are single-use, bound and short-lived.
- **Simplicity (KISS/YAGNI)**: one service, one store, pure functions for rules (`gate.rule`, `validate`);
  no new infrastructure was added for the review fixes.
- **Twelve-factor**: configuration from the environment (`INTENTSEAL_*`), logs to stdout, secrets from Secret Manager.
- **Testability**: 72 Delegate tests (unit, API, ADK runtime with a stand-in model), the 54-scenario evaluation, and a
  scripted browser replay of every journey.
