"""CORE-9 Action guard: deterministic checks on every tool call before it runs.

Checks, in order (first hard violation wins; all observations are recorded):
  1. tool allowed for this agent
  2. pinned fields: values come from trusted session context, never from the model
  3. outbound scan: canaries / secrets / registered values in any argument
  4. recipient allow-list (send-type tools)
  5. URL domain allow-list (fetch-type tools) - stops data leaving through a URL
  6. path stays inside the sandbox (file tools)
  7. provenance: high-risk target that appears only in untrusted content
  8. session risk: high-risk tool while the session carries risk from untrusted content
  8b. intent contract (D-064): a capability the user prohibited is blocked; a high-impact capability the user never
      asked for is out of scope (held for approval). Without a contract: the explicit-intent terms
  9. data flow: flagged content copied into a high-risk tool
"""

from __future__ import annotations

import fnmatch
import time
from dataclasses import dataclass, field
from email.utils import getaddresses
from typing import Any

from . import contract as contracts
from .outbound import scan_args
from .policy import AgentPolicy, ToolPolicy
from .session import Session, domain_of
from .types import ActionResult, Decision


@dataclass
class ActionCheck:
    rule: str
    decision: Decision
    reason: str
    labels: list[str] = field(default_factory=list)


def _recipient_ok(addr: str, allowed: list[str]) -> bool:
    a = addr.strip().lower()
    if a.count("@") != 1 or not a.split("@")[0] or not a.split("@")[1]:
        return False
    for rule in allowed:
        r = rule.lower()
        if (r.startswith("@") and a.endswith(r)) or a == r:
            return True
    return False


def recipients(value: Any) -> list[str]:
    """Addresses a mailer would deliver to, parsed the way mail libraries parse them (display names, angle
    brackets, comma/semicolon lists). A piece that does not parse to an address is kept as is, so it fails."""
    raw = ", ".join(map(str, value)) if isinstance(value, (list, tuple)) else str(value)
    out: list[str] = []
    for piece in raw.replace(";", ",").split(","):
        if not piece.strip():
            continue
        parsed = [a for _, a in getaddresses([piece])]
        out += parsed if parsed and all(parsed) else [piece]
    # getaddresses splits "Name <x@a> @b" into two entries; any leftover fragment must also pass.
    for _, a in getaddresses([raw.replace(";", ",")]):
        if a and a not in out:
            out.append(a)
    return out


def _domain_ok(url: str, allowed: list[str]) -> bool:
    d = domain_of(url)
    if not d:
        return False
    return any(d == a.lower() or d.endswith("." + a.lower()) or fnmatch.fnmatch(d, a.lower()) for a in allowed)


def _path_ok(path: str) -> bool:
    p = path.replace("\\", "/")
    return not (p.startswith("/") or ".." in p.split("/") or (len(p) > 1 and p[1] == ":"))


def check_action(tool: str, args: dict[str, Any], policy: AgentPolicy, session: Session,
                 registered_secrets: list[str] | None = None) -> ActionResult:
    t0 = time.perf_counter()
    args = dict(args or {})
    checks: list[ActionCheck] = []
    tp: ToolPolicy | None = policy.tools.get(tool)

    if tp is None:
        if policy.unknown_tools == "block":
            checks.append(ActionCheck("tool-not-allowed", Decision.BLOCK, f"'{tool}' is not an allowed tool for this agent",
                                      ["Tool Abuse"]))
        tp = ToolPolicy()

    # 2. pinned fields (rewrite, never trust the model's value)
    overrides: dict[str, Any] = {}
    for f in tp.pinned:
        if f in session.trusted:
            if str(args.get(f, "")).strip() != str(session.trusted[f]).strip():
                overrides[f] = {"requested": args.get(f), "pinned": session.trusted[f]}
            args[f] = session.trusted[f]
    if overrides:
        checks.append(ActionCheck("pinned-field-override", Decision.ALLOW,
                                  f"pinned field(s) {', '.join(overrides)} set from trusted context", ["Tool Abuse"]))

    # 3. outbound secrets / canaries
    hits = scan_args(args, policy.canary_values(), registered_secrets or [])
    if hits:
        kinds = sorted({h.kind for h in hits})
        checks.append(ActionCheck("outbound-secret", Decision.BLOCK,
                                  f"arguments contain protected data ({', '.join(kinds)}) in {', '.join(sorted({h.field for h in hits}))}",
                                  ["Secret Extraction", "Credential Theft"]))

    # 4. recipients
    if tp.allowed_recipients and tp.recipient_arg in args:
        parsed = recipients(args[tp.recipient_arg])
        bad = [r for r in parsed if not _recipient_ok(r, tp.allowed_recipients)]
        if bad or not parsed:
            checks.append(ActionCheck("recipient-not-allowed", Decision.BLOCK,
                                      f"recipient outside allowed list ({len(bad)} address(es))", ["Tool Abuse"]))

    # 5. URL domains
    if tp.allowed_domains and tp.url_arg in args:
        if not _domain_ok(str(args[tp.url_arg]), tp.allowed_domains):
            checks.append(ActionCheck("domain-not-allowed", Decision.BLOCK,
                                      f"URL domain '{domain_of(str(args[tp.url_arg]))}' is not approved", ["Tool Abuse"]))

    # 6. sandbox paths
    if tp.path_arg and tp.path_arg in args and not _path_ok(str(args[tp.path_arg])):
        checks.append(ActionCheck("path-outside-sandbox", Decision.BLOCK, "path leaves the workspace", ["Tool Abuse"]))

    # 7. provenance of high-risk targets
    targets: list[dict[str, Any]] = []
    if tp.risk == "high":
        for key in (tp.recipient_arg, tp.url_arg):
            if key in args and session.from_untrusted_only(str(args[key])):
                if origin := session.untrusted_origin(str(args[key])):
                    targets.append({"arg": key, "origin": origin[0], "source": origin[1], "flagged": False,
                                    "coverage": 1.0, "kind": "target"})
                checks.append(ActionCheck("target-from-untrusted-content",
                                          Decision.ESCALATE if tp.escalate_if_uncertain else Decision.BLOCK,
                                          f"'{key}' value appears only in untrusted content, not in the user's request",
                                          ["Indirect Prompt Injection", "Tool Abuse"]))

    # 8. session risk on high-risk tools
    if tp.risk == "high" and session.risk >= policy.session.escalate_at:
        hard = session.risk >= policy.session.block_at
        checks.append(ActionCheck("high-risk-tool-in-risky-session", Decision.BLOCK if hard else Decision.ESCALATE,
                                  f"session risk {session.risk:.2f} after untrusted content",
                                  sorted(session.labels) or ["Multi-Step Jailbreaks"]))

    # 8b. intent contract (D-064): what the user authorised, from the user's own words only
    verdict = None
    if session.contract is not None and policy.contract.mode == "enforce":
        verdict = contracts.evaluate(session.contract, tool, tp.capability, policy.contract.require_mention)
        clause = verdict.clause
        if verdict.effect == "prohibit" and clause is not None:
            checks.append(ActionCheck("contract-prohibited", Decision.BLOCK,
                                      f"the user ruled out '{verdict.capability}' (contract v{session.contract.version}, "
                                      f"clause {clause.id}: \"{clause.quote}\")", ["Tool Abuse"]))
        elif verdict.effect == "out-of-scope":
            checks.append(ActionCheck("contract-out-of-scope",
                                      Decision.BLOCK if policy.contract.out_of_scope == "block" else Decision.ESCALATE,
                                      f"'{verdict.capability}' is outside what the user asked for (contract "
                                      f"v{session.contract.version}); it needs the user's explicit authorisation",
                                      ["Tool Abuse"]))
    # 8b'. explicit intent without a contract (docs/13 L1): a share/destructive tool the user never asked for is held
    if verdict is None and tp.intent_terms and not session.user_asked_for(tp.intent_terms):
        checks.append(ActionCheck("action-not-requested", Decision.ESCALATE,
                                  f"the user's messages do not ask for this action ({', '.join(tp.intent_terms[:4])}…)",
                                  ["Tool Abuse"]))

    # 9. data flow (CORE-17): where each argument's text came from; flagged content into a high-risk tool
    taint = session.taint.trace(args, policy.taint.min_coverage) if policy.taint.enabled else []
    flagged = sorted({h.arg for h in taint if h.flagged})
    if tp.risk == "high" and flagged and policy.taint.flagged_to_high_risk in ("escalate", "block"):
        checks.append(ActionCheck("tainted-from-flagged-content",
                                  Decision.BLOCK if policy.taint.flagged_to_high_risk == "block" else Decision.ESCALATE,
                                  f"argument(s) {', '.join(flagged)} copy text from content the gateway flagged",
                                  ["Indirect Prompt Injection", "Tool Abuse"]))

    order = {Decision.BLOCK: 3, Decision.ESCALATE: 2, Decision.NEUTRALISE: 1, Decision.ALLOW: 0}
    worst = max(checks, key=lambda c: order[c.decision], default=None)
    decision = worst.decision if worst else Decision.ALLOW
    enforced = decision if policy.mode == "enforce" else Decision.ALLOW
    labels = sorted({l for c in checks for l in c.labels})
    reason = "; ".join(c.reason for c in checks) or "within policy"
    notice = None
    if enforced == Decision.BLOCK:
        notice = f"Action '{tool}' was blocked by the security policy: {worst.reason}. Continue the user's task without it."
    elif enforced == Decision.ESCALATE:
        notice = f"Action '{tool}' needs human approval ({worst.reason}). It was not performed; tell the user it is awaiting approval."
    if decision != Decision.ALLOW:
        session.add_risk(0.5 if decision == Decision.BLOCK else 0.3, labels)
    ruling = None
    if session.contract is not None and policy.contract.mode == "enforce":
        ruling = (verdict.as_dict(session.contract) if verdict is not None else
                  {"version": session.contract.version, "hash": session.contract.hash, "effect": "not-governed",
                   "capability": None, "clause": None})
        ruling["rule"] = worst.rule if worst else "allow"
    return ActionResult(decision=decision, enforced=enforced,
                        final_args=args if enforced == Decision.ALLOW else None,
                        pinned_overrides=overrides, labels=labels, contract=ruling,
                        taint=[h.as_dict() for h in taint] + targets,
                        policy_rule=worst.rule if worst else "allow", reason=reason, notice=notice,
                        latency_ms=round((time.perf_counter() - t0) * 1000, 2))
