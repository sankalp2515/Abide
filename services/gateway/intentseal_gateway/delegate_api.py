"""Abide API. Two groups of routes:

/v1/delegations/...     for any agent framework: start a delegation from the employee's request, confirm it, amend it
                        with the employee's next words, inspect content, seal handoffs, check tool calls, report what
                        ran, answer confirmations, and get the work receipt.
/v1/delegate-demo/...   the Console's live Ravi scenario (sandboxed workspace, two agents), built on the same calls.

Scopes: reading needs any key; acting needs a project key (an employee's app or an agent runtime).
"""

from __future__ import annotations

import asyncio

import base64
import json
import time
from collections import OrderedDict
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from intentseal_core.delegate import load_delegate_policy
from intentseal_core.delegate.compiler import ModelDrafter
from intentseal_core.delegate.handoff import SealError
from intentseal_core.delegate.bigquery import BigQuery
from intentseal_core.delegate.explain import ModelExplainer
from intentseal_core.delegate.receipts import ModelWriter
from intentseal_core.delegate.service import DelegateError, DelegateForbidden, DelegateService

from .security import Principal, human_only, issue_agent_key, require

log = logging.getLogger("intentseal.delegate")
router = APIRouter(tags=["delegate"])


class StartRequest(BaseModel):
    request: str = Field(min_length=1, max_length=4000)
    owner: str = Field("employee", max_length=120)
    workspace: str = Field("workspace", pattern=r"^[a-z0-9][a-z0-9-]{1,39}$")   # a built-in business or your own


class ConfirmRequest(BaseModel):
    hash: str = Field(min_length=8, max_length=64)     # the card's fingerprint: you confirm what you saw
    accept: list[str] = Field(default_factory=list)     # ticked clauses that needed the employee's OK


class WordsRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class ReadRequest(BaseModel):
    agent: str
    source: str = "document"
    content: str | None = None
    content_b64: str | None = None
    filename: str | None = None
    resource: str | None = None          # the directory file or record this content is, if any


class HandoffRequest(BaseModel):
    from_agent: str
    to_agent: str
    content: str
    sources: list[str] = Field(default_factory=list)


class OpenRequest(BaseModel):
    envelope: dict[str, Any]


class CheckRequest(BaseModel):
    agent: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class ExecutedRequest(BaseModel):
    receipt: str
    executed: bool
    detail: str = ""


class DecideRequest(BaseModel):
    approve: bool


class DemoStart(BaseModel):
    request: str = Field("", max_length=2000)
    attack: str = Field("hidden-pdf", pattern="^(hidden-pdf|email-body|none)$")
    firewall: bool = True
    domain: str = Field("office", pattern="^(office|desk)$")
    agents: str = Field("auto", pattern="^(auto|gemini|scripted)$")   # auto: live Gemini agents when reachable
    compromised: bool = False      # live agents act on instructions in what they read (a hijacked agent)
    attack_text: str = Field("", max_length=2000)   # your own attack, planted where `attack` says (pdf or email)


WORKSPACES = ("workspace", "desk")      # policies/delegate/<name>.yaml: one service per business, one store


def _services(request: Request) -> dict[str, DelegateService]:
    app = request.app
    svcs = getattr(app.state, "delegate_services", None)
    if svcs is None:
        engine = app.state.engine
        router_ = getattr(engine, "router", None)
        roles = getattr(getattr(router_, "settings", None), "roles", {}) or {}
        drafter = ModelDrafter(router_) if "contract" in roles else None
        writer = ModelWriter(router_) if "receipt" in roles else None
        explainer = ModelExplainer(router_) if "explain" in roles else None
        analytics = app.state.delegate_analytics = BigQuery.from_env()   # INTENTSEAL_BIGQUERY_DATASET
        from intentseal_core.delegate.model_armor import ModelArmor
        armor = ModelArmor.from_env()           # INTENTSEAL_MODEL_ARMOR_TEMPLATE: Google Model Armor as a second screen
        app.state.delegate_parts = (engine, drafter, writer, armor, explainer, analytics)
        svcs = app.state.delegate_services = {
            name: DelegateService(load_delegate_policy(name), engine=engine, drafter=drafter, writer=writer,
                                  model_armor=armor, explainer=explainer, analytics=analytics)
            for name in WORKSPACES}
        log.info("delegate: drafter=%s writer=%s model_armor=%s", drafter.name if drafter else "rules",
                 writer.name if writer else "template", armor.template if armor else "off")
    return svcs


# ---- businesses registered over the API ---------------------------------------------------------------------------
# A company describes its tools, contacts, files and rules once; its employees' agents then work under Delegate like
# the built-in sandboxes. A registered business is private to the key that registered it (and admins).

WORKSPACE_SCHEMA = ["""CREATE TABLE IF NOT EXISTS delegate_workspaces (id TEXT PRIMARY KEY, owner TEXT, policy TEXT,
                       description TEXT, created REAL)"""]
MAX_WORKSPACES_PER_KEY = 20
CUSTOM_CACHE = 64


class WorkspaceRequest(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,39}$")
    description: str = Field("", max_length=300)
    policy: dict[str, Any]


def _store(request: Request) -> Any:
    st = next(iter(_services(request).values())).store
    if not getattr(request.app.state, "delegate_ws_schema", False):
        st.ensure_schema(WORKSPACE_SCHEMA)
        request.app.state.delegate_ws_schema = True
    return st


def _reserved(request: Request) -> set[str]:
    return {*WORKSPACES, *(s.policy.workspace for s in _services(request).values())}


def _custom_row(request: Request, wid: str) -> dict[str, Any] | None:
    rows = _store(request).query("SELECT * FROM delegate_workspaces WHERE id = ?", (wid,))
    return rows[0] if rows else None


def _custom_service(request: Request, wid: str) -> DelegateService | None:
    from intentseal_core.delegate.model import custom_delegate_policy
    cache = getattr(request.app.state, "delegate_custom", None)
    if cache is None:
        cache = request.app.state.delegate_custom = OrderedDict()
    if wid in cache:
        cache.move_to_end(wid)
        return cache[wid]
    row = _custom_row(request, wid)
    if row is None:
        return None
    engine, drafter, writer, armor, explainer, analytics = request.app.state.delegate_parts
    svc = DelegateService(custom_delegate_policy(json.loads(row["policy"]), wid), engine=engine, drafter=drafter,
                          writer=writer, model_armor=armor, explainer=explainer, analytics=analytics)
    cache[wid] = svc
    while len(cache) > CUSTOM_CACHE:
        cache.popitem(last=False)
    return svc


def service(request: Request, wf: str | None = None, workspace: str | None = None) -> DelegateService:
    """The service for a business (`workspace`: a built-in policy file name or a registered business id), or for the
    business a workflow belongs to."""
    svcs = _services(request)
    if wf is not None:
        any_svc = next(iter(svcs.values()))
        rows = any_svc.store.query("SELECT workspace FROM delegate_workflows WHERE id = ?", (wf,))
        if rows:
            for svc in svcs.values():
                if svc.policy.workspace == rows[0]["workspace"]:
                    return svc
            custom = _custom_service(request, rows[0]["workspace"])
            if custom is not None:
                return custom
    if workspace is None or workspace in svcs:
        return svcs.get(workspace or "workspace") or svcs["workspace"]
    custom = _custom_service(request, workspace)
    if custom is None:
        raise HTTPException(404, "unknown workspace")
    return custom


def _may_use(request: Request, workspace: str, p: Principal) -> None:
    """Built-in sandboxes are open to every project key; a registered business only to its owner and admins."""
    if workspace in WORKSPACES or p.scope == "admin":
        return
    row = _custom_row(request, workspace)
    if row is None or row["owner"] != p.name:
        raise HTTPException(404, "unknown workspace")


@router.post("/v1/delegate-workspaces", status_code=201)
async def register_workspace(body: WorkspaceRequest, request: Request,
                             p: Principal = require("project")) -> dict[str, Any]:
    """Register (or replace) your business: tools and their fields, contacts, files, records and company rules."""
    human_only(p)                      # an agent could otherwise add its own contacts and widen what it may do
    from intentseal_core.delegate.model import custom_delegate_policy
    if body.id in _reserved(request):
        raise HTTPException(409, "that id belongs to a built-in sandbox")
    try:
        policy = custom_delegate_policy(body.policy, body.id)
    except (ValueError, TypeError) as e:          # pydantic's ValidationError is a ValueError
        raise HTTPException(422, f"invalid policy: {str(e)[:400]}") from None
    st, existing = _store(request), _custom_row(request, body.id)
    if existing is not None and existing["owner"] != p.name and p.scope != "admin":
        raise HTTPException(409, "that id is taken")
    if existing is None and len(st.query("SELECT id FROM delegate_workspaces WHERE owner = ?", (p.name,))) \
            >= MAX_WORKSPACES_PER_KEY:
        raise HTTPException(429, f"at most {MAX_WORKSPACES_PER_KEY} businesses per key")
    stored = json.dumps(body.policy, ensure_ascii=False)
    if existing is None:
        st.execute("INSERT INTO delegate_workspaces VALUES (?,?,?,?,?)",
                   (body.id, p.name, stored, body.description, time.time()))
    else:
        st.execute("UPDATE delegate_workspaces SET policy = ?, description = ? WHERE id = ?",
                   (stored, body.description, body.id))
    getattr(request.app.state, "delegate_custom", {}).pop(body.id, None)
    return {"id": body.id, "tools": {n: t.action for n, t in policy.tools.items()},
            "contacts": len(policy.contacts), "resources": len(policy.resources), "records": len(policy.records)}


@router.get("/v1/insights")
async def insights(request: Request, days: int = 30, p: Principal = require("public")) -> dict[str, Any]:
    """BigQuery over every receipt: decisions, what agents were stopped from doing, where the instructions came
    from, per day. A project key sees its own work, an admin everything, the public Console the built-in sandboxes."""
    svcs = _services(request)
    bq = getattr(request.app.state, "delegate_analytics", None)
    if bq is None:
        return {"configured": False, "engine": "BigQuery", "note": "set INTENTSEAL_BIGQUERY_DATASET to enable"}
    scope = p.scope if p.scope in ("admin", "project") else "public"
    try:
        return await asyncio.to_thread(bq.insights, p.name, scope, tuple(s.policy.workspace for s in svcs.values()),
                                       max(1, min(days, 365)))
    except Exception as e:
        log.warning("insights failed: %s", str(e)[:200])
        raise HTTPException(503, "BigQuery is unavailable right now") from None


@router.get("/v1/delegate-workspaces")
async def list_workspaces(request: Request, p: Principal = require("project")) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"id": name, "builtin": True, "business": svc.policy.workspace}
                                 for name, svc in _services(request).items()]
    admin = p.scope == "admin"
    rows = _store(request).query("SELECT id, description FROM delegate_workspaces"
                                 + ("" if admin else " WHERE owner = ?"), () if admin else (p.name,))
    return out + [{"id": r["id"], "builtin": False, "description": r["description"]} for r in rows]


class AgentKeyRequest(BaseModel):
    label: str = Field("my agent", max_length=60)


@router.post("/v1/agent-keys", status_code=201)
async def new_agent_key(body: AgentKeyRequest, request: Request, p: Principal = require("project")) -> dict[str, Any]:
    """A key for your agent: it acts inside your work orders and can never confirm, widen or approve them."""
    return issue_agent_key(request.app.state.keys, p, body.label)


@router.get("/v1/agent-keys")
async def list_agent_keys(request: Request, p: Principal = require("project")) -> list[dict[str, Any]]:
    human_only(p)
    rows = request.app.state.keys.agent_store.query(
        "SELECT id, label, created, expires, revoked FROM agent_keys WHERE owner = ? ORDER BY created DESC", (p.name,))
    return [dict(r, active=not r["revoked"] and r["expires"] > time.time()) for r in rows]


@router.delete("/v1/agent-keys/{kid}")
async def revoke_agent_key(kid: str, request: Request, p: Principal = require("project")) -> dict[str, Any]:
    human_only(p)
    ring = request.app.state.keys
    rows = ring.agent_store.query("SELECT id FROM agent_keys WHERE id = ? AND owner = ?", (kid, p.name))
    if not rows:
        raise HTTPException(404, "no such agent key")
    ring.agent_store.execute("UPDATE agent_keys SET revoked = ? WHERE id = ?", (time.time(), kid))
    ring._trial_cache.clear()                       # the key stops working now, not in 30 seconds
    return {"id": kid, "revoked": True}


@router.get("/v1/my-agents")
async def my_agents(request: Request, p: Principal = require("project")) -> list[dict[str, Any]]:
    """The agents that acted under your work orders, from the receipts: how often each was checked, what was allowed,
    stopped or asked, and when it last acted. Sandbox agents (Gemini on ADK) and your own agents alike."""
    svcs = _services(request)
    store = next(iter(svcs.values())).store
    builtin = [s.policy.workspace for s in svcs.values()]
    marks = ",".join("?" for _ in builtin)
    who = "" if p.scope == "admin" else "AND pr.principal = ?"
    rows = store.query(
        "SELECT r.agent AS agent, COUNT(*) AS checks, COUNT(DISTINCT r.workflow_id) AS tasks, MAX(r.created) AS last, "
        "SUM(CASE WHEN r.decision = 'allow' THEN 1 ELSE 0 END) AS allowed, "
        "SUM(CASE WHEN r.decision = 'block' THEN 1 ELSE 0 END) AS stopped, "
        "SUM(CASE WHEN r.decision = 'ask' THEN 1 ELSE 0 END) AS asked, "
        f"MAX(CASE WHEN w.workspace IN ({marks}) THEN 1 ELSE 0 END) AS sandbox, MAX(w.workspace) AS business "
        "FROM delegate_receipts r JOIN delegate_principals pr ON pr.workflow_id = r.workflow_id "
        "JOIN delegate_workflows w ON w.id = r.workflow_id "
        f"WHERE r.kind = 'check' AND r.agent != '' {who} GROUP BY r.agent ORDER BY last DESC LIMIT 50",
        (*builtin, *([] if p.scope == "admin" else [p.name])))
    return [{**r, "checks": int(r["checks"]), "tasks": int(r["tasks"]), "allowed": int(r["allowed"] or 0),
             "stopped": int(r["stopped"] or 0), "asked": int(r["asked"] or 0), "sandbox": bool(r["sandbox"]),
             "kind": "Gemini on Google ADK (sandbox)" if r["sandbox"] else "Your agent, through the Abide API"}
            for r in rows]


@router.get("/v1/delegate-workspaces/{wid}")
async def get_workspace(wid: str, request: Request, p: Principal = require("project")) -> dict[str, Any]:
    _may_use(request, wid, p)
    svc = service(request, workspace=wid)
    return {"id": wid, "builtin": wid in WORKSPACES, "policy": svc.policy.model_dump()}


def actor(request: Request, wf: str, p: Principal) -> str:
    """Who is speaking: the key's own name. An admin key may act for the employee who delegated the task. An agent
    key speaks as the agent, never as its owner, so the service refuses it anything only the employee may do."""
    if p.agent:
        return f"agent:{p.name}"
    return service(request, wf).owner_of(wf) if p.scope == "admin" else p.name


def owner_only(request: Request, wf: str, p: Principal) -> DelegateService:
    """G1: a delegation (its words, receipts, pasted text) is visible to the person who started it and to admins.
    Anyone else gets 404, so they cannot learn that it exists."""
    svc = service(request, wf)
    try:
        owner = svc.owner_of(wf)
    except DelegateError:
        raise HTTPException(404, "unknown workflow") from None
    if p.scope != "admin" and p.name != owner:
        raise HTTPException(404, "unknown workflow")
    return svc


def demo_enabled() -> None:
    """G14: the sandbox demo can be switched off where it is not wanted (INTENTSEAL_DELEGATE_DEMO=0)."""
    import os
    if os.environ.get("INTENTSEAL_DELEGATE_DEMO", "1") == "0":
        raise HTTPException(404, "the demo is not enabled on this server")


def _err(e: Exception) -> HTTPException:
    if isinstance(e, DelegateForbidden):
        return HTTPException(403, str(e))
    return HTTPException(404 if "unknown" in str(e) else 409, str(e))


# ---- general API ------------------------------------------------------------------------------------------------------

@router.post("/v1/delegations")
async def start(body: StartRequest, request: Request, p: Principal = require("project")) -> dict[str, Any]:
    _may_use(request, body.workspace, p)
    svc = service(request, workspace=body.workspace)
    c = await svc.start(body.owner, body.request, principal=p.name)
    return svc.state(c.workflow_id)


DEMO_EMPLOYEES = {"Ravi Kumar", "Arjun Mehta"}     # delegate_demo.live.EMPLOYEE (kept here to avoid importing ADK)


@router.get("/v1/delegations")
async def list_delegations(request: Request, p: Principal = require("project")) -> list[dict[str, Any]]:
    """Your tasks, newest first, with what the "Your agents" page shows: the request, its status and counts."""
    any_svc = next(iter(_services(request).values()))        # one store holds every business's workflows
    rows = [w for w in any_svc.workflows(500) if p.scope == "admin" or any_svc.owner_of(w["id"]) == p.name][:50]
    demos, out = _demos(request), []
    for w in rows:
        try:
            svc = service(request, w["id"])
            c = svc.contract(w["id"])
        except DelegateError:
            continue
        checks = svc.store.query("SELECT decision, COUNT(*) AS n FROM delegate_receipts WHERE workflow_id = ? AND "
                                 "kind = 'check' GROUP BY decision", (w["id"],))
        waiting = svc.store.query("SELECT COUNT(*) AS n FROM delegate_approvals WHERE workflow_id = ? AND status = "
                                  "'pending'", (w["id"],))
        counts = {r["decision"]: int(r["n"]) for r in checks}
        sandbox = w["id"] in demos or w["owner"] in DEMO_EMPLOYEES     # the sandbox demo files tasks as Ravi or Arjun
        out.append({**w, "request": c.request[:300], "status": c.status, "business": svc.policy.workspace,
                    "source": "demo" if sandbox else "api", "live": w["id"] in demos, "allowed": counts.get("allow", 0),
                    "stopped": counts.get("block", 0), "asked": counts.get("ask", 0),
                    "waiting": int(waiting[0]["n"]) if waiting else 0})
    return out


@router.get("/v1/delegations/{wf}")
async def get_delegation(wf: str, request: Request, p: Principal = require("project")) -> dict[str, Any]:
    try:
        return owner_only(request, wf, p).state(wf)
    except DelegateError as e:
        raise _err(e) from None


@router.post("/v1/delegations/{wf}/confirm")
async def confirm(wf: str, body: ConfirmRequest, request: Request, p: Principal = require("project")) -> dict[str, Any]:
    try:
        return service(request, wf).confirm(wf, body.hash, actor=actor(request, wf, p), accept=body.accept).as_dict(service(request, wf).policy)
    except DelegateError as e:
        raise _err(e) from None


@router.post("/v1/delegations/{wf}/amend")
async def amend(wf: str, body: WordsRequest, request: Request, p: Principal = require("project")) -> dict[str, Any]:
    try:
        return (await service(request, wf).amend(wf, body.text, actor=actor(request, wf, p))).as_dict(
            service(request, wf).policy)
    except DelegateError as e:
        raise _err(e) from None


@router.post("/v1/delegations/{wf}/read")
async def read(wf: str, body: ReadRequest, request: Request, p: Principal = require("project")) -> dict[str, Any]:
    if body.content is None and body.content_b64 is None:
        raise HTTPException(422, "provide content or content_b64")
    try:
        data: Any = base64.b64decode(body.content_b64, validate=True) if body.content_b64 else body.content
    except ValueError:
        raise HTTPException(422, "content_b64 is not valid base64") from None
    try:
        return await service(request, wf).read(wf, body.agent, data, body.source, body.filename,
                                           resource=body.resource)
    except DelegateError as e:
        raise _err(e) from None


@router.post("/v1/delegations/{wf}/handoff")
async def seal(wf: str, body: HandoffRequest, request: Request, p: Principal = require("project")) -> dict[str, Any]:
    try:
        return service(request, wf).seal(wf, body.from_agent, body.to_agent, body.content, body.sources)
    except DelegateError as e:
        raise _err(e) from None


@router.post("/v1/delegations/{wf}/open")
async def open_(wf: str, body: OpenRequest, request: Request, p: Principal = require("project")) -> dict[str, Any]:
    from intentseal_core.delegate.handoff import as_agent_input
    try:
        env = service(request, wf).open(body.envelope, wf)
    except SealError as e:
        raise HTTPException(409, str(e)) from None
    return {"ok": True, "agent_input": as_agent_input(env), "untrusted": env["untrusted"]}


@router.post("/v1/delegations/{wf}/check")
async def check(wf: str, body: CheckRequest, request: Request, p: Principal = require("project")) -> dict[str, Any]:
    try:
        return await service(request, wf).check(wf, body.agent, body.tool, body.args)
    except DelegateError as e:
        raise _err(e) from None


@router.post("/v1/delegations/{wf}/executed")
async def executed(wf: str, body: ExecutedRequest, request: Request, p: Principal = require("project")) -> dict[str, Any]:
    try:
        return {"receipt": service(request, wf).executed(wf, body.receipt, body.executed, body.detail)}
    except DelegateError as e:
        raise _err(e) from None


@router.post("/v1/delegations/{wf}/approvals/{approval_id}")
async def decide(wf: str, approval_id: str, body: DecideRequest, request: Request,
                 p: Principal = require("project")) -> dict[str, Any]:
    try:
        out = service(request, wf).resolve(wf, approval_id, body.approve, actor(request, wf, p))
    except DelegateError as e:
        raise _err(e) from None
    demo = _demos(request).get(wf)
    if demo is not None:
        await demo.decided(approval_id, body.approve)
    return out


@router.get("/v1/delegations/{wf}/report")
async def report(wf: str, request: Request, p: Principal = require("project")) -> dict[str, Any]:
    try:
        return await owner_only(request, wf, p).report(wf)
    except DelegateError as e:
        raise _err(e) from None


# ---- live demo --------------------------------------------------------------------------------------------------------

def _demos(request: Request) -> dict[str, Any]:
    if not hasattr(request.app.state, "delegate_demos"):
        request.app.state.delegate_demos = {}
    return request.app.state.delegate_demos


async def _demo_state(request: Request, wf: str) -> dict[str, Any]:
    demo = _demos(request).get(wf)
    if demo is None:
        raise HTTPException(404, "unknown demo workflow")
    svc = service(request, wf)
    out = svc.state(wf) | {"demo": demo.view()}
    if demo.phase == "worked":                  # written once per new receipt, not on every poll (a model call)
        n = len(out["receipts"])
        cached = getattr(demo, "report_cache", None)
        if cached is None or cached[0] != n:
            demo.report_cache = (n, await svc.report(wf))
        out["report"] = demo.report_cache[1]
    return out


@router.post("/v1/delegate-demo")
async def demo_start(body: DemoStart, request: Request, p: Principal = require("project")) -> dict[str, Any]:
    demo_enabled()
    human_only(p)
    from .delegate_demo.live import EMPLOYEE, POLICY_FILE, LiveDemo
    from .delegate_demo.live import REQUESTS
    svc = service(request, workspace=POLICY_FILE[body.domain])
    c = await svc.start(EMPLOYEE[body.domain], body.request.strip() or REQUESTS[body.domain], principal=p.name)
    demos = _demos(request)
    while len(demos) >= 50:                     # demo sandboxes are kept in memory: bounded
        demos.pop(next(iter(demos)))
    demos[c.workflow_id] = LiveDemo(svc, c.workflow_id, body.attack, body.firewall, body.domain, body.agents,
                                    body.compromised, body.attack_text.strip() or None)
    return await _demo_state(request, c.workflow_id)


@router.get("/v1/delegate-demo/{wf}")
async def demo_get(wf: str, request: Request, p: Principal = require("project")) -> dict[str, Any]:
    demo_enabled()
    owner_only(request, wf, p)
    return await _demo_state(request, wf)


@router.post("/v1/delegate-demo/{wf}/confirm")
async def demo_confirm(wf: str, body: ConfirmRequest, request: Request,
                       p: Principal = require("project")) -> dict[str, Any]:
    demo = _demos(request).get(wf)
    if demo is None:
        raise HTTPException(404, "unknown demo workflow")
    try:
        service(request, wf).confirm(wf, body.hash, actor=actor(request, wf, p), accept=body.accept)
    except DelegateError as e:
        raise _err(e) from None
    if demo.phase == "contract":
        await demo.work()
    return await _demo_state(request, wf)


@router.post("/v1/delegate-demo/{wf}/say")
async def demo_say(wf: str, body: WordsRequest, request: Request, p: Principal = require("project")) -> dict[str, Any]:
    demo = _demos(request).get(wf)
    if demo is None:
        raise HTTPException(404, "unknown demo workflow")
    try:
        await demo.say(body.text, actor(request, wf, p))
    except DelegateError as e:
        raise _err(e) from None
    return await _demo_state(request, wf)


# ---- evaluation summary (the landing page reads it; the numbers are never typed into the UI) ----------------------------

@router.get("/v1/delegate-eval")
async def delegate_eval(request: Request) -> dict[str, Any]:
    """The latest local evaluation of Abide (eval/reports/delegate-local.json), summarised. Public."""
    import json as _json
    path = request.app.state.reports / "delegate-local.json"
    if not path.is_file():
        raise HTTPException(404, "no Delegate evaluation report")
    rep = _json.loads(path.read_text(encoding="utf-8"))
    arms = {name: {k: v for k, v in arm["summary"].items() if k != "by_attack_type"}
            for name, arm in rep.get("arms", {}).items()}
    return {"created": rep.get("created"), "method": rep.get("method"), "arms": arms}


# ---- the client, as one file (for notebooks and quick starts: no package to install) -------------------------------

@router.get("/v1/sdk/abide.py", include_in_schema=False)
@router.get("/v1/sdk/intentseal_delegate.py", include_in_schema=False)
async def sdk_client() -> Any:
    """The Delegate client and Google ADK guard (`intentseal.delegate`), served as a single importable file."""
    from importlib.resources import files

    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(files("intentseal").joinpath("delegate.py").read_text(encoding="utf-8"),
                             media_type="text/x-python")
