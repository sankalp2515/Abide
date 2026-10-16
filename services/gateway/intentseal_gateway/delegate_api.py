"""IntentSeal Delegate API. Two groups of routes:

/v1/delegations/...     for any agent framework: start a delegation from the employee's request, confirm it, amend it
                        with the employee's next words, inspect content, seal handoffs, check tool calls, report what
                        ran, answer confirmations, and get the work receipt.
/v1/delegate-demo/...   the Console's live Ravi scenario (sandboxed workspace, two agents), built on the same calls.

Scopes: reading needs any key; acting needs a project key (an employee's app or an agent runtime).
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from intentseal_core.delegate import load_delegate_policy
from intentseal_core.delegate.compiler import ModelDrafter
from intentseal_core.delegate.handoff import SealError
from intentseal_core.delegate.receipts import ModelWriter
from intentseal_core.delegate.service import DelegateError, DelegateForbidden, DelegateService

from .security import Principal, require

log = logging.getLogger("intentseal.delegate")
router = APIRouter(tags=["delegate"])


class StartRequest(BaseModel):
    request: str = Field(min_length=1, max_length=4000)
    owner: str = Field("employee", max_length=120)
    workspace: str = Field("workspace", pattern="^(workspace|desk)$")


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
        svcs = app.state.delegate_services = {
            name: DelegateService(load_delegate_policy(name), engine=engine, drafter=drafter, writer=writer)
            for name in WORKSPACES}
        log.info("delegate: drafter=%s writer=%s", drafter.name if drafter else "rules",
                 writer.name if writer else "template")
    return svcs


def service(request: Request, wf: str | None = None, workspace: str | None = None) -> DelegateService:
    """The service for a business (`workspace`: a policy file name), or for the business a workflow belongs to."""
    svcs = _services(request)
    if wf is not None:
        any_svc = next(iter(svcs.values()))
        rows = any_svc.store.query("SELECT workspace FROM delegate_workflows WHERE id = ?", (wf,))
        if rows:
            for svc in svcs.values():
                if svc.policy.workspace == rows[0]["workspace"]:
                    return svc
    return svcs.get(workspace or "workspace") or svcs["workspace"]


def actor(request: Request, wf: str, p: Principal) -> str:
    """Who is speaking: the key's own name. An admin key may act for the employee who delegated the task."""
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
    svc = service(request, workspace=body.workspace)
    c = await svc.start(body.owner, body.request, principal=p.name)
    return svc.state(c.workflow_id)


@router.get("/v1/delegations")
async def list_delegations(request: Request, p: Principal = require("project")) -> list[dict[str, Any]]:
    rows = [w for svc in _services(request).values() for w in svc.workflows(500)
            if p.scope == "admin" or svc.owner_of(w["id"]) == p.name]
    return sorted(rows, key=lambda w: -w["created"])[:50]


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
    if demo.phase != "contract":
        out["report"] = await svc.report(wf)
    return out


@router.post("/v1/delegate-demo")
async def demo_start(body: DemoStart, request: Request, p: Principal = require("project")) -> dict[str, Any]:
    demo_enabled()
    from .delegate_demo.live import EMPLOYEE, POLICY_FILE, LiveDemo
    from .delegate_demo.live import REQUESTS
    svc = service(request, workspace=POLICY_FILE[body.domain])
    c = await svc.start(EMPLOYEE[body.domain], body.request.strip() or REQUESTS[body.domain], principal=p.name)
    demos = _demos(request)
    while len(demos) >= 50:                     # demo sandboxes are kept in memory: bounded
        demos.pop(next(iter(demos)))
    demos[c.workflow_id] = LiveDemo(svc, c.workflow_id, body.attack, body.firewall, body.domain)
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
    """The latest local evaluation of IntentSeal Delegate (eval/reports/delegate-local.json), summarised. Public."""
    import json as _json
    path = request.app.state.reports / "delegate-local.json"
    if not path.is_file():
        raise HTTPException(404, "no Delegate evaluation report")
    rep = _json.loads(path.read_text(encoding="utf-8"))
    arms = {name: {k: v for k, v in arm["summary"].items() if k != "by_attack_type"}
            for name, arm in rep.get("arms", {}).items()}
    return {"created": rep.get("created"), "method": rep.get("method"), "arms": arms}
