# Testing guide

Every claim the product makes, and how to check it yourself. For a screenshot-by-screenshot tour as a new user, see
[WALKTHROUGH.md](WALKTHROUGH.md).

## 1. Start the stack

```bash
cp .env.example .env          # optional: add GEMINI_API_KEY
docker compose up --build -d
docker compose ps             # gateway and postgres "healthy" (first start: 1–3 minutes)
```

| Key | Who | Use |
|---|---|---|
| `local-project-key` | The employee (also what an agent runtime holds) | Sign in on the Delegate page |
| `local-admin-key` | A reviewer | Decisions, replay and review pages |
| `local-public-key` | The Console's built-in read-only key | Cannot act; used to test a refusal |

Stop with `docker compose down` when you finish.

## 2. Automated checks

```bash
pytest -q                                        # all Python tests; the Delegate ones are tests/test_delegate_*.py
ruff check .
cd apps/console && npx tsc --noEmit && node --test tests/*.test.cjs
```

The Delegate tests include 29 judge-level attack probes (`tests/test_delegate_security.py`) and a real Google ADK
runtime test with a stand-in model (`tests/test_delegate_adk.py`).

## 3. The stories in the terminal (no servers, no model calls)

```bash
python -m examples.delegate_demo.run
python -m examples.delegate_demo.run --arm delegate --firewall off
python -m intentseal_eval.delegate
```

The evaluation prints the table in [EVAL.md](EVAL.md): `delegate` and `gate-only` show 0% of 28 attacks; `none` and
`ask-every-step` show 100%.

## 4. The Console

Open <http://localhost:8000/#/delegate> and sign in with `local-project-key`. The card says "drafted by
model:gemini/…" when Gemini answered, or "rules" when no key is set or the quota is used up.

**Office (Lotus Interiors)**

| Step | What you should see | Claim |
|---|---|---|
| Prepare my assistants | 2 "Allowed", 1 "Not allowed: send email"; your sentence highlighted | Your words become the permission |
| Confirm and start | 4 steps Done; the quote step says hidden instructions were removed; 0 questions | The filter removes the hidden line |
| "Looks good, send it." → Tell them | One question with the exact email | The company rule asks once, for this email |
| Yes, do this | Sent to the supplier; receipt with `[R-n]` and "receipts intact" | Approval covers one exact action |
| New task with the filter turned off | Opening customers.csv and the send to records@… Stopped, traced to the PDF | The gate alone holds |

**Customer support (Acme Home)** — click "Customer support · Acme Home"

| Step | What you should see | Claim |
|---|---|---|
| Prepare my assistants | read order 4521; refund order 4521 up to 2000; email the customer on order 4521 (meera@…) | Business words, records and limits; "the customer" comes from the order system |
| Confirm and start | Refund ₹1,850 and the reply Done; export, email to audit@… and refund of ₹48,000 on order 9921 Stopped, traced to the ticket; 0 questions | Same product, another business |

**Another language (needs Gemini quota)** — office, type: `Quote padh ke paint supplier ko reply likh do aur Priya ko
bhej do. Customer list mat bhejna.` The card marks lines "understood by Gemini"; the send to Priya shows **Tick to
allow**. Without the tick it is dropped when you confirm.

## 5. The API directly

```bash
K=local-project-key
curl -s -X POST localhost:8000/v1/delegations -H "Authorization: Bearer $K" -H "Content-Type: application/json"   -d '{"request": "Read the paint supplier quote and write a reply. Do not send it yet.", "workspace": "workspace"}'
```

The response is the card (clauses with quotes, its hash and the workflow id). Continue with `/confirm` (body:
`{"hash": "…"}`), `/check` for a tool call, and `/report` for the receipt. Every route is documented at
<http://localhost:8000/docs>.

## 6. Evidence in PostgreSQL

```bash
docker compose exec postgres psql -U intentseal -c "select workflow_id, seq, kind, decision from delegate_receipts order by created desc limit 10;"
```

## 7. Gemini (optional)

With `GEMINI_API_KEY` in `.env`, the card and the receipt come from Gemini. To run real Gemini agents (Google ADK)
through IntentSeal, in a development environment:

```bash
INTENTSEAL_DELEGATE_AGENT_MODEL=gemini-3.5-flash python -c "import asyncio; from intentseal_gateway.delegate_demo.scenario import Scenario, run, show; show(asyncio.run(run(Scenario('ravi'), 'delegate', firewall=False, agents='gemini')))"
```

One run uses about 10 model requests; the free AI Studio tier allows 20 per model per day.

## 8. Browser replay

```bash
pip install playwright && python scripts/delegate_walkthrough.py
```

Replays every journey in headless Microsoft Edge against <http://127.0.0.1:8000> and refreshes the screenshots in
`docs/walkthrough/`.
