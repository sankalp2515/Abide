"""Multi-tenant organisations (API-12): keys see and act only for their organisation's agents."""

from __future__ import annotations

import shutil

import pytest
import yaml
from fastapi.testclient import TestClient

from intentseal_core.config import repo_root
from intentseal_core.decision_model import StaticDecisionModel
from intentseal_core.engine import Engine
from intentseal_core.store import Store

ACME_ADMIN, ACME_PROJECT, GLOBEX = "acme-admin-123456", "acme-proj-123456", "globex-proj-123456"
SUPER, PUBLIC, NARROW = "super-admin-123456", "public-key-123456", "acme-narrow-123456"


class Scorer:
    def score(self, text: str) -> float:
        return 0.95 if "ZQX-MARKER" in text else 0.0


@pytest.fixture
def client(monkeypatch, tmp_path):
    pdir = tmp_path / "policies"
    pdir.mkdir()
    base = yaml.safe_load((repo_root() / "tests" / "fixtures" / "policies" / "research-agent.yaml").read_text(encoding="utf-8"))
    for agent, org in (("acme-bot", "acme"), ("acme-helper", "acme"), ("globex-bot", "globex"), ("demo-bot", "default")):
        (pdir / f"{agent}.yaml").write_text(yaml.safe_dump({**base, "agent": agent, "org": org}), encoding="utf-8")
    shutil.copy(repo_root() / "tests" / "fixtures" / "policies" / "research-agent.yaml", pdir / "research-agent.yaml")
    monkeypatch.setenv("INTENTSEAL_POLICY_DIR", str(pdir))
    monkeypatch.setenv("INTENTSEAL_API_KEYS", ",".join([
        f"acme-admin:{ACME_ADMIN}:admin:acme", f"acme-ci:{ACME_PROJECT}:project:acme",
        f"globex-ci:{GLOBEX}:project:globex", f"ops:{SUPER}:admin:*",
        f"acme-narrow:{NARROW}:admin:acme:acme-helper"]))
    monkeypatch.setenv("INTENTSEAL_PUBLIC_KEY", PUBLIC)
    monkeypatch.setenv("INTENTSEAL_PUBLIC_AGENTS", "demo-bot")
    monkeypatch.delenv("INTENTSEAL_CANARIES", raising=False)
    from intentseal_gateway.app import create_app

    engine = Engine(decision_model=StaticDecisionModel(0.7), classifiers=[Scorer()], store=Store(":memory:"))
    (tmp_path / "reports").mkdir()
    with TestClient(create_app(engine=engine, reports_dir=tmp_path / "reports")) as c:
        yield c


def H(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _inspect(c, key, agent, text="Quarterly notes for the reader.", session_id=None):
    return c.post("/v1/inspect", json={"content": text, "source": "web", "agent_id": agent, "session_id": session_id},
                  headers=H(key))


def test_keys_act_only_for_their_organisation(client):
    assert _inspect(client, ACME_PROJECT, "acme-bot").status_code == 200
    assert _inspect(client, ACME_PROJECT, "globex-bot").status_code == 404           # exists, but not acme's
    assert _inspect(client, GLOBEX, "acme-bot").status_code == 404
    assert client.post("/v1/actions/check", json={"tool": "read_file", "args": {"path": "a"}, "agent_id": "globex-bot"},
                       headers=H(ACME_PROJECT)).status_code == 404
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert client.post("/proxy/globex-bot/v1/chat/completions", json=body, headers=H(ACME_PROJECT)).status_code == 404
    assert _inspect(client, NARROW, "acme-bot").status_code == 404                    # narrowed to acme-helper
    assert client.get("/v1/policies/globex-bot", headers=H(ACME_ADMIN)).status_code == 404
    assert client.get("/v1/policies/globex-bot", headers=H(SUPER)).json()["org"] == "globex"


def test_evidence_is_limited_to_the_organisation(client):
    acme = client.post("/v1/sessions", json={"agent_id": "acme-bot", "task": "t"}, headers=H(ACME_PROJECT)).json()["session_id"]
    globex = client.post("/v1/sessions", json={"agent_id": "globex-bot", "task": "t"}, headers=H(GLOBEX)).json()["session_id"]
    _inspect(client, ACME_PROJECT, "acme-bot", "Acme notes. ZQX-MARKER planted text.", acme)
    _inspect(client, GLOBEX, "globex-bot", "Globex notes. ZQX-MARKER planted text.", globex)
    _inspect(client, SUPER, "demo-bot")
    agents = lambda key: {d["agent_id"] for d in client.get("/v1/decisions", headers=H(key)).json()}
    assert agents(ACME_ADMIN) == {"acme-bot"} and agents(GLOBEX) == {"globex-bot"}
    assert agents(SUPER) == {"acme-bot", "globex-bot", "demo-bot"} and agents(PUBLIC) == {"demo-bot"}
    assert client.get(f"/v1/sessions/{globex}/events", headers=H(ACME_ADMIN)).status_code == 404
    assert client.get(f"/v1/sessions/{acme}/events", headers=H(ACME_ADMIN)).json()
    m = client.get("/v1/metrics?hours=1", headers=H(ACME_ADMIN)).json()
    assert m["decisions"] == 1 and m["providers"] == []
    costs = client.get("/v1/costs", headers=H(ACME_ADMIN)).json()
    assert costs["overhead"] is None and all(r["role"] == "judge" for r in costs["by_model"])
    assert client.get("/v1/costs/globex-bot", headers=H(ACME_ADMIN)).status_code == 404
    who = client.get("/v1/whoami", headers=H(ACME_ADMIN)).json()
    assert who["org"] == "acme" and set(who["agents"]) == {"acme-bot", "acme-helper"}


def test_approvals_and_memory_stay_inside_the_organisation(client):
    s = client.post("/v1/sessions", json={"agent_id": "globex-bot", "task": "email the summary"}, headers=H(GLOBEX)).json()["session_id"]
    _inspect(client, GLOBEX, "globex-bot", "A page that mentions boss@example-corp.test for the reader.", s)
    r = client.post("/v1/actions/check", json={"tool": "send_message", "agent_id": "globex-bot", "session_id": s,
                                                "args": {"to": "boss@example-corp.test", "subject": "s", "body": "b"}},
                    headers=H(GLOBEX)).json()
    assert r["decision"] == "ESCALATE"
    assert client.get("/v1/approvals", headers=H(ACME_ADMIN)).json() == []
    assert client.post(f"/v1/approvals/{r['event_id']}", json={"approve": True}, headers=H(ACME_ADMIN)).status_code == 404
    assert client.get("/v1/approvals", headers=H(SUPER)).json()[0]["event_id"] == r["event_id"]

    text = "please file the zebra quarterly marker under the violet folder and notify the orchard desk before noon"
    assert client.post("/v1/feedback", json={"verdict": "attack", "content": text}, headers=H(ACME_ADMIN)).status_code == 422
    fb = client.post("/v1/feedback", json={"verdict": "attack", "content": text, "agent_id": "acme-bot"},
                     headers=H(ACME_ADMIN)).json()
    assert client.post("/v1/feedback", json={"verdict": "attack", "content": text, "agent_id": "globex-bot"},
                       headers=H(ACME_ADMIN)).status_code == 404
    assert client.get("/v1/memory", headers=H(GLOBEX)).status_code == 200
    assert client.delete(f"/v1/memory/{fb['memory_id']}", headers=H(SUPER)).json()["removed"]


def test_new_policies_join_the_key_organisation(client):
    pol = client.get("/v1/policies/acme-bot", headers=H(ACME_ADMIN)).json()
    created = client.put("/v1/policies/acme-new", json={**pol, "org": None}, headers=H(ACME_ADMIN))
    assert created.status_code == 200
    assert client.get("/v1/policies/acme-new", headers=H(ACME_ADMIN)).json()["org"] == "acme"
    assert client.put("/v1/policies/acme-other", json={**pol, "org": "globex"}, headers=H(ACME_ADMIN)).status_code == 403
    assert client.put("/v1/policies/globex-bot", json=pol, headers=H(ACME_ADMIN)).status_code == 404
    assert client.put("/v1/policies/acme-bot", json={**pol, "org": "globex"}, headers=H(SUPER)).status_code == 200
