# Changelog

## 0.1.0 — 2026-10-16

First release of IntentSeal Delegate.

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
