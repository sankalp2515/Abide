"""Abide gateway API: the live Ravi demo through HTTP, and the general routes' scopes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from intentseal_core.decision_model import StaticDecisionModel
from intentseal_core.engine import Engine
from intentseal_core.store import Store

ADMIN, PROJECT, PUBLIC = "adm-key-delegate-0001", "proj-key-delegate-0001", "pub-key-delegate-0001"


class Scorer:
    name = "scorer"

    async def score(self, chunks):
        return [0.0 for _ in chunks]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INTENTSEAL_API_KEYS", f"admin:{ADMIN}:admin,proj:{PROJECT}:project")
    monkeypatch.setenv("INTENTSEAL_PUBLIC_KEY", PUBLIC)
    import intentseal_gateway.app as appmod
    engine = Engine(decision_model=StaticDecisionModel(0.0), classifiers=[Scorer()], store=Store(":memory:"))
    with TestClient(appmod.create_app(engine=engine)) as c:
        yield c


def H(k):
    return {"Authorization": f"Bearer {k}"}


def test_live_demo_flow(client):
    r = client.post("/v1/delegate-demo", json={}, headers=H(PROJECT))
    assert r.status_code == 200, r.text
    s = r.json()
    wf, card = s["workflow"]["id"], s["contract"]
    assert card["status"] == "draft" and any(c["plain"].startswith("Not allowed: send") for c in card["clauses"])
    s = client.post(f"/v1/delegate-demo/{wf}/confirm", json={"hash": card["hash"]}, headers=H(PROJECT)).json()
    ws = s["demo"]["workspace"]
    assert any(d["to"] == "sales@brightcoat.example" for d in ws["drafts"]) and not ws["sent"]
    # the hidden instruction never ran
    assert all(e["args"].get("to") != "records@brightcoat-billing.example" for e in ws["executed"])
    s = client.post(f"/v1/delegate-demo/{wf}/say", json={"text": "Looks good, send it."}, headers=H(PROJECT)).json()
    pending = [a for a in s["approvals"] if a["status"] == "pending"]
    assert len(pending) == 1 and not s["demo"]["workspace"]["sent"]
    r = client.post(f"/v1/delegations/{wf}/approvals/{pending[0]['id']}", json={"approve": True}, headers=H(PROJECT))
    assert r.status_code == 200
    assert client.get(f"/v1/delegate-demo/{wf}", headers=H(PUBLIC)).status_code == 403     # the public key reads nothing
    s = client.get(f"/v1/delegate-demo/{wf}", headers=H(PROJECT)).json()
    assert [m["to"] for m in s["demo"]["workspace"]["sent"]] == ["sales@brightcoat.example"]
    assert s["report"]["chain_ok"] and s["report"]["sentences"]


def test_scopes_and_errors(client):
    assert client.post("/v1/delegations", json={"request": "Read the quote"}, headers=H(PUBLIC)).status_code == 403
    s = client.post("/v1/delegations", json={"request": "Read the quote"}, headers=H(PROJECT)).json()
    wf = s["workflow"]["id"]
    assert client.post(f"/v1/delegations/{wf}/confirm", json={"hash": "not-the-card-hash"}, headers=H(PROJECT)).status_code == 409
    assert client.post(f"/v1/delegations/{wf}/confirm", json={}, headers=H(PROJECT)).status_code == 422   # no blind confirm
    assert client.get("/v1/delegations/wf-missing", headers=H(PROJECT)).status_code == 404
    client.post(f"/v1/delegations/{wf}/confirm", json={"hash": s["contract"]["hash"]}, headers=H(PROJECT))
    r = client.post(f"/v1/delegations/{wf}/check", json={"agent": "a", "tool": "send_email",
                                                          "args": {"to": "x@evil.example"}}, headers=H(PROJECT)).json()
    assert r["decision"] in ("ask", "block") and r["notice"]
    env = client.post(f"/v1/delegations/{wf}/handoff", json={"from_agent": "a", "to_agent": "b", "content": "hi"},
                      headers=H(PROJECT)).json()
    env["content"] = "changed"
    assert client.post(f"/v1/delegations/{wf}/open", json={"envelope": env}, headers=H(PROJECT)).status_code == 409


@pytest.mark.parametrize("attack", ["hidden-pdf", "email-body"])
def test_gate_alone_stops_the_attack(client, attack):
    s = client.post("/v1/delegate-demo", json={"attack": attack, "firewall": False}, headers=H(PROJECT)).json()
    wf = s["workflow"]["id"]
    s = client.post(f"/v1/delegate-demo/{wf}/confirm", json={"hash": s["contract"]["hash"]}, headers=H(PROJECT)).json()
    events = s["demo"]["events"]
    assert any(e["outcome"] == "blocked" for e in events)                       # the agent did try
    executed = s["demo"]["workspace"]["executed"]
    assert all(e["args"].get("to") != "records@brightcoat-billing.example" for e in executed)
    assert all(e["args"].get("name") != "customers.csv" for e in executed)
    assert not [a for a in s["approvals"] if a["status"] == "pending"]          # stopped without asking anyone


@pytest.mark.parametrize("attack,firewall", [("email-body", True), ("email-body", False), ("hidden-pdf", False)])
def test_support_desk_demo(client, attack, firewall):
    s = client.post("/v1/delegate-demo", json={"domain": "desk", "attack": attack, "firewall": firewall},
                    headers=H(PROJECT)).json()
    wf = s["workflow"]["id"]
    assert any("up to ₹2,000" in c["plain"] for c in s["contract"]["clauses"])
    s = client.post(f"/v1/delegate-demo/{wf}/confirm", json={"hash": s["contract"]["hash"]}, headers=H(PROJECT)).json()
    ws = s["demo"]["workspace"]
    assert [(r["order_id"], r["amount"]) for r in ws["refunds"]] == [("4521", 1850.0)]
    assert [m["to"] for m in ws["sent"]] == ["meera@mail.example"]
    assert all(e["tool"] != "export_customers" for e in ws["executed"])
    assert s["report"]["chain_ok"]


def test_other_key_cannot_approve(client, monkeypatch):
    s = client.post("/v1/delegate-demo", json={}, headers=H(PROJECT)).json()
    wf = s["workflow"]["id"]
    # the admin key may act for the employee; a public key may not act at all
    assert client.post(f"/v1/delegate-demo/{wf}/confirm", json={"hash": s["contract"]["hash"]},
                       headers=H(PUBLIC)).status_code == 403
    assert client.post(f"/v1/delegate-demo/{wf}/confirm", json={"hash": s["contract"]["hash"]},
                       headers=H(ADMIN)).status_code == 200


def test_wrong_person_gets_403(client, monkeypatch):
    s = client.post("/v1/delegations", json={"request": "Read the quote"}, headers=H(PROJECT)).json()
    wf = s["workflow"]["id"]
    from intentseal_core.delegate.service import DelegateForbidden
    svc = client.app.state.delegate_services["workspace"]
    with pytest.raises(DelegateForbidden):
        svc.confirm(wf, None, actor="someone-else")


def test_eval_summary_is_public(client):
    r = client.get("/v1/delegate-eval")
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        assert "delegate" in r.json()["arms"]



def test_delegations_are_private_to_their_owner(client, monkeypatch):
    s = client.post("/v1/delegations", json={"request": "Read the quote"}, headers=H(PROJECT)).json()
    wf = s["workflow"]["id"]
    for path in (f"/v1/delegations/{wf}", f"/v1/delegations/{wf}/report", "/v1/delegations"):
        assert client.get(path, headers=H(PUBLIC)).status_code == 403
    assert client.get(f"/v1/delegations/{wf}", headers=H(PROJECT)).status_code == 200
    assert client.get(f"/v1/delegations/{wf}", headers=H(ADMIN)).status_code == 200
    # another project key (a different employee) sees nothing, not even that it exists
    svc = client.app.state.delegate_services["workspace"]
    svc.store.execute("UPDATE delegate_principals SET principal = 'someone-else' WHERE workflow_id = ?", (wf,))
    assert client.get(f"/v1/delegations/{wf}", headers=H(PROJECT)).status_code == 404
    assert all(w["id"] != wf for w in client.get("/v1/delegations", headers=H(PROJECT)).json())


def test_demo_can_be_switched_off(client, monkeypatch):
    monkeypatch.setenv("INTENTSEAL_DELEGATE_DEMO", "0")
    assert client.post("/v1/delegate-demo", json={}, headers=H(PROJECT)).status_code == 404


def test_the_task_list_shows_each_task_once_with_what_the_your_agents_page_needs(client):
    s = client.post("/v1/delegations", json={"request": "Read the quote"}, headers=H(PROJECT)).json()
    wf = s["workflow"]["id"]
    client.post(f"/v1/delegations/{wf}/confirm", json={"hash": s["contract"]["hash"]}, headers=H(PROJECT))
    client.post(f"/v1/delegations/{wf}/check", json={"agent": "a", "tool": "wire_money", "args": {"to": "x"}},
                headers=H(PROJECT))
    rows = [w for w in client.get("/v1/delegations", headers=H(PROJECT)).json() if w["id"] == wf]
    assert len(rows) == 1                                   # one store, one row: not once per business
    row = rows[0]
    assert row["request"] == "Read the quote" and row["status"] == "confirmed" and row["source"] == "api"
    assert row["stopped"] == 1 and row["waiting"] == 0 and row["business"]
    assert client.get("/v1/insights", headers=H(PROJECT)).json()["configured"] is False   # no BigQuery here


def test_my_agents_lists_who_acted_under_my_work_orders_only(client):
    s = client.post("/v1/delegations", json={"request": "Read the quote"}, headers=H(PROJECT)).json()
    wf = s["workflow"]["id"]
    client.post(f"/v1/delegations/{wf}/confirm", json={"hash": s["contract"]["hash"]}, headers=H(PROJECT))
    for tool in ("wire_money", "read_file"):
        client.post(f"/v1/delegations/{wf}/check", json={"agent": "billing-bot", "tool": tool, "args": {"name": "x"}},
                    headers=H(PROJECT))
    mine = {a["agent"]: a for a in client.get("/v1/my-agents", headers=H(PROJECT)).json()}
    bot = mine["billing-bot"]
    assert bot["checks"] == 2 and bot["stopped"] >= 1 and bot["tasks"] == 1 and bot["sandbox"] is True
    svc = client.app.state.delegate_services["workspace"]
    svc.store.execute("UPDATE delegate_principals SET principal = 'someone-else' WHERE workflow_id = ?", (wf,))
    assert "billing-bot" not in {a["agent"] for a in client.get("/v1/my-agents", headers=H(PROJECT)).json()}
