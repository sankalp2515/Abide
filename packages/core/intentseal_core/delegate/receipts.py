"""⑨ Work receipts and the report the employee reads.

Receipts are append-only and hash-chained per workflow (each receipt's hash covers the previous one), so a changed or
removed receipt breaks the chain. The report is written from receipts only. A writer (a model, or the deterministic
template) proposes sentences with the receipt ids they rely on; `check_report` keeps a sentence only if:
  - it cites at least one receipt, and every cited id exists in this workflow;
  - every email address and file id it mentions appears in the receipts it cites.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from .model import EMAIL

log = logging.getLogger("intentseal.delegate")


def chain_hash(prev: str, row: dict[str, Any]) -> str:
    body = json.dumps({"prev": prev, **{k: row.get(k) for k in sorted(row) if k not in ("hash", "prev")}},
                      sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]


def verify_chain(rows: list[dict[str, Any]]) -> bool:
    prev = ""
    for r in rows:
        if r["prev"] != prev or chain_hash(prev, r) != r["hash"]:
            return False
        prev = r["hash"]
    return True


@dataclass
class Sentence:
    text: str
    cites: list[str]


class Writer(Protocol):
    name: str

    async def write(self, request: str, receipts: list[dict[str, Any]]) -> list[Sentence]: ...


def _evidence(r: dict[str, Any]) -> str:
    return json.dumps(r, ensure_ascii=False, default=str).lower()


def check_report(sentences: list[Sentence], receipts: list[dict[str, Any]],
                 resource_ids: list[str] | None = None,
                 names: dict[str, str] | None = None) -> tuple[list[Sentence], list[dict[str, Any]]]:
    """`names`: resource id -> the name people see ("Customer list"). A sentence that names a file or record is
    checked like one that writes its id: the id must be in the receipts it cites."""
    by_id = {r["rid"]: r for r in receipts}
    kept: list[Sentence] = []
    dropped: list[dict[str, Any]] = []
    for s in sentences:
        cites = [c.strip() for c in s.cites if c and c.strip()]
        if not cites:
            dropped.append({"text": s.text, "reason": "cites no receipt"})
            continue
        unknown = [c for c in cites if c not in by_id]
        if unknown:
            dropped.append({"text": s.text, "reason": f"cites unknown receipt {', '.join(unknown)}"})
            continue
        evidence = " ".join(_evidence(by_id[c]) for c in cites)
        claims = [m.group().lower() for m in EMAIL.finditer(s.text)]
        low = s.text.lower()
        claims += [rid for rid, name in (names or {}).items()
                   if len(name) >= 4 and name.lower() != rid and name.lower() in low]
        claims += [rid for rid in (resource_ids or []) if re.search(r"(?<![\w-])" + re.escape(rid) + r"(?![\w-])",
                                                                     s.text.lower())]
        missing = [c for c in claims if c not in evidence]
        if missing:
            dropped.append({"text": s.text, "reason": f"{', '.join(missing)} is not in the cited receipts"})
            continue
        kept.append(Sentence(s.text, cites))
    return kept, dropped


_DOING = {"send_email": "send an email", "draft_email": "save an email draft", "read_file": "open a file",
          "share_file": "share a file", "delete_file": "delete a file", "read_email": "read an email",
          "list_inbox": "check the inbox"}


class TemplateWriter:
    """Deterministic report: one plain sentence per meaningful receipt. Names come with the raw ids (addresses, file
    ids), so the citation check can verify every sentence."""
    name = "template"

    def __init__(self, label: Any = None, tools: dict[str, str] | None = None) -> None:
        self.label = label or (lambda f, v: v)
        self.tools = tools or {}

    def doing(self, tool: str) -> str:
        if tool in _DOING:
            return _DOING[tool]
        d = self.tools.get(tool, "")
        return (d[0].lower() + d[1:]) if d else f"run {tool}"

    def _names(self, field_: str, values: list[str] | None) -> str:
        out = []
        for v in values or []:
            name = self.label(field_, v)
            out.append(name)
        return ", ".join(out)

    async def write(self, request: str, receipts: list[dict[str, Any]]) -> list[Sentence]:
        out: list[Sentence] = []
        for r in receipts:
            rid, kind, d = r["rid"], r["kind"], r.get("data") or {}
            vals = d.get("values") or {}
            to = self._names("recipient", vals.get("recipient"))
            res = self._names("resource", vals.get("resource"))
            if kind == "contract" and r.get("decision") == "confirmed":
                out.append(Sentence("You confirmed what your assistants may do for this task.", [rid]))
            elif kind == "contract" and r.get("decision") == "amended":
                out.append(Sentence(f'You added: "{d.get("words", "")}"', [rid]))
            elif kind == "inspect" and d.get("flagged"):
                out.append(Sentence(f"Found hidden instructions in {d.get('source')} and kept them away from your "
                                    "assistants.", [rid]))
            elif kind == "executed" and d.get("executed"):
                tool = d.get("tool", "")
                what = {"send_email": f"Sent an email to {to}", "draft_email": f"Saved an email draft to {to}",
                        "read_file": f"Read {res}", "share_file": f"Shared {res} with {to}",
                        "read_email": "Read an email", "list_inbox": "Checked the inbox",
                        "delete_file": f"Deleted {res}", "issue_refund": f"Refunded {res}" + (
                            f" ({', '.join(self.label('money', a) for a in vals.get('amount') or [])})"
                            if vals.get("amount") else ""),
                        "reply_customer": f"Emailed {to}"}.get(tool, "Done: " + self.doing(tool))
                out.append(Sentence(what + ".", [rid, d.get("check")] if d.get("check") else [rid]))
            elif kind == "check" and r.get("decision") == "block":
                target = " ".join(x for x in (res, ("to " + to) if to else "") if x)
                origin = f" The instruction came from {d['traced_to']}, not from you." if d.get("traced_to") else ""
                reason = str(r.get("reason") or "")
                out.append(Sentence(f"Stopped an attempt to {self.doing(d.get('tool', ''))}"
                                    f"{(' (' + target + ')') if target else ''}. {reason}.{origin}", [rid]))
            elif kind == "approval" and d.get("status") in ("approved", "denied"):
                out.append(Sentence(f"You {d['status']} this exact action: {self.doing(d.get('tool', ''))}"
                                    f"{(' to ' + to) if to else ''}.", [rid]))
        return out


class ModelWriter:
    """A chat model behind the router role `receipt` (Gemini in the Google build). Output is checked like any other."""

    def __init__(self, router: Any, role: str = "receipt") -> None:
        self.router, self.role, self.name = router, role, f"model:{role}"

    async def write(self, request: str, receipts: list[dict[str, Any]]) -> list[Sentence]:
        from ..llm.adapters import ChatRequest
        if self.router is None or self.role not in getattr(getattr(self.router, "settings", None), "roles", {}):
            raise RuntimeError(f"no model configured for role '{self.role}'")
        compact = [{"rid": r["rid"], "kind": r["kind"], "agent": r.get("agent"), "decision": r.get("decision"),
                    "reason": r.get("reason"), "data": r.get("data")} for r in receipts]
        prompt = ("Write a short plain-English report for the employee about what their AI assistants did. "
                  'Return JSON only: {"sentences": [{"text": "...", "cites": ["R-1"]}]}. Every sentence must cite '
                  "the receipt ids it is based on. Mention only facts in those receipts. At most 8 sentences.\n"
                  f"Employee's request: {request}\nReceipts: {json.dumps(compact, ensure_ascii=False, default=str)[:12000]}")
        res = await self.router.chat(self.role, ChatRequest(messages=[{"role": "user", "content": prompt}],
                                                            temperature=0.0, max_tokens=4000,
                                                            response_format={"type": "json_object"}))
        self.name = f"model:{res.provider}/{res.model}"
        m = re.search(r"\{.*\}", res.content or "", re.S)
        data = json.loads(m.group()) if m else {}
        return [Sentence(str(s.get("text", "")), [str(c) for c in s.get("cites") or []])
                for s in data.get("sentences") or [] if isinstance(s, dict)]


async def write_report(request: str, receipts: list[dict[str, Any]], writer: Writer | None = None,
                       resource_ids: list[str] | None = None, label: Any = None,
                       tools: dict[str, str] | None = None) -> dict[str, Any]:
    used, dropped = "template", []
    names: dict[str, str] = {}
    if label is not None:                      # every file or record the receipts touch, by the name people see
        for r in receipts:
            for v in ((r.get("data") or {}).get("values") or {}).get("resource", []) or []:
                names[v] = label("resource", v)
        for rid in resource_ids or []:
            names.setdefault(rid, label("resource", rid))
    sentences: list[Sentence] = []
    if writer is not None:
        try:
            proposed = await writer.write(request, receipts)
            sentences, dropped = check_report(proposed, receipts, resource_ids, names)
            used = writer.name
        except Exception as e:  # noqa: BLE001 - the template always works
            log.info("report writer unavailable: %s", e)
    if not sentences:
        proposed = await TemplateWriter(label, tools).write(request, receipts)
        sentences, more = check_report(proposed, receipts, resource_ids, names)
        dropped += more
        used = "template" if used == "template" else f"template (model sentences dropped: {len(dropped)})"
    return {"writer": used, "sentences": [{"text": s.text, "cites": s.cites} for s in sentences],
            "dropped": dropped, "chain_ok": verify_chain(receipts)}
