"""SIEM export (API-12): pull (JSON Lines / CEF) and push (file, webhook, syslog); content never leaves."""

from __future__ import annotations

import json
import socket
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from intentseal_core import siem
from intentseal_core.decision_model import StaticDecisionModel
from intentseal_core.engine import Engine
from intentseal_core.store import Store

PLANTED = "ZQX-MARKER planted text for the reader"
ADMIN, PROJECT = "admin-key-123456", "proj-key-123456"


class Scorer:
    def score(self, text: str) -> float:
        return 0.95 if "ZQX-MARKER" in text else 0.0


def _row(**kw):
    row = {"event_id": "e1", "agent_id": "research-agent", "session_id": "s1", "decision": "BLOCK", "enforced": "BLOCK",
           "policy_rule": "content-risk-block", "reason": "Content risk | reached = block\nthreshold", "labels": ["Tool Abuse"],
           "risk": 0.91, "session_risk": 0.9, "tier": 2, "latency_ms": 12.5, "degraded": 0, "budget_degraded": 0,
           "model": "groq/m", "input_tokens": 10, "output_tokens": 5, "kind": "content_in", "source": "web",
           "created_at": 1_760_000_000.25}
    return {**row, **kw}


def test_record_fields_and_cef_escaping():
    rec = siem.record(_row(), "acme")
    assert rec["@timestamp"].endswith("Z") and rec["event"]["kind"] == "alert" and rec["event"]["severity"] == 8
    assert rec["rule"]["name"] == "content-risk-block" and rec["organization"] == {"name": "acme"}
    assert rec["intentseal"]["ts"] == 1_760_000_000.25 and rec["intentseal"]["judge_tokens"] == 15
    line = siem.cef(rec)
    assert line.startswith("CEF:0|IntentSeal|Gateway|") and "|content-risk-block|BLOCK content_in|8|" in line
    assert "msg=Content risk | reached \\= block\\nthreshold" in line and "cs5=acme" in line and "cn1=91" in line
    assert siem.record(_row(decision="ALLOW", enforced="ALLOW"))["event"]["kind"] == "event"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("INTENTSEAL_API_KEYS", f"admin:{ADMIN}:admin,proj:{PROJECT}:project")
    from intentseal_gateway.app import create_app

    engine = Engine(decision_model=StaticDecisionModel(0.95), classifiers=[Scorer()], store=Store(":memory:"))
    (tmp_path / "reports").mkdir()
    with TestClient(create_app(engine=engine, reports_dir=tmp_path / "reports")) as c:
        yield c


def _inspect(c, text):
    return c.post("/v1/inspect", json={"content": text, "source": "web", "agent_id": "research-agent"},
                  headers={"Authorization": f"Bearer {PROJECT}"}).json()


def test_pull_export_jsonl_and_cef_with_continuation(client):
    since = time.time() - 5
    ids = [_inspect(client, f"Notes {i}. {PLANTED}.")["event_id"] for i in range(3)]
    h = {"Authorization": f"Bearer {PROJECT}"}
    r = client.get(f"/v1/export/decisions?since={since}", headers=h)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    recs = [json.loads(line) for line in r.text.splitlines()]
    assert [x["event"]["id"] for x in recs] == ids and PLANTED not in r.text
    nxt = client.get(f"/v1/export/decisions?since={recs[0]['intentseal']['ts']}&limit=1", headers=h).text.splitlines()
    assert len(nxt) == 1 and json.loads(nxt[0])["event"]["id"] == ids[1]
    cef = client.get(f"/v1/export/decisions?since={since}&format=cef", headers=h).text.splitlines()
    assert len(cef) == 3 and all(x.startswith("CEF:0|IntentSeal|") for x in cef)
    assert client.get("/v1/export/decisions", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_push_to_jsonl_file(client, monkeypatch, tmp_path):
    path = tmp_path / "siem" / "decisions.jsonl"
    monkeypatch.setenv("INTENTSEAL_SIEM_SINKS", f"jsonl:{path}")
    try:
        eid = _inspect(client, f"Notes. {PLANTED}.")["event_id"]
        siem.forwarder().close()
        lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
        assert lines[-1]["event"]["id"] == eid and lines[-1]["organization"] == {"name": "default"}
        assert PLANTED not in path.read_text(encoding="utf-8")
    finally:
        monkeypatch.delenv("INTENTSEAL_SIEM_SINKS")
        siem.forwarder()


def test_push_webhook_batches_retries_and_min_decision():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503 if len(calls) == 1 else 200)   # first attempt fails, retry succeeds

    sink = siem.WebhookSink("https://siem.example.test/ingest", {"Authorization": "Splunk t"},
                            transport=httpx.MockTransport(handler))
    fw = siem.Forwarder([sink], min_decision="NEUTRALISE", flush_s=0.05)
    fw.submit(siem.record(_row(decision="ALLOW", enforced="ALLOW")))     # below the minimum: not sent
    fw.submit(siem.record(_row()))
    fw.submit(siem.record(_row(event_id="e2")))
    time.sleep(1.2)
    fw.close()
    assert fw.stats["sent"] == 2 and fw.stats["dropped"] == 0
    body = calls[-1].content.decode().splitlines()
    assert [json.loads(x)["event"]["id"] for x in body] == ["e1", "e2"] and calls[-1].headers["authorization"] == "Splunk t"


def test_push_cef_over_syslog_udp():
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    srv.settimeout(5)
    fw = siem.Forwarder(siem.sinks_from(f"syslog:udp://127.0.0.1:{srv.getsockname()[1]}"), flush_s=0.05)
    try:
        fw.submit(siem.record(_row()))
        data = srv.recv(65535).decode()
        assert "CEF:0|IntentSeal|Gateway|" in data and "act=BLOCK" in data
    finally:
        fw.close()
        srv.close()
