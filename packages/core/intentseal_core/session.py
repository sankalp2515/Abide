"""CORE-6 Session state: accumulated risk (with decay) and simple provenance.

Provenance: entities (email addresses, URLs/domains, record IDs) that appeared in untrusted content but
not in the user's own messages or trusted context. A high-risk action whose target comes only from
untrusted content is a strong sign the action was steered by that content.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .taint import TaintTracker

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL = re.compile(r"https?://[^\s\"'<>)\]]+", re.I)
_DOMAIN = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.I)
_RECORD_ID = re.compile(r"\b[A-Z]{1,4}-\d{3,}\b")


def entities(text: str) -> set[str]:
    """Normalised entities an action could target."""
    out: set[str] = set()
    for e in _EMAIL.findall(text or ""):
        out.add(e.lower())
    for u in _URL.findall(text or ""):
        out.add(u.lower().rstrip(".,;"))
        if d := domain_of(u):
            out.add(d)
    for r in _RECORD_ID.findall(text or ""):
        out.add(r.upper())
    return out


_HOST = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*$")


def domain_of(url_or_email: str) -> str:
    """Host an HTTP client or mailer would actually use; "" when it is ambiguous.

    Parsed with the standard library, so a fragment, query, port or user part can never pose as the host
    (`https://evil.example#.allowed.test` is evil.example). Backslashes, whitespace and user parts are
    rejected outright: clients disagree on them."""
    s = url_or_email.lower().strip()
    if "@" in s and "://" not in s:
        return s.rsplit("@", 1)[-1].strip(" >")
    if "\\" in s or any(c.isspace() or ord(c) < 32 for c in s):
        return ""
    try:
        parts = urlsplit(s if "://" in s else "//" + s)
        host = parts.hostname or ""
    except ValueError:
        return ""
    if "@" in parts.netloc or not _HOST.match(host):
        return ""
    return host


@dataclass
class Session:
    agent_id: str
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    user_ref: str | None = None            # stored hashed
    task: str = ""
    trusted: dict[str, Any] = field(default_factory=dict)   # e.g. {"customer_id": "C-1042"}
    risk: float = 0.0                      # all risk: gates actions and further third-party content
    user_risk: float = 0.0                 # risk from the user's own turns only: gates the user's messages (multi-step)
    turns: int = 0
    labels: set[str] = field(default_factory=set)
    trusted_entities: set[str] = field(default_factory=set)
    trusted_words: set[str] = field(default_factory=set)   # words of the user's own messages (explicit-intent rule)
    untrusted_entities: set[str] = field(default_factory=set)
    flagged_untrusted: bool = False        # any finding in untrusted content this session
    risk_seen: set[str] = field(default_factory=set)   # content already counted toward risk (D-063: once per content)
    taint: TaintTracker = field(default_factory=TaintTracker)   # CORE-17 data-flow origins (fingerprints only)
    entity_origin: dict[str, tuple[str, str]] = field(default_factory=dict)   # untrusted entity -> (event id, source)
    contract: Any = None                   # D-064 current intent contract (contract.Contract), immutable versions
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.user_ref:
            self.user_ref = hashlib.sha256(self.user_ref.encode()).hexdigest()[:16]
        self.note_trusted(self.task)
        for v in self.trusted.values():
            self.note_trusted(str(v))

    def note_trusted(self, text: str) -> None:
        self.trusted_entities |= entities(text)
        self.trusted_words |= set(re.findall(r"[a-z0-9]+", (text or "").lower()))
        self.taint.observe(text, "", "user", trusted=True, flagged=False)

    def note_untrusted(self, text: str, event_id: str = "", source: str = "") -> None:
        new = entities(text) - self.trusted_entities
        self.untrusted_entities |= new
        if event_id:                       # provenance: the first untrusted content each target appeared in
            for e in new:
                self.entity_origin.setdefault(e, (event_id, source))

    def untrusted_origin(self, value: str) -> tuple[str, str] | None:
        for e in entities(value) or {value.lower().strip()}:
            if e in self.entity_origin and e not in self.trusted_entities:
                return self.entity_origin[e]
        return None

    def user_asked_for(self, terms: list[str]) -> bool:
        """Did the user's own words include any of these terms (word prefix: "send" matches "sends", "sending")?"""
        wanted = [t.lower().strip() for t in terms if t.strip()]
        return any(w.startswith(t) for t in wanted for w in self.trusted_words)

    def from_untrusted_only(self, value: str) -> bool:
        ents = entities(value) or {value.lower().strip()}
        return any(e in self.untrusted_entities and e not in self.trusted_entities for e in ents)

    def new_turn(self, decay: float) -> None:
        self.turns += 1
        self.risk = max(0.0, self.risk - decay)
        self.user_risk = max(0.0, self.user_risk - decay)

    def add_risk(self, amount: float, labels: list[str] | None = None, user: bool = False, key: str | None = None) -> None:
        """key: a fingerprint of the content. The same content read again (another tool, a retry) adds no new risk:
        an agent re-reading one poisoned file is one attack, not several (D-063)."""
        if key is not None and amount > 0:
            if key in self.risk_seen:
                self.labels |= set(labels or [])
                return
            self.risk_seen.add(key)
        self.risk += max(0.0, amount)
        if user:
            self.user_risk += max(0.0, amount)
        self.labels |= set(labels or [])


class SessionStore:
    """In-process session registry (the Gateway API persists sessions through the logger).

    Bounded: the least recently used sessions are dropped beyond `max_sessions` (INTENTSEAL_MAX_SESSIONS,
    default 20000), so a stream of one-off session ids cannot grow memory without limit."""

    def __init__(self, max_sessions: int | None = None) -> None:
        self._s: OrderedDict[str, Session] = OrderedDict()
        self.max_sessions = max_sessions or int(os.environ.get("INTENTSEAL_MAX_SESSIONS") or "20000")

    def __contains__(self, session_id: object) -> bool:
        return session_id in self._s

    def __len__(self) -> int:
        return len(self._s)

    def _keep(self, s: Session) -> Session:
        self._s[s.session_id] = s
        self._s.move_to_end(s.session_id)
        while len(self._s) > self.max_sessions:
            self._s.popitem(last=False)
        return s

    def create(self, agent_id: str, **kw: Any) -> Session:
        if not kw.get("session_id"):
            kw.pop("session_id", None)
        return self._keep(Session(agent_id=agent_id, **kw))

    def get(self, session_id: str | None, agent_id: str) -> Session:
        if session_id and session_id in self._s:
            return self._keep(self._s[session_id])
        return self._keep(Session(agent_id=agent_id, session_id=session_id or uuid.uuid4().hex[:16]))
