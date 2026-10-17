"""Your own agent, remotely: the one-file client and its Google ADK callbacks against a running gateway."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from intentseal.delegate import DelegateClient, RemoteAdkGuard
from intentseal_core.decision_model import StaticDecisionModel
from intentseal_core.engine import Engine
from intentseal_core.store import Store
from test_delegate_workspaces import CLINIC

KEY = "remote-key-000000001"


class Scorer:
    name = "scorer"

    async def score(self, chunks):
        return [0.0 for _ in chunks]


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("INTENTSEAL_API_KEYS", f"me:{KEY}:project")
    import intentseal_gateway.app as appmod
    engine = Engine(decision_model=StaticDecisionModel(0.0), classifiers=[Scorer()], store=Store(":memory:"))
    a = appmod.create_app(engine=engine)
    with TestClient(a) as tc:
        yield a, tc


def tool(name):
    return SimpleNamespace(name=name)


def test_a_remote_adk_agent_is_held_to_the_card(app):
    a, _ = app

    async def go():
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=a), base_url="http://gw")
        client = DelegateClient("http://gw", KEY, client=http)
        await client.register_workspace("sunrise-clinic", CLINIC)
        card = await client.start("Read patient #10442's record and message Dr Rao about it.",
                                  workspace="sunrise-clinic")
        await client.confirm(card)
        wf = card["workflow_id"]
        g = RemoteAdkGuard(client, wf, "nurse-bot")

        assert await g.before_tool(tool("read_record"), {"patient_id": "10442"}) is None       # runs
        seen = await g.after_tool(tool("read_record"), {"patient_id": "10442"}, None,
                                  {"result": "Patient 10442: follow-up in 2 weeks."})
        assert "10442" in seen["result"]

        stopped = await g.before_tool(tool("send_message"), {"to": "leak@elsewhere.example", "body": "records"})
        assert stopped["status"] in ("stopped", "waiting")                                     # never runs
        assert await g.before_tool(tool("send_message"), {"to": "rao@sunrise-clinic.example", "body": "Follow-up"}) \
            is None
        await g.after_tool(tool("send_message"), {"to": "rao@sunrise-clinic.example", "body": "Follow-up"}, None,
                           {"result": "sent"})
        outcomes = [e["outcome"] for e in g.events]
        rep = await client.report(wf)
        await http.aclose()
        return outcomes, rep

    outcomes, rep = asyncio.run(go())
    assert outcomes[0] == "ran" and outcomes[-1] == "ran" and outcomes[1] in ("blocked", "waiting")
    assert rep["chain_ok"] is True and rep["sentences"]


def test_the_client_is_served_as_one_importable_file(app):
    _, tc = app
    r = tc.get("/v1/sdk/abide.py")
    assert r.status_code == 200 and "class RemoteAdkGuard" in r.text
    compile(r.text, "abide.py", "exec")
    assert "import intentseal" not in r.text                     # self-contained: httpx only
