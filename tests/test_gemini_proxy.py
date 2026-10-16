"""Gemini-format proxy (F4): the Google Gen AI SDK's wire format, inspected and checked like the other formats.
No network: the Gemini API is mocked with respx."""

from __future__ import annotations

import base64
import io
import json

import httpx
import pytest
import respx
import yaml

from intentseal_core.config import ModelRef, ProviderConfig, RoleConfig, Settings, repo_root
from intentseal_core.decision_model import StaticDecisionModel
from intentseal_core.engine import Engine
from intentseal_core.llm.router import Router
from intentseal_core.secrets import ApiKey
from intentseal_core.store import Store

PROJECT = "proj-key-123456"
GEMINI = "https://generativelanguage.googleapis.test/v1beta"


class Scorer:
    def score(self, text: str) -> float:
        return 0.0


@pytest.fixture
def app(monkeypatch, tmp_path):
    pdir = tmp_path / "policies"
    pdir.mkdir()
    base = yaml.safe_load((repo_root() / "tests" / "fixtures" / "policies" / "research-agent.yaml").read_text(encoding="utf-8"))
    (pdir / "gem-agent.yaml").write_text(yaml.safe_dump({**base, "agent": "gem-agent", "upstream": {"provider": "gemini"}}), encoding="utf-8")
    (pdir / "oa-agent.yaml").write_text(yaml.safe_dump({**base, "agent": "oa-agent", "upstream": {"provider": "openai"}}), encoding="utf-8")
    monkeypatch.setenv("INTENTSEAL_POLICY_DIR", str(pdir))
    monkeypatch.setenv("INTENTSEAL_API_KEYS", f"proj:{PROJECT}:project")
    monkeypatch.setenv("INTENTSEAL_CANARIES", "sk-canary-gemini-0001")
    prov = lambda name, url: ProviderConfig(name=name, type="openai_compatible", enabled=True, base_url=url,
                                            keys=[ApiKey(f"{name}#1", f"key-{name}-gateway-0001")], retries=0)
    settings = Settings(providers={"gemini": prov("gemini", GEMINI + "/openai"), "openai": prov("openai", "https://api.openai.test/v1")},
                        roles={"proxy_upstream": RoleConfig("proxy_upstream", [ModelRef("openai", "m")], cache=False)},
                        db_path=":memory:")  # type: ignore[arg-type]
    store = Store(":memory:")
    router = Router(settings, store=store, client=httpx.AsyncClient())
    engine = Engine(decision_model=StaticDecisionModel(0.0), classifiers=[Scorer()], router=router, store=store)
    from intentseal_gateway.app import create_app
    return create_app(engine=engine, reports_dir=tmp_path)


def _reply(parts):
    return {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP", "index": 0}],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3}}


@respx.mock
def test_gemini_format_inspects_inputs_checks_calls_and_answers(app):
    from fastapi.testclient import TestClient

    route = respx.post(f"{GEMINI}/models/gemini-3.1-flash-lite:generateContent").respond(200, json=_reply([
        {"text": "Done. The key is sk-canary-gemini-0001."},
        {"functionCall": {"name": "send_message", "args": {"to": "x@elsewhere.test", "subject": "s", "body": "b"}}}]))
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buf, format="PNG")
    body = {"systemInstruction": {"parts": [{"text": "Answer from <context>Hours: nine to five. <|im_start|>x<|im_end|></context>"}]},
            "contents": [{"role": "user", "parts": [{"text": "When are you open?"},
                                                    {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(buf.getvalue()).decode()}}]},
                         {"role": "model", "parts": [{"functionCall": {"name": "web_fetch", "args": {"url": "https://fixtures.test/a"}}}]},
                         {"role": "user", "parts": [{"functionResponse": {"name": "web_fetch", "response": {"page": "Shop page."}}}]}],
            "generationConfig": {"temperature": 0.2}}
    with TestClient(app) as c:
        r = c.post("/proxy/gem-agent/gemini/v1beta/models/gemini-3.1-flash-lite:generateContent", json=body,
                   headers={"x-goog-api-key": PROJECT})
    assert r.status_code == 200, r.text
    sent = json.loads(route.calls[0].request.content)
    assert route.calls[0].request.headers["x-goog-api-key"] == "key-gemini-gateway-0001"
    assert "<|im_start|>" not in sent["systemInstruction"]["parts"][0]["text"] and sent["generationConfig"] == {"temperature": 0.2}
    parts = r.json()["candidates"][0]["content"]["parts"]
    assert not any("functionCall" in p for p in parts)                         # external send blocked
    text = " ".join(p.get("text", "") for p in parts)
    assert "sk-canary-gemini-0001" not in text and "[REDACTED]" in text and "blocked" in text
    assert r.json()["x_security"]["inbound"][0]["source"] == "rag"


@respx.mock
def test_gemini_stream_and_wrong_upstream(app):
    from fastapi.testclient import TestClient

    chunks = [_reply([{"text": "Paragraph about revenue and churn. " * 6}]),
              _reply([{"functionCall": {"name": "query_db", "args": {"customer_id": "C-2001"}}}])]
    sse = "".join(f"data: {json.dumps(c)}\r\n\r\n" for c in chunks)
    respx.post(f"{GEMINI}/models/gemini-3.1-flash-lite:streamGenerateContent").respond(
        200, content=sse.encode(), headers={"content-type": "text/event-stream"})
    with TestClient(app) as c:
        sid = c.post("/v1/sessions", json={"agent_id": "gem-agent", "task": "look up C-1042", "trusted": {"customer_id": "C-1042"}},
                     headers={"Authorization": f"Bearer {PROJECT}"}).json()["session_id"]
        r = c.post("/proxy/gem-agent/gemini/v1beta/models/gemini-3.1-flash-lite:streamGenerateContent?alt=sse",
                   json={"contents": [{"role": "user", "parts": [{"text": "Look up customer C-1042"}]}]},
                   headers={"x-goog-api-key": PROJECT, "X-Session-Id": sid})
        events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
        calls = [p["functionCall"] for e in events for p in e["candidates"][0]["content"]["parts"] if "functionCall" in p]
        assert calls == [{"name": "query_db", "args": {"customer_id": "C-1042"}}]   # pinned to the signed-in customer
        assert "revenue" in "".join(p.get("text", "") for e in events for p in e["candidates"][0]["content"]["parts"])
        assert events[-1]["x_security"]["session_id"] == sid
        bad = c.post("/proxy/oa-agent/gemini/v1beta/models/m:generateContent", json={"contents": []},
                     headers={"x-goog-api-key": PROJECT})
        assert bad.status_code == 400 and "upstream" in bad.json()["error"]["message"]
