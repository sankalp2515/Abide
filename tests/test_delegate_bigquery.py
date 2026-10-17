"""BigQuery: every receipt is streamed with whose work it is, insights are scoped to the caller, and an outage never
touches a decision."""

from __future__ import annotations

import asyncio
import json

import httpx

from intentseal_core.delegate import load_delegate_policy
from intentseal_core.delegate.bigquery import BigQuery
from intentseal_core.delegate.service import DelegateService
from intentseal_core.store import Store

P = load_delegate_policy("workspace")


class Recorder:                                   # the service side: what would be streamed
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def emit(self, row):
        self.rows.append(row)


def test_every_receipt_is_streamed_with_its_principal_and_decision():
    rec = Recorder()
    s = DelegateService(P, store=Store(":memory:"), secret=b"k", analytics=rec)
    c = asyncio.run(s.start("ravi", "Read the paint supplier's quote.", principal="trial-abc"))
    s.confirm(c.workflow_id, c.hash, actor="trial-abc")
    asyncio.run(s.check(c.workflow_id, "worker", "wire_money", {"to": "x", "amount": 5000}))
    assert [r["kind"] for r in rec.rows] == ["contract", "contract", "check"]
    assert {r["principal"] for r in rec.rows} == {"trial-abc"} and rec.rows[-1]["decision"] == "block"
    assert rec.rows[-1]["created"].endswith("Z") and json.loads(rec.rows[-1]["data"])["tool"] == "wire_money"
    assert [r["hash"] for r in rec.rows] == [r["hash"] for r in s.receipts(c.workflow_id)]


def fake_bigquery(handler) -> BigQuery:
    return BigQuery("proj", "abide_audit", client=httpx.Client(transport=httpx.MockTransport(handler)),
                    token=lambda: "t")


def test_rows_are_inserted_with_stable_ids_so_retries_do_not_duplicate():
    seen = {}

    def handler(req):
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        return httpx.Response(200, json={})
    bq = fake_bigquery(handler)
    assert bq.flush([{"workflow_id": "wf-1", "seq": 3, "kind": "check"}])
    assert seen["url"].endswith("/projects/proj/datasets/abide_audit/tables/receipts/insertAll")
    assert seen["body"]["rows"][0]["insertId"] == "wf-1:3" and bq.sent == 1


def test_an_outage_is_counted_not_raised():
    def handler(req):
        raise httpx.ConnectError("down")
    bq = fake_bigquery(handler)
    assert bq.flush([{"workflow_id": "wf-1", "seq": 1}]) is False and bq.failed == 1


def test_insights_are_scoped_to_the_caller():
    queries = []

    def handler(req):
        body = json.loads(req.content)
        queries.append(body)
        if "COUNT(DISTINCT" in body["query"]:
            return httpx.Response(200, json={"schema": {"fields": [{"name": "n"}]}, "rows": [{"f": [{"v": "4"}]}]})
        return httpx.Response(200, json={"schema": {"fields": [{"name": "decision"}, {"name": "n"}]},
                                         "rows": [{"f": [{"v": "block"}, {"v": "7"}]}]})
    bq = fake_bigquery(handler)
    out = bq.insights("trial-abc", "project")
    assert out["workflows"] == 4 and out["decisions"] == [{"decision": "block", "n": 7}]
    assert all("principal = @principal" in q["query"] for q in queries)
    assert {p["name"] for p in queries[0]["queryParameters"]} == {"principal", "demo", "since"}
    queries.clear()
    bq.insights("public", "public", ("Ravi's office",))
    assert all("workspace IN UNNEST(@demo)" in q["query"] and "principal" not in q["query"] for q in queries)
