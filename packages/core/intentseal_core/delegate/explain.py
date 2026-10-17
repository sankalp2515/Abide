"""Plain-language explanations of stopped actions (router role `explain`, Gemini in the Google build).

The rules decide; the explainer only words the decision for the employee: what the assistant tried, why it was
stopped, where the instruction came from, and what they can do. It never changes a decision, and when the model is
slow or unavailable the rule's own reason stands alone.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

log = logging.getLogger("intentseal.delegate")

PROMPT = (
    "You explain an AI safety decision to a non-technical employee. Their assistant tried an action and the safety "
    "rules {verb} it. In at most two short sentences of plain English: say what the assistant tried, why it was "
    "{verb} (use the rule's reason; if it was traced to a document, say the instruction came from that document, "
    "not from them), and what they can do next. Do not invent facts beyond the details below. No markdown.\n"
    "Employee's request: {request}\nAction: {tool} {args}\nDecision: {decision}\nRule's reason: {reason}\n"
    "Traced to: {traced}")


class ModelExplainer:
    def __init__(self, router: Any, role: str = "explain", timeout_s: float = 15.0) -> None:
        self.router, self.role, self.timeout_s, self.name = router, role, timeout_s, f"model:{role}"

    async def explain(self, request: str, tool: str, args: dict[str, Any], decision: str, reason: str,
                      traced_to: str | None = None) -> str:
        """One or two sentences, or "" when no model answers in time. Never raises."""
        from ..llm.adapters import ChatRequest
        if self.router is None or self.role not in getattr(getattr(self.router, "settings", None), "roles", {}):
            return ""
        prompt = PROMPT.format(verb="blocked" if decision == "block" else "paused for your confirmation of",
                               request=request[:1500], tool=tool,
                               args=json.dumps(args, ensure_ascii=False, default=str)[:600], decision=decision,
                               reason=reason[:600], traced=traced_to or "nothing")
        try:
            res = await asyncio.wait_for(self.router.chat(self.role, ChatRequest(
                messages=[{"role": "user", "content": prompt}], temperature=0.0, max_tokens=1500)), self.timeout_s)
        except Exception as e:                       # slow, rate-limited or down: the rule's reason stands alone
            log.info("explainer unavailable: %s", type(e).__name__)
            return ""
        self.name = f"model:{res.provider}/{res.model}"
        return " ".join((res.content or "").split())[:500]
