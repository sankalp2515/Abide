"""D-064 Intent contracts: what the user authorised, as a versioned, machine-checkable record bound to the session.

A contract is compiled from the user's own words (the task, then each user turn) and from the agent's policy. It
says which capabilities the task permits, which the user prohibited ("do not contact them"), and which high-impact
capabilities need explicit authorisation. The action guard checks every tool call against it and records the
clause that permitted or denied the call.

Rules that make it a contract rather than another detector:
  - only the user's own words amend it (the task, user turns, and the request part of a turn that carries pasted
    material, D-060). Third-party content (web pages, files, email, tool output) never reaches the compiler, so an
    injection cannot widen the scope, whatever it says and whether or not it was detected;
  - it is deterministic: no model writes or rewrites it, so it cannot be talked into anything;
  - every version is immutable and hash-chained to the one before (version, parent hash, the user words that
    changed it), so a replay shows exactly what was authorised when each action was decided;
  - a prohibition wins over a permission for the same capability (conservative), and a high-impact capability the
    user never asked for is held for approval (or blocked, by policy).

Wording rules (the compiler is small on purpose; docs/DECISIONS.md D-064):
  - permission needs the capability used as a verb ("send the summary", "please email Bob", "and then reply"), so
    "summarise my email" or "draft an email" does not authorise sending one;
  - prohibition: a negation cue ("do not", "don't", "never", "without", "no", "must not", ...) followed in the same
    clause by a capability term, as a verb or a noun ("do not contact them", "no emails", "without paying").
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any

# Capability -> word stems (prefix match on whole words). Kept small and reviewable.
TERMS: dict[str, tuple[str, ...]] = {
    "send": ("send", "sent", "email", "e-mail", "mail", "reply", "replie", "respond", "forward", "notify", "notifie",
             "contact", "share", "shari", "message", "dm", "cc", "text"),
    "submit": ("submit", "upload", "publish", "post", "fill", "register", "apply", "sign"),
    "purchase": ("buy", "bought", "purchas", "order", "pay", "paid", "checkout", "book", "subscrib", "transfer"),
    "delete": ("delet", "remov", "eras", "wipe", "purg", "drop", "destroy", "clear", "clean"),
    "execute": ("run", "ran", "execut", "install", "build", "compil", "deploy", "launch"),
    "write": ("writ", "wrote", "save", "creat", "prepar", "draft", "edit", "updat", "modif", "record", "store", "patch"),
    "read": ("read", "research", "search", "find", "look", "compar", "summar", "check", "review", "list", "open",
             "fetch", "browse", "get", "analys", "analyz", "explain", "tell", "show"),
}
_SUFFIXES = {"", "s", "es", "e", "ed", "d", "n", "er", "ers", "ing", "ion", "ions", "ment", "ments", "ped", "ping",
             "ted", "ting", "ned", "ning"}
# Tool name -> capability when the policy does not set one.
_TOOL_HINTS: tuple[tuple[str, str], ...] = (
    ("send|email|mail|message|notify|reply|forward|slack|sms|share", "send"),
    ("submit|upload|publish|webhook|form|post", "submit"),
    ("buy|purchase|order|pay|checkout|transfer|book", "purchase"),
    ("delete|remove|erase|wipe|purge|drop|destroy|unlink|rm", "delete"),
    ("terminal|shell|exec|execute|command|run|code|bash|powershell", "execute"),
    ("write|save|create|edit|update|patch|append|put", "write"),
    ("read|search|fetch|get|list|query|browse|open|find|lookup|ocr|extract|navigate", "read"),
)
_NEGATION = re.compile(r"\b(?:do\s+not|don'?t|dont|never|without|must\s+not|mustn'?t|should\s+not|shouldn'?t|"
                       r"can\s*not|can'?t|won'?t|will\s+not|no|not|avoid|refrain\s+from|stop)\b", re.I)
_NO_SKIP = {"need", "to", "more", "further", "any", "longer"}
WINDOW = 8                           # words after a negation cue that it governs
_CLAUSE_END = re.compile(r"[.;!?\n]|,\s*(?:but|then|and\s+then|just|only|instead)\b|\b(?:but|instead|except|only)\b", re.I)
# A capability word counts as a verb (permission) after these words or at the start of a clause.
_VERB_LEAD = {"", "please", "and", "then", "also", "to", "can", "could", "would", "will", "you", "kindly", "just",
              "now", "pls", "plz", "should", "must", "go", "ok", "okay", "finally", "first", "next", "lets", "let's",
              "i", "we", "me", "us", "need", "want", "or", "&", "->", "-", "*", ",", "afterwards", "after", "later"}
_SPLIT = re.compile(r"[.;!?\n]+")
_WORD = re.compile(r"[a-z][a-z'-]*|&|->|,")


def tool_capability(tool: str, declared: str | None = None) -> str | None:
    """The capability a tool exercises: the policy's `capability`, else a hint from its name, else None."""
    if declared:
        return declared
    for part in re.split(r"[^a-z]+", tool.lower()):    # the verb leads: read_email reads, send_email sends
        for words, cap in _TOOL_HINTS:
            if part in words.split("|"):
                return cap
    return None


def _cap_of(word: str) -> str | None:
    for cap, stems in TERMS.items():
        if any(word.startswith(s) and (word[len(s):] in _SUFFIXES or (len(s) >= 5 and len(word) - len(s) <= 4))
               for s in stems):
            return cap
    return None


@dataclass(frozen=True)
class Clause:
    id: str
    effect: str              # permit | prohibit | require (needs explicit authorisation) | policy (an existing control)
    capability: str          # send, submit, ... or a policy control name
    quote: str               # the user's words that created it, or the policy rule it reflects
    origin: str              # "task", "user turn N", "policy", "reviewer"

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "effect": self.effect, "capability": self.capability, "quote": self.quote,
                "origin": self.origin}


@dataclass(frozen=True)
class Contract:
    agent_id: str
    session_id: str
    version: int
    clauses: tuple[Clause, ...]
    targets: tuple[str, ...]           # addresses / domains the user named
    parent: str                        # hash of the previous version ("" for version 1)
    origin: str                        # what produced this version
    created_at: float = field(default_factory=time.time)
    seen: frozenset[str] = frozenset()  # hashes of user text already compiled (a resent turn changes nothing)

    @property
    def hash(self) -> str:
        body = json.dumps({"agent": self.agent_id, "session": self.session_id, "version": self.version,
                           "clauses": [c.as_dict() for c in self.clauses], "targets": list(self.targets),
                           "parent": self.parent}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]

    def effect(self, capability: str) -> Clause | None:
        """The deciding clause for a capability: the user's latest word on it (a later "go ahead and send it" lifts an
        earlier "don't send anything", and the reverse), else the policy's requirement."""
        for c in reversed(self.clauses):
            if c.capability == capability and c.effect in ("prohibit", "permit"):
                return c
        return next((c for c in self.clauses if c.capability == capability and c.effect == "require"), None)

    def as_dict(self) -> dict[str, Any]:
        return {"agent_id": self.agent_id, "session_id": self.session_id, "version": self.version, "hash": self.hash,
                "parent": self.parent, "origin": self.origin, "created_at": self.created_at,
                "clauses": [c.as_dict() for c in self.clauses], "targets": list(self.targets)}


def _clauses_from_text(text: str, origin: str) -> tuple[list[tuple[str, str, str]], set[str]]:
    """(effect, capability, quote) triples from one piece of the user's own words, plus named targets."""
    out: list[tuple[str, str, str]] = []
    from .session import entities
    for sentence in _SPLIT.split(text or ""):
        s = sentence.strip()
        if not s:
            continue
        low = s.lower()
        negated: list[tuple[int, int]] = []
        cues: list[tuple[str, int, int]] = []
        for m in _NEGATION.finditer(low):
            end = _CLAUSE_END.search(low, m.end())
            cues.append((m.group(), m.end(), end.start() if end else len(low)))
        prohibited_here: set[str] = set()
        for cue, start, end in cues:
            words = [w for w in _WORD.finditer(low, start, end) if w.group() != ","]
            if cue == "no":                  # "no emails", "no need to pay": only the word it qualifies
                while words and words[0].group() in _NO_SKIP:
                    words = words[1:]
                words = words[:1]
            words = words[:WINDOW]
            if words:
                negated.append((start, words[-1].end()))
            first_cap = None
            for i, w in enumerate(words):
                cap = _cap_of(w.group())
                if cap is None:
                    continue
                # the first capability after the cue, and any joined to it by "or" / "nor" / "and"
                if first_cap is None or (i > 0 and words[i - 1].group() in ("or", "nor", "and")):
                    first_cap = first_cap or cap
                    if cap != "read":        # reading is bounded by the policy (sandbox, domains), not by wording
                        prohibited_here.add(cap)
                        out.append(("prohibit", cap, s[:160]))
                else:
                    break
        tokens = list(_WORD.finditer(low))
        for i, m in enumerate(tokens):
            cap = _cap_of(m.group())
            if cap is None or cap in prohibited_here or any(a <= m.start() < b for a, b in negated):
                continue
            lead = tokens[i - 1].group() if i else ""
            if lead in _VERB_LEAD or i == 0:
                out.append(("permit", cap, s[:160]))
    targets = {e for e in entities(text or "") if "@" in e or "." in e}
    return out, targets


def compile_contract(agent_id: str, session_id: str, policy: Any, task: str = "") -> Contract:
    """Version 1: the policy's bounds plus what the task says."""
    clauses: list[Clause] = []
    cp = policy.contract
    for cap in cp.require_mention:
        clauses.append(Clause(f"R-{cap}", "require", cap, f"policy: '{cap}' needs the user's explicit request", "policy"))
    for name, tp in policy.tools.items():
        if tp.allowed_recipients:
            clauses.append(Clause(f"P-{name}-recipients", "policy", "recipients",
                                  f"{name}: only to {', '.join(tp.allowed_recipients)}", "policy"))
        if tp.allowed_domains and tp.allowed_domains != ["*"]:
            clauses.append(Clause(f"P-{name}-domains", "policy", "domains",
                                  f"{name}: only {', '.join(tp.allowed_domains)}", "policy"))
        if tp.pinned:
            clauses.append(Clause(f"P-{name}-pinned", "policy", "pinned",
                                  f"{name}: {', '.join(tp.pinned)} from the signed-in user's context", "policy"))
        if tp.path_arg:
            clauses.append(Clause(f"P-{name}-sandbox", "policy", "sandbox", f"{name}: paths stay in the workspace", "policy"))
    if policy.unknown_tools == "block":
        clauses.append(Clause("P-tools", "policy", "tools", "only the policy's tools: " + ", ".join(policy.tools), "policy"))
    clauses.append(Clause("P-data", "policy", "data", "no canaries, registered secrets or credentials leave in arguments",
                          "policy"))
    base = Contract(agent_id, session_id, 1, tuple(clauses), (), "", "policy")
    return amend(base, task, "task", bump=False) if task else base


def amend(contract: Contract, text: str, origin: str, bump: bool = True) -> Contract:
    """A new version when the user's words add a permission, a prohibition or a target; else the same contract.
    Only call this with the user's own words."""
    digest = hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]
    if not text or digest in contract.seen:
        return contract
    found, targets = _clauses_from_text(text, origin)
    prohibited = {cap for effect, cap, _ in found if effect == "prohibit"}   # in one message, a prohibition wins
    new: list[Clause] = []
    n = len(contract.clauses)
    for effect, cap, quote in found:
        if effect == "permit" and cap in prohibited:
            continue
        current = next((c for c in reversed(contract.clauses + tuple(new))
                        if c.capability == cap and c.effect in ("prohibit", "permit")), None)
        if current is not None and current.effect == effect:
            continue
        if current is None and effect == "permit" and any(c.effect == effect and c.capability == cap for c in new):
            continue
        n += 1
        new.append(Clause(f"{'U' if effect == 'permit' else 'X'}{n}-{cap}", effect, cap, quote, origin))
    new_targets = tuple(sorted(set(contract.targets) | targets))
    seen = contract.seen | {digest}
    if not new and new_targets == contract.targets:
        return replace(contract, seen=seen)
    if not bump:
        return replace(contract, clauses=contract.clauses + tuple(new), targets=new_targets, origin=origin, seen=seen)
    return Contract(contract.agent_id, contract.session_id, contract.version + 1, contract.clauses + tuple(new),
                    new_targets, contract.hash, origin, seen=seen)


@dataclass(frozen=True)
class Verdict:
    effect: str              # permit | prohibit | out-of-scope | not-governed
    capability: str | None
    clause: Clause | None

    def as_dict(self, contract: Contract) -> dict[str, Any]:
        return {"version": contract.version, "hash": contract.hash, "effect": self.effect, "capability": self.capability,
                "clause": self.clause.as_dict() if self.clause else None}


def evaluate(contract: Contract, tool: str, declared: str | None, governed: list[str]) -> Verdict:
    """How the contract rules on a call to `tool`. `governed`: the capabilities the policy requires the user to ask
    for. Capabilities outside it are permitted unless the user prohibited them."""
    cap = tool_capability(tool, declared)
    if cap is None:
        return Verdict("not-governed", None, None)
    c = contract.effect(cap)
    if c is not None and c.effect == "prohibit":
        return Verdict("prohibit", cap, c)
    if c is not None and c.effect == "permit":
        return Verdict("permit", cap, c)
    if cap in governed:
        return Verdict("out-of-scope", cap, c)
    return Verdict("permit", cap, None)
