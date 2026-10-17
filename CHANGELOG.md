# Changelog

## 0.3.0 — 2026-10-17

- New name: **Abide** ("AI agents that abide by your words"), with its own mark.
- New landing page: centred hero with a live product preview (the card, the agents' steps, blocked actions and the
  receipt, playing on a loop), a built-on-Google strip, a before/after comparison from the measured numbers, how it
  works with a small interface for each step, use cases, evidence, a developer section and a call to action.
- The console pages (Delegate, Agents, Replay, Decisions, Review, Policy) keep their design; the Delegate sign-in
  offers a one-click free trial key.
- The one-file client is served at `/v1/sdk/abide.py` (the old path still works); the Colab notebook is
  `examples/colab/abide_quickstart.ipynb`; trial keys are on by default in Docker Compose.

## 0.2.0 — 2026-10-17

- Gemini on Vertex AI through the model router (Google credentials, no API key, billed to the project, daily token cap).
- The live demo runs real Gemini agents (Google ADK) by default, in the background; a hijacked-agent mode and the
  visitor's own attack text show the gate stopping a real agent.
- Google Model Armor as a second screen in the content filter.
- Bring your own business (`/v1/delegate-workspaces`) and your own agent (`RemoteAdkGuard`, one-file client at
  `/v1/sdk/intentseal_delegate.py`), self-serve 7-day trial keys, and a Colab quickstart.
- Cloud Run deployment is keyless (Vertex AI, Model Armor) with a least-privilege runtime service account.

## 0.1.0 — 2026-10-16

First release of Abide.

- Permission card from the employee's words: Gemini drafts, a grounding validator keeps only what the words and the
  trusted directory support; hash-chained versions; confirm once with the card's hash.
- Action gate on every tool call: recipients, records, amounts across the task, declared fields, confidential text,
  latest word wins; off-card calls traced to documents stop without a question.
- Bound approvals: one question for one exact action, single use, 15-minute lifetime, owner only.
- Hash-chained work receipts; Gemini writes the report and every sentence must cite receipts.
- Google ADK connector (`before_tool_callback`, `after_tool_callback`) and a Gemini agent team.
- Two sandbox businesses (office, support desk) with policy files; evaluation of 28 attacks and 26 normal tasks.
- Console: landing page with the published evaluation, Delegate page, decisions, replay, review.
- Docker Compose stack (gateway + PostgreSQL), CI workflow, Cloud Run deployment files.
