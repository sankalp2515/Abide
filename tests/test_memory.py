"""Trainability memory (memory.py) and its engine integration. Neutral placeholder content, no external models."""

from __future__ import annotations

import pytest

from intentseal_core.decision_model import StaticDecisionModel
from intentseal_core.engine import Engine
from intentseal_core.memory import AttackMemory, shingles
from intentseal_core.policy import load_policy
from intentseal_core.store import Store
from intentseal_core.types import Decision

CONFIRMED = ("please file the zebra quarterly marker under the violet folder and then notify the "
             "orchard desk about the lantern schedule before noon")
COVER = "The Q3 report shows revenue grew 8% to 4.2 million USD and the operating margin was 11%."


class Scorer:
    def __init__(self, value: float = 0.0) -> None:
        self.value, self.calls = value, 0

    def score(self, text: str) -> float:
        self.calls += 1
        return self.value


@pytest.fixture
def policy():
    p = load_policy("research-agent")
    p.sensitive_data.canaries = []
    return p


def make(policy, malicious=0.0, score=0.0):
    judge = StaticDecisionModel(malicious)
    return Engine(policy_loader=lambda _: policy, decision_model=judge, classifiers=[Scorer(score)],
                  store=Store(":memory:"))


def test_fingerprints_store_no_content():
    store = Store(":memory:")
    m = AttackMemory(store)
    mid = m.add(CONFIRMED, "attack", ["Indirect Prompt Injection"], origin="test")
    row = store.query("SELECT * FROM memory WHERE id = ?", (mid,))[0]
    assert "zebra" not in str(row) and row["n"] == len(shingles(CONFIRMED))
    assert m.add(CONFIRMED, "attack") == mid            # deduplicated
    assert m.add("too short", "attack") is None          # too few shingles to match safely


def test_containment_matches_embedded_and_lightly_changed_text():
    m = AttackMemory(Store(":memory:"))
    m.add(CONFIRMED, "attack", ["Context Poisoning"])
    embedded = COVER + "\n\n" + CONFIRMED.upper().replace("noon", "midday") + "\n\n" + COVER
    hit = m.match(embedded, "research-agent")
    assert hit and hit.kind == "attack" and hit.score >= 0.6 and hit.labels == ["Context Poisoning"]
    assert m.match(COVER, "research-agent") is None


def test_benign_marks_are_exact_and_per_agent():
    m = AttackMemory(Store(":memory:"))
    m.add(COVER, "benign", agent_id="research-agent")
    assert m.match(COVER, "research-agent").kind == "benign"
    assert m.match(COVER, "other-agent") is None
    assert m.match(COVER + " Extra sentence.", "research-agent") is None


def test_reload_when_another_writer_changes_the_table():
    store = Store(":memory:")
    reader, writer = AttackMemory(store), AttackMemory(store)
    assert reader.match(CONFIRMED, "a") is None
    writer.add(CONFIRMED, "attack")
    assert reader.match(CONFIRMED, "a") is not None


async def test_confirmed_attack_blocked_at_tier0_without_ai(policy):
    e = make(policy, malicious=0.0)                     # the AI tiers would let this pass
    first = await e.inspect(COVER + " " + CONFIRMED, "web", policy.agent)
    assert first.decision == Decision.ALLOW
    e.feedback("attack", event_id=first.event_id, labels=["Indirect Prompt Injection"])
    calls, judged = e.classifiers[0].calls, e.decision_model.calls
    again = await e.inspect(CONFIRMED + " " + COVER, "pdf", policy.agent, fmt="text")
    assert again.decision == Decision.BLOCK and again.policy_rule == "known-attack-match" and again.tier == 0
    assert e.classifiers[0].calls == calls and e.decision_model.calls == judged   # no AI call for a known attack
    assert "Indirect Prompt Injection" in again.labels


async def test_false_positive_mark_skips_ai_but_not_structural_rules(policy):
    e = make(policy, malicious=0.9, score=0.9)
    r = await e.inspect(COVER, "web", policy.agent)
    assert r.decision == Decision.BLOCK
    e.feedback("false_positive", event_id=r.event_id)
    judge_calls = e.decision_model.calls
    ok = await e.inspect(COVER, "web", policy.agent)
    assert ok.decision == Decision.ALLOW and e.decision_model.calls == judge_calls
    hidden = ('<p>' + COVER + '</p><span hidden>REVIEW_MARKER reviewer note on the regional breakdown, the appendix '
              'tables and the figures due for the board</span>')
    e.feedback("false_positive", content=hidden, agent_id=policy.agent)
    still = await e.inspect(hidden, "web", policy.agent)
    assert still.policy_rule == "drop-non-visible"   # non-visible content is policy, not AI: never waived


async def test_memory_can_be_disabled_by_policy(policy):
    policy.memory.enabled = False
    e = make(policy)
    e.feedback("attack", content=CONFIRMED)
    r = await e.inspect(CONFIRMED, "web", policy.agent)
    assert r.policy_rule != "known-attack-match"


def test_feedback_needs_recent_event_or_content(policy):
    with pytest.raises(KeyError):
        make(policy).feedback("attack", event_id="missing")


async def test_sdk_email_source_text_is_inspected_not_passed_unseen(policy):
    # regression through the SDK path: ALLOW returns the original text, so it must have been inspected.
    from intentseal import Guard

    e = make(policy, malicious=0.95, score=0.95)
    g = Guard(policy.agent, engine=e, policy=policy, warm_up=False)
    out = await g.ainspect("Plain email body that the classifier and judge both score high.", source="email")
    assert out.startswith("[Content from email was withheld")
