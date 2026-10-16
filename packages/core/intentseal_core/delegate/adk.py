"""Connect Google ADK agents to IntentSeal Delegate (step ⑦ at the tool boundary, plus ⑤ on what tools return).

    guard = AdkGuard(service, workflow_id, agent_name="drafter")
    agent = LlmAgent(..., tools=[...], before_tool_callback=guard.before_tool, after_tool_callback=guard.after_tool)

before_tool  - every call is checked against the employee's contract. Allowed: the tool runs. Stopped: the tool does
               not run and the agent receives the notice as the tool's result, so it carries on with the rest of the
               task. Needs a confirmation: `on_ask` decides (a simulated employee in evaluations); without it the call
               waits and is listed in `waiting` (the live Console resumes it after the employee answers).
after_tool   - the execution is recorded for the receipt, and for reading tools the result goes through the content
               filter: the agent sees the cleaned text, and the text is remembered for tracing later calls.
               A tool may return {"content_b64": ..., "filename": ...} for files (PDFs): the filter reads the file
               itself, so text hidden in it is found and kept away from the model.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Awaitable, Callable

AskHandler = Callable[[dict[str, Any]], Awaitable[bool]]


def _key(tool: str, args: dict[str, Any]) -> str:
    return tool + ":" + json.dumps(args, sort_keys=True, default=str)


class AdkGuard:
    def __init__(self, svc: Any, workflow_id: str, agent_name: str, on_ask: AskHandler | None = None,
                 inspect: bool | None = None) -> None:
        self.svc, self.wf, self.agent = svc, workflow_id, agent_name
        self.on_ask, self.inspect = on_ask, inspect
        self._receipts: dict[str, str] = {}
        self.sources: list[str] = []                 # receipts of content this agent read (for the handoff seal)
        self.waiting: dict[str, tuple[str, dict[str, Any]]] = {}
        self.events: list[dict[str, Any]] = []

    def _is_read(self, tool: str) -> bool:
        spec = self.svc.policy.tools.get(tool)
        return spec is not None and spec.action == "read"

    async def before_tool(self, tool: Any, args: dict[str, Any], tool_context: Any = None) -> dict[str, Any] | None:
        name, args = tool.name, dict(args or {})
        r = await self.svc.check(self.wf, self.agent, name, args)
        if r["decision"] == "ask":
            if self.on_ask is None:
                self.waiting[r["approval"]] = (name, args)
                self.events.append({"agent": self.agent, "tool": name, "args": args, "outcome": "waiting",
                                    "note": r["reason"]})
                return {"status": "waiting", "message": r["notice"]}
            ok = await self.on_ask(r)
            self.svc.resolve(self.wf, r["approval"], ok, self.svc.owner_of(self.wf))
            if not ok:
                self.events.append({"agent": self.agent, "tool": name, "args": args, "outcome": "declined",
                                    "note": r["reason"]})
                return {"status": "declined", "message": "The employee declined this exact action. Continue without it."}
            r = await self.svc.check(self.wf, self.agent, name, args)
        if r["decision"] != "allow":
            note = r["reason"] + (f" (traced to {r['traced_to']})" if r.get("traced_to") else "")
            self.events.append({"agent": self.agent, "tool": name, "args": args, "outcome": "blocked", "note": note})
            return {"status": "stopped", "message": r["notice"]}
        self._receipts[_key(name, args)] = r["receipt"]
        return None

    async def after_tool(self, tool: Any, args: dict[str, Any], tool_context: Any,
                         tool_response: Any) -> dict[str, Any] | None:
        name, args = tool.name, dict(args or {})
        rid = self._receipts.pop(_key(name, args), None)
        if rid is None:                               # the tool did not run through the gate (stopped or waiting)
            return None
        self.svc.executed(self.wf, rid, True)
        self.events.append({"agent": self.agent, "tool": name, "args": args, "outcome": "ran", "note": ""})
        if not self._is_read(name):
            return None
        resp = tool_response if isinstance(tool_response, dict) else {"result": tool_response}
        filename = resp.get("filename")
        content: str | bytes
        if resp.get("content_b64"):
            content = base64.b64decode(resp["content_b64"])
        else:
            content = str(resp.get("result", resp.get("content", "")))
        spec = self.svc.policy.tools[name]
        res = next((r_ for a in spec.resource_args if (r_ := self.svc.policy.resource(str(args.get(a, ""))))), None)
        source = ("pdf" if (filename or "").endswith(".pdf") else
                  "email" if name in ("read_email", "read_ticket") else f"tool:{name}")
        seen = await self.svc.read(self.wf, self.agent, content, source, filename=filename, inspect=self.inspect,
                                   resource=res.id if res else None)
        self.sources.append(seen["receipt"])
        if seen["flagged"]:
            self.events[-1]["note"] = "Hidden or suspicious instructions removed before the agent read it"
        return {"result": seen["content"]}

    async def resume(self, approval_id: str, run: Callable[[str, dict[str, Any]], Any]) -> Any:
        """After the employee approves a waiting call: run it now (`run(tool, args)` executes the tool)."""
        item = self.waiting.pop(approval_id, None)
        if item is None:
            return None
        name, args = item
        r = await self.svc.check(self.wf, self.agent, name, args)
        if r["decision"] != "allow":
            return None
        out = run(name, args)
        self.svc.executed(self.wf, r["receipt"], True)
        for e in self.events:
            if e["outcome"] == "waiting" and e["tool"] == name and e["args"] == args:
                e["outcome"] = "asked->ran"
        return out
