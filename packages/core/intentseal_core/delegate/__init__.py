"""IntentSeal Delegate: an employee's request becomes the permission for every agent that works on it.

Architecture steps (docs/DESIGN_NOTES.md):
  ② compiler.py  - a model (or the rule drafter) proposes clauses, each quoting the employee's words
  ③ compiler.py  - the grounding validator keeps only clauses and values it can find in those words
  ⑥ handoff.py   - signed envelopes carry the contract and the taint of what an agent read to the next agent
  ⑦ gate.py      - every tool call is checked, argument by argument, against the confirmed contract
  ⑧ approvals.py - an approval covers one exact action under one contract version, once
  ⑨ receipts.py  - hash-chained receipts, and a report whose every sentence cites them
  ① ⑤ service.py - intake, content inspection through the engine, and the decisions that tie it together
"""

from .model import Clause, DelegationContract, DelegatePolicy, ToolCall, load_delegate_policy

__all__ = ["Clause", "DelegationContract", "DelegatePolicy", "ToolCall", "load_delegate_policy"]
