# Abide — design notes and build log

How the system was built: the architecture map, the milestones in the order they were delivered, and every design
decision (D-D1 to D-D12) with its reason. For the current architecture see [ARCHITECTURE.md](ARCHITECTURE.md).

**Principles**

- Every model call goes through a provider-neutral interface (`Drafter`, `Writer`) with a deterministic fallback, so
  the whole system runs and is tested with no model.
- Gemini proposes, code decides. No permission exists unless the validator can find it in the employee's words.
- Proof comes from the sandbox tool log (what actually executed), never from what an agent says.

## Architecture map

| Step | Component | Module | Milestone |
|---|---|---|---|
| ① | Delegation intake (request, workflow id, policy) | `delegate/service.py`, gateway `delegate_api.py` | M3 |
| ② | Contract compiler (model drafts clauses with quotes) | `delegate/compiler.py` (`LLMDrafter`, `RuleDrafter`) | M2 |
| ③ | Grounding validator (quote must be in the request, values must be in the quote or resolve from the trusted directory) | `delegate/compiler.py` (`validate`) | M2 |
| ④ | Agent team (Reader → Drafter) | `examples/delegate_demo/` | M6 |
| ⑤ | Content firewall (existing engine, 9 types, taint) | `Engine.inspect` + `delegate/service.py` | M3 |
| ⑥ | Handoff seal (contract id + taint on every agent-to-agent message, signed) | `delegate/handoff.py` | M3 |
| ⑦ | Action gate (arguments vs contract, block and continue) | `delegate/gate.py` | M1, M4 |
| ⑧ | Bound approval (one approval for one exact action) | `delegate/service.py` | M4 |
| ⑨ | Work receipt (report that cites receipt ids; unknown ids rejected) | `delegate/receipts.py` | M5 |
| store | Evidence store (contracts, receipts, approvals, hash-chained) | `Store` (SQLite for tests, PostgreSQL in Docker) | M3 |

## Milestones

| # | Milestone | Done when | Status |
|---|---|---|---|
| M0 | Baseline: engine tests pass before any change | `pytest` passes on the untouched code | done (266 passed) |
| M1 | Argument-level contract model | A "draft to Priya" clause allows `draft_email(to=priya)` and stops `draft_email(to=bob)`; a resource clause stops reading an unnamed file; prohibit beats permit; tests | done |
| M2 | Compiler + grounding validator | Rule drafter and model drafter produce clauses; a planted clause without a quote, or with a value not in the quote/directory, is rejected; names resolve only from the trusted directory; ambiguity becomes a question; tests with a fake model | done |
| M3 | Delegation service, API, handoff seal | Create → confirm → amend (hash-chained versions); content inspected per workflow; sealed handoff verified and tamper-evident; gateway endpoints; tests | done |
| M4 | Gate decisions, recovery, bound approval | Off-scope + traced to untrusted content → blocked with a notice (agent continues); off-scope otherwise → one approval; approval works once and only for the same action hash; tests | done |
| M5 | Work receipt | Report sentences each cite existing receipt ids; a sentence with an unknown id or an unsupported address is dropped; deterministic writer fallback; tests | done |
| M6 | Local two-agent demo | `python -m examples.delegate_demo.run` runs the Ravi scenario (paint supplier quote with hidden text) in three arms; tool log shows no unauthorised execution with Delegate | done (live version in the gateway) |
| M7 | Console Delegation page | Contract card with highlighted quotes, Confirm, approval prompt, receipt view | done (`/#/delegate`, checked in the browser) |
| M8 | Local eval + manual test guide | 40 scenarios × 4 arms from tool logs; `docs/TESTING.md` lists every claim and how to check it by hand | done ([EVAL.md](EVAL.md), [TESTING.md](TESTING.md)) |
| G1 | Google integration | Gemini drafter/writer/agents, ADK `before_tool_callback` adapter, Cloud Run | see G1a–G1f |

## Decisions

- D-D1 Delegation is a new module (`intentseal_core.delegate`) beside the existing session contract (D-064). The old
  contract keeps working for existing agents; Delegate adds argument values, a trusted directory, workflows that span
  agents, and a model drafter behind a validator.
- D-D2 Off-scope handling: if the off-scope call can be traced to untrusted content (argument text overlaps tainted
  content, or a value such as an address appears in untrusted content but not in the request), it is blocked without
  asking anyone and the agent is told why. Otherwise the employee is asked once. This is what keeps interruptions low.
- D-D3 "Ask before" policy: an organisation can require confirmation for some actions (for example any email that
  leaves the company) even when the contract permits them. The confirmation is bound to the exact action hash.
- D-D5 Local mode without a judge model: when the content filter would hold a PDF or Word file only because no judge
  is available, the hidden text is quarantined and the visible text alone is inspected again. A clean visible part is
  given to the agent; otherwise the file stays held.
- D-D6 A recipient named in another sentence of the employee's words fills a reply that names nobody, only when exactly
  one person is named. An empty argument names nobody and nothing.
- D-D4 Fail closed: if the drafter or validator fails, governed actions (send, submit, purchase, delete, execute,
  share) stay off, and reading is limited to the resources named in the request.

## Judge-probe fixes and the Google phase (16 Oct, later)

| # | Item | Status |
|---|---|---|
| S1 | Pasted material grants nothing (D-D7) | done |
| S2 | Only the delegating person may confirm, amend or approve (D-D9) | done |
| S3 | Undeclared fields refused on actions that leave (D-D8) | done |
| S4 | Confidential file text cannot leave in a body (D-D10) | done |
| X1 | Business verbs, record ids, amount limits for the whole task (D-D11) | done |
| X2 | Second business: Acme Home support desk (policy, sandbox, agents, eval, live demo, Console) | done |
| G1a | Gemini drafts the contract (router role `contract`, gemini-3.5-flash) | done, live-tested |
| G1b | Gemini writes the work receipt (role `receipt`) | done, live-tested |
| G1c | Other languages: model-understood clauses, a tick for what leaves (D-D12) | done |
| G1d | Google ADK connector + Gemini agent team | done; tested with a stand-in model; live run waits for quota |
| G1e | Cloud Run deployment (Cloud Build, Artifact Registry, Secret Manager, Vertex option) | files ready; needs a project |
| G1f | PostgreSQL evidence store (Docker; Cloud SQL on Google Cloud); Model Armor in step 5 | PostgreSQL done; Cloud SQL and Model Armor later |

- D-D7 Quoted or pasted text in the employee's request is material, not instruction: masked before drafting, tracked
  as untrusted content.
- D-D8 For actions that leave, a tool call may only carry fields the policy declares.
- D-D9 Confirm, amend and approve require the principal who started the workflow (an admin key may act for them).
- D-D10 Text read from a confidential resource may reach an outside recipient only if that resource is named in the
  permit for the action.
- D-D11 Policies may declare business verbs and record types; an amount limit in the request applies to the whole task.
- D-D12 Words the checks cannot read: prohibitions are kept (they only shrink the contract), read/draft permits are kept
  as "understood by the model", permits for actions that leave need the employee's tick at confirmation.
