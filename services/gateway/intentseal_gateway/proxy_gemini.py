"""LLM proxy, Google Gemini format (PRX-1…PRX-6): protect an agent that uses the Gemini API natively.

    POST /proxy/<agent>/gemini/v1beta/models/<model>:generateContent
    POST /proxy/<agent>/gemini/v1beta/models/<model>:streamGenerateContent?alt=sse

Point the Google Gen AI SDK at the gateway (`http_options={"base_url": "<gateway>/proxy/<agent>/gemini"}`) with the
IntentSeal key as its API key. The same checkpoints as the OpenAI and Anthropic formats: user text, retrieved
documents in the system instruction or user text (`<context>` tags), inline images and PDFs and function responses
(tool results) are inspected before the model sees them; function calls are checked before the agent sees them;
protected data in the answer is redacted or withheld. The agent's upstream must be the `gemini` provider (native
pass-through: every Gemini request field is forwarded unchanged after inspection).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from intentseal_core.llm.adapters import ProviderError, classify_http_error

from .proxy import (_AnswerGuard, _check, _file_bytes, _inspect, _inspect_text, _key, _session, _upstream,
                    _without_context)
from .security import Principal, require
from .tenancy import require_agent

router = APIRouter(tags=["proxy"])


def _error(status: int, message: str, kind: str = "INVALID_ARGUMENT") -> JSONResponse:
    return JSONResponse({"error": {"code": status, "message": message[:300], "status": kind}}, status_code=status)


def _texts(content: dict[str, Any] | None) -> str:
    return "\n".join(p.get("text", "") for p in (content or {}).get("parts") or [] if isinstance(p, dict) and p.get("text"))


@router.post("/proxy/{agent_id}/gemini/v1beta/models/{model_action}")
async def gemini_generate(agent_id: str, model_action: str, request: Request, p: Principal = require("project")) -> Any:
    model, _, action = model_action.partition(":")
    if action not in ("generateContent", "streamGenerateContent"):
        return _error(404, f"unsupported method '{action}': generateContent and streamGenerateContent are proxied", "NOT_FOUND")
    require_agent(request, p, agent_id)
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("contents"), list):
        return _error(400, "body must be a generateContent request with contents")
    engine = request.app.state.engine
    body = json.loads(json.dumps(body))   # private copy: inspection rewrites it
    system = body.get("systemInstruction") or body.get("system_instruction")
    first_user = next((_without_context(_texts(c)) for c in body["contents"] if (c or {}).get("role", "user") == "user"), "")
    session_id = _session(request, agent_id, [_without_context(_texts(system)), first_user], first_user)

    findings: list[dict[str, Any]] = []
    for part in (system or {}).get("parts") or []:   # the operator's instruction: only retrieved documents are checked
        if isinstance(part, dict) and part.get("text"):
            rep = await _inspect_text(request, agent_id, session_id, None, part["text"], findings)
            if rep is not None:
                part["text"] = rep
    for content in body["contents"]:
        if not isinstance(content, dict) or content.get("role") == "model":
            continue
        parts = []
        for part in content.get("parts") or []:
            if not isinstance(part, dict):
                parts.append(part)
                continue
            if part.get("text"):
                rep = await _inspect_text(request, agent_id, session_id, "user", part["text"], findings)
                parts.append(part if rep is None else {**part, "text": rep})
            elif "functionResponse" in part or "function_response" in part:
                fr = part.get("functionResponse") or part.get("function_response") or {}
                text = json.dumps(fr.get("response"), ensure_ascii=False) if not isinstance(fr.get("response"), str) else fr["response"]
                rep = await _inspect(request, agent_id, session_id, f"tool:{fr.get('name', 'tool')}", text, findings)
                parts.append(part if rep is None else {"functionResponse": {**fr, "response": {"content": rep}}})
            elif "inlineData" in part or "inline_data" in part:
                blob = part.get("inlineData") or part.get("inline_data") or {}
                f = _file_bytes(source={"type": "base64", "data": blob.get("data"),
                                        "media_type": blob.get("mimeType") or blob.get("mime_type")})
                if f is None:
                    parts.append(part)
                    continue
                kind = "image" if str(blob.get("mimeType") or blob.get("mime_type") or "").startswith("image/") else "document"
                rep = await _inspect(request, agent_id, session_id, kind, "", findings, data=f[0], filename=f[1])
                parts.append(part if rep is None else {"text": rep})
            else:
                parts.append(part)
        content["parts"] = parts

    try:
        up, client_key = _upstream(request, agent_id)
        provider = engine.router.settings.providers.get(up.provider) if up.provider else None
        if provider is None or up.provider != "gemini":
            return _error(400, "the Gemini format needs the agent's upstream provider to be 'gemini' (policy upstream); "
                               "other providers: use the OpenAI or Anthropic format", "FAILED_PRECONDITION")
        key = _key(engine, provider, client_key)
    except ProviderError as e:
        return _error(e.http_status or 401, str(e), "UNAUTHENTICATED")
    base = provider.base_url.removesuffix("/openai").rstrip("/")
    url = f"{base}/models/{up.model or model}:{action}" + ("?alt=sse" if action == "streamGenerateContent" else "")
    headers = {"x-goog-api-key": key.value, "Content-Type": "application/json"}
    client: httpx.AsyncClient = engine.router.client
    timeout = httpx.Timeout(provider.timeout_s, connect=provider.connect_timeout_s)
    if action == "streamGenerateContent":
        req = client.build_request("POST", url, headers=headers, json=body, timeout=timeout)
        try:
            resp = await client.send(req, stream=True)
        except httpx.HTTPError as e:
            return _error(502, f"upstream transport: {type(e).__name__}", "UNAVAILABLE")
        if resp.status_code >= 400:
            await resp.aread()
            await resp.aclose()
            err = classify_http_error(resp)
            return _error(resp.status_code, str(err), "UNAVAILABLE")
        return StreamingResponse(_stream(engine, resp, agent_id, session_id, findings), media_type="text/event-stream")
    try:
        resp = await client.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.HTTPError as e:
        return _error(502, f"upstream transport: {type(e).__name__}", "UNAVAILABLE")
    if resp.status_code >= 400:
        return JSONResponse(_safe_json(resp), status_code=resp.status_code)
    out = await _checked(engine, resp.json(), agent_id, session_id)
    out["x_security"] = {"session_id": session_id, "inbound": findings}
    return out


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return {"error": {"code": resp.status_code, "message": resp.text[:300], "status": "UNAVAILABLE"}}


async def _checked(engine: Any, data: dict[str, Any], agent_id: str, session_id: str) -> dict[str, Any]:
    """Function calls checked (blocked ones replaced by a notice, pinned fields rewritten); answer text checked."""
    for cand in data.get("candidates") or []:
        content = cand.get("content") or {}
        parts, notices = [], []
        for part in content.get("parts") or []:
            call = part.get("functionCall") or part.get("function_call")
            if call:
                final, notice = await _check(engine, call.get("name", ""), call.get("args") or {}, agent_id, session_id)
                if notice:
                    notices.append(notice)
                else:
                    parts.append({**part, "functionCall": {**call, "args": final}})
            elif part.get("text") and not part.get("thought"):
                r = await engine.check_output(part["text"], agent_id, session_id=session_id)
                parts.append({**part, "text": r.cleaned_content})
            else:
                parts.append(part)
        if notices:
            parts.append({"text": "\n".join(notices)})
        content["parts"] = parts
        cand["content"] = content
    return data


async def _stream(engine: Any, resp: httpx.Response, agent_id: str, session_id: str, findings: list[dict[str, Any]]):
    """Text streams through (held back and scanned for protected data); function calls are buffered until the
    upstream stream ends, then checked and released whole, or replaced by a notice."""
    answer = _AnswerGuard(engine, agent_id, session_id)
    calls: list[dict[str, Any]] = []
    last: dict[str, Any] = {}

    def event(parts: list[dict[str, Any]], extra: dict[str, Any] | None = None, **top: Any) -> str:
        chunk = {**{k: v for k, v in last.items() if k != "candidates"},
                 "candidates": [{"content": {"role": "model", "parts": parts}, "index": 0, **(extra or {})}], **top}
        return f"data: {json.dumps(chunk)}\n\n"

    finish: str | None = None
    try:
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            try:
                chunk = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            last = chunk
            for cand in chunk.get("candidates") or []:
                finish = cand.get("finishReason") or finish
                out_parts = []
                for part in (cand.get("content") or {}).get("parts") or []:
                    call = part.get("functionCall") or part.get("function_call")
                    if call:
                        calls.append(call)
                    elif part.get("text") and not part.get("thought"):
                        text = answer.feed(part["text"])
                        if text:
                            out_parts.append({"text": text})
                    else:
                        out_parts.append(part)
                if out_parts:
                    yield event(out_parts)
    except httpx.HTTPError as e:
        yield f"data: {json.dumps({'error': {'code': 502, 'message': f'upstream stream failed ({type(e).__name__})'}})}\n\n"
        return
    finally:
        await resp.aclose()
    tail = answer.flush()
    parts: list[dict[str, Any]] = [{"text": tail}] if tail else []
    withheld = await answer.finish()
    if withheld:
        parts.append({"text": withheld})
    notices = []
    for call in calls:
        final, notice = await _check(engine, call.get("name", ""), call.get("args") or {}, agent_id, session_id)
        if notice:
            notices.append(notice)
        else:
            parts.append({"functionCall": {**call, "args": final}})
    if notices:
        parts.append({"text": "\n".join(notices)})
    yield event(parts, {"finishReason": finish or "STOP"},
                x_security={"session_id": session_id, "inbound": findings, "blocked_tool_calls": len(notices)})
