"""Multi-tenant organisations (API-12): which agents a key may see and act for.

An agent belongs to the organisation named in its policy (`org:`, default "default"); a key belongs to one
organisation (or "*" for all) and may be narrowed to a list of agents. Every endpoint that names an agent,
reads decisions, sessions, approvals, memory or metrics, or exports events is limited to the key's agents.
Agents outside a key's view answer 404, exactly like agents that do not exist.

A deployment where every key and policy is in the "default" organisation behaves as a single tenant:
deployment-wide usage (LLM calls by model and provider) is visible to all keys. With more than one
organisation, only keys for every organisation see it; other keys see the spend of their own agents.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import HTTPException, Request

from intentseal_core.policy import list_policies

from .security import ALL_ORGS, Principal

_TTL_S = 30.0


def _orgs(request: Request) -> dict[str, str]:
    """agent -> org for every agent with a policy or with recorded data (refreshed every 30 s)."""
    state = request.app.state
    cached = getattr(state, "agent_orgs", None)
    if cached and time.monotonic() - cached[0] < _TTL_S:
        return cached[1]
    engine = state.engine
    agents = set(list_policies())
    agents |= {r["agent_id"] for r in engine.store.query("SELECT DISTINCT agent_id FROM sessions") if r["agent_id"]}
    out: dict[str, str] = {}
    for agent in agents:
        try:
            out[agent] = engine.policy_loader(agent).org
        except Exception:   # invalid id recorded by an old version, unreadable policy: nobody's agent but "*"
            out[agent] = ""
    state.agent_orgs = (time.monotonic(), out)
    return out


def refresh(request: Request) -> None:
    request.app.state.agent_orgs = None


def single_tenant(request: Request) -> bool:
    orgs = set(_orgs(request).values()) | request.app.state.keys.orgs()
    return orgs <= {"default", ALL_ORGS, ""}


def visible_agents(request: Request, p: Principal) -> set[str] | None:
    """The agents this key can see; None means no restriction (every agent: an all-organisations key, or any key
    of a single-tenant deployment that is not narrowed to an agent list)."""
    if p.agents is None and (p.all_orgs or single_tenant(request)):
        return None
    orgs = _orgs(request)
    agents = set(orgs) if p.all_orgs else {a for a, org in orgs.items() if org == p.org}
    return agents & set(p.agents) if p.agents is not None else agents


def can_see(request: Request, p: Principal, agent_id: str) -> bool:
    if p.all_orgs and p.agents is None:
        return True
    if p.agents is not None and agent_id not in p.agents:
        return False
    if p.all_orgs:
        return True
    try:
        return request.app.state.engine.policy_loader(agent_id).org == p.org
    except ValueError:
        return False


def require_agent(request: Request, p: Principal, agent_id: str) -> None:
    """The agent must exist (have a policy, F4) and be within the key's organisation and agent list."""
    if not request.app.state.engine.has_policy(agent_id) or not can_see(request, p, agent_id):
        raise HTTPException(404, f"no policy for agent '{agent_id[:64]}' visible to this key; "
                                 "add policies/<agent>.yaml (or PUT /v1/policies/<agent>)")


def agent_filter(column: str, agents: set[str] | None) -> tuple[str, list[Any]]:
    """SQL condition limiting `column` to the visible agents ("" when unrestricted)."""
    if agents is None:
        return "", []
    if not agents:
        return f" AND {column} IS NULL AND 1 = 0", []
    return f" AND {column} IN ({', '.join('?' for _ in agents)})", sorted(agents)
