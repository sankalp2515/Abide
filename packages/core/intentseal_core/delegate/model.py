"""Delegation contracts, the workspace policy they are checked against, and the trusted directory.

A clause names an action (read, draft, send, ...) and, optionally, the exact values it covers: who may receive
something (`recipient`) and which files or records may be used (`resource`). Values are stored resolved: email
addresses in lower case, resources by their directory id. Names are resolved only through the trusted directory,
never from content an agent read.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ..config import repo_root

ACTIONS = ("read", "draft", "write", "send", "share", "submit", "purchase", "delete", "execute")
# Actions that leave the workspace or cannot be undone: only with a permit clause from the employee's words.
GOVERNED = frozenset({"send", "share", "submit", "purchase", "delete", "execute"})
FIELDS = ("recipient", "resource", "amount")      # amount: the most the employee allowed (one number)
EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_NUMBER = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*(k|lakh|lac|lakhs|thousand)?\b", re.I)
_SCALE = {"k": 1_000, "thousand": 1_000, "lakh": 100_000, "lac": 100_000, "lakhs": 100_000}


def to_number(text: str) -> float | None:
    """"46,000" -> 46000; "2k" -> 2000; "1.5 lakh" -> 150000; None when there is no number."""
    m = _NUMBER.search(str(text or ""))
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    return value * _SCALE.get((m.group(2) or "").lower(), 1)


def norm(text: str) -> str:
    """Case, quote style and spacing do not matter when matching a quote or a value against the request."""
    text = (text or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", text).strip().casefold()


def _loose(name: str) -> str:
    """A file name without extension or separators: "Customer_List.csv" -> "customer list"."""
    stem = re.sub(r"\.(csv|xlsx?|docx?|pdf|txt|json)$", "", norm(name))
    return re.sub(r"[\s_\-]+", " ", stem).strip()


# ---- workspace policy ----------------------------------------------------------------------------------------------

class ToolSpec(BaseModel):
    action: str                                            # one of ACTIONS
    recipient_args: list[str] = Field(default_factory=list)
    resource_args: list[str] = Field(default_factory=list)
    content_args: list[str] = Field(default_factory=list)  # free text (bodies): traced for untrusted origin
    amount_args: list[str] = Field(default_factory=list)   # money or quantities: checked against the stated limit
    args: list[str] = Field(default_factory=list)          # other harmless fields (ids, flags)
    description: str = ""

    def declared(self) -> set[str]:
        return {*self.recipient_args, *self.resource_args, *self.content_args, *self.amount_args, *self.args}


class RecordType(BaseModel):
    """Records too many to list (orders, tickets, invoices): named by a word and a number in the employee's words,
    e.g. "order #4521". The id is "<type>:<number>"."""
    type: str                                              # order
    words: list[str] = Field(default_factory=list)         # ["order", "ord"]
    pattern: str = r"\d{3,8}"                              # the number part
    data_class: str = "internal"


class Contact(BaseModel):
    id: str
    name: str
    address: str
    aliases: list[str] = Field(default_factory=list)


class Resource(BaseModel):
    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    data_class: str = "internal"                           # public | internal | confidential


class AskBefore(BaseModel):
    action: str
    when: str = "always"                                   # always | external_recipient


class DelegatePolicy(BaseModel):
    workspace: str = "default"
    agent_id: str = "delegate-workspace"                   # engine policy used for content inspection
    org_domains: list[str] = Field(default_factory=list)
    confidential_classes: list[str] = Field(default_factory=lambda: ["confidential"])
    tools: dict[str, ToolSpec] = Field(default_factory=dict)
    contacts: list[Contact] = Field(default_factory=list)
    resources: list[Resource] = Field(default_factory=list)
    ask_before: list[AskBefore] = Field(default_factory=list)
    records: list[RecordType] = Field(default_factory=list)
    # Extra words for an action in this business, e.g. {"purchase": ["refund", "reimburse"]}.
    verbs: dict[str, list[str]] = Field(default_factory=dict)
    # Who owns a record, from the system of record (a contact with address "@record-owner" resolves through this).
    record_owners: dict[str, str] = Field(default_factory=dict)
    currency: str = "₹"                                    # how amounts are shown to people

    # -- directory lookups --
    def contact_by_address(self, address: str) -> Contact | None:
        a = address.strip().lower()
        return next((c for c in self.contacts if c.address.lower() == a), None)

    def record(self, key: str) -> Resource | None:
        """A record named by its type word and number ("order #4521", "ORD-4521"), or a bare number in a field that
        holds that record type (handled by the caller). Returns a pseudo-resource "<type>:<number>"."""
        k = norm(key).lstrip("./")
        for rt in self.records:
            m = re.fullmatch(r"(?:(?:" + "|".join(map(re.escape, rt.words or [rt.type])) + r")s?\s*[-#:]?\s*"
                             r"(?:no\.?|number)?\s*#?\s*|#\s*)(" + rt.pattern + r")", k, re.I)
            if m:
                return Resource(id=f"{rt.type}:{m.group(1)}", name=f"{rt.type} {m.group(1)}", data_class=rt.data_class)
        k_id = k.split(":", 1)
        if len(k_id) == 2:
            rt2 = next((r for r in self.records if r.type == k_id[0]), None)
            if rt2 and re.fullmatch(rt2.pattern, k_id[1]):
                return Resource(id=k, name=f"{rt2.type} {k_id[1]}", data_class=rt2.data_class)
        return None

    def resource(self, key: str) -> Resource | None:
        k = norm(key).lstrip("./").strip()
        found = next((r for r in self.resources if k in (r.id.lower(), norm(r.name), *(norm(a) for a in r.aliases))),
                     None)
        if found is None:      # a file name an agent made up: "customer_list.csv" is still the customer list
            loose = _loose(k)
            found = next((r for r in self.resources
                          if loose in {_loose(x) for x in (r.id, r.name, *r.aliases)}), None) if loose else None
        return found or self.record(k)

    def action_words(self, action: str) -> list[str]:
        return [w.lower() for w in self.verbs.get(action, [])]

    def is_internal(self, address: str) -> bool:
        domain = address.rsplit("@", 1)[-1].lower()
        return any(domain == d.lower().lstrip("@") for d in self.org_domains)

    def is_confidential(self, resource_id: str) -> bool:
        r = self.resource(resource_id)
        return r is not None and r.data_class in self.confidential_classes

    def label(self, field_: str, value: str) -> str:
        """A value as an employee knows it: "BrightCoat Paints (sales@brightcoat.example)", "Customer list"."""
        if field_ == "recipient":
            c = self.contact_by_address(value)
            if c is None and value in {o.lower() for o in self.record_owners.values()}:
                rec = next(k for k, o in self.record_owners.items() if o.lower() == value)
                return f"the customer on {rec.replace(':', ' ')} ({value})"
            return f"{c.name} ({c.address})" if c else value.removeprefix("unknown:")
        if field_ in ("amount", "money"):
            n = to_number(value)
            money = f"{self.currency}{n:,.0f}" if n is not None else value.removeprefix("unknown:")
            return money if field_ == "money" else f"up to {money}"
        r = self.resource(value)
        return r.name if r else value.removeprefix("unknown:")

    def mentions(self, text: str) -> dict[str, list[tuple[str, str]]]:
        """Directory entries named in `text`: field -> [(alias as written, resolved value)]. Longest alias first, so
        "paint supplier's quote" wins over "quote". A contact and a resource may share words ("the paint supplier's
        quote" names both the supplier and the quote)."""
        low = norm(text)
        found: dict[str, list[tuple[str, str]]] = {"recipient": [], "resource": []}
        taken: dict[str, list[tuple[int, int]]] = {"recipient": [], "resource": []}
        entries: list[tuple[str, str, str]] = []
        for c in self.contacts:
            for a in {c.name, *c.aliases}:
                entries.append((norm(a), "recipient", c.address.lower()))
        for r in self.resources:
            for a in {r.name, *r.aliases}:
                entries.append((norm(a), "resource", r.id))
        for alias, fld, value in sorted(entries, key=lambda e: -len(e[0])):
            if not alias:
                continue
            for m in re.finditer(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", low):
                if any(a < m.end() and m.start() < b for a, b in taken[fld]):
                    continue
                taken[fld].append((m.start(), m.end()))
                if (alias, value) not in found[fld]:
                    found[fld].append((alias, value))
        for m in EMAIL.finditer(text or ""):
            addr = m.group().lower()
            if all(v != addr for _, v in found["recipient"]):
                found["recipient"].append((addr, addr))
        for rt in self.records:   # "order #4521", "orders 4521": needs the type word or a '#'
            words = "|".join(map(re.escape, rt.words or [rt.type]))
            for m in re.finditer(r"(?<![a-z0-9])(?:(?:" + words + r")s?\s*[-#:]?\s*(?:no\.?|number)?\s*#?\s*|#)(" +
                                 rt.pattern + r")(?![0-9])", low, re.I):
                rid = f"{rt.type}:{m.group(1)}"
                if all(v != rid for _, v in found["resource"]):
                    found["resource"].append((m.group().strip(), rid))
        # "the customer" = the owner of a record named in the same words, from the system of record
        owners = [self.record_owners[v] for _, v in found["resource"] if v in self.record_owners]
        resolved = []
        for alias, v in found["recipient"]:
            if v == "@record-owner":
                resolved += [(alias, o.lower()) for o in owners]
            else:
                resolved.append((alias, v))
        found["recipient"] = list(dict.fromkeys(resolved))
        return found


def load_delegate_policy(name: str = "workspace", directory: Path | None = None) -> DelegatePolicy:
    path = (directory or repo_root() / "policies" / "delegate") / f"{name}.yaml"
    return DelegatePolicy.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


# ---- contract -------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Clause:
    id: str
    effect: str                                  # permit | prohibit
    action: str                                  # one of ACTIONS
    values: dict[str, tuple[str, ...]]           # field -> resolved values; a missing field means "not named"
    quote: str                                   # the employee's words that grant or forbid it
    origin: str = "request"                      # request | turn N | inherited
    note: str = ""
    verified: bool = True                        # False: understood by the model, not checked word by word (D-D12)

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "effect": self.effect, "action": self.action,
                "values": {k: list(v) for k, v in sorted(self.values.items())}, "quote": self.quote,
                "origin": self.origin, "note": self.note, "verified": self.verified}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Clause":
        return cls(d["id"], d["effect"], d["action"], {k: tuple(v) for k, v in (d.get("values") or {}).items()},
                   d["quote"], d.get("origin", "request"), d.get("note", ""), d.get("verified", True))

    def plain(self, policy: "DelegatePolicy | None" = None) -> str:
        """The clause as one line an employee can read, e.g. "Allowed: write an email draft to BrightCoat Paints"."""
        def lab(f: str, v: str) -> str:
            return policy.label(f, v) if policy else v
        res = ", ".join(lab("resource", v) for v in self.values.get("resource", ()))
        to = ", ".join(lab("recipient", v) for v in self.values.get("recipient", ()))
        records = ", ".join(lab("resource", v) for v in self.values.get("resource", ()) if ":" in v)
        files = ", ".join(lab("resource", v) for v in self.values.get("resource", ()) if ":" not in v)
        if self.action == "draft":
            text = "write an email draft" + (f" to {to}" if to else "")
        elif self.action == "send":
            text = "send email" + (f" to {to}" if to else (" (only inside the company)" if self.effect == "permit"
                                                            else ""))
            text += f" about {records}" if records else ""
            text += f", with {files} attached" if files and self.effect == "permit" else ""
        elif self.action == "share":
            text = f"share {res or 'files'}" + (f" with {to}" if to else "")
        else:
            verb = {"read": "read", "write": "write", "submit": "submit", "purchase": "pay or refund",
                    "delete": "delete", "execute": "run"}[self.action]
            text = f"{verb} {res}" if res else (f"{verb} anything" if self.effect == "prohibit"
                                                 else f"{verb} (nothing confidential)")
        if self.values.get("amount"):
            text += ", " + (policy.label("amount", self.values["amount"][0]) if policy
                            else f"up to {self.values['amount'][0]}")
        return ("Allowed: " if self.effect == "permit" else "Not allowed: ") + text


@dataclass(frozen=True)
class DelegationContract:
    id: str
    workflow_id: str
    owner: str
    request: str                                 # every word the employee has given, in order
    version: int
    clauses: tuple[Clause, ...]
    parent: str = ""                             # hash of the previous version
    status: str = "draft"                        # draft | confirmed
    compiler: str = "rules"                      # rules | model:<name>
    questions: tuple[str, ...] = ()
    rejected: tuple[dict[str, Any], ...] = ()    # what the validator refused, and why (shown, never enforced)
    notes: tuple[str, ...] = ()                  # plain messages for the employee (e.g. about pasted text)
    created_at: float = field(default_factory=time.time)

    @property
    def hash(self) -> str:
        body = json.dumps({"id": self.id, "workflow": self.workflow_id, "owner": self.owner, "request": self.request,
                           "version": self.version, "clauses": [c.as_dict() for c in self.clauses],
                           "parent": self.parent, "status": self.status}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]

    def as_dict(self, policy: "DelegatePolicy | None" = None) -> dict[str, Any]:
        sup = superseded(self.clauses)
        return {"id": self.id, "workflow_id": self.workflow_id, "owner": self.owner, "request": self.request,
                "version": self.version, "hash": self.hash, "parent": self.parent, "status": self.status,
                "compiler": self.compiler,
                "clauses": [c.as_dict() | {"plain": c.plain(policy), "superseded_by": sup.get(c.id)}
                            for c in self.clauses], "notes": list(self.notes),
                "questions": list(self.questions), "rejected": list(self.rejected), "created_at": self.created_at}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DelegationContract":
        return cls(d["id"], d["workflow_id"], d["owner"], d["request"], d["version"],
                   tuple(Clause.from_dict(c) for c in d["clauses"]), d.get("parent", ""), d.get("status", "draft"),
                   d.get("compiler", "rules"), tuple(d.get("questions") or ()), tuple(d.get("rejected") or ()),
                   tuple(d.get("notes") or ()), d.get("created_at", time.time()))


def superseded(clauses: tuple[Clause, ...]) -> dict[str, str]:
    """Clause id -> the later clause that replaced it: the employee's later word on the same action, with the opposite
    effect, covering the same values (or all of them). Mirrors the gate's "latest word wins" rule, for display."""
    out: dict[str, str] = {}
    fam = {"draft": "write", "write": "write"}
    for i, c in enumerate(clauses):
        for later in clauses[i + 1:]:
            if fam.get(later.action, later.action) != fam.get(c.action, c.action) or later.effect == c.effect:
                continue
            if not c.values or any(set(c.values.get(f, ())) & set(later.values.get(f, ())) for f in c.values):
                out[c.id] = later.id
                break
    return out


@dataclass(frozen=True)
class ToolCall:
    tool: str
    args: dict[str, Any]
    agent: str = ""

    def action_hash(self, contract_hash: str) -> str:
        body = json.dumps({"tool": self.tool, "args": self.args, "contract": contract_hash}, sort_keys=True,
                          ensure_ascii=False, default=str)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]


def _flat(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        if not value.strip():
            return []                      # an empty field names nobody and nothing
        return [v for v in re.split(r"[,;]\s*", value) if v.strip()] if "@" in value else [value]
    if isinstance(value, (list, tuple)):
        return [x for v in value for x in _flat(v)]
    return [str(value)]


def call_values(spec: ToolSpec, args: dict[str, Any], policy: DelegatePolicy) -> dict[str, list[str]]:
    """The recipients and resources a call touches, resolved like clause values. An unknown resource keeps its raw
    name with an `unknown:` prefix, so it can never match a clause."""
    out: dict[str, list[str]] = {"recipient": [], "resource": [], "amount": []}
    for a in spec.recipient_args:
        for v in _flat(args.get(a)):
            m = EMAIL.search(v)
            out["recipient"].append(m.group().lower() if m else "unknown:" + norm(v))
    for a in spec.resource_args:
        for v in _flat(args.get(a)):
            r = policy.resource(v)
            if r is None and re.fullmatch(r"\s*\d+\s*", v) and len(policy.records) == 1:
                r = policy.record("#" + v.strip())       # a bare number in a field that holds one record type
            out["resource"].append(r.id if r else "unknown:" + norm(v))
    for a in spec.amount_args:
        for v in _flat(args.get(a)):
            n = to_number(v)
            out["amount"].append(f"{n:g}" if n is not None else "unknown:" + norm(v))
    return out
