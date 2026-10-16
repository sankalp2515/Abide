"""SIEM export (API-12): every decision as a security event, pulled through the API or pushed as it happens.

Record format: one JSON object per decision with Elastic Common Schema (ECS) style field names, so Splunk,
Elastic, Sentinel or any log pipeline can index it without a custom parser; the same record renders as an
ArcSight Common Event Format (CEF) line for syslog collectors. Content never leaves: records carry the
redacted reason, labels, rule and scores the decision log already holds, never content or arguments.

Push sinks (INTENTSEAL_SIEM_SINKS, comma separated; each sink is optional):
    jsonl:/var/log/intentseal/decisions.jsonl     rotating file for a log shipper (Filebeat, Fluent Bit, Vector)
    webhook:https://siem.example/ingest           HTTP POST of JSON Lines batches (e.g. Splunk HEC raw, Logstash http)
    syslog:udp://siem.example:514                 CEF over syslog (udp or tcp)
INTENTSEAL_SIEM_WEBHOOK_HEADERS   e.g. "Authorization=Splunk <token>" (several separated by ";")
INTENTSEAL_SIEM_MIN_DECISION      ALLOW (default: every decision) | NEUTRALISE | ESCALATE | BLOCK

Delivery is asynchronous and bounded: a background thread drains a queue of at most 10,000 records; webhook
batches are retried three times with backoff; records that cannot be delivered are counted, never block a
decision. Counters are reported by the Gateway's /v1/metrics.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from . import __version__

log = logging.getLogger("intentseal.siem")
SEVERITY = {"ALLOW": 1, "NEUTRALISE": 5, "ESCALATE": 6, "BLOCK": 8}
ORDER = {"ALLOW": 0, "NEUTRALISE": 1, "ESCALATE": 2, "BLOCK": 3}


def record(row: dict[str, Any], org: str | None = None) -> dict[str, Any]:
    """A decision (row of the decision log, joined with its event) as an ECS-style security event."""
    labels = row.get("labels") or []
    if isinstance(labels, str):
        labels = json.loads(labels or "[]")
    decision, enforced = row.get("decision") or "", row.get("enforced") or ""
    ts = float(row.get("created_at") or time.time())
    return {
        "@timestamp": datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "event": {"kind": "alert" if decision != "ALLOW" else "event", "category": ["intrusion_detection"],
                  "type": ["denied"] if enforced in ("BLOCK", "ESCALATE") else ["allowed"],
                  "action": decision.lower(), "outcome": "success", "id": row.get("event_id"),
                  "dataset": "intentseal.decision", "severity": SEVERITY.get(decision, 1),
                  "duration": int((row.get("latency_ms") or 0) * 1_000_000)},
        "rule": {"name": row.get("policy_rule"), "category": labels},
        "message": row.get("reason"),
        "observer": {"vendor": "IntentSeal", "product": "IntentSeal Gateway", "version": __version__},
        "organization": {"name": org} if org else None,
        "intentseal": {
            "agent_id": row.get("agent_id"), "session_id": row.get("session_id"), "kind": row.get("kind"),
            "source": row.get("source"), "decision": decision, "enforced": enforced, "labels": labels,
            "risk": row.get("risk"), "session_risk": row.get("session_risk"), "tier": row.get("tier"),
            "degraded": bool(row.get("degraded")), "budget_degraded": bool(row.get("budget_degraded")),
            "model": row.get("model"), "judge_tokens": (row.get("input_tokens") or 0) + (row.get("output_tokens") or 0),
            "ts": ts,   # epoch seconds: pass as `since` to continue an export
        },
    }


def _cef_header(value: Any) -> str:
    return str(value if value is not None else "").replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _cef_ext(value: Any) -> str:
    return (str(value if value is not None else "").replace("\\", "\\\\").replace("=", "\\=")
            .replace("\r", "\\r").replace("\n", "\\n"))


def cef(rec: dict[str, Any]) -> str:
    i, ev = rec["intentseal"], rec["event"]
    ext = {
        "rt": int(datetime.fromisoformat(rec["@timestamp"].replace("Z", "+00:00")).timestamp() * 1000),
        "act": i["decision"], "outcome": i["enforced"], "externalId": ev["id"],
        "cs1Label": "agent", "cs1": i["agent_id"], "cs2Label": "session", "cs2": i["session_id"],
        "cs3Label": "labels", "cs3": ",".join(i["labels"]), "cs4Label": "source", "cs4": i["source"],
        "cs5Label": "org", "cs5": (rec.get("organization") or {}).get("name", ""),
        "cn1Label": "riskPercent", "cn1": int(round((i["risk"] or 0) * 100)), "cn2Label": "tier", "cn2": i["tier"],
        "msg": rec["message"],
    }
    head = "|".join(_cef_header(v) for v in ("CEF:0", "IntentSeal", "Gateway", __version__, rec["rule"]["name"] or "decision",
                                             f"{i['decision']} {i['kind'] or ''}".strip(), ev["severity"]))
    return head + "|" + " ".join(f"{k}={_cef_ext(v)}" for k, v in ext.items() if v not in (None, ""))


# ---- push ------------------------------------------------------------------------------------------------

class _Sink:
    def send(self, batch: list[dict[str, Any]]) -> None: ...

    def close(self) -> None: ...


class JsonlFileSink(_Sink):
    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.handler = logging.handlers.RotatingFileHandler(path, maxBytes=10_000_000, backupCount=5, encoding="utf-8")

    def send(self, batch: list[dict[str, Any]]) -> None:
        for rec in batch:
            self.handler.emit(logging.makeLogRecord({"msg": json.dumps(rec, default=str), "levelno": logging.INFO}))

    def close(self) -> None:
        self.handler.close()


class WebhookSink(_Sink):
    def __init__(self, url: str, headers: dict[str, str] | None = None, transport: Any = None) -> None:
        import httpx

        self.url = url
        self.client = httpx.Client(timeout=10, headers={"Content-Type": "application/x-ndjson", **(headers or {})},
                                   transport=transport)

    def send(self, batch: list[dict[str, Any]]) -> None:
        body = "".join(json.dumps(r, default=str) + "\n" for r in batch)
        for attempt in range(3):
            try:
                r = self.client.post(self.url, content=body)
                if r.status_code < 300:
                    return
                if r.status_code < 500 and r.status_code != 429:
                    raise RuntimeError(f"webhook rejected the batch (HTTP {r.status_code})")
            except RuntimeError:
                raise
            except Exception:   # transport error: retry
                pass
            time.sleep(0.5 * 2 ** attempt)
        raise RuntimeError("webhook unavailable after 3 attempts")

    def close(self) -> None:
        self.client.close()


class SyslogCefSink(_Sink):
    def __init__(self, target: str) -> None:
        import socket

        u = urlsplit(target)
        self.handler = logging.handlers.SysLogHandler(
            address=(u.hostname or "localhost", u.port or 514),
            socktype=socket.SOCK_STREAM if u.scheme == "tcp" else socket.SOCK_DGRAM)

    def send(self, batch: list[dict[str, Any]]) -> None:
        for rec in batch:
            self.handler.emit(logging.makeLogRecord({"msg": cef(rec), "levelno": logging.WARNING}))

    def close(self) -> None:
        self.handler.close()


def _headers(spec: str) -> dict[str, str]:
    out = {}
    for item in filter(None, (s.strip() for s in spec.split(";"))):
        k, _, v = item.partition("=")
        if k and v:
            out[k.strip()] = v.strip()
    return out


def sinks_from(spec: str) -> list[_Sink]:
    out: list[_Sink] = []
    for item in filter(None, (s.strip() for s in spec.split(","))):
        kind, _, target = item.partition(":")
        if kind == "jsonl":
            out.append(JsonlFileSink(target))
        elif kind == "webhook":
            out.append(WebhookSink(target, _headers(os.environ.get("INTENTSEAL_SIEM_WEBHOOK_HEADERS", ""))))
        elif kind == "syslog":
            out.append(SyslogCefSink(target))
        else:
            log.warning("unknown SIEM sink '%s' ignored (jsonl | webhook | syslog)", kind)
    return out


class Forwarder:
    """Bounded asynchronous delivery of decision records to the configured sinks."""

    def __init__(self, sinks: list[_Sink], min_decision: str = "ALLOW", max_queue: int = 10_000,
                 batch: int = 100, flush_s: float = 2.0) -> None:
        self.sinks, self.min = sinks, ORDER.get(min_decision.upper(), 0)
        self.q: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=max_queue)
        self.batch, self.flush_s = batch, flush_s
        self.stats = {"queued": 0, "sent": 0, "dropped": 0, "errors": 0}
        self._thread = threading.Thread(target=self._run, name="intentseal-siem", daemon=True)
        self._thread.start()

    def submit(self, rec: dict[str, Any]) -> None:
        if ORDER.get(rec["intentseal"]["decision"], 0) < self.min:
            return
        try:
            self.q.put_nowait(rec)
            self.stats["queued"] += 1
        except queue.Full:
            self.stats["dropped"] += 1

    def _run(self) -> None:
        stop = False
        while not stop:
            batch: list[dict[str, Any]] = []
            deadline = time.monotonic() + self.flush_s
            while len(batch) < self.batch:
                try:
                    item = self.q.get(timeout=max(0.01, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if item is None:
                    stop = True
                    break
                batch.append(item)
            if not batch:
                continue
            for sink in self.sinks:
                try:
                    sink.send(batch)
                    self.stats["sent"] += len(batch)
                except Exception as e:
                    self.stats["errors"] += 1
                    self.stats["dropped"] += len(batch)
                    log.warning("SIEM sink %s failed: %s", type(sink).__name__, type(e).__name__)

    def close(self, timeout: float = 5.0) -> None:
        self.q.put(None)
        self._thread.join(timeout)
        for sink in self.sinks:
            sink.close()


_forwarder: tuple[str, Forwarder | None] | None = None
_lock = threading.Lock()


def forwarder() -> Forwarder | None:
    """The process-wide forwarder for INTENTSEAL_SIEM_SINKS (None when no sink is configured)."""
    global _forwarder
    spec = os.environ.get("INTENTSEAL_SIEM_SINKS", "").strip()
    with _lock:
        if _forwarder is None or _forwarder[0] != spec:
            if _forwarder and _forwarder[1]:
                _forwarder[1].close()
            fw = Forwarder(sinks_from(spec), os.environ.get("INTENTSEAL_SIEM_MIN_DECISION", "ALLOW")) if spec else None
            _forwarder = (spec, fw)
        return _forwarder[1]

