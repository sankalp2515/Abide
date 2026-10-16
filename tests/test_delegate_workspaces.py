"""Bring your own business and your own agent: register a policy over the API, delegate under it, check calls
remotely; and self-serve trial keys, so nobody has to ask the operator for access."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from intentseal_core.decision_model import StaticDecisionModel
from intentseal_core.engine import Engine
from intentseal_core.store import Store

ADMIN, ALICE, BOB = "adm-key-ws-000000001", "alice-key-ws-0000001", "bob-key-ws-000000001"

CLINIC = {
    "org_domains": ["sunrise-clinic.example"],
    "tools": {
        "read_record": {"action": "read", "resource_args": ["patient_id"]},
        "send_message": {"action": "send", "recipient_args": ["to"], "content_args": ["body"]},
        "book_slot": {"action": "submit", "resource_args": ["patient_id"], "args": ["time"]},
    },
    "contacts": [{"id": "dr-rao", "name": "Dr Rao", "address": "rao@sunrise-clinic.example", "aliases": ["dr rao"]}],
    "resources": [{"id": "billing", "name": "Billing ledger", "aliases": ["billing ledger"],
                   "data_class": "confidential"}],
    "records": [{"type": "patient", "words": ["patient"], "pattern": "digits", "data_class": "confidential"}],
}


class Scorer:
    name = "scorer"

    async def score(self, chunks):
        return [0.0 for _ in chunks]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INTENTSEAL_API_KEYS", f"admin:{ADMIN}:admin,alice:{ALICE}:project,bob:{BOB}:project")
    monkeypatch.setenv("INTENTSEAL_TRIAL_KEYS", "1")
    import intentseal_gateway.app as appmod
    engine = Engine(decision_model=StaticDecisionModel(0.0), classifiers=[Scorer()], store=Store(":memory:"))
    with TestClient(appmod.create_app(engine=engine)) as c:
        yield c


def H(k):
    return {"Authorization": f"Bearer {k}"}


def test_register_a_business_and_delegate_under_it(client):
    r = client.post("/v1/delegate-workspaces", json={"id": "sunrise-clinic", "policy": CLINIC}, headers=H(ALICE))
    assert r.status_code == 201, r.text
    assert r.json()["tools"] == {"read_record": "read", "send_message": "send", "book_slot": "submit"}
    s = client.post("/v1/delegations", headers=H(ALICE), json={
        "workspace": "sunrise-clinic", "request": "Read patient #10442's record and message Dr Rao about it."}).json()
    wf, card = s["contract"]["workflow_id"], s["contract"]
    assert client.post(f"/v1/delegations/{wf}/confirm", json={"hash": card["hash"]}, headers=H(ALICE)).status_code == 200

    def check(tool, args):
        return client.post(f"/v1/delegations/{wf}/check", headers=H(ALICE),
                           json={"agent": "my-agent", "tool": tool, "args": args}).json()

    ok = check("read_record", {"patient_id": "10442"})
    assert ok["decision"] == "allow" and ok["action"] == "read"
    assert check("send_message", {"to": "rao@sunrise-clinic.example", "body": "Summary"})["decision"] == "allow"
    assert check("send_message", {"to": "someone@elsewhere.example", "body": "Summary"})["decision"] != "allow"
    assert check("read_record", {"patient_id": "99999"})["decision"] != "allow"
    assert check("book_slot", {"patient_id": "10442", "time": "10:00"})["decision"] != "allow"   # never asked


def test_a_registered_business_is_private_to_its_owner(client):
    client.post("/v1/delegate-workspaces", json={"id": "sunrise-clinic", "policy": CLINIC}, headers=H(ALICE))
    start = {"workspace": "sunrise-clinic", "request": "Read patient #10442's record."}
    assert client.post("/v1/delegations", headers=H(BOB), json=start).status_code == 404
    assert client.get("/v1/delegate-workspaces/sunrise-clinic", headers=H(BOB)).status_code == 404
    assert client.post("/v1/delegate-workspaces", headers=H(BOB),
                       json={"id": "sunrise-clinic", "policy": CLINIC}).status_code == 409
    assert "sunrise-clinic" not in [w["id"] for w in client.get("/v1/delegate-workspaces", headers=H(BOB)).json()]
    assert client.post("/v1/delegations", headers=H(ADMIN), json=start).status_code == 200


def test_bad_policies_are_refused(client):
    def reg(policy, wid="bad-one"):
        return client.post("/v1/delegate-workspaces", json={"id": wid, "policy": policy}, headers=H(ALICE))
    assert reg({"tools": {}}).status_code == 422
    assert reg({"tools": {"x": {"action": "teleport"}}}).status_code == 422
    assert reg({**CLINIC, "records": [{"type": "t", "pattern": "(a+)+$"}]}).status_code == 422      # no custom regex
    assert reg({**CLINIC, "contacts": [CLINIC["contacts"][0]] * 201}).status_code == 422
    assert reg(CLINIC, wid="desk").status_code in (409, 422)                                          # built-in
    assert client.post("/v1/delegations", headers=H(ALICE),
                       json={"workspace": "nowhere", "request": "Read it."}).status_code == 404


def test_trial_keys_are_self_serve_private_and_limited(client):
    r = client.post("/v1/trial-keys")
    assert r.status_code == 201
    key = r.json()["key"]
    assert key.startswith("isk_trial_")
    assert client.get("/v1/whoami", headers=H(key)).json()["scope"] == "project"
    s = client.post("/v1/delegate-demo", json={"agents": "scripted"}, headers=H(key)).json()
    wf = s["workflow"]["id"]
    assert client.get(f"/v1/delegate-demo/{wf}", headers=H(ALICE)).status_code in (403, 404)    # private
    assert client.post("/v1/trial-keys").status_code == 201
    assert client.post("/v1/trial-keys").status_code == 201
    assert client.post("/v1/trial-keys").status_code == 429                                       # 3 per hour
    assert client.get("/v1/whoami", headers=H("isk_trial_not-a-real-key")).status_code == 401
