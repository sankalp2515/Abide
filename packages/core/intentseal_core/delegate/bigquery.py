"""BigQuery: every receipt streamed to an analytics table, and the questions a security team asks of them.

The receipts in the database stay the evidence (hash-chained); BigQuery is a copy for analysis across every workflow:
what agents were stopped from doing, which documents carried the instructions, how often employees had to confirm.
Streaming is best effort and off the request path: a background thread sends batches, and an outage only delays or
drops analytics rows, never a decision.

    INTENTSEAL_BIGQUERY_DATASET=abide_audit   (with GOOGLE_CLOUD_PROJECT; table `receipts`, created by the deploy)
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from typing import Any

import httpx

log = logging.getLogger("intentseal.delegate")

API = "https://bigquery.googleapis.com/bigquery/v2/projects/{project}"
TABLE = "receipts"
SCHEMA = [  # the deploy creates the table with this schema (scripts/deploy_cloud_run.ps1)
    ("workflow_id", "STRING"), ("seq", "INT64"), ("rid", "STRING"), ("kind", "STRING"), ("agent", "STRING"),
    ("tool", "STRING"), ("decision", "STRING"), ("reason", "STRING"), ("ruling", "STRING"), ("traced_to", "STRING"),
    ("explanation", "STRING"), ("workspace", "STRING"), ("principal", "STRING"), ("created", "TIMESTAMP"),
    ("hash", "STRING"), ("data", "STRING"),
]

INSIGHTS_SQL = {
    # {who}: a project key sees its own workflows, an admin all of them, the public Console only the built-in sandboxes
    "decisions": """SELECT decision, COUNT(*) AS n FROM `{t}` WHERE kind = 'check' AND created >= @since
                    AND {who} GROUP BY decision ORDER BY n DESC""",
    "stopped_tools": """SELECT tool, COUNT(*) AS n FROM `{t}` WHERE kind = 'check' AND decision != 'allow'
                        AND created >= @since AND {who}
                        GROUP BY tool ORDER BY n DESC LIMIT 8""",
    "sources": """SELECT IFNULL(traced_to, 'not traced') AS source, COUNT(*) AS n FROM `{t}` WHERE kind = 'check'
                  AND decision = 'block' AND created >= @since AND {who}
                  GROUP BY source ORDER BY n DESC LIMIT 8""",
    "daily": """SELECT FORMAT_DATE('%Y-%m-%d', DATE(created)) AS day, COUNTIF(decision = 'allow') AS allowed,
                COUNTIF(decision = 'block') AS blocked, COUNTIF(decision = 'ask') AS asked FROM `{t}`
                WHERE kind = 'check' AND created >= @since AND {who}
                GROUP BY day ORDER BY day""",
    "workflows": """SELECT COUNT(DISTINCT workflow_id) AS n FROM `{t}` WHERE created >= @since
                    AND {who}""",
}


def _token() -> str:
    from ..llm.vertex_adapter import _TOKEN          # the same Google credentials as Vertex AI (Cloud Run identity)
    return _TOKEN.get()


class BigQuery:
    def __init__(self, project: str, dataset: str, batch_s: float = 2.0, max_queue: int = 10000,
                 client: httpx.Client | None = None, token: Any = None) -> None:
        self.project, self.dataset, self.batch_s = project, dataset, batch_s
        self.table = f"{project}.{dataset}.{TABLE}"
        self._q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max_queue)
        self._http = client or httpx.Client(timeout=20)
        self._token = token or _token
        self._thread: threading.Thread | None = None
        self.sent = self.dropped = self.failed = 0
        self._cache: dict[tuple[str, str, int], tuple[float, dict[str, Any]]] = {}

    @classmethod
    def from_env(cls) -> "BigQuery | None":
        dataset = os.environ.get("INTENTSEAL_BIGQUERY_DATASET", "").strip()
        project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
        return cls(project, dataset) if dataset and project else None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}", "x-goog-user-project": self.project}

    # ---- streaming ------------------------------------------------------------------------------------------------
    def emit(self, row: dict[str, Any]) -> None:
        """Queue one receipt row; never blocks or raises."""
        try:
            self._q.put_nowait(row)
        except queue.Full:
            self.dropped += 1
            return
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="bigquery-sink", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while True:
            rows = [self._q.get()]
            time.sleep(self.batch_s)                      # gather a batch
            while len(rows) < 500:
                try:
                    rows.append(self._q.get_nowait())
                except queue.Empty:
                    break
            self.flush(rows)

    def flush(self, rows: list[dict[str, Any]]) -> bool:
        body = {"ignoreUnknownValues": True,
                "rows": [{"insertId": f"{r['workflow_id']}:{r['seq']}", "json": r} for r in rows]}
        try:
            r = self._http.post(f"{API.format(project=self.project)}/datasets/{self.dataset}/tables/{TABLE}/insertAll",
                                headers=self._headers(), json=body)
            errors = r.json().get("insertErrors") if r.status_code < 400 else None
            if r.status_code >= 400 or errors:
                self.failed += len(rows)
                log.warning("bigquery: %d rows not inserted (%s)", len(rows), r.status_code if r.status_code >= 400
                            else str(errors)[:200])
                return False
            self.sent += len(rows)
            return True
        except Exception as e:                            # network or credentials: analytics only, never a decision
            self.failed += len(rows)
            log.warning("bigquery unavailable: %s", type(e).__name__)
            return False

    # ---- insights -------------------------------------------------------------------------------------------------
    def insights(self, principal: str, scope: str = "project", demo: tuple[str, ...] = (), days: int = 30,
                 cache_s: float = 20.0) -> dict[str, Any]:
        """The security team's questions, answered by BigQuery over every streamed receipt."""
        who = {"admin": "TRUE", "project": "principal = @principal"}.get(scope, "workspace IN UNNEST(@demo)")
        key = (principal, scope, days)
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < cache_s:
            return hit[1]
        params = [
            {"name": "principal", "parameterType": {"type": "STRING"}, "parameterValue": {"value": principal}},
            {"name": "demo", "parameterType": {"type": "ARRAY", "arrayType": {"type": "STRING"}},
             "parameterValue": {"arrayValues": [{"value": d} for d in demo]}},
            {"name": "since", "parameterType": {"type": "TIMESTAMP"},
             "parameterValue": {"value": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - days * 86400))}},
        ]
        out: dict[str, Any] = {"table": self.table, "days": days, "engine": "BigQuery"}
        t0 = time.time()
        for name, sql in INSIGHTS_SQL.items():
            r = self._http.post(f"{API.format(project=self.project)}/queries", headers=self._headers(), json={
                "query": sql.format(t=self.table, who=who), "useLegacySql": False, "parameterMode": "NAMED",
                "queryParameters": params, "timeoutMs": 15000, "location": os.environ.get("INTENTSEAL_BIGQUERY_LOCATION", "US")})
            if r.status_code >= 400:
                raise RuntimeError(f"BigQuery {r.status_code}: {r.text[:200]}")
            res = r.json()
            fields = [f["name"] for f in res.get("schema", {}).get("fields", [])]
            out[name] = [{f: (c.get("v")) for f, c in zip(fields, row["f"])} for row in res.get("rows", [])]
        for rows in out.values():
            if isinstance(rows, list):
                for row in rows:
                    for k, v in row.items():
                        if isinstance(v, str) and v.isdigit():
                            row[k] = int(v)
        out["workflows"] = out["workflows"][0]["n"] if out.get("workflows") else 0
        out["query_ms"] = round((time.time() - t0) * 1000)
        self._cache[key] = (time.time(), out)
        return out


def receipt_row(row: dict[str, Any], workspace: str, principal: str) -> dict[str, Any]:
    data = row.get("data") or {}
    expl = data.get("explanation") or {}
    return {"workflow_id": row["workflow_id"], "seq": row["seq"], "rid": row["rid"], "kind": row["kind"],
            "agent": row.get("agent") or "", "tool": row.get("tool") or "", "decision": row.get("decision") or "",
            "reason": (row.get("reason") or "")[:1000], "ruling": data.get("ruling"),
            "traced_to": data.get("traced_to"), "explanation": expl.get("text") if isinstance(expl, dict) else None,
            "workspace": workspace, "principal": principal,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(row["created"])) + f".{int(row['created'] % 1 * 1000):03d}Z",
            "hash": row["hash"],
            "data": json.dumps(data, ensure_ascii=False, default=str)[:20000]}
