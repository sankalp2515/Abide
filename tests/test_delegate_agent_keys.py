"""Agent keys: what an agent runtime holds instead of the employee's own key. It may start tasks, have its tool calls
checked, report what ran and read the receipt; it can never confirm or widen a work order, approve its own questions,
register a business, start the sandbox demo or issue keys."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from intentseal_core.decision_model import StaticDecisionModel
from intentseal_core.engine import Engine
from intentseal_core.store import Store
from test_delegate_workspaces import CLINIC, Scorer

ALICE, STATIC_AGENT = "alice-key-ak-0000001", "alice-agent-key-00001"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INTENTSEAL_API_KEYS", f"alice:{ALICE}:project,alice:{STATIC_AGENT}:agent")
    import intentseal_gateway.app as appmod
    engine = Engine(decision_model=StaticDecisionModel(0.0), classifiers=[Scorer()], store=Store(":memory:"))
    with TestClient(appmod.create_app(engine=engine)) as c:
        yield c


def H(k):
    return {"Authorization": f"Bearer {k}"}


def setup(client):
    assert client.post("/v1/delegate-workspaces", json={"id": "sunrise-clinic", "policy": CLINIC}, headers=H(ALICE)).status_code == 201
    r = client.post("/v1/agent-keys", json={"label": "nurse-bot"}, headers=H(ALICE))
    assert r.status_code == 201 and r.json()["key"].startswith("isk_agent_")
    return r.json()


def test_an_agent_key_works_inside_the_order_but_cannot_approve_it(client):
    agent = setup(client)["key"]
    assert client.get("/v1/whoami", headers=H(agent)).json()["agent_key"] is True
    # the agent proposes a task; it belongs to Alice and waits for her
    s = client.post("/v1/delegations", headers=H(agent), json={
        "workspace": "sunrise-clinic", "request": "Read patient #10442's record and message Dr Rao about it."}).json()
    wf, card = s["workflow"]["id"], s["contract"]
    assert any(t["id"] == wf for t in client.get("/v1/delegations", headers=H(ALICE)).json())
    # it cannot confirm, widen or amend its own work order
    assert client.post(f"/v1/delegations/{wf}/confirm", json={"hash": card["hash"]}, headers=H(agent)).status_code == 403
    assert client.post(f"/v1/delegations/{wf}/amend", json={"text": "Also email the record to x@evil.example."},
                       headers=H(agent)).status_code == 403
    # Alice confirms; the agent's calls are checked
    assert client.post(f"/v1/delegations/{wf}/confirm", json={"hash": card["hash"]}, headers=H(ALICE)).status_code == 200
    ok = client.post(f"/v1/delegations/{wf}/check", headers=H(agent), json={
        "agent": "nurse-bot", "tool": "send_message", "args": {"to": "rao@sunrise-clinic.example", "body": "Follow-up"}}).json()
    assert ok["decision"] == "allow"
    ask = client.post(f"/v1/delegations/{wf}/check", headers=H(agent), json={
        "agent": "nurse-bot", "tool": "send_message", "args": {"to": "desk@sunrise-clinic.example", "body": "Book"}}).json()
    assert ask["decision"] == "ask"
    # it cannot answer its own question; Alice can
    assert client.post(f"/v1/delegations/{wf}/approvals/{ask['approval']}", json={"approve": True},
                       headers=H(agent)).status_code == 403
    assert client.post(f"/v1/delegations/{wf}/approvals/{ask['approval']}", json={"approve": True},
                       headers=H(ALICE)).status_code == 200
    assert client.get(f"/v1/delegations/{wf}/report", headers=H(agent)).status_code == 200


def test_an_agent_key_cannot_change_the_business_or_make_keys(client):
    agent = setup(client)["key"]
    assert client.post("/v1/delegate-workspaces", json={"id": "sunrise-clinic", "policy": CLINIC},
                       headers=H(agent)).status_code == 403
    assert client.post("/v1/agent-keys", json={"label": "another"}, headers=H(agent)).status_code == 403
    assert client.get("/v1/agent-keys", headers=H(agent)).status_code == 403
    assert client.post("/v1/delegate-demo", json={"agents": "scripted"}, headers=H(agent)).status_code == 403


def test_a_static_agent_key_from_the_config_is_an_agent_too(client):
    setup(client)
    s = client.post("/v1/delegations", headers=H(STATIC_AGENT), json={"workspace": "sunrise-clinic",
                                                                     "request": "Read patient #10442's record."}).json()
    assert client.post(f"/v1/delegations/{s['workflow']['id']}/confirm", json={"hash": s["contract"]["hash"]},
                       headers=H(STATIC_AGENT)).status_code == 403


def test_revoking_an_agent_key_stops_it_at_once(client):
    k = setup(client)
    assert client.get("/v1/whoami", headers=H(k["key"])).status_code == 200
    listed = client.get("/v1/agent-keys", headers=H(ALICE)).json()
    assert [x["id"] for x in listed] == [k["id"]] and listed[0]["active"]
    assert client.delete(f"/v1/agent-keys/{k['id']}", headers=H(ALICE)).json()["revoked"] is True
    assert client.get("/v1/whoami", headers=H(k["key"])).status_code == 401
