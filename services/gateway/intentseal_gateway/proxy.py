"""LLM proxy (PRX-1…PRX-6): put IntentSeal between an unmodified agent and its model, by base URL alone.

Two wire formats, one checkpoint:
  OpenAI-compatible   POST /proxy/<agent>/v1/chat/completions        OPENAI_BASE_URL=<gateway>/proxy/<agent>/v1
  Anthropic Messages  POST /proxy/<agent>/anthropic/v1/messages      ANTHROPIC_BASE_URL=<gateway>/proxy/<agent>/anthropic

The client authenticates with its gateway key (the SDK's api key). The session comes from X-Session-Id or, for
clients that send none, from a fingerprint of the conversation's opening (PRX-5). Every user message and every tool
result is inspected before the model sees it (PRX-2), each once per session; its decision is re-applied when the
client resends the history. Every tool call the model returns is checked before the agent sees it (PRX-3): blocked
calls are removed and replaced by a notice, pinned fields are rewritten. Streaming: text streams through, tool calls
are buffered, checked, then released (PRX-6). System messages are the operator's and are trusted.

Where the call goes (PRX-4) is the agent's policy `upstream:` - its own provider and model (OpenAI, Anthropic,
Gemini, Groq, Azure OpenAI, a local server, ...) with the gateway's key or the agent's own key (X-Upstream-Key) -
or, when unset, the gateway's `proxy_upstream` role (free-tier models used by the demo and the evaluation).
Provider errors are returned with the provider's own status code, so client retry logic keeps working.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import replace
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from intentseal_core.llm.adapters import ChatRequest, ProviderError, get_adapter
from intentseal_core.llm.router import AllProvidersFailed
from intentseal_core.secrets import ApiKey, registry
from intentseal_core.types import Decision

from .security import Principal, require
from .tenancy import require_agent

router = APIRouter(tags=["proxy"])
PROXY_ROLE = "proxy_upstream"
_SEEN_MAX = 2000   # sessions whose per-message decisions are kept in memory
# Request fields the proxy itself handles; everything else is forwarded to the agent's own upstream unchanged.
_OPENAI_HANDLED = {"model", "messages", "tools", "tool_choice", "temperature", "max_tokens", "stream", "stream_options", "n"}
_STATUS = {"rate_limited": 429, "auth_failed": 401, "not_found": 404, "bad_request": 400, "timeout": 504}
# RAG apps put retrieved documents into the system or user message. Text inside these tags is third-party content:
# it is inspected as untrusted (source "rag") on its own, never trusted as the operator's or the user's words (D-050).
_CONTEXT_TAGS = [t.strip() for t in (os.environ.get("INTENTSEAL_PROXY_CONTEXT_TAGS") or
                 "document,documents,context,search_results,retrieved,retrieved_context,knowledge").split(",") if t.strip()]
_CONTEXT = re.compile(r"<(%s)\b[^>]*>(.*?)</\1\s*>" % "|".join(map(re.escape, _CONTEXT_TAGS)), re.S | re.I)
_DATA_URL = re.compile(r"^data:([\w.+-]+/[\w.+-]+)?(?:;[^,;]*)*;base64,(.*)$", re.S)
_EXT = {"image/png": "upload.png", "image/jpeg": "upload.jpg", "image/jpg": "upload.jpg", "application/pdf": "upload.pdf"}


def _text(content: Any) -> str:
    if isinstance(content, list):   # OpenAI content parts / Anthropic text blocks
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return content or ""


def _seen(request: Request, session_id: str) -> dict[str, tuple[str, dict[str, Any] | None]]:
    state = request.app.state
    if not hasattr(state, "proxy_seen"):
        state.proxy_seen = OrderedDict()
    seen = state.proxy_seen.setdefault(session_id, {})
    state.proxy_seen.move_to_end(session_id)
    while len(state.proxy_seen) > _SEEN_MAX:
        state.proxy_seen.popitem(last=False)
    return seen


def _session(request: Request, agent_id: str, opening: list[Any], first_user: str) -> str:
    """X-Session-Id, or a stable id from the system prompt + first user message (PRX-5)."""
    engine = request.app.state.engine
    sid = request.headers.get("x-session-id") or (
        "conv-" + hashlib.sha256(json.dumps([agent_id, opening], default=str).encode()).hexdigest()[:16])
    if sid not in engine.sessions:
        engine.start_session(agent_id, task=first_user, session_id=sid)
    return sid


async def _inspect(request: Request, agent_id: str, session_id: str, source: str, text: str,
                   findings: list[dict[str, Any]], data: bytes | None = None, filename: str | None = None) -> str | None:
    """PRX-2: the text the model may see instead of `text` (or of the file `data`), or None when it is allowed
    unchanged. Files (images, PDFs) are extracted by the engine (OCR for images); a file that is not allowed is
    replaced by its cleaned text or a notice."""
    if not text and not data:
        return None
    seen = _seen(request, session_id)
    key = hashlib.sha256(source.encode() + b"\n" + (data if data is not None else text.encode())).hexdigest()
    if key not in seen:
        r = await request.app.state.engine.inspect(data if data is not None else text, source, agent_id,
                                                   session_id=session_id, filename=filename)
        if r.enforced == Decision.ALLOW:
            seen[key] = (text, None)
        else:
            replacement = (r.cleaned_content if r.enforced == Decision.NEUTRALISE
                           else f"[Content withheld by the security policy: {r.reason}]")
            seen[key] = (replacement, {"source": source, "decision": r.enforced.value, "rule": r.policy_rule,
                                       "labels": r.labels, "event_id": r.event_id})
    replacement, finding = seen[key]
    if finding is None:
        return None
    findings.append(finding)
    return replacement


def _without_context(text: str) -> str:
    """The message text with retrieved-document spans removed: what the user (or operator) actually wrote."""
    return _CONTEXT.sub(" ", text or "")


async def _inspect_text(request: Request, agent_id: str, session_id: str, source: str | None, text: str,
                        findings: list[dict[str, Any]]) -> str | None:
    """One system or user message text. Retrieved-document spans (`<context>…</context>`, `<document>…`) are
    inspected as untrusted "rag" content and cleaned in place; the rest is inspected as `source` ("user"), or left
    alone when `source` is None (the operator's system prompt). Returns the new text, or None when unchanged.
    If the user's own words are not allowed, the whole message is replaced."""
    if not text:
        return None
    spans = list(_CONTEXT.finditer(text))
    if source is not None:
        # The user's own words first: risk from a poisoned retrieved document must not hold the user's question.
        own = _without_context(text) if spans else text
        if own.strip():
            rep = await _inspect(request, agent_id, session_id, source, own, findings)
            if rep is not None:
                return rep
    out, pos, changed = [], 0, False
    for m in spans:
        rep = await _inspect(request, agent_id, session_id, "rag", m.group(2), findings)
        out += [text[pos:m.start(2)], m.group(2) if rep is None else rep]
        changed |= rep is not None
        pos = m.end(2)
    out.append(text[pos:])
    return "".join(out) if changed else None


async def _inspect_openai_message(request: Request, agent_id: str, session_id: str, m: dict[str, Any],
                                  source: str | None, findings: list[dict[str, Any]]) -> None:
    """A system/developer (source None) or user message: text parts as in `_inspect_text`, inline images and PDFs
    (`data:` URLs) through the engine's extraction and OCR as untrusted content. Rewrites `m` in place."""
    content = m.get("content")
    if isinstance(content, str):
        rep = await _inspect_text(request, agent_id, session_id, source, content, findings)
        if rep is not None:
            m["content"] = rep
        return
    if not isinstance(content, list):
        return
    parts: list[Any] = []
    for part in content:
        kind = part.get("type") if isinstance(part, dict) else None
        if kind == "text":
            rep = await _inspect_text(request, agent_id, session_id, source, part.get("text", ""), findings)
            parts.append(part if rep is None else {**part, "text": rep})
            continue
        url = ((part.get("image_url") or {}).get("url") if kind == "image_url" else
               (part.get("file") or {}).get("file_data") if kind == "file" else None)
        f = _file_bytes(url=url) if isinstance(url, str) else None
        if f is None:
            parts.append(part)
            continue
        file_source = "image" if kind == "image_url" else "document"
        rep = await _inspect(request, agent_id, session_id, file_source, "", findings, data=f[0], filename=f[1])
        parts.append(part if rep is None else {"type": "text", "text": rep})
    m["content"] = parts


def _file_bytes(source: dict[str, Any] | None = None, url: str | None = None) -> tuple[bytes, str] | None:
    """Inline file content (an OpenAI `data:` URL or an Anthropic base64 source) as (bytes, filename hint)."""
    try:
        if url is not None:
            m = _DATA_URL.match(url.strip())
            if not m:
                return None   # remote URLs are fetched by the provider, not by the gateway
            return base64.b64decode(re.sub(r"\s+", "", m.group(2)), validate=True), _EXT.get((m.group(1) or "").lower(), "upload.bin")
        if source and source.get("type") == "base64" and source.get("data"):
            return (base64.b64decode(re.sub(r"\s+", "", str(source["data"])), validate=True),
                    _EXT.get(str(source.get("media_type", "")).lower(), "upload.bin"))
    except (binascii.Error, ValueError):
        return None   # not valid base64: the provider rejects it too, so nothing readable reaches the model
    return None


async def _check(engine: Any, name: str, args: Any, agent_id: str, session_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """PRX-3: (arguments to release, None) when allowed (pinned fields rewritten), (None, notice) when not."""
    if not isinstance(args, dict):
        args = {"_raw": args}
    ar = await engine.check_action(name, args, agent_id, session_id=session_id)
    if ar.enforced == Decision.ALLOW:
        return (ar.final_args if ar.pinned_overrides else args), None
    return None, ar.notice or f"Tool call '{name}' was blocked by the security policy."


async def _check_tool_calls(engine: Any, tool_calls: list[dict[str, Any]], agent_id: str,
                            session_id: str) -> tuple[list[dict[str, Any]], list[str]]:
    kept, stopped = await _check_tool_calls_detailed(engine, tool_calls, agent_id, session_id)
    return kept, [notice for _, notice in stopped]


# The model is told what was stopped, as the result of the stopped call, and asked to go on (D-062): the agent's work
# continues another way instead of ending on a notice. At most this many extra model calls per request.
CONTINUE_ROUNDS = 2
CONTINUE_NOTE = (" The call was not run. Continue the user's task without it if you can (another way within the "
                 "rules); otherwise tell the user what was stopped and why.")


async def _check_tool_calls_detailed(engine: Any, tool_calls: list[dict[str, Any]], agent_id: str, session_id: str
                                     ) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], str]]]:
    kept, stopped = [], []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"_raw": fn.get("arguments")}
        final, notice = await _check(engine, fn.get("name", ""), args, agent_id, session_id)
        if notice:
            stopped.append((tc, notice))
        else:
            kept.append({**tc, "function": {**fn, "arguments": json.dumps(final) if final is not args else fn.get("arguments", "{}")}})
    return kept, stopped


async def _continue_after_stop(engine: Any, call: Any, req: ChatRequest, content: str,
                               stopped: list[tuple[dict[str, Any], str]], agent_id: str, session_id: str
                               ) -> tuple[str, list[dict[str, Any]], list[tuple[dict[str, Any], str]], Any]:
    """Every call in the model's turn was stopped: tell the model (as the calls' results) and let it continue, up to
    CONTINUE_ROUNDS times. Returns the final (content, kept calls, still-stopped calls, last upstream result)."""
    messages, res = list(req.messages), None
    for _ in range(CONTINUE_ROUNDS):
        for tc, _n in stopped:
            tc["id"] = tc.get("id") or f"call_{uuid.uuid4().hex[:12]}"
        messages += [{"role": "assistant", "content": content or None, "tool_calls": [tc for tc, _ in stopped]}]
        messages += [{"role": "tool", "tool_call_id": tc["id"], "content": notice + CONTINUE_NOTE} for tc, notice in stopped]
        res = await call(replace(req, messages=messages))
        content = res.content or ""
        kept, stopped = await _check_tool_calls_detailed(engine, res.tool_calls, agent_id, session_id)
        if kept or not stopped:
            return content, kept, stopped, res
    return content, [], stopped, res


def _upstream(request: Request, agent_id: str) -> tuple[Any, str | None]:
    """The agent's upstream policy and the client's own provider key (if the policy asks for one)."""
    up = request.app.state.engine.policy_loader(agent_id).upstream
    client_key = request.headers.get("x-upstream-key") if up.key == "client" else None
    if up.provider and up.key == "client" and not client_key:
        raise ProviderError("auth_failed", "this agent forwards its own provider key: send it in the X-Upstream-Key header", 401)
    return up, client_key


def _openai_error(e: Exception) -> JSONResponse:
    if isinstance(e, AllProvidersFailed):
        return JSONResponse({"error": {"message": "all upstream providers unavailable", "type": "upstream_unavailable",
                                       "detail": str(e)[:300]}}, status_code=503)
    kind = getattr(e, "kind", "error")
    status = getattr(e, "http_status", None) or _STATUS.get(kind, 502)
    headers = {"Retry-After": str(int(e.retry_after_s))} if getattr(e, "retry_after_s", None) else None
    return JSONResponse({"error": {"message": str(e)[:300], "type": kind, "code": status}}, status_code=status, headers=headers)


# ---- OpenAI-compatible ---------------------------------------------------------------------------------------

@router.post("/proxy/v1/chat/completions")
async def chat_completions(request: Request, p: Principal = require("project")) -> Any:
    return await _complete(request, request.headers.get("x-agent-id", "default-agent"), p)


@router.post("/proxy/{agent_id}/v1/chat/completions")
async def agent_chat_completions(agent_id: str, request: Request, p: Principal = require("project")) -> Any:
    return await _complete(request, agent_id, p)


@router.get("/proxy/{agent_id}/v1/models")
async def agent_models(agent_id: str, request: Request, p: Principal = require("project")) -> dict[str, Any]:
    """Model list for clients that discover models first (agent frameworks, chat UIs): the model the policy pins
    (it replaces whatever the client asks for), a placeholder when the gateway's own role answers, and nothing when
    the client's own model is forwarded."""
    require_agent(request, p, agent_id)
    up = request.app.state.engine.policy_loader(agent_id).upstream
    model = up.model or (None if up.provider else "intentseal-default")
    return {"object": "list", "data": [{"id": model, "object": "model", "created": 0, "owned_by": "intentseal"}] if model else []}


async def _complete(request: Request, agent_id: str, p: Principal) -> Any:
    require_agent(request, p, agent_id)
    try:
        body = await request.json()
    except ValueError:
        body = None
    messages_ok = isinstance(body, dict) and isinstance(body.get("messages", []), list) \
        and all(isinstance(m, dict) for m in body.get("messages", []))
    if not messages_ok:
        return JSONResponse({"error": {"message": "body must be a chat completion request with a messages list",
                                       "type": "invalid_request_error"}}, status_code=422)
    engine = request.app.state.engine
    messages: list[dict[str, Any]] = [dict(m) for m in body.get("messages", [])]
    # Retrieved documents are not part of the conversation's identity or of the user's task (D-050).
    opening = [(m.get("role"), _without_context(_text(m.get("content"))))
               for m in messages if m.get("role") in ("system", "user")][:2]
    first_user = next((_without_context(_text(m.get("content"))) for m in messages if m.get("role") == "user"), "")
    session_id = _session(request, agent_id, opening, first_user)

    call_names: dict[str, str] = {}
    findings: list[dict[str, Any]] = []
    for m in messages:
        for tc in m.get("tool_calls") or []:
            call_names[tc.get("id", "")] = (tc.get("function") or {}).get("name", "tool")
        role = m.get("role")
        if role == "tool":
            source = f"tool:{call_names.get(m.get('tool_call_id', ''), 'tool')}"
            replacement = await _inspect(request, agent_id, session_id, source, _text(m.get("content")), findings)
            if replacement is not None:
                m["content"] = replacement
        elif role in ("user", "system", "developer"):
            await _inspect_openai_message(request, agent_id, session_id, m, "user" if role == "user" else None, findings)

    try:
        up, client_key = _upstream(request, agent_id)
    except ProviderError as e:
        return _openai_error(e)
    if up.provider:   # the agent's own provider and model; its other request fields pass through unchanged
        req = ChatRequest(messages=messages, tools=body.get("tools"), tool_choice=body.get("tool_choice"),
                          temperature=body.get("temperature"), max_tokens=body.get("max_tokens"),
                          extra={k: v for k, v in body.items() if k not in _OPENAI_HANDLED})
        model = up.model or body.get("model") or ""
    else:             # the gateway's own free-tier role (demo, evaluation)
        req = ChatRequest(messages=messages, tools=body.get("tools"), tool_choice=body.get("tool_choice"),
                          temperature=body.get("temperature", 0), max_tokens=body.get("max_tokens") or 1024)
        model = ""
    security = {"session_id": session_id, "inbound": findings}
    try:
        if body.get("stream"):
            upstream = (await engine.router.call_upstream(up.provider, model, req, client_key, stream=True) if up.provider
                        else await engine.router.stream_chat(PROXY_ROLE, req))
            include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
            async def call_once(r: ChatRequest) -> Any:
                return (await engine.router.call_upstream(up.provider, model, r, client_key) if up.provider
                        else await engine.router.chat(PROXY_ROLE, r))

            async def resume(content: str, stopped: list[tuple[dict[str, Any], str]]) -> Any:
                return await _continue_after_stop(engine, call_once, req, content, stopped, agent_id, session_id)
            return StreamingResponse(_stream(engine, upstream, agent_id, session_id, security, include_usage,
                                             model if up.provider else None, resume), media_type="text/event-stream")
        res = (await engine.router.call_upstream(up.provider, model, req, client_key) if up.provider
               else await engine.router.chat(PROXY_ROLE, req))
    except (AllProvidersFailed, ProviderError) as e:
        return _openai_error(e)

    async def call(r: ChatRequest) -> Any:
        return (await engine.router.call_upstream(up.provider, model, r, client_key) if up.provider
                else await engine.router.chat(PROXY_ROLE, r))

    kept, stopped = await _check_tool_calls_detailed(engine, res.tool_calls, agent_id, session_id)
    content, blocked = res.content or "", len(stopped)
    if stopped and not kept:
        try:
            content, kept, stopped, res2 = await _continue_after_stop(engine, call, req, content, stopped, agent_id, session_id)
            blocked += len(stopped)
            res = res2 or res
        except (AllProvidersFailed, ProviderError):
            pass   # no second answer: the notices below tell the agent what was stopped
    notices = [n for _, n in stopped]
    if content:   # protected data in the answer is redacted, or the answer withheld
        content = (await engine.check_output(content, agent_id, session_id=session_id)).cleaned_content
    if notices:
        content = (content + "\n\n" if content else "") + "\n".join(notices)
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if kept:
        message["tool_calls"] = kept
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24], "object": "chat.completion", "created": int(time.time()),
        "model": res.model if up.provider else f"{res.provider}/{res.model}",
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if kept else "stop"}],
        "usage": {"prompt_tokens": res.input_tokens, "completion_tokens": res.output_tokens,
                  "total_tokens": res.input_tokens + res.output_tokens},
        "x_security": {**security, "blocked_tool_calls": blocked},
    }


class _AnswerGuard:
    """The model's answer on its way out: protected data (canaries, registered secrets, secret formats, also encoded)
    must not reach the client. Streamed text is held back (twice the longest protected value, at least 96 characters) and scanned before it is released, so a
    protected value is caught before it is fully sent; the final answer is checked (and logged) by the engine."""

    def __init__(self, engine: Any, agent_id: str, session_id: str) -> None:
        self.engine, self.agent_id, self.session_id = engine, agent_id, session_id
        policy = engine.policy_loader(agent_id)
        self.enforce = policy.mode == "enforce"
        # enough to hold a whole protected value in its longest form (hex doubles it) before any of it is sent
        longest = max((len(v) for v in [*policy.canary_values(), *getattr(engine, "_secrets", [])]), default=0)
        self.HOLD = max(96, 2 * longest + 8)
        self.full, self.pending, self.withheld = "", "", False

    def feed(self, text: str) -> str:
        """Text that is safe to send now."""
        if self.withheld:
            return ""
        self.full += text
        self.pending += text
        if len(self.pending) <= self.HOLD:
            return ""
        return self._release(keep=self.HOLD)

    def flush(self) -> str:
        """End of a text block: everything still held, if it is clean."""
        return "" if self.withheld else self._release(keep=0)

    def _release(self, keep: int) -> str:
        if self.enforce and self.engine.output_hits(self.full, self.agent_id):
            self.withheld, self.pending = True, ""
            return ""
        cut = len(self.pending) - keep
        out, self.pending = self.pending[:cut], self.pending[cut:]
        return out

    async def finish(self) -> str | None:
        """Check (and log) the whole answer; the notice to append when part of it was withheld."""
        if not self.full:
            return None
        r = await self.engine.check_output(self.full, self.agent_id, session_id=self.session_id)
        if self.withheld or r.enforced != Decision.ALLOW:
            return "[Part of this answer was withheld by the security policy: it contained protected data]"
        return None


def _sse(obj: Any) -> str:
    return f"data: {json.dumps(obj)}\n\n"


async def _stream(engine: Any, upstream: Any, agent_id: str, session_id: str, security: dict[str, Any],
                  include_usage: bool, model_name: str | None = None, resume: Any = None):
    """PRX-6: text deltas stream through as they arrive; tool-call deltas are buffered until the upstream stream
    ends, assembled, checked, and only then released (allowed calls) or replaced by a notice (blocked calls).
    If the upstream fails part-way, buffered tool calls are never released."""
    cid, created = "chatcmpl-" + uuid.uuid4().hex[:24], int(time.time())
    model = model_name or f"{upstream.provider}/{upstream.model}"

    def chunk(delta: dict[str, Any], finish: str | None = None, **extra: Any) -> str:
        return _sse({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra})

    calls: dict[int, dict[str, Any]] = {}
    finish, started = None, False
    answer = _AnswerGuard(engine, agent_id, session_id)
    try:
        async for c in upstream.chunks():
            for choice in c.get("choices") or []:
                delta = choice.get("delta") or {}
                for pos, tc in enumerate(delta.get("tool_calls") or []):
                    i = tc.get("index", pos)
                    slot = calls.setdefault(i, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["function"]["name"] += fn.get("name") or ""
                    slot["function"]["arguments"] += fn.get("arguments") or ""
                if delta.get("content"):
                    out = answer.feed(delta["content"])
                    if out:
                        yield chunk({**({} if started else {"role": "assistant"}), "content": out})
                        started = True
                finish = choice.get("finish_reason") or finish
    except Exception as e:   # upstream failed mid-stream: say so; buffered tool calls are dropped, never released
        yield _sse({"error": {"message": "upstream stream failed", "type": "upstream_error", "detail": type(e).__name__}})
        yield "data: [DONE]\n\n"
        return
    rest = answer.flush()
    if rest:
        yield chunk({**({} if started else {"role": "assistant"}), "content": rest})
        started = True
    withheld = await answer.finish()
    ordered = [calls[i] for i in sorted(calls)]
    for tc in ordered:
        tc["id"] = tc["id"] or f"call_{uuid.uuid4().hex[:12]}"
    kept, stopped = await _check_tool_calls_detailed(engine, ordered, agent_id, session_id)
    blocked = len(stopped)
    if stopped and not kept and resume is not None and not withheld:
        try:   # every call was stopped: the model is told and continues (D-062); its next answer is checked as well
            more, kept, stopped, _ = await resume("", stopped)
            blocked += len(stopped)
            if more:
                more = (await engine.check_output(more, agent_id, session_id=session_id)).cleaned_content
                yield chunk({**({} if started else {"role": "assistant"}), "content": ("\n\n" if started else "") + more})
                started = True
        except (AllProvidersFailed, ProviderError):
            pass
    notices = ([withheld] if withheld else []) + [n for _, n in stopped]
    if notices:
        text = ("\n\n" if started else "") + "\n".join(notices)
        yield chunk({**({} if started else {"role": "assistant"}), "content": text})
        started = True
    if kept:
        yield chunk({**({} if started else {"role": "assistant"}),
                     "tool_calls": [{**tc, "index": i} for i, tc in enumerate(kept)]})
    final = "tool_calls" if kept else ("stop" if finish in (None, "tool_calls") else finish)
    yield chunk({}, final, x_security={**security, "blocked_tool_calls": blocked, "answer_withheld": bool(withheld)})
    if include_usage:
        h = upstream.handle
        yield _sse({"id": cid, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [],
                    "usage": {"prompt_tokens": h.input_tokens, "completion_tokens": h.output_tokens,
                              "total_tokens": h.input_tokens + h.output_tokens}})
    yield "data: [DONE]\n\n"


# ---- Anthropic Messages ------------------------------------------------------------------------------------

def _anthropic_error(e: Exception) -> JSONResponse:
    kind = getattr(e, "kind", "upstream_unavailable" if isinstance(e, AllProvidersFailed) else "error")
    status = 503 if isinstance(e, AllProvidersFailed) else (getattr(e, "http_status", None) or _STATUS.get(kind, 502))
    etype = {429: "rate_limit_error", 401: "authentication_error", 403: "permission_error", 404: "not_found_error",
             400: "invalid_request_error"}.get(status, "api_error")
    headers = {"Retry-After": str(int(e.retry_after_s))} if getattr(e, "retry_after_s", None) else None
    return JSONResponse({"type": "error", "error": {"type": etype, "message": str(e)[:300]}}, status_code=status,
                        headers=headers)


@router.post("/proxy/{agent_id}/anthropic/v1/messages")
async def anthropic_messages(agent_id: str, request: Request, p: Principal = require("project")) -> Any:
    """Anthropic Messages API, protected: point the Anthropic SDK's base URL at /proxy/<agent>/anthropic."""
    from intentseal_core.llm import anthropic_adapter as fmt

    require_agent(request, p, agent_id)
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list) \
            or not all(isinstance(m, dict) for m in body["messages"]) or not body.get("model"):
        return JSONResponse({"type": "error", "error": {"type": "invalid_request_error",
                                                        "message": "body must be a Messages request with model and messages"}},
                            status_code=400)
    engine = request.app.state.engine
    body = json.loads(json.dumps(body))   # private copy: inspection rewrites content in place
    first_user = next((_without_context(_text(m["content"]) if isinstance(m.get("content"), list) else str(m.get("content") or ""))
                       for m in body["messages"] if m.get("role") == "user"), "")
    system = body.get("system")
    opening = [_without_context(_text(system) if isinstance(system, list) else str(system or "")), first_user]
    session_id = _session(request, agent_id, opening, first_user)

    findings: list[dict[str, Any]] = []
    # The operator's system prompt is trusted; retrieved documents placed in it are not (D-050).
    if isinstance(system, str):
        rep = await _inspect_text(request, agent_id, session_id, None, system, findings)
        if rep is not None:
            body["system"] = rep
    elif isinstance(system, list):
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text":
                rep = await _inspect_text(request, agent_id, session_id, None, block.get("text", ""), findings)
                if rep is not None:
                    block["text"] = rep
    tool_names: dict[str, str] = {}
    for m in body["messages"]:
        content = m.get("content")
        if m.get("role") == "assistant":
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "tool_use":
                    tool_names[block.get("id", "")] = block.get("name", "tool")
            continue
        if isinstance(content, str):
            replacement = await _inspect_text(request, agent_id, session_id, "user", content, findings)
            if replacement is not None:
                m["content"] = replacement
            continue
        blocks = []
        for block in content or []:
            kind = block.get("type") if isinstance(block, dict) else None
            if kind == "text":
                replacement = await _inspect_text(request, agent_id, session_id, "user", block.get("text", ""), findings)
                if replacement is not None:
                    block["text"] = replacement
            elif kind == "tool_result":
                c = block.get("content")
                text = _text(c) if isinstance(c, list) else ("" if c is None else str(c))
                source = f"tool:{tool_names.get(block.get('tool_use_id', ''), 'tool')}"
                replacement = await _inspect(request, agent_id, session_id, source, text, findings)
                if replacement is not None:
                    block["content"] = replacement
            elif kind in ("document", "image"):
                # Documents (citations, PDFs) and images reach the model as content: inspect what it would read.
                src = block.get("source") or {}
                f = _file_bytes(source=src)
                text = (str(src.get("data") or "") if src.get("type") == "text" else
                        _text(src.get("content")) if src.get("type") == "content" else "")
                if f is not None or text:
                    file_source = "image" if kind == "image" else "document"
                    replacement = await _inspect(request, agent_id, session_id, file_source, text, findings,
                                                 data=f[0] if f else None, filename=f[1] if f else None)
                    if replacement is not None:
                        block = {"type": "text", "text": replacement}
            blocks.append(block)
        m["content"] = blocks

    try:
        up, client_key = _upstream(request, agent_id)
        provider = engine.router.settings.providers.get(up.provider) if up.provider else None
        if provider is not None and provider.type == "anthropic":   # native: every Messages feature passes through
            body["model"] = up.model or body["model"]
            stream = bool(body.pop("stream", False))
            key = _key(engine, provider, client_key)
            out = await get_adapter("anthropic").messages(provider, key, body, stream=stream)
            if stream:
                return StreamingResponse(_anthropic_stream(engine, out, agent_id, session_id, findings, provider.name,
                                                           body["model"], key.alias), media_type="text/event-stream")
            _record(engine, provider.name, body["model"], key.alias, out.get("usage") or {})
            return await _anthropic_checked(engine, out, agent_id, session_id, findings)
        # Any other upstream (or the gateway's own role): translate through the router's shape.
        req = fmt.from_messages_request(body)
        model = up.model or body["model"]
        res = (await engine.router.call_upstream(up.provider, model, req, client_key) if up.provider
               else await engine.router.chat(PROXY_ROLE, req))
    except (AllProvidersFailed, ProviderError) as e:
        return _anthropic_error(e)
    msg = fmt.to_message(res.content, res.tool_calls, res.finish_reason, model if up.provider else body["model"],
                         res.input_tokens, res.output_tokens)
    checked = await _anthropic_checked(engine, msg, agent_id, session_id, findings)
    if body.get("stream"):
        return StreamingResponse(_anthropic_events(checked), media_type="text/event-stream")
    return checked


def _key(engine: Any, provider: Any, client_key: str | None) -> ApiKey:
    if client_key:
        registry.register(client_key)
        return ApiKey(f"{provider.name}#client-{hashlib.sha256(client_key.encode()).hexdigest()[:8]}", client_key)
    keys = engine.router.healthy_keys(provider)
    if not keys:
        raise ProviderError("auth_failed", f"no key configured on the gateway for upstream '{provider.name}'", 401)
    return keys[0]


def _record(engine: Any, provider: str, model: str, alias: str, usage: dict[str, Any]) -> None:
    engine.router.store.record_usage(role=PROXY_ROLE, provider=provider, key_alias=alias, model=model, status="ok",
                                     http_status=200, input_tokens=int(usage.get("input_tokens") or 0),
                                     output_tokens=int(usage.get("output_tokens") or 0))


async def _anthropic_checked(engine: Any, msg: dict[str, Any], agent_id: str, session_id: str,
                             findings: list[dict[str, Any]]) -> dict[str, Any]:
    """PRX-3 on a Messages response: blocked tool_use blocks removed (a notice text block added), pinned fields
    rewritten in the released ones, stop_reason corrected when no tool call is left."""
    content, notices = [], []
    for block in msg.get("content") or []:
        if block.get("type") == "text" and block.get("text"):   # protected data in the answer: redacted or withheld
            r = await engine.check_output(block["text"], agent_id, session_id=session_id)
            content.append({**block, "text": r.cleaned_content})
            continue
        if block.get("type") != "tool_use":
            content.append(block)
            continue
        final, notice = await _check(engine, block.get("name", ""), block.get("input") or {}, agent_id, session_id)
        if notice:
            notices.append(notice)
        else:
            content.append({**block, "input": final})
    if notices:
        content.append({"type": "text", "text": "\n".join(notices)})
    out = {**msg, "content": content}
    if msg.get("stop_reason") == "tool_use" and not any(b.get("type") == "tool_use" for b in content):
        out["stop_reason"] = "end_turn"
    out["x_security"] = {"session_id": session_id, "inbound": findings, "blocked_tool_calls": len(notices)}
    return out


def _event(kind: str, data: dict[str, Any]) -> str:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n"


async def _anthropic_events(msg: dict[str, Any]):
    """A checked Message as a Messages-API event stream (used when the upstream answered in one piece)."""
    yield _event("message_start", {"message": {**{k: v for k, v in msg.items() if k not in ("content", "x_security")},
                                               "content": [], "stop_reason": None}})
    for i, block in enumerate(msg.get("content") or []):
        if block.get("type") == "tool_use":
            yield _event("content_block_start", {"index": i, "content_block": {**block, "input": {}}})
            yield _event("content_block_delta", {"index": i, "delta": {"type": "input_json_delta",
                                                                       "partial_json": json.dumps(block.get("input") or {})}})
        else:
            yield _event("content_block_start", {"index": i, "content_block": {**block, "text": ""}})
            yield _event("content_block_delta", {"index": i, "delta": {"type": "text_delta", "text": block.get("text", "")}})
        yield _event("content_block_stop", {"index": i})
    yield _event("message_delta", {"delta": {"stop_reason": msg.get("stop_reason"), "stop_sequence": None},
                                   "usage": {"output_tokens": (msg.get("usage") or {}).get("output_tokens", 0)}})
    yield _event("message_stop", {})


async def _anthropic_stream(engine: Any, events: Any, agent_id: str, session_id: str, findings: list[dict[str, Any]],
                            provider: str, model: str, alias: str):
    """PRX-6 for the native Messages stream: text, thinking and other blocks stream through (re-indexed);
    tool_use blocks are buffered until the upstream message ends, checked, then emitted whole (allowed, pinned
    fields rewritten) or replaced by a notice text block. A failed upstream never releases a buffered call."""
    out_index, index_map = 0, {}
    tools: dict[int, dict[str, Any]] = {}
    stop_delta: dict[str, Any] | None = None
    usage = {"input_tokens": 0, "output_tokens": 0}
    answer = _AnswerGuard(engine, agent_id, session_id)
    try:
        async for event in events:
            e = event.to_dict() if hasattr(event, "to_dict") else dict(event)
            kind = e.get("type")
            if kind == "message_start":
                usage["input_tokens"] = int(((e.get("message") or {}).get("usage") or {}).get("input_tokens") or 0)
                yield _event(kind, {k: v for k, v in e.items() if k != "type"})
            elif kind == "content_block_start":
                block = e.get("content_block") or {}
                if block.get("type") == "tool_use":
                    tools[e["index"]] = {**block, "json": ""}
                else:
                    index_map[e["index"]] = out_index
                    yield _event(kind, {**e, "index": out_index})
                    out_index += 1
            elif kind == "content_block_delta":
                d = e.get("delta") or {}
                if e.get("index") in tools:
                    tools[e["index"]]["json"] += d.get("partial_json", "")
                elif d.get("type") == "text_delta":
                    out = answer.feed(d.get("text", ""))
                    if out:
                        yield _event(kind, {**e, "index": index_map.get(e.get("index"), e.get("index")),
                                            "delta": {**d, "text": out}})
                else:
                    yield _event(kind, {**e, "index": index_map.get(e.get("index"), e.get("index"))})
            elif kind == "content_block_stop":
                if e.get("index") not in tools:
                    idx = index_map.get(e.get("index"), e.get("index"))
                    rest = answer.flush()
                    if rest:
                        yield _event("content_block_delta", {"index": idx, "delta": {"type": "text_delta", "text": rest}})
                    yield _event(kind, {**e, "index": idx})
            elif kind == "message_delta":
                stop_delta = e
                usage["output_tokens"] = int((e.get("usage") or {}).get("output_tokens") or 0)
            elif kind == "message_stop":
                break
            else:   # ping and future event types pass through
                yield _event(kind, {k: v for k, v in e.items() if k != "type"})
    except Exception as exc:
        yield _event("error", {"error": {"type": "api_error", "message": f"upstream stream failed ({type(exc).__name__})"}})
        return
    released, notices = 0, []
    withheld = await answer.finish()
    if withheld:
        notices.append(withheld)
    for i in sorted(tools):
        t = tools[i]
        try:
            args = json.loads(t["json"] or "{}")
        except json.JSONDecodeError:
            args = {"_raw": t["json"]}
        final, notice = await _check(engine, t.get("name", ""), args, agent_id, session_id)
        if notice:
            notices.append(notice)
            continue
        block = {"type": "tool_use", "id": t.get("id"), "name": t.get("name"), "input": {}}
        yield _event("content_block_start", {"index": out_index, "content_block": block})
        yield _event("content_block_delta", {"index": out_index, "delta": {"type": "input_json_delta",
                                                                           "partial_json": json.dumps(final)}})
        yield _event("content_block_stop", {"index": out_index})
        out_index += 1
        released += 1
    if notices:
        yield _event("content_block_start", {"index": out_index, "content_block": {"type": "text", "text": ""}})
        yield _event("content_block_delta", {"index": out_index, "delta": {"type": "text_delta", "text": "\n".join(notices)}})
        yield _event("content_block_stop", {"index": out_index})
    delta = dict((stop_delta or {}).get("delta") or {"stop_reason": "end_turn"})
    if delta.get("stop_reason") == "tool_use" and not released:
        delta["stop_reason"] = "end_turn"
    yield _event("message_delta", {"delta": delta, "usage": (stop_delta or {}).get("usage") or {},
                                   "x_security": {"session_id": session_id, "inbound": findings,
                                                  "blocked_tool_calls": len(notices)}})
    yield _event("message_stop", {})
    _record(engine, provider, model, alias, usage)
