"""Abide client: put your own agents under the employee's words, against a hosted Abide gateway.

Self-contained (only `httpx`), so it also works as a single downloaded file:

    curl -O https://<gateway>/v1/sdk/abide.py

    from abide import DelegateClient, RemoteAdkGuard

    client = DelegateClient("https://<gateway>", key)              # a project key (or POST /v1/trial-keys)
    await client.register_workspace("my-company", policy)          # your tools, contacts, files, rules (once)
    card = await client.start("Read the quote and draft a reply. Don't send it.", workspace="my-company")
    await client.confirm(card)                                     # the employee confirms the card once
    guard = RemoteAdkGuard(client, card["workflow_id"], "drafter")
    agent = LlmAgent(..., before_tool_callback=guard.before_tool, after_tool_callback=guard.after_tool)
    ...
    print(await client.report(card["workflow_id"]))                # the receipt: every step, cited

Every tool call your agent makes is checked against the card before it runs. A call outside it never runs and the
agent is told why, so it carries on with the rest of the task. What reading tools return is inspected (hidden
instructions removed) before the agent sees it.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Awaitable, Callable

import httpx

__all__ = ["DelegateClient", "DelegateError", "RemoteAdkGuard"]


class DelegateError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status, self.detail = status, detail


class DelegateClient:
    """The Delegate REST API. Every method is a coroutine (works in notebooks with top-level await)."""

    def __init__(self, base_url: str, api_key: str, timeout_s: float = 60.0,
                 client: httpx.AsyncClient | None = None) -> None:
        self.base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    @classmethod
    async def with_trial_key(cls, base_url: str, **kw: Any) -> "DelegateClient":
        """A fresh 7-day key for yourself, when the gateway allows it (no sign-up)."""
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(base_url.rstrip("/") + "/v1/trial-keys")
        if r.status_code >= 400:
            raise DelegateError(r.status_code, r.text[:300])
        return cls(base_url, r.json()["key"], **kw)

    async def _call(self, method: str, path: str, body: Any = None) -> Any:
        r = await self._client.request(method, self.base + path, headers=self._headers,
                                       json=body if body is not None else None)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise DelegateError(r.status_code, str(detail)[:500])
        return r.json()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- your business --
    async def register_workspace(self, workspace_id: str, policy: dict[str, Any], description: str = "") -> Any:
        return await self._call("POST", "/v1/delegate-workspaces",
                                {"id": workspace_id, "policy": policy, "description": description})

    async def workspaces(self) -> Any:
        return await self._call("GET", "/v1/delegate-workspaces")

    # -- the employee --
    async def start(self, request: str, workspace: str = "workspace", owner: str = "employee") -> dict[str, Any]:
        """Turn the employee's words into a card. Returns the card (`clauses`, `hash`, `workflow_id`)."""
        state = await self._call("POST", "/v1/delegations", {"request": request, "workspace": workspace,
                                                              "owner": owner})
        return state["contract"]

    async def confirm(self, card: dict[str, Any], accept: list[str] | None = None) -> Any:
        """Confirm exactly the card you were shown (its hash); `accept` ticks lines that need the employee's OK."""
        return await self._call("POST", f"/v1/delegations/{card['workflow_id']}/confirm",
                                {"hash": card["hash"], "accept": accept or []})

    async def say(self, workflow_id: str, words: str) -> Any:
        """The employee's next words (they can widen or narrow the card)."""
        return await self._call("POST", f"/v1/delegations/{workflow_id}/amend", {"text": words})

    async def answer(self, workflow_id: str, approval_id: str, approve: bool) -> Any:
        """The employee's answer to one question about one exact action."""
        return await self._call("POST", f"/v1/delegations/{workflow_id}/approvals/{approval_id}",
                                {"approve": approve})

    async def state(self, workflow_id: str) -> Any:
        return await self._call("GET", f"/v1/delegations/{workflow_id}")

    async def report(self, workflow_id: str) -> Any:
        """The work receipt: sentences that each cite receipts, and whether the receipt chain is intact."""
        return await self._call("GET", f"/v1/delegations/{workflow_id}/report")

    # -- the agent runtime --
    async def check(self, workflow_id: str, agent: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return await self._call("POST", f"/v1/delegations/{workflow_id}/check",
                                {"agent": agent, "tool": tool, "args": args})

    async def executed(self, workflow_id: str, receipt: str, ok: bool = True, detail: str = "") -> Any:
        return await self._call("POST", f"/v1/delegations/{workflow_id}/executed",
                                {"receipt": receipt, "executed": ok, "detail": detail})

    async def read(self, workflow_id: str, agent: str, content: str | bytes, source: str = "document",
                   filename: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"agent": agent, "source": source, "filename": filename}
        if isinstance(content, bytes):
            body["content_b64"] = base64.b64encode(content).decode()
        else:
            body["content"] = content
        return await self._call("POST", f"/v1/delegations/{workflow_id}/read", body)


def _args(args: dict[str, Any] | None) -> dict[str, Any]:
    """Python tools spell a reserved word with a trailing "_" (with_): send the policy's field name."""
    return {(k[:-1] if k.endswith("_") and not k.endswith("__") else k): v for k, v in dict(args or {}).items()}


def _key(tool: str, args: dict[str, Any]) -> str:
    return tool + ":" + json.dumps(args, sort_keys=True, default=str)


AskHandler = Callable[[dict[str, Any]], Awaitable[bool]]


class RemoteAdkGuard:
    """Google ADK callbacks that check every tool call with a remote Abide gateway.

    before_tool: allowed -> the tool runs; stopped -> it never runs and the agent gets the reason; needs the employee
    -> `on_ask(check)` decides (await a person, a chat, a ticket), or without it the call waits and the agent is told.
    after_tool: the run is recorded, and what a reading tool returned is inspected before the agent sees it.
    Tools must be named in your business's policy (`DelegateClient.register_workspace`).
    """

    def __init__(self, client: DelegateClient, workflow_id: str, agent_name: str,
                 on_ask: AskHandler | None = None) -> None:
        self.client, self.wf, self.agent, self.on_ask = client, workflow_id, agent_name, on_ask
        self._pending: dict[str, tuple[str, str | None]] = {}       # call -> (check receipt, tool action)
        self.events: list[dict[str, Any]] = []                      # what happened, for your own logs or UI

    async def before_tool(self, tool: Any, args: dict[str, Any], tool_context: Any = None) -> dict[str, Any] | None:
        name, args = tool.name, _args(args)
        r = await self.client.check(self.wf, self.agent, name, args)
        if r["decision"] == "ask" and self.on_ask is not None:
            ok = await self.on_ask(r)
            await self.client.answer(self.wf, r["approval"], ok)
            if not ok:
                self.events.append({"tool": name, "args": args, "outcome": "declined", "note": r["reason"]})
                return {"status": "declined", "message": "The employee declined this exact action. Continue without it."}
            r = await self.client.check(self.wf, self.agent, name, args)
        if r["decision"] == "ask":
            self.events.append({"tool": name, "args": args, "outcome": "waiting", "note": r["reason"],
                                "approval": r["approval"]})
            return {"status": "waiting", "message": r.get("notice", "Waiting for the employee.")}
        if r["decision"] != "allow":
            note = r["reason"] + (f" (traced to {r['traced_to']})" if r.get("traced_to") else "")
            self.events.append({"tool": name, "args": args, "outcome": "blocked", "note": note})
            return {"status": "stopped", "message": r.get("notice", note)}
        self._pending[_key(name, args)] = (r["receipt"], r.get("action"))
        return None

    async def after_tool(self, tool: Any, args: dict[str, Any], tool_context: Any,
                         tool_response: Any) -> dict[str, Any] | None:
        name, args = tool.name, _args(args)
        pending = self._pending.pop(_key(name, args), None)
        if pending is None:                       # stopped or waiting: it did not run
            return None
        receipt, action = pending
        await self.client.executed(self.wf, receipt, True)
        self.events.append({"tool": name, "args": args, "outcome": "ran", "note": ""})
        if action != "read":
            return None
        resp = tool_response if isinstance(tool_response, dict) else {"result": tool_response}
        content: str | bytes = (base64.b64decode(resp["content_b64"]) if resp.get("content_b64")
                                else json.dumps(resp, default=str) if not isinstance(resp.get("result"), str)
                                else resp["result"])
        seen = await self.client.read(self.wf, self.agent, content, source=f"tool:{name}",
                                      filename=resp.get("filename"))
        if seen.get("flagged"):
            self.events[-1]["note"] = "Hidden or suspicious instructions removed before the agent read it"
        return {"result": seen["content"]}
