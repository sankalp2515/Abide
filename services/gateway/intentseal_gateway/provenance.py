"""Attack-to-action provenance (D-065): how untrusted content reached an attempted action, which control stopped it,
and whether the action ran, built only from the decision log, never from what a model says.

For each action the session attempted, a chain:
  source     the untrusted content that influenced it: a proven link (the call's arguments copy text from that content,
             CORE-17 data-flow fingerprints, or its target first appeared there) or, without one, the flagged content
             read earlier in the session, marked as context (not proven)
  content    what the inspection found there (evidence spans, signals, the content decision)
  action     the proposed tool call (redacted arguments)
  control    the rule that decided it, and the intent-contract clause (D-064) that permitted or denied it
  outcome    executed or not, with the evidence: the enforced decision, the application's execution receipt, a
             reviewer's approval
Content that was stopped before any action (quarantined, held) is a chain without an action.
"""

from __future__ import annotations

from typing import Any

UNTRUSTED_SKIP = {"user", "system", "output"}


def _source_node(ev: dict[str, Any]) -> dict[str, Any]:
    return {"event_id": ev["id"], "seq": ev["seq"], "source": ev["source"], "decision": ev.get("decision"),
            "rule": ev.get("policy_rule"), "reason": ev.get("reason"), "labels": ev.get("labels") or [],
            "preview": ev.get("preview") or "",
            "evidence": [{"signal": f.get("signal"), "origin": f.get("origin"), "span": f.get("span_preview") or "",
                          "score": f.get("score")}   # strongest first: the judge's or classifier's highest-scored span
                         for f in sorted(ev.get("findings") or [], key=lambda f: -(f.get("score") or 0))][:6]}


def _outcome(ev: dict[str, Any], approval: dict[str, Any] | None) -> dict[str, Any]:
    act = ev["action"]
    receipt = act.get("receipt")
    enforced = ev.get("enforced") or ev.get("decision")
    evidence: list[str] = []
    if enforced == "ALLOW":
        if receipt and receipt["executed"]:
            state, evidence = "executed", ["the application's execution receipt reports the call ran"]
        elif receipt:
            state, evidence = "not-executed", [f"the application reports it did not run ({receipt.get('detail') or 'no detail'})"]
        else:
            state, evidence = "allowed", ["allowed by policy; the application did not report execution"]
        if ev.get("policy_rule") == "human-approved":
            evidence.append("a reviewer approved this exact call")
    else:
        state = "not-executed"
        evidence = [f"decision {enforced} was enforced: the call was withheld (LLM proxy: removed from the model's "
                    "response before the agent saw it; SDK / REST: the tool function was not invoked)"]
        if receipt and receipt["executed"]:
            state = "contradiction"
            evidence.append("the application nevertheless reported running it: investigate the integration")
        elif receipt:
            evidence.append("the application's receipt confirms it did not run")
        if approval:
            evidence.append(f"approval request: {approval['status']}"
                            + (f" by {approval['reviewer']}" if approval.get("reviewer") else ""))
    return {"state": state, "enforced": enforced, "evidence": evidence, "receipt": receipt}


def build(events: list[dict[str, Any]], approvals: dict[str, dict[str, Any]] | None = None,
          contracts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    approvals = approvals or {}
    by_id = {e["id"]: e for e in events}
    chains: list[dict[str, Any]] = []
    linked: set[str] = set()
    for i, ev in enumerate(events):
        if ev.get("kind") != "action" or not ev.get("action"):
            continue
        act = ev["action"]
        proven: list[dict[str, Any]] = []
        for t in act.get("taint") or []:
            src = by_id.get(t.get("origin") or "")
            if src is None:
                continue
            link = ("target first appeared in this content" if t.get("kind") == "target"
                    else f"argument '{t.get('arg')}' copies {round((t.get('coverage') or 0) * 100)}% of its text from this content")
            proven.append({**_source_node(src), "link": link, "arg": t.get("arg"), "proven": True})
            linked.add(src["id"])
        context: list[dict[str, Any]] = []
        if not proven:
            for prev in events[:i]:
                if (prev.get("kind") == "content_in" and prev.get("source") not in UNTRUSTED_SKIP
                        and (prev.get("decision") not in (None, "ALLOW") or prev.get("findings"))):
                    context.append({**_source_node(prev), "link": "read earlier in this session (context, not a proven link)",
                                    "proven": False})
            context = context[-3:]
        decided = ev.get("decision")
        if decided == "ALLOW" and not proven:
            continue   # an ordinary call with no link to untrusted content: not part of an attack path
        contract = act.get("contract")
        rule = ev.get("policy_rule") or ""
        ruled = bool(contract and contract.get("effect") in ("prohibit", "out-of-scope") and decided != "ALLOW")
        layer = ("intent contract" if rule.startswith("contract-")
                 else "action guard and intent contract" if ruled else "action guard")
        chains.append({
            "kind": "action",
            "sources": proven or context,
            "action": {"event_id": ev["id"], "seq": ev["seq"], "tool": act.get("tool"), "args": act.get("args_redacted"),
                       "final_args": act.get("final_args_redacted"), "pinned": act.get("pinned_overrides")},
            "control": {"decision": decided, "rule": ev.get("policy_rule"), "reason": ev.get("reason"),
                        "labels": ev.get("labels") or [],
                        "layer": layer, "contract": contract},
            "outcome": _outcome(ev, approvals.get(ev["id"])),
        })
    for ev in events:
        if (ev.get("kind") == "content_in" and ev.get("source") not in UNTRUSTED_SKIP and ev["id"] not in linked
                and ev.get("decision") in ("NEUTRALISE", "BLOCK", "ESCALATE")):
            chains.append({
                "kind": "content",
                "sources": [{**_source_node(ev), "link": "inspected when it was read", "proven": True}],
                "action": None,
                "control": {"decision": ev.get("decision"), "rule": ev.get("policy_rule"), "reason": ev.get("reason"),
                            "labels": ev.get("labels") or [], "layer": "content inspection", "contract": None},
                "outcome": {"state": "quarantined" if ev.get("decision") == "NEUTRALISE" else "held",
                            "enforced": ev.get("enforced"), "receipt": None,
                            "evidence": ["the flagged part never reached the model" if ev.get("decision") == "NEUTRALISE"
                                         else "the content was replaced by a notice before the model saw it"]},
            })
    chains.sort(key=lambda c: (c["action"] or c["sources"][0])["seq"])
    nodes = {}
    edges = []
    for c in chains:
        for s in c["sources"]:
            nodes[s["event_id"]] = {"id": s["event_id"], "type": "content", "source": s["source"], "decision": s["decision"]}
        if c["action"]:
            a = c["action"]
            nodes[a["event_id"]] = {"id": a["event_id"], "type": "action", "tool": a["tool"], "decision": c["control"]["decision"]}
            edges += [{"from": s["event_id"], "to": a["event_id"], "proven": s["proven"], "link": s["link"]} for s in c["sources"]]
    return {"chains": chains, "nodes": list(nodes.values()), "edges": edges, "contracts": contracts or [],
            "summary": {"actions_stopped": sum(1 for c in chains if c["action"] and c["outcome"]["state"] == "not-executed"),
                        "content_stopped": sum(1 for c in chains if not c["action"]),
                        "executed_with_untrusted_link": sum(1 for c in chains if c["action"] and c["outcome"]["state"] == "executed"),
                        "contradictions": sum(1 for c in chains if c["outcome"]["state"] == "contradiction")}}
