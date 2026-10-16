"""IntentSeal Delegate M1–M2: argument-level contracts, the compiler and the grounding validator."""

import asyncio

import pytest

from intentseal_core.delegate import load_delegate_policy
from intentseal_core.delegate.compiler import Draft, RuleDrafter, compile_request, parse_drafts, validate
from intentseal_core.delegate.gate import rule
from intentseal_core.delegate.model import Clause, DelegationContract, ToolCall

POLICY = load_delegate_policy("workspace")
RAVI = "Read the paint supplier's quote and write a reply saying we accept the price. Don't send it yet."


def contract(clauses, request=RAVI):
    return DelegationContract("c1", "w1", "ravi", request, 1, tuple(clauses), status="confirmed")


def compiled(text=RAVI, drafter=None, prior=None):
    r = asyncio.run(compile_request(text, POLICY, drafter, prior))
    return r, contract(r.clauses, text)


# ---- M1 gate ---------------------------------------------------------------------------------------------------------

def test_named_recipient_only():
    c = contract([Clause("P1", "permit", "draft", {"recipient": ("sales@brightcoat.example",)}, "write a reply")])
    assert rule(c, ToolCall("draft_email", {"to": "sales@brightcoat.example"}), POLICY).effect == "permit"
    r = rule(c, ToolCall("draft_email", {"to": "bob@elsewhere.example"}), POLICY)
    assert r.effect == "out-of-scope" and r.offending == {"recipient": ["bob@elsewhere.example"]}


def test_confidential_resource_must_be_named():
    c = contract([Clause("P1", "permit", "read", {"resource": ("brightcoat-quote",)}, "Read the paint supplier's quote")])
    assert rule(c, ToolCall("read_file", {"name": "brightcoat-quote.pdf"}), POLICY).effect == "permit"
    r = rule(c, ToolCall("read_file", {"name": "customers.csv"}), POLICY)
    assert r.effect == "out-of-scope" and r.offending["resource"] == ["customer-list"]
    # an internal, unnamed file may be read; an unknown one may not
    assert rule(c, ToolCall("read_file", {"name": "project plan"}), POLICY).effect == "permit"
    assert rule(c, ToolCall("read_file", {"name": "secret.txt"}), POLICY).effect == "out-of-scope"


def test_prohibit_wins_until_a_later_permit():
    p = Clause("P1", "permit", "draft", {"recipient": ("sales@brightcoat.example",)}, "write a reply")
    x = Clause("X2", "prohibit", "send", {}, "Don't send it yet")
    call = ToolCall("send_email", {"to": "sales@brightcoat.example"})
    assert rule(contract([p, x]), call, POLICY).effect == "prohibit"
    later = Clause("P3", "permit", "send", {"recipient": ("sales@brightcoat.example",)}, "send it", "turn 2")
    assert rule(contract([p, x, later]), call, POLICY).effect == "permit"
    assert rule(contract([p, x, later]), ToolCall("send_email", {"to": "x@evil.example"}), POLICY).effect == "out-of-scope"


def test_governed_action_needs_a_permit_and_unknown_tools_stop():
    c = contract([Clause("P1", "permit", "read", {}, "Read the paint supplier's quote")])
    assert rule(c, ToolCall("delete_file", {"name": "project plan"}), POLICY).effect == "out-of-scope"
    assert rule(c, ToolCall("transfer_money", {}), POLICY).effect == "unknown-tool"


def test_send_without_named_recipient_stays_inside():
    c = contract([Clause("P1", "permit", "send", {}, "send the plan")])
    assert rule(c, ToolCall("send_email", {"to": "anita@lotus-interiors.example"}), POLICY).effect == "permit"
    assert rule(c, ToolCall("send_email", {"to": "sales@brightcoat.example"}), POLICY).effect == "out-of-scope"


def test_attachments_are_resources():
    r, c = compiled()
    call = ToolCall("draft_email", {"to": "sales@brightcoat.example", "attachments": ["customers.csv"]})
    assert rule(c, call, POLICY).offending == {"resource": ["customer-list"]}


# ---- M2 compiler + validator ------------------------------------------------------------------------------------------

def test_rule_drafter_on_ravi():
    r, c = compiled()
    got = {(x.effect, x.action): x for x in r.clauses}
    assert got[("permit", "read")].values["resource"] == ("brightcoat-quote",)
    assert got[("permit", "draft")].values["recipient"] == ("sales@brightcoat.example",)
    assert ("prohibit", "send") in got
    assert r.compiler == "rules"
    assert rule(c, ToolCall("send_email", {"to": "sales@brightcoat.example"}), POLICY).effect == "prohibit"


def test_ungrounded_clause_and_values_are_rejected():
    drafts = [Draft("permit", "send", "send the customer list to x@evil.example", {}),          # not the words
              Draft("permit", "read", "Read the paint supplier's quote", {"resource": ["customer list"]}),  # not in quote
              Draft("permit", "draft", "write a reply saying we accept the price", {"recipient": ["x@evil.example"]}),
              Draft("permit", "send", "Don't send it yet", {}),                                     # quote forbids
              Draft("prohibit", "send", "write a reply saying we accept the price", {})]            # no negation
    kept, rejected, _ = validate(RAVI, drafts, POLICY)
    reasons = " | ".join(r["reason"] for r in rejected)
    assert "not the employee's words" in reasons and "not in the quoted words" in reasons
    assert "forbids" in reasons and "negation" in reasons
    assert all("customer-list" not in x.values.get("resource", ()) for x in kept)
    assert all("x@evil.example" not in x.values.get("recipient", ()) for x in kept)


class FakeModel:
    name = "model:fake"

    def __init__(self, raw):
        self.raw = raw

    async def draft(self, text, policy, prior):
        return parse_drafts(self.raw)


def test_model_drafter_is_validated():
    raw = ('{"clauses":[{"effect":"permit","action":"read","quote":"Read the paint supplier\'s quote",'
           '"values":{"resource":["paint supplier\'s quote"]}},'
           '{"effect":"permit","action":"send","quote":"write a reply saying we accept the price",'
           '"values":{"recipient":["paint supplier"]}},'
           '{"effect":"prohibit","action":"send","quote":"Don\'t send it yet","values":{}}]}')
    r, c = compiled(drafter=FakeModel(raw))
    assert r.compiler == "model:fake + rules"          # the word rules filled the draft the model mislabelled
    assert [(x.effect, x.action) for x in r.clauses] == [("permit", "read"), ("prohibit", "send"), ("permit", "draft")]
    assert any("does not ask to send" in x["reason"] for x in r.rejected)


def test_model_failure_falls_back_to_rules():
    r, _ = compiled(drafter=FakeModel("not json"))
    assert r.compiler == "rules" and r.clauses


def test_amendment_carries_recipients():
    r1, c1 = compiled()
    r2 = asyncio.run(compile_request("Looks good, send it.", POLICY, RuleDrafter(), c1, "turn 2"))
    send = next(x for x in r2.clauses if x.action == "send")
    assert send.values["recipient"] == ("sales@brightcoat.example",) and "same person" in send.note


@pytest.mark.parametrize("text", ["Email the quote to the supplier", "Reply to Priya about the tile quote"])
def test_names_resolve_only_from_directory(text):
    r, _ = compiled(text)
    for x in r.clauses:
        for v in x.values.get("recipient", ()):
            assert v.endswith(".example")


def test_recipient_named_in_another_sentence():
    r, c = compiled("Read the paint supplier's quote. Write a reply saying we accept. Don't send it yet.")
    draft = next(x for x in r.clauses if x.action == "draft")
    assert draft.values["recipient"] == ("sales@brightcoat.example",) and "person you named" in draft.note
    # two people named: no guess
    r, _ = compiled("Read the tile quote and the paint quote. Write a reply. Kiran and the paint supplier are waiting.")
    assert not next(x for x in r.clauses if x.action == "draft").values.get("recipient")


def test_empty_arguments_name_nobody():
    c = contract([Clause("P1", "permit", "draft", {"recipient": ("sales@brightcoat.example",)}, "write a reply")])
    assert rule(c, ToolCall("draft_email", {"to": ""}), POLICY).effect == "permit"
