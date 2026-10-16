"""Onboarding endpoints: templates, providers, create an agent, connection snippets, self-test."""

from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient

from intentseal_core.config import repo_root
from intentseal_core.decision_model import StaticDecisionModel
from intentseal_core.engine import Engine
from intentseal_core.store import Store

ADMIN, PROJECT, PUBLIC = "admin-key-123456", "proj-key-123456", "public-key-123456"


class Scorer:
    def score(self, text: str) -> float:
        return 0.0


@pytest.fixture
def client(monkeypatch, tmp_path):
    pdir = tmp_path / "policies"
    pdir.mkdir()
    shutil.copy(repo_root() / "tests" / "fixtures" / "policies" / "research-agent.yaml", pdir / "research-agent.yaml")
    monkeypatch.setenv("INTENTSEAL_POLICY_DIR", str(pdir))
    monkeypatch.setenv("INTENTSEAL_API_KEYS", f"admin:{ADMIN}:admin,proj:{PROJECT}:project")
    monkeypatch.setenv("INTENTSEAL_PUBLIC_KEY", PUBLIC)
    monkeypatch.setenv("INTENTSEAL_PUBLIC_URL", "https://gw.example.test")
    from intentseal_gateway.app import create_app

    engine = Engine(decision_model=StaticDecisionModel(0.0), classifiers=[Scorer()], store=Store(":memory:"))
    with TestClient(create_app(engine=engine, reports_dir=tmp_path)) as c:
        yield c


def H(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_templates_and_agent_list(client):
    t = {x["id"]: x for x in client.get("/v1/setup/templates", headers=H(PUBLIC)).json()}
    assert {"blank", "research-assistant", "customer-support", "coding-agent", "rag-assistant"} <= set(t)
    assert t["rag-assistant"]["tools"]["search_knowledge"] == "low" and t["rag-assistant"]["tools"]["send_message"] == "high"
    assert t["customer-support"]["tools"]["issue_refund"] == "high" and t["blank"]["mode"] == "monitor"
    agents = client.get("/v1/agents", headers=H(PUBLIC)).json()
    assert [a["agent"] for a in agents] == ["research-agent"] and agents[0]["tools"]["send_message"] == "high"


def test_create_connect_and_self_test(client):
    body = {"agent": "support-bot", "template": "customer-support",
            "upstream": {"provider": None, "model": None, "key": "client"}}
    assert client.post("/v1/agents", json=body, headers=H(PROJECT)).status_code == 403      # admin only
    r = client.post("/v1/agents", json=body, headers=H(ADMIN))
    assert r.status_code == 201 and r.json()["mode"] == "monitor" and "issue_refund" in r.json()["tools"]
    assert client.post("/v1/agents", json=body, headers=H(ADMIN)).status_code == 409
    assert client.post("/v1/agents", json={**body, "agent": "../x"}, headers=H(ADMIN)).status_code == 422
    assert client.post("/v1/agents", json={**body, "agent": "x2", "template": "nope"}, headers=H(ADMIN)).status_code == 422
    assert "support-bot" in [a["agent"] for a in client.get("/v1/agents", headers=H(PUBLIC)).json()]

    con = client.get("/v1/agents/support-bot/connect", headers=H(PUBLIC)).json()
    ids = {i["id"]: i for i in con["integrations"]}
    assert con["gateway_url"] == "https://gw.example.test"
    assert "https://gw.example.test/proxy/support-bot/v1" in ids["openai-python"]["code"]
    assert "X-Upstream-Key" in ids["openai-python"]["code"]                       # key: client
    assert "/proxy/support-bot/anthropic" in ids["anthropic-python"]["code"]
    assert {"openai-env", "openai-node", "sdk-python", "rag", "rest"} <= set(ids)
    assert "<context>" in ids["rag"]["code"] and 'source="rag"' in ids["rag"]["code"]
    assert client.get("/v1/agents/nobody/connect", headers=H(PUBLIC)).status_code == 404

    st = client.post("/v1/agents/support-bot/self-test", headers=H(PROJECT)).json()
    assert st["passed"] and len(st["checks"]) == 3 and st["mode"] == "monitor"
    assert {c["rule"] for c in st["checks"]} >= {"drop-non-visible", "strip-role-markers", "tool-not-allowed"}
    for c in st["checks"]:                                                           # recorded like any session
        assert len(client.get(f"/v1/sessions/{c['session_id']}/events", headers=H(PUBLIC)).json()) == 1
