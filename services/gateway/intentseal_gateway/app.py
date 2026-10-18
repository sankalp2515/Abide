"""Gateway API (API-1…API-11) and LLM proxy mount. Run: uvicorn intentseal_gateway.app:app --port 8000

All surfaces share one Core Engine instance (CORE-1). OpenAPI docs at /docs (API-10).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from intentseal_core import siem
from intentseal_core.config import repo_root
from intentseal_core.costs import overhead, usage_by_role
from intentseal_core.engine import Engine
from intentseal_core.llm.router import Router
from intentseal_core.policy import AgentPolicy, load_policy, save_policy

from . import provenance, tenancy
from .security import ALL_ORGS, BodySizeLimit, KeyRing, Principal, require
from .tenancy import agent_filter, require_agent, visible_agents


class InspectRequest(BaseModel):
    content: str | None = None
    content_b64: str | None = Field(None, description="binary files (PDF, DOCX, images) as base64")
    filename: str | None = None
    source: str = "user"
    agent_id: str
    session_id: str | None = None


class OutputRequest(BaseModel):
    text: str
    agent_id: str
    session_id: str | None = None


class ActionRequest(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    agent_id: str
    session_id: str | None = None


class ReceiptRequest(BaseModel):
    executed: bool
    detail: str = Field("", max_length=500)


class SessionRequest(BaseModel):
    agent_id: str
    user_ref: str | None = None
    task: str = ""
    trusted: dict[str, Any] = Field(default_factory=dict)


class FeedbackRequest(BaseModel):
    verdict: str = Field(..., pattern="^(attack|false_positive)$")
    event_id: str | None = None
    content: str | None = Field(None, description="when the event is no longer in recent memory")
    agent_id: str = "*"
    labels: list[str] = Field(default_factory=list)
    note: str = ""


class ApprovalRequest(BaseModel):
    approve: bool
    note: str = ""


class SuggestionRequest(BaseModel):
    apply: bool


access = logging.getLogger("intentseal.access")


def _percentile(values: list[float], q: float) -> float | None:
    values = sorted(v for v in values if v is not None)
    return round(values[min(len(values) - 1, int(q * len(values)))], 1) if values else None


def build_engine() -> Engine:
    router = Router()
    return Engine(router=router, store=router.store, tier0_timeout_s=float(os.environ.get("INTENTSEAL_EXTRACT_TIMEOUT_S") or "10"))


def create_app(engine: Engine | None = None, reports_dir: Path | None = None) -> FastAPI:
    if not logging.getLogger().handlers:   # the gateway is an application: INFO logs (access log), keys redacted
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = engine or build_engine()
        if engine is None:   # load classifier + OCR before the first request (no cold-start degraded decisions)
            import asyncio

            from intentseal.guard import _warm_up
            await asyncio.to_thread(_warm_up)
            from intentseal_core.llm.vertex_adapter import warm as _warm_vertex
            await asyncio.to_thread(_warm_vertex)   # Gemini on Vertex AI: the first access token, before any user waits
            from .delegate_demo.live import warm_agents
            await asyncio.to_thread(warm_agents)    # live Gemini agents: load Google ADK now, not on the first task
        app.state.keys = KeyRing()
        from .security import AGENT_SCHEMA
        app.state.engine.store.ensure_schema(AGENT_SCHEMA)     # agent keys (POST /v1/agent-keys)
        app.state.keys.agent_store = app.state.engine.store
        if os.environ.get("INTENTSEAL_TRIAL_KEYS", "0") == "1":     # self-serve 7-day keys (POST /v1/trial-keys)
            from .security import TRIAL_SCHEMA
            app.state.engine.store.ensure_schema(TRIAL_SCHEMA)
            app.state.keys.trial_store = app.state.engine.store
        # Ephemeral key for the demo runner's own calls to the proxy (S9); random per process, never exposed.
        import secrets as _secrets
        app.state.internal_key = _secrets.token_urlsafe(24)
        app.state.keys.add(app.state.internal_key, Principal("internal-demo", "project", ALL_ORGS))
        app.state.reports = reports_dir or repo_root() / "eval" / "reports"
        app.state.started = time.time()
        from .investigator import Investigator
        app.state.investigator = Investigator(app.state.engine)
        watcher = None
        if engine is None and os.environ.get("INTENTSEAL_INVESTIGATOR_AUTO", "1") != "0":
            import asyncio
            watcher = asyncio.create_task(app.state.investigator.watch())   # sessions awaiting approval get triaged
        yield
        if watcher is not None:
            watcher.cancel()
        router = getattr(app.state.engine, "router", None)
        if router is not None and hasattr(router, "aclose"):
            await router.aclose()

    app = FastAPI(title="Abide Gateway API", version="0.1.0", lifespan=lifespan,
                  description="Keep AI agents inside the user's intent. Inspect content, check actions, replay sessions. Rules decide; AI advises.")
    origins = [o.strip() for o in os.environ.get("INTENTSEAL_CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "PUT", "DELETE"],
                       allow_headers=["Authorization", "X-API-Key", "Content-Type", "X-Agent-Id", "X-Session-Id",
                                      "X-Request-Id"], expose_headers=["X-Request-Id"])
    app.add_middleware(BodySizeLimit)

    @app.middleware("http")
    async def request_ids(request: Request, call_next):
        # Observability (NFR §12): one id per request, echoed back and written to the structured access log.
        rid = request.headers.get("x-request-id", "")[:64] or uuid.uuid4().hex[:16]
        t0 = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        access.info(json.dumps({"request_id": rid, "method": request.method, "path": request.url.path,
                                "status": response.status_code, "ms": round((time.perf_counter() - t0) * 1000, 1),
                                "session_id": request.headers.get("x-session-id")}))
        return response

    @app.exception_handler(ValueError)
    async def bad_value(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)[:200] or "invalid request"}, status_code=422)

    @app.get("/healthz", tags=["ops"])
    @app.get("/v1/health", tags=["ops"])           # Cloud Run's front end answers /healthz itself
    async def healthz(request: Request) -> dict[str, Any]:
        return {"ok": True, "uptime_s": round(time.time() - request.app.state.started, 1)}

    @app.get("/readyz", tags=["ops"])
    async def readyz(request: Request) -> JSONResponse:
        """Readiness: database answers, policies load, Tier 1 classifier loaded. LLM providers are reported, not
        required: without them flagged content is held (fail closed), which is degraded but safe."""
        e = request.app.state.engine
        from intentseal_core.classifier import tier1_status
        from intentseal_core.policy import list_policies

        tier1 = {"loaded": True, "error": ""} if e.classifiers is not None else tier1_status()
        router = getattr(e, "router", None)
        settings = getattr(router, "settings", None)
        providers = sum(1 for p in settings.providers.values() if p.active) if settings else 0
        checks = {"database": e.store.ping(), "policies": bool(list_policies()) or not e._policy_files[1],
                  "tier1_classifier": bool(tier1["loaded"]), "llm_providers": providers}
        ready = checks["database"] and checks["policies"] and checks["tier1_classifier"]
        body = {"ready": ready, "checks": checks, "tier1_error": tier1["error"] or None,
                "judge": "available" if providers else "unavailable: flagged content is held"}
        return JSONResponse(body, status_code=200 if ready else 503)

    @app.post("/v1/trial-keys", tags=["ops"], status_code=201)
    async def trial_key(request: Request) -> dict[str, Any]:
        """Create a 7-day project key for yourself (when the deployment allows it). No sign-up, nothing to install."""
        from .security import issue_trial_key
        return issue_trial_key(request.app.state.keys, request.client.host if request.client else "")

    @app.get("/v1/whoami", tags=["ops"])
    async def whoami(request: Request, p: Principal = require("public")) -> dict[str, Any]:
        """Which key is this? Lets the Console confirm a reviewer key is admin-scoped before showing reviewer actions."""
        agents = visible_agents(request, p)
        return {"name": p.name, "scope": p.scope, "org": p.org, "agents": None if agents is None else sorted(agents),
                "agent_key": p.agent}

    # ---- API-1 inspect ------------------------------------------------------------------------
    @app.post("/v1/inspect", tags=["engine"])
    async def inspect(body: InspectRequest, request: Request, p: Principal = require("project")) -> dict[str, Any]:
        require_agent(request, p, body.agent_id)
        if body.content is None and body.content_b64 is None:
            raise HTTPException(422, "provide content or content_b64")
        try:
            data: bytes | str = base64.b64decode(body.content_b64, validate=True) if body.content_b64 else body.content  # type: ignore[assignment]
        except ValueError:
            raise HTTPException(422, "content_b64 is not valid base64") from None
        r = await request.app.state.engine.inspect(data, body.source, body.agent_id, session_id=body.session_id,
                                                   filename=body.filename)
        return r.model_dump(mode="json")

    # ---- API-2 action check -------------------------------------------------------------------
    @app.post("/v1/actions/check", tags=["engine"])
    async def check(body: ActionRequest, request: Request, p: Principal = require("project")) -> dict[str, Any]:
        require_agent(request, p, body.agent_id)
        r = await request.app.state.engine.check_action(body.tool, body.args, body.agent_id, session_id=body.session_id)
        return r.model_dump(mode="json")

    # ---- the model's answer: protected data never reaches the reader -----------------------------
    @app.post("/v1/outputs/check", tags=["engine"])
    async def check_output(body: OutputRequest, request: Request, p: Principal = require("project")) -> dict[str, Any]:
        require_agent(request, p, body.agent_id)
        r = await request.app.state.engine.check_output(body.text, body.agent_id, session_id=body.session_id)
        return r.model_dump(mode="json")

    # ---- API-3 sessions -----------------------------------------------------------------------
    @app.post("/v1/sessions", tags=["sessions"])
    async def new_session(body: SessionRequest, request: Request, p: Principal = require("project")) -> dict[str, Any]:
        require_agent(request, p, body.agent_id)
        s = request.app.state.engine.start_session(body.agent_id, user_ref=body.user_ref, task=body.task, trusted=body.trusted)
        out: dict[str, Any] = {"session_id": s.session_id}
        if s.contract is not None:   # D-064: what this session is authorised to do, from the task's own words
            out["contract"] = s.contract.as_dict()
        return out

    @app.get("/v1/sessions/{session_id}/events", tags=["sessions"])
    async def session_events(session_id: str, request: Request, p: Principal = require("public")) -> list[dict[str, Any]]:
        e = request.app.state.engine
        owner = e.store.query("SELECT agent_id FROM sessions WHERE id = ?", (session_id,))
        if owner and not tenancy.can_see(request, p, owner[0]["agent_id"]):
            raise HTTPException(404, "unknown session")
        return e.logger.session_events(session_id)

    # ---- API-4 decisions feed -----------------------------------------------------------------
    @app.get("/v1/decisions", tags=["evidence"])
    async def decisions(request: Request, decision: str | None = None, label: str | None = None,
                        agent: str | None = None, since: float | None = None, before: float | None = None,
                        limit: int = Query(100, ge=1, le=500), p: Principal = require("public")) -> list[dict[str, Any]]:
        return request.app.state.engine.logger.decisions(decision=decision, label=label, agent=agent, since=since,
                                                         limit=limit, before=before, agents=visible_agents(request, p))

    # ---- API-5 scorecard ----------------------------------------------------------------------
    @app.get("/v1/scorecard/{agent}", tags=["evidence"])
    async def scorecard(agent: str, request: Request, p: Principal = require("public")) -> dict[str, Any]:
        d: Path = request.app.state.reports
        reports = sorted(d.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True) if d.exists() else []
        if not reports:
            raise HTTPException(404, "no evaluation report yet")
        latest, summary = {}, None
        for f in reports:
            rep = json.loads(f.read_text(encoding="utf-8"))
            if rep.get("kind") == "summary":   # grid claim + headline metrics (intentseal_eval.summary)
                summary = summary or {"file": f.name, **rep}
                continue
            key = f"{rep.get('suite', 'broad')}:{rep.get('protection')}"
            latest.setdefault(key, {"file": f.name, **rep})
        return {"agent": agent, "summary": summary, "reports": latest}

    # ---- API-6 policy (read) ------------------------------------------------------------------
    @app.get("/v1/policies/{agent}", tags=["policy"])
    async def policy(agent: str, request: Request, p: Principal = require("public")) -> dict[str, Any]:
        require_agent(request, p, agent)
        pol = load_policy(agent).model_dump(mode="json")
        pol["sensitive_data"]["canaries"] = [c if c == "from_env" else "[REDACTED]" for c in pol["sensitive_data"]["canaries"]]
        return pol

    # ---- API-11 costs (P1) --------------------------------------------------------------------
    @app.get("/v1/costs", tags=["evidence"])
    async def costs(request: Request, days: int = Query(7, ge=1, le=90), p: Principal = require("public")) -> dict[str, Any]:
        since = time.time() - days * 86400
        if p.all_orgs or tenancy.single_tenant(request):   # deployment-wide LLM usage (agent + security roles)
            rows = usage_by_role(request.app.state.engine.store, since)
            return {"days": days, "by_model": rows, "overhead": overhead(rows)}
        cond, args = agent_filter("agent_id", visible_agents(request, p))   # multi-tenant: this org's security spend
        rows = request.app.state.engine.store.query(
            "SELECT model, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
            "SUM(est_cost) AS est_cost_usd FROM decisions WHERE created_at >= ? AND model IS NOT NULL" + cond +
            " GROUP BY model ORDER BY calls DESC", (since, *args))
        for r in rows:
            r.update(role="judge", provider=(r.pop("model") or "/").split("/", 1)[0], est_cost_usd=round(r["est_cost_usd"] or 0, 6))
        return {"days": days, "by_model": rows, "overhead": None}

    @app.get("/v1/costs/{agent}", tags=["evidence"])
    async def agent_costs(agent: str, request: Request, days: int = Query(7, ge=1, le=90),
                          p: Principal = require("public")) -> dict[str, Any]:
        """Security spend attributed to one agent: decisions and judge tokens/cost by tier (CORE-14)."""
        if not tenancy.can_see(request, p, agent):
            raise HTTPException(404, "unknown agent")
        since = time.time() - days * 86400
        rows = request.app.state.engine.store.query(
            "SELECT tier, COUNT(*) AS decisions, SUM(input_tokens) AS judge_input_tokens, SUM(est_cost) AS est_cost_usd, "
            "SUM(degraded) AS degraded, SUM(budget_degraded) AS budget_degraded FROM decisions "
            "WHERE agent_id = ? AND created_at >= ? GROUP BY tier ORDER BY tier", (agent, since))
        for r in rows:
            r["est_cost_usd"] = round(r["est_cost_usd"] or 0, 6)
        return {"agent": agent, "days": days, "by_tier": rows}

    # ---- API-6 policy write (admin, P1) -------------------------------------------------------
    @app.put("/v1/policies/{agent}", tags=["policy"])
    async def put_policy(agent: str, body: dict[str, Any], request: Request, p: Principal = require("admin")) -> dict[str, Any]:
        """Replace an agent's policy. Validated against the policy schema, version bumped, written atomically.
        A new agent joins the key's organisation; only an all-organisations key can move an agent between them."""
        exists = request.app.state.engine.has_policy(agent)
        if exists and not tenancy.can_see(request, p, agent):
            raise HTTPException(404, "unknown agent")
        if p.agents is not None and agent not in p.agents:
            raise HTTPException(404, "unknown agent")
        old = load_policy(agent)
        org = body.get("org") or (old.org if exists else ("default" if p.all_orgs else p.org))
        if not p.all_orgs and org != p.org:
            raise HTTPException(403, "this key cannot assign an agent to another organisation")
        body = {**body, "org": org}
        # GET returns literal canaries as "[REDACTED]"; a round-tripped policy keeps the stored values, never the mask.
        sd = dict(body.get("sensitive_data") or {})
        if "[REDACTED]" in (sd.get("canaries") or []):
            literal = [c for c in old.sensitive_data.canaries if c != "from_env"]
            sd["canaries"] = [c for c in sd["canaries"] if c != "[REDACTED]"] + [c for c in literal if c not in sd["canaries"]]
            body = {**body, "sensitive_data": sd}
        try:
            new = AgentPolicy.model_validate({**body, "agent": agent})
        except ValidationError as e:
            raise HTTPException(422, json.loads(e.json(include_url=False))[:5]) from None
        new.version = str(int(old.version) + 1) if old.version.isdigit() else old.version + ".1"
        save_policy(new)
        tenancy.refresh(request)
        access.info(json.dumps({"event": "policy_updated", "agent": agent, "org": new.org, "version": new.version, "by": p.name}))
        return {"agent": agent, "version": new.version, "mode": new.mode}

    # ---- CON-8 reviewer feedback -> trainability memory (fingerprints only) --------------------
    @app.post("/v1/feedback", tags=["review"])
    async def feedback(body: FeedbackRequest, request: Request, p: Principal = require("admin")) -> dict[str, Any]:
        if not body.event_id and not body.content:
            raise HTTPException(422, "provide event_id or content")
        e = request.app.state.engine
        recent = e._recent.get(body.event_id or "")
        if recent is not None and not tenancy.can_see(request, p, recent[0]):
            raise HTTPException(404, "event is no longer in recent memory; send the content instead")
        if body.agent_id == "*" and visible_agents(request, p) is not None:
            if recent is None:   # a "*" entry would apply to every organisation's agents
                if body.event_id:
                    raise HTTPException(404, "event is no longer in recent memory; send the content instead")
                raise HTTPException(422, "name the agent (agent_id) this verdict applies to")
            body.agent_id = recent[0]
        elif body.agent_id != "*" and not tenancy.can_see(request, p, body.agent_id):
            raise HTTPException(404, "unknown agent")
        try:
            mid = request.app.state.engine.feedback(body.verdict, event_id=body.event_id, content=body.content,
                                                    agent_id=body.agent_id, labels=body.labels,
                                                    origin=f"reviewer:{p.name}", note=body.note)
        except KeyError:
            raise HTTPException(404, "event is no longer in recent memory; send the content instead") from None
        if mid is None:
            raise HTTPException(422, "content too short to fingerprint safely")
        return {"memory_id": mid, "verdict": body.verdict}

    @app.get("/v1/memory", tags=["review"])
    async def memory(request: Request, kind: str | None = Query(None, pattern="^(attack|benign)$"),
                     p: Principal = require("public")) -> dict[str, Any]:
        m = request.app.state.engine.memory
        agents = visible_agents(request, p)
        if agents is None:
            return {"stats": m.stats(), "entries": m.entries(kind)}
        entries = [x for x in m.entries(kind, limit=1000) if x["agent_id"] in agents or x["agent_id"] == "*"]
        stats = {k: sum(1 for x in entries if x["kind"] == k) for k in ("attack", "benign")}
        return {"stats": stats, "entries": entries[:100]}

    @app.delete("/v1/memory/{memory_id}", tags=["review"])
    async def forget(memory_id: str, request: Request, p: Principal = require("admin")) -> dict[str, bool]:
        owner = request.app.state.engine.store.query("SELECT agent_id FROM memory WHERE id = ?", (memory_id,))
        if owner and visible_agents(request, p) is not None and (
                owner[0]["agent_id"] == "*" or not tenancy.can_see(request, p, owner[0]["agent_id"])):
            raise HTTPException(404, "unknown memory entry")
        if not request.app.state.engine.memory.remove(memory_id):
            raise HTTPException(404, "unknown memory entry")
        return {"removed": True}

    # ---- CON-9 approvals inbox (ESCALATE decisions) ------------------------------------------
    @app.get("/v1/approvals", tags=["review"])
    async def approvals(request: Request, status: str | None = Query("pending", pattern="^(pending|approved|denied)$"),
                        agent: str | None = None, p: Principal = require("public")) -> list[dict[str, Any]]:
        return request.app.state.engine.logger.approvals(status=status, agent=agent, agents=visible_agents(request, p))

    @app.post("/v1/approvals/{event_id}", tags=["review"])
    async def resolve(event_id: str, body: ApprovalRequest, request: Request, p: Principal = require("admin")) -> dict[str, Any]:
        """Approve or deny one escalated action. An approval lets the same call in the same session run once."""
        owner = request.app.state.engine.store.query("SELECT agent_id FROM approvals WHERE event_id = ?", (event_id,))
        if owner and not tenancy.can_see(request, p, owner[0]["agent_id"]):
            raise HTTPException(404, "no pending approval with this id")
        if not request.app.state.engine.logger.resolve_approval(event_id, body.approve, p.name, body.note):
            raise HTTPException(404, "no pending approval with this id")
        return {"event_id": event_id, "status": "approved" if body.approve else "denied"}

    # ---- Investigator: agentic triage of flagged sessions (D-061) ------------------------------
    def _session_owner(request: Request, p: Principal, session_id: str) -> str:
        e = request.app.state.engine
        owner = e.store.query("SELECT agent_id FROM sessions WHERE id = ?", (session_id,))
        agent = owner[0]["agent_id"] if owner else None
        if agent is None or not tenancy.can_see(request, p, agent):
            raise HTTPException(404, "unknown session")
        return agent

    @app.get("/v1/sessions/{session_id}/contract", tags=["sessions"])
    async def session_contract(session_id: str, request: Request, p: Principal = require("public")) -> dict[str, Any]:
        """D-064: every version of the session's intent contract (immutable, hash-chained), newest last."""
        _session_owner(request, p, session_id)
        versions = request.app.state.engine.logger.contracts(session_id)
        return {"session_id": session_id, "current": versions[-1] if versions else None, "versions": versions}

    @app.get("/v1/sessions/{session_id}/provenance", tags=["sessions"])
    async def session_provenance(session_id: str, request: Request, p: Principal = require("public")) -> dict[str, Any]:
        """D-065: attack-to-action chains (untrusted source -> content -> proposed call -> control -> outcome), built
        from the decision log, contracts and execution receipts."""
        _session_owner(request, p, session_id)
        e = request.app.state.engine
        approvals = {a["event_id"]: a for a in e.store.query(
            "SELECT event_id, status, reviewer FROM approvals WHERE session_id = ?", (session_id,))}
        return provenance.build(e.logger.session_events(session_id), approvals, e.logger.contracts(session_id))

    @app.post("/v1/actions/{event_id}/receipt", tags=["engine"])
    async def action_receipt(event_id: str, body: ReceiptRequest, request: Request,
                             p: Principal = require("project")) -> dict[str, Any]:
        """Execution receipt: the application reports whether an allowed (or blocked) call actually ran."""
        e = request.app.state.engine
        owner = e.store.query("SELECT agent_id FROM events WHERE id = ? AND kind = 'action'", (event_id,))
        if not owner or not tenancy.can_see(request, p, owner[0]["agent_id"]):
            raise HTTPException(404, "unknown action")
        require_agent(request, p, owner[0]["agent_id"])
        return {"ok": e.record_execution(event_id, body.executed, body.detail)}

    @app.post("/v1/sessions/{session_id}/investigate", tags=["review"])
    async def investigate(session_id: str, request: Request, p: Principal = require("public")) -> dict[str, Any]:
        """Run the Investigator on one session: it plans, uses read-only tools on the log, evidence, decoders, memory
        and policy, and returns a report with recommendations and proposals that wait for a reviewer. Any key that can
        see the session may run it (read-only, rate-limited per key); applying a proposal stays reviewer-only."""
        _session_owner(request, p, session_id)
        try:
            return await request.app.state.investigator.investigate(session_id, trigger=f"requested by {p.name}")
        except KeyError:
            raise HTTPException(404, "unknown session") from None

    @app.get("/v1/sessions/{session_id}/investigations", tags=["review"])
    async def session_investigations(session_id: str, request: Request, p: Principal = require("public")) -> list[dict[str, Any]]:
        _session_owner(request, p, session_id)
        return request.app.state.investigator.for_session(session_id)

    @app.get("/v1/investigations", tags=["review"])
    async def investigations(request: Request, limit: int = Query(50, ge=1, le=200),
                             p: Principal = require("public")) -> list[dict[str, Any]]:
        return request.app.state.investigator.recent(visible_agents(request, p), limit)

    @app.post("/v1/suggestions/{suggestion_id}", tags=["review"])
    async def resolve_suggestion(suggestion_id: str, body: SuggestionRequest, request: Request,
                                 p: Principal = require("admin")) -> dict[str, Any]:
        """Apply or dismiss an Investigator proposal. Memory proposals are applied like reviewer feedback; policy
        proposals are advice: applying one marks it accepted, the policy itself is edited on the Policy page."""
        rows = request.app.state.engine.store.query("SELECT agent_id FROM suggestions WHERE id = ?", (suggestion_id,))
        if not rows or not tenancy.can_see(request, p, rows[0]["agent_id"]):
            raise HTTPException(404, "no pending suggestion with this id")
        try:
            return request.app.state.investigator.resolve(suggestion_id, body.apply, p.name)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'")) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    # ---- Observability (NFR §12) -------------------------------------------------------------
    @app.get("/v1/metrics", tags=["ops"])
    async def metrics(request: Request, hours: float = Query(24, gt=0, le=24 * 30),
                      p: Principal = require("public")) -> dict[str, Any]:
        """Decisions by type and rule, tier escalation rate, latency p50/p95 by tier, provider errors, memory."""
        e = request.app.state.engine
        since = time.time() - hours * 3600
        agents = visible_agents(request, p)
        cond, args = agent_filter("d.agent_id", agents)
        rows = e.store.query("SELECT d.decision, d.policy_rule, d.tier, d.latency_ms, d.degraded, d.labels, ev.kind "
                             "FROM decisions d JOIN events ev ON ev.id = d.event_id WHERE d.created_at >= ?" + cond,
                             (since, *args))
        providers = (e.store.query("SELECT provider, status, COUNT(*) AS n FROM llm_usage WHERE ts >= ? "
                                   "GROUP BY provider, status ORDER BY provider, status", (since,))
                     if p.all_orgs or tenancy.single_tenant(request) else [])
        content = [r for r in rows if r["kind"] == "content_in"]
        count = lambda key: {k: sum(1 for r in rows if r[key] == k) for k in sorted({r[key] for r in rows})}
        labels: dict[str, int] = {}
        for r in rows:
            for label in json.loads(r["labels"] or "[]"):
                labels[label] = labels.get(label, 0) + 1
        latency = lambda rs: {"n": len(rs), "p50": _percentile([r["latency_ms"] for r in rs], 0.5),
                              "p95": _percentile([r["latency_ms"] for r in rs], 0.95)}
        judged = sum(1 for r in content if r["tier"] == 2)
        return {
            "window_hours": hours, "decisions": len(rows), "by_decision": count("decision"),
            "by_rule": count("policy_rule"), "by_label": dict(sorted(labels.items())),
            "inspections": {"n": len(content), "escalated_to_judge": judged,
                            "escalation_rate": round(judged / len(content), 3) if content else None,
                            "degraded": sum(1 for r in content if r["degraded"])},
            "latency_ms": {"fast_path": latency([r for r in content if (r["tier"] or 0) < 2]),
                           "judge_path": latency([r for r in content if r["tier"] == 2])},
            "providers": providers,
            "memory": e.memory.stats() if agents is None else None,
            "siem": dict(siem.forwarder().stats) if agents is None and siem.forwarder() else None,
            "approvals_pending": len(e.logger.approvals("pending", agents=agents)),
        }

    # ---- API-12 SIEM export (pull) ---------------------------------------------------------------
    @app.get("/v1/export/decisions", tags=["evidence"])
    async def export(request: Request, since: float | None = None, until: float | None = None,
                     format: str = Query("jsonl", pattern="^(jsonl|cef)$"), limit: int = Query(10000, ge=1, le=100000),
                     p: Principal = require("project")) -> StreamingResponse:
        """Decisions as security events for a SIEM, oldest first: JSON Lines (ECS-style fields) or CEF lines.
        Continue with `since` = the last record's `intentseal.ts`. Limited to this key's agents."""
        e = request.app.state.engine
        lo, hi = since if since is not None else time.time() - 86400, until if until is not None else time.time()
        cond, args = agent_filter("d.agent_id", visible_agents(request, p))

        def lines():
            cursor, last_id, sent = lo, (2 ** 62 if since is not None else -1), 0   # `since` is exclusive
            while sent < limit:
                rows = e.store.query(
                    "SELECT d.*, ev.kind, ev.source FROM decisions d JOIN events ev ON ev.id = d.event_id "
                    "WHERE (d.created_at > ? OR (d.created_at = ? AND d.id > ?)) AND d.created_at <= ?" + cond +
                    " ORDER BY d.created_at ASC, d.id ASC LIMIT ?", (cursor, cursor, last_id, hi, *args, min(1000, limit - sent)))
                if not rows:
                    return
                for row in rows:
                    rec = siem.record(row, e._org_of(row["agent_id"]))
                    yield (siem.cef(rec) if format == "cef" else json.dumps(rec, default=str)) + "\n"
                sent += len(rows)
                cursor, last_id = rows[-1]["created_at"], rows[-1]["id"]

        return StreamingResponse(lines(), media_type="text/plain" if format == "cef" else "application/x-ndjson")

    from .proxy import router as proxy_router
    app.include_router(proxy_router)
    from .proxy_gemini import router as gemini_router
    app.include_router(gemini_router)
    from .setup import router as setup_router
    app.include_router(setup_router)
    from .delegate_api import router as delegate_router
    app.include_router(delegate_router)
    # One-container setup: the built Console served from the gateway's own origin (the Docker image sets this).
    # Mounted last, so every API route above takes precedence.
    console_dir = os.environ.get("INTENTSEAL_CONSOLE_DIR")
    if console_dir and (Path(console_dir) / "index.html").is_file():
        from fastapi.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=console_dir, html=True), name="console")
    return app


app = create_app()
