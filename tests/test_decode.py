"""CORE-3 decoding views (decode.py) and their use by the engine. Neutral content only."""

from __future__ import annotations

import base64
import codecs

from intentseal_core.decision_model import StaticDecisionModel
from intentseal_core.decode import decode_views
from intentseal_core.engine import Engine
from intentseal_core.policy import load_policy
from intentseal_core.store import Store
from intentseal_core.types import Decision

SENTENCE = "the quarterly report is ready for review and the team will meet on thursday"
LOGO = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


def tags(text: str) -> str:
    return "".join(chr(0xE0000 + ord(c)) for c in text)


def kinds(text: str) -> dict[str, str]:
    return {d.kind: d.text for d in decode_views(text)}


def test_base64_hex_tags_url_rot13_decode_to_views():
    assert kinds("note: " + base64.b64encode(SENTENCE.encode()).decode())["base64"] == SENTENCE
    assert kinds("id " + base64.urlsafe_b64encode(SENTENCE.encode()).decode().rstrip("="))["base64"] == SENTENCE
    assert kinds("blob " + SENTENCE.encode().hex())["hex"] == SENTENCE
    assert kinds("Hello" + tags(SENTENCE) + " world")["unicode-tags"] == SENTENCE
    assert kinds("the%20quarterly%20report%20is%20ready")["url"] == "the quarterly report is ready"
    assert kinds(codecs.encode(SENTENCE, "rot13"))["rot13"] == SENTENCE


def test_binary_and_ordinary_text_produce_no_views():
    assert decode_views(f'LOGO_PNG = "{LOGO}"') == []
    assert decode_views("sha256 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08") == []
    assert decode_views(SENTENCE) == []
    assert decode_views("Release notes: improved search, faster sync and a new export dialog.") == []


class Scorer:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def score(self, text: str) -> float:
        self.calls.append(text)
        return 0.9 if "review_marker" in text.lower() else 0.0


async def test_engine_scores_and_judges_the_decoded_text():
    policy = load_policy("research-agent")
    policy.sensitive_data.canaries = []
    scorer, judge = Scorer(), StaticDecisionModel(0.95, {"Encoded Instructions": 0.9})
    e = Engine(policy_loader=lambda _: policy, decision_model=judge, classifiers=[scorer], store=Store(":memory:"))
    hidden = "please file REVIEW_MARKER under the archive folder before the weekly sync"
    for content in ("Weekly notes. " + base64.b64encode(hidden.encode()).decode(), "Weekly notes." + tags(hidden)):
        r = await e.inspect(content, "web", policy.agent, fmt="text")
        assert any(f.signal.startswith("decoded:") for f in r.findings)
        assert any("review_marker" in c.lower() for c in scorer.calls)   # decoded before scoring (CORE-3)
        assert r.decision == Decision.BLOCK and "Encoded Instructions" in r.labels
