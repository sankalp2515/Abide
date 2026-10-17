"""Gemini's plain-language explanation of a stopped action: advice only, never a decision, and an outage is silent."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from intentseal_core.delegate import load_delegate_policy
from intentseal_core.delegate.explain import ModelExplainer
from intentseal_core.delegate.service import DelegateService
from intentseal_core.store import Store

P = load_delegate_policy("workspace")


def run(coro):
    return asyncio.run(coro)


class FakeRouter:
    def __init__(self, text: str = "", fail: bool = False) -> None:
        self.settings = SimpleNamespace(roles={"explain": {}})
        self.text, self.fail, self.prompts = text, fail, []

    async def chat(self, role, req):
        self.prompts.append(req.messages[0]["content"])
        if self.fail:
            raise TimeoutError("model down")
        return SimpleNamespace(content=self.text, provider="vertex", model="gemini-3.5-flash")


def service(router) -> tuple[DelegateService, str]:
    s = DelegateService(P, store=Store(":memory:"), secret=b"k", explainer=ModelExplainer(router))
    c = run(s.start("ravi", "Read the paint supplier's quote."))
    s.confirm(c.workflow_id, c.hash, actor="ravi")
    return s, c.workflow_id


def test_a_blocked_action_gets_a_plain_explanation_recorded_in_the_receipt():
    router = FakeRouter("Your assistant tried to wire money, which you never asked for, so it was stopped.")
    s, wf = service(router)
    r = run(s.check(wf, "worker", "wire_money", {"to": "x", "amount": 5000}))
    assert r["decision"] == "block"
    assert r["explanation"].startswith("Your assistant tried to wire money")
    assert "wire_money" in router.prompts[0] and "Read the paint supplier's quote." in router.prompts[0]
    rec = [x for x in s.receipts(wf) if x["kind"] == "check"][-1]
    assert rec["data"]["explanation"] == {"text": r["explanation"], "by": "model:vertex/gemini-3.5-flash"}
    assert r["notice"] and "wire money" not in r["notice"]      # the agent is told the rule, not the model's words


def test_allowed_actions_are_not_explained():
    router = FakeRouter("should not be used")
    s, wf = service(router)
    r = run(s.check(wf, "reader", "read_file", {"name": "paint supplier's quote"}))
    assert r["decision"] == "allow" and r["explanation"] is None and router.prompts == []


def test_a_model_outage_leaves_the_decision_and_reason_unchanged():
    s, wf = service(FakeRouter(fail=True))
    r = run(s.check(wf, "worker", "wire_money", {"to": "x", "amount": 5000}))
    assert r["decision"] == "block" and r["explanation"] is None and r["reason"]
    assert "explanation" not in [x for x in s.receipts(wf) if x["kind"] == "check"][-1]["data"]


def test_a_question_is_explained_before_it_exists_so_an_answer_cannot_arrive_first():
    """The employee can answer a question as soon as it exists; the agent must be waiting by then. A slow explanation
    between the two lost approvals in the live demo, so the explanation is written first."""
    seen = {}

    class Router(FakeRouter):
        async def chat(self, role, req):
            seen["approvals_while_explaining"] = len(s.approvals(wf))
            return await super().chat(role, req)
    router = Router("Your assistant wants to email someone you did not name; it waits for you.")
    s, wf = service(router)
    r = run(s.check(wf, "worker", "send_email", {"to": "someone@elsewhere.example", "subject": "x", "body": "y"}))
    assert r["decision"] == "ask" and r["explanation"]
    assert seen["approvals_while_explaining"] == 0 and len(s.approvals(wf, "pending")) == 1
