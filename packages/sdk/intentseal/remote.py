"""Remote mode (SDK-6): the Guard talks to a Gateway API instead of running the engine in-process.

    guard = Guard("my-agent", remote="https://gateway.example", api_key=os.environ["INTENTSEAL_API_KEY"])

`RemoteEngine` implements the part of the Engine interface the Guard uses (start_session, inspect, check_action,
register_secrets), so every SDK feature works the same way; decisions are made by the gateway's engine.
Differences from embedded mode:
  - the session's trusted context (pinned fields) is fixed when the session is created;
  - canaries/secrets are configured on the gateway (INTENTSEAL_CANARIES), not registered from the client;
  - if the gateway is unreachable, content is withheld (fail closed) and tools follow their failure policy (SDK-5).
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, Iterable

import httpx

from intentseal_core.policy import AgentPolicy
from intentseal_core.session import Session
from intentseal_core.types import ActionResult, InspectResult

log = logging.getLogger("intentseal.sdk.remote")


class RemoteEngine:
    def __init__(self, base_url: str, api_key: str, timeout_s: float = 30.0, client: httpx.Client | None = None) -> None:
        self.http = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_s)
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        r = self.http.post(path, json=body, headers=self.headers)
        r.raise_for_status()
        return r.json()

    def policy(self, agent_id: str) -> AgentPolicy:
        r = self.http.get(f"/v1/policies/{agent_id}", headers=self.headers)
        r.raise_for_status()
        return AgentPolicy.model_validate(r.json())

    def register_secrets(self, values: Iterable[str]) -> None:
        if list(values):
            log.info("remote mode: secrets/canaries are configured on the gateway (INTENTSEAL_CANARIES), not sent from the client")

    def start_session(self, agent_id: str, user_ref: str | None = None, task: str = "",
                      trusted: dict[str, Any] | None = None) -> Session:
        sid = self._post("/v1/sessions", {"agent_id": agent_id, "user_ref": user_ref, "task": task,
                                          "trusted": dict(trusted or {})})["session_id"]
        return Session(agent_id=agent_id, session_id=sid, user_ref=user_ref, task=task, trusted=dict(trusted or {}))

    async def inspect(self, content: str | bytes, source: str, agent_id: str, session_id: str | None = None,
                      filename: str | None = None, fmt: str | None = None, task: str | None = None) -> InspectResult:
        body: dict[str, Any] = {"source": source, "agent_id": agent_id, "session_id": session_id, "filename": filename}
        if isinstance(content, bytes):
            body["content_b64"] = base64.b64encode(content).decode()
        else:
            body["content"] = content
        return InspectResult.model_validate(await asyncio.to_thread(self._post, "/v1/inspect", body))

    async def check_action(self, tool: str, args: dict[str, Any], agent_id: str,
                           session_id: str | None = None) -> ActionResult:
        body = {"tool": tool, "args": args, "agent_id": agent_id, "session_id": session_id}
        return ActionResult.model_validate(await asyncio.to_thread(self._post, "/v1/actions/check", body))

    async def check_output(self, text: str, agent_id: str, session_id: str | None = None) -> InspectResult:
        body = {"agent_id": agent_id, "session_id": session_id, "text": text}
        return InspectResult.model_validate(await asyncio.to_thread(self._post, "/v1/outputs/check", body))

    def record_execution(self, event_id: str, executed: bool, detail: str = "") -> bool:
        return bool(self._post(f"/v1/actions/{event_id}/receipt", {"executed": executed, "detail": detail}).get("ok"))

    def close(self) -> None:
        self.http.close()
