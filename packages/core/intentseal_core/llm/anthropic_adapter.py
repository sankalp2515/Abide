"""Native Anthropic (Claude) provider, through the official `anthropic` SDK (optional extra
`intentseal-core[anthropic]`).

Two uses:
- Router adapter (`type: anthropic` in config/providers.yaml): the router speaks the OpenAI chat shape internally
  (ChatRequest / AdapterResponse / OpenAI-style stream chunks); this adapter translates to and from the Messages API,
  so any role (judge, agent, proxy upstream) can run on Claude.
- Native pass-through for the gateway's Anthropic-compatible proxy endpoint (`messages()`): the client's Messages
  request is forwarded as is (after inspection), so Claude features the router shape does not model (thinking,
  cache control, documents, images) keep working.

The SDK's own retries are off (`max_retries=0`): the router owns retries, failover and circuit breaking.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, AsyncIterator

from ..config import ProviderConfig
from ..secrets import ApiKey
from .adapters import AdapterResponse, ChatRequest, ProviderError

FINISH = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls", "max_tokens": "length",
          "refusal": "content_filter", "pause_turn": "stop"}
_clients: dict[str, Any] = {}


def _sdk():
    try:
        import anthropic
    except ImportError as e:   # pragma: no cover - depends on the optional extra
        raise ProviderError("bad_request", "the anthropic provider needs: pip install 'intentseal-core[anthropic]'") from e
    return anthropic


def _error(e: Exception) -> ProviderError:
    a = _sdk()
    status = getattr(e, "status_code", None)
    msg = f"{type(e).__name__}: {str(e)[:300]}"
    if isinstance(e, a.APITimeoutError):
        return ProviderError("timeout", msg)
    if isinstance(e, a.APIConnectionError):
        return ProviderError("server_error", msg)
    if isinstance(e, a.RateLimitError):
        retry = e.response.headers.get("retry-after") if getattr(e, "response", None) is not None else None
        return ProviderError("rate_limited", msg, 429, float(retry) if retry and retry.replace(".", "", 1).isdigit() else None)
    if isinstance(e, (a.AuthenticationError, a.PermissionDeniedError)):
        return ProviderError("auth_failed", msg, status)
    if isinstance(e, a.NotFoundError):
        return ProviderError("not_found", msg, status)
    if isinstance(e, a.APIStatusError):
        if status is not None and status >= 500:
            return ProviderError("server_error", msg, status)
        return ProviderError("bad_request", msg, status)
    return ProviderError("error", msg)


def _text(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") in ("text", None))
    return "" if content is None else str(content)


def _image(url: str) -> dict[str, Any]:
    if url.startswith("data:") and ";base64," in url:
        media, data = url[5:].split(";base64,", 1)
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def to_messages_request(req: ChatRequest, model: str, sampling: bool = False) -> dict[str, Any]:
    """OpenAI chat shape -> Anthropic Messages request body."""
    system: list[str] = []
    msgs: list[dict[str, Any]] = []

    def add(role: str, blocks: list[dict[str, Any]]) -> None:
        if msgs and msgs[-1]["role"] == role:     # the Messages API alternates roles: merge neighbours
            msgs[-1]["content"].extend(blocks)
        else:
            msgs.append({"role": role, "content": list(blocks)})

    for m in req.messages:
        role, content = m.get("role"), m.get("content")
        if role in ("system", "developer"):
            system.append(_text(content))
            continue
        if role == "tool":
            add("user", [{"type": "tool_result", "tool_use_id": m.get("tool_call_id") or "", "content": _text(content)}])
            continue
        blocks: list[dict[str, Any]] = []
        if isinstance(content, list):
            for part in content:
                if part.get("type") == "text":
                    blocks.append({"type": "text", "text": part.get("text", "")})
                elif part.get("type") == "image_url":
                    blocks.append(_image((part.get("image_url") or {}).get("url", "")))
        elif content:
            blocks.append({"type": "text", "text": str(content)})
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments")}
            blocks.append({"type": "tool_use", "id": tc.get("id") or "", "name": fn.get("name", ""), "input": args})
        if blocks:
            add("assistant" if role == "assistant" else "user", blocks)
    body: dict[str, Any] = {"model": model, "max_tokens": req.max_tokens or 4096, "messages": msgs}
    if system:
        body["system"] = "\n\n".join(s for s in system if s)
    if sampling and req.temperature is not None:   # current Claude models reject sampling parameters (400)
        body["temperature"] = req.temperature
    if req.tools:
        body["tools"] = [{"name": (t.get("function") or {}).get("name", ""),
                          "description": (t.get("function") or {}).get("description", ""),
                          "input_schema": (t.get("function") or {}).get("parameters") or {"type": "object", "properties": {}}}
                         for t in req.tools if t.get("type", "function") == "function"]
        # Forced tool choice ("required" / a named function) is rejected by current Claude models: use auto.
        body["tool_choice"] = {"type": "none"} if req.tool_choice == "none" else {"type": "auto"}
    return body


def to_adapter_response(msg: dict[str, Any]) -> AdapterResponse:
    """Anthropic Message -> the router's OpenAI-shaped response."""
    texts, calls = [], []
    for block in msg.get("content") or []:
        if block.get("type") == "text":
            texts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            calls.append({"id": block.get("id"), "type": "function",
                          "function": {"name": block.get("name"), "arguments": json.dumps(block.get("input") or {})}})
    usage = msg.get("usage") or {}
    return AdapterResponse(content="".join(texts) or None, tool_calls=calls,
                           finish_reason=FINISH.get(msg.get("stop_reason") or "", msg.get("stop_reason")),
                           input_tokens=int(usage.get("input_tokens") or 0), output_tokens=int(usage.get("output_tokens") or 0),
                           raw=msg)


def from_messages_request(body: dict[str, Any]) -> ChatRequest:
    """Anthropic Messages request -> the router's OpenAI chat shape (for upstreams that are not Anthropic)."""
    msgs: list[dict[str, Any]] = []
    system = body.get("system")
    if system:
        msgs.append({"role": "system", "content": _text(system) if isinstance(system, list) else str(system)})
    for m in body.get("messages") or []:
        content = m.get("content")
        if isinstance(content, str):
            msgs.append({"role": m.get("role", "user"), "content": content})
            continue
        texts, calls = [], []
        for block in content or []:
            kind = block.get("type")
            if kind == "text":
                texts.append(block.get("text", ""))
            elif kind == "tool_use":
                calls.append({"id": block.get("id"), "type": "function",
                              "function": {"name": block.get("name"), "arguments": json.dumps(block.get("input") or {})}})
            elif kind == "tool_result":
                c = block.get("content")
                msgs.append({"role": "tool", "tool_call_id": block.get("tool_use_id"),
                             "content": _text(c) if isinstance(c, list) else ("" if c is None else str(c))})
        if m.get("role") == "assistant":
            msgs.append({"role": "assistant", "content": "".join(texts) or None, **({"tool_calls": calls} if calls else {})})
        elif texts:
            msgs.append({"role": "user", "content": "\n".join(texts)})
    tools = [{"type": "function", "function": {"name": t.get("name"), "description": t.get("description", ""),
                                               "parameters": t.get("input_schema") or {"type": "object"}}}
             for t in body.get("tools") or [] if t.get("name")]
    choice = (body.get("tool_choice") or {}).get("type")
    return ChatRequest(messages=msgs, tools=tools or None, tool_choice="none" if choice == "none" else None,
                       temperature=body.get("temperature"), max_tokens=body.get("max_tokens"))


def to_message(content: str | None, tool_calls: list[dict[str, Any]], finish: str | None, model: str,
               input_tokens: int = 0, output_tokens: int = 0) -> dict[str, Any]:
    """The router's OpenAI-shaped result -> an Anthropic Message (for the Anthropic-compatible proxy endpoint)."""
    blocks: list[dict[str, Any]] = [{"type": "text", "text": content}] if content else []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"_raw": fn.get("arguments")}
        blocks.append({"type": "tool_use", "id": tc.get("id") or "toolu_" + hashlib.sha256(json.dumps(fn).encode()).hexdigest()[:20],
                       "name": fn.get("name", ""), "input": args})
    stop = "tool_use" if tool_calls else {"length": "max_tokens"}.get(finish or "", "end_turn")
    return {"id": "msg_" + hashlib.sha256(json.dumps(blocks).encode()).hexdigest()[:24], "type": "message",
            "role": "assistant", "model": model, "content": blocks, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}}


class AnthropicStreamHandle:
    """Anthropic stream events translated to OpenAI-style chunks (same interface as ChatStreamHandle)."""

    def __init__(self, events: Any) -> None:
        self.events = events
        self.input_tokens = self.output_tokens = 0
        self.finish_reason: str | None = None

    async def chunks(self) -> AsyncIterator[dict[str, Any]]:
        tool_index: dict[int, int] = {}
        try:
            async for event in self.events:
                e = event.to_dict() if hasattr(event, "to_dict") else dict(event)
                kind = e.get("type")
                if kind == "message_start":
                    self.input_tokens = int(((e.get("message") or {}).get("usage") or {}).get("input_tokens") or 0)
                elif kind == "content_block_start" and (e.get("content_block") or {}).get("type") == "tool_use":
                    block = e["content_block"]
                    i = tool_index.setdefault(e.get("index", 0), len(tool_index))
                    yield {"choices": [{"index": 0, "delta": {"tool_calls": [{
                        "index": i, "id": block.get("id"), "type": "function",
                        "function": {"name": block.get("name", ""), "arguments": ""}}]}, "finish_reason": None}]}
                elif kind == "content_block_delta":
                    delta = e.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        yield {"choices": [{"index": 0, "delta": {"content": delta.get("text", "")}, "finish_reason": None}]}
                    elif delta.get("type") == "input_json_delta":
                        i = tool_index.setdefault(e.get("index", 0), len(tool_index))
                        yield {"choices": [{"index": 0, "delta": {"tool_calls": [{
                            "index": i, "function": {"arguments": delta.get("partial_json", "")}}]}, "finish_reason": None}]}
                elif kind == "message_delta":
                    stop = (e.get("delta") or {}).get("stop_reason")
                    self.finish_reason = FINISH.get(stop or "", stop)
                    self.output_tokens = int((e.get("usage") or {}).get("output_tokens") or self.output_tokens)
                    yield {"choices": [{"index": 0, "delta": {}, "finish_reason": self.finish_reason}]}
        except ProviderError:
            raise
        except Exception as e:
            raise _error(e) from None
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        close = getattr(self.events, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:
                pass


class AnthropicAdapter:
    """`type: anthropic`. Options (providers.yaml): `sampling: true` sends temperature (older models only)."""

    http_client_factory: Any = None     # tests inject a mock transport here

    def client(self, provider: ProviderConfig, key: ApiKey) -> Any:
        a = _sdk()
        cache_key = hashlib.sha256(f"{provider.base_url}\n{key.value}".encode()).hexdigest()
        factory = AnthropicAdapter.http_client_factory   # read from the class: a plain function, not a method
        if cache_key not in _clients or factory is not None:
            kw: dict[str, Any] = {"api_key": key.value or None, "max_retries": 0,
                                  "timeout": a.Timeout(provider.timeout_s, connect=provider.connect_timeout_s)}
            if provider.base_url:
                kw["base_url"] = provider.base_url
            if factory is not None:
                kw["http_client"] = factory()
            if provider.extra_headers:
                kw["default_headers"] = dict(provider.extra_headers)
            _clients[cache_key] = a.AsyncAnthropic(**kw)
        return _clients[cache_key]

    async def chat(self, provider: ProviderConfig, key: ApiKey, model: str, req: ChatRequest,
                   client: Any = None, params: dict[str, Any] | None = None) -> AdapterResponse:
        body = {**to_messages_request(req, model, bool(provider.options.get("sampling"))), **(params or {})}
        return to_adapter_response(await self.messages(provider, key, body))

    async def open_stream(self, provider: ProviderConfig, key: ApiKey, model: str, req: ChatRequest,
                          client: Any = None, params: dict[str, Any] | None = None) -> AnthropicStreamHandle:
        body = {**to_messages_request(req, model, bool(provider.options.get("sampling"))), **(params or {})}
        return AnthropicStreamHandle(await self.messages(provider, key, body, stream=True))

    async def messages(self, provider: ProviderConfig, key: ApiKey, body: dict[str, Any], stream: bool = False) -> Any:
        """One Messages API call with a request body in the Messages format. Returns the message as a dict, or the
        SDK's raw event stream when `stream` is set. Errors are ProviderError (router semantics)."""
        rest = {k: v for k, v in body.items() if k not in ("model", "max_tokens", "messages", "stream")}
        try:
            out = await self.client(provider, key).messages.create(
                model=body["model"], max_tokens=int(body.get("max_tokens") or 4096), messages=body["messages"],
                stream=stream, extra_body=rest or None,
                timeout=_sdk().Timeout(provider.timeout_s, connect=provider.connect_timeout_s))
        except Exception as e:
            raise _error(e) from None
        return out if stream else out.to_dict()

    async def list_models(self, provider: ProviderConfig, key: ApiKey, client: Any = None) -> list[str]:
        try:
            page = await self.client(provider, key).models.list()
            return sorted(m.id for m in page.data)
        except Exception as e:
            raise _error(e) from None
