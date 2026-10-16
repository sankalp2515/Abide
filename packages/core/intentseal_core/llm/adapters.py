"""Provider adapters, registered by `type` name in config/providers.yaml.

An adapter turns a provider-neutral ChatRequest into one HTTP call and classifies failures
into ProviderError kinds the router understands. Adapters never log key values.
"""

from __future__ import annotations

import email.utils
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from ..config import ProviderConfig
from ..secrets import ApiKey, redact


@dataclass
class ChatRequest:
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    temperature: float | None = 0.0
    max_tokens: int | None = 512
    response_format: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AdapterResponse:
    content: str | None
    tool_calls: list[dict[str, Any]]
    finish_reason: str | None
    input_tokens: int
    output_tokens: int
    raw: dict[str, Any]


class ProviderError(Exception):
    """kind: rate_limited | auth_failed | server_error | timeout | bad_request | not_found | error"""

    def __init__(self, kind: str, message: str, http_status: int | None = None,
                 retry_after_s: float | None = None) -> None:
        super().__init__(redact(message))
        self.kind = kind
        self.http_status = http_status
        self.retry_after_s = retry_after_s


_RETRY_IN_BODY = re.compile(r"(?:retry in|try again in|retryDelay\"?:\s*\"?)\s*([0-9.]+)\s*(ms|s)?", re.I)


def parse_retry_after(headers: httpx.Headers, body: str = "") -> float | None:
    v = headers.get("retry-after")
    if v:
        try:
            return max(0.0, float(v))
        except ValueError:
            dt = email.utils.parsedate_to_datetime(v)
            if dt:
                return max(0.0, dt.timestamp() - time.time())
    m = _RETRY_IN_BODY.search(body or "")
    if m:
        val = float(m.group(1))
        return val / 1000 if (m.group(2) or "").lower() == "ms" else val
    return None


def classify_http_error(resp: httpx.Response) -> ProviderError:
    body = resp.text[:2000]
    s = resp.status_code
    if s == 429:
        return ProviderError("rate_limited", f"HTTP 429: {body[:300]}", s, parse_retry_after(resp.headers, body))
    if s in (401, 403):
        return ProviderError("auth_failed", f"HTTP {s}", s)
    if s == 404:
        return ProviderError("not_found", f"HTTP 404: {body[:300]}", s)
    if s in (400, 413, 422):
        return ProviderError("bad_request", f"HTTP {s}: {body[:300]}", s)
    if s >= 500:
        return ProviderError("server_error", f"HTTP {s}: {body[:200]}", s, parse_retry_after(resp.headers, body))
    return ProviderError("error", f"HTTP {s}: {body[:300]}", s)


class ChatStreamHandle:
    """An open server-sent-events completion: `chunks()` yields each `data:` object; usage and the finish reason
    are collected on the way (estimated from the streamed text when the provider sends no usage)."""

    def __init__(self, resp: httpx.Response) -> None:
        self.resp = resp
        self.input_tokens = self.output_tokens = 0
        self.finish_reason: str | None = None
        self._chars = 0

    async def chunks(self):
        try:
            async for line in self.resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("error"):
                    raise ProviderError("server_error", f"stream error: {str(chunk['error'])[:200]}")
                usage = chunk.get("usage") or (chunk.get("x_groq") or {}).get("usage")
                if usage:
                    self.input_tokens = int(usage.get("prompt_tokens") or self.input_tokens)
                    self.output_tokens = int(usage.get("completion_tokens") or self.output_tokens)
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    self._chars += len(delta.get("content") or "")
                    for tc in delta.get("tool_calls") or []:
                        self._chars += len(str((tc.get("function") or {}).get("arguments") or ""))
                    if choice.get("finish_reason"):
                        self.finish_reason = choice["finish_reason"]
                yield chunk
        except httpx.TimeoutException as e:
            raise ProviderError("timeout", f"stream timeout: {type(e).__name__}") from None
        except httpx.HTTPError as e:
            raise ProviderError("server_error", f"stream transport: {type(e).__name__}") from None
        finally:
            await self.aclose()
        if not self.output_tokens:
            self.output_tokens = self._chars // 4

    async def aclose(self) -> None:
        await self.resp.aclose()


class OpenAICompatibleAdapter:
    """Any provider exposing POST {base_url}/chat/completions in the OpenAI format."""

    def _headers(self, provider: ProviderConfig, key: ApiKey) -> dict[str, str]:
        auth: dict[str, str] = {}
        if key.value:   # keyless local servers (Ollama, vLLM, LM Studio) send no credentials
            # Azure OpenAI takes the key in an `api-key` header; everyone else uses a bearer token.
            header = provider.options.get("auth_header", "authorization")
            auth = {"api-key": key.value} if header == "api-key" else {"Authorization": f"Bearer {key.value}"}
        return {**auth, "Content-Type": "application/json", **provider.extra_headers}

    async def chat(self, provider: ProviderConfig, key: ApiKey, model: str, req: ChatRequest,
                   client: httpx.AsyncClient, params: dict[str, Any] | None = None) -> AdapterResponse:
        body: dict[str, Any] = {"model": model, "messages": req.messages}
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.max_tokens is not None:
            body["max_tokens"] = req.max_tokens
        if req.tools:
            body["tools"] = req.tools
            if req.tool_choice is not None:
                body["tool_choice"] = req.tool_choice
        if req.response_format:
            body["response_format"] = req.response_format
        body.update(params or {})
        body.update(req.extra)
        timeout = httpx.Timeout(provider.timeout_s, connect=provider.connect_timeout_s)
        try:
            resp = await client.post(f"{provider.base_url}/chat/completions",
                                     headers=self._headers(provider, key), json=body, timeout=timeout)
        except httpx.TimeoutException as e:
            raise ProviderError("timeout", f"timeout: {type(e).__name__}") from None
        except httpx.HTTPError as e:
            raise ProviderError("server_error", f"transport: {type(e).__name__}") from None
        if resp.status_code >= 400:
            raise classify_http_error(resp)
        try:
            data = resp.json()
        except json.JSONDecodeError:
            raise ProviderError("server_error", "invalid JSON from provider") from None
        if "choices" not in data or not data["choices"]:
            # Some gateways return 200 with an error object.
            err = data.get("error") or {}
            code = err.get("code") if isinstance(err, dict) else None
            if code == 429:
                raise ProviderError("rate_limited", str(err)[:300], 429)
            raise ProviderError("server_error", f"no choices: {str(data)[:300]}")
        choice = data["choices"][0]
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}
        return AdapterResponse(
            content=msg.get("content"),
            tool_calls=list(msg.get("tool_calls") or []),
            finish_reason=choice.get("finish_reason"),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            raw=data,
        )

    async def open_stream(self, provider: ProviderConfig, key: ApiKey, model: str, req: ChatRequest,
                          client: httpx.AsyncClient, params: dict[str, Any] | None = None) -> "ChatStreamHandle":
        """Start a streamed completion. Errors before the first byte raise ProviderError (so the router can
        retry or fail over); the returned handle yields parsed chunks and collects usage."""
        body: dict[str, Any] = {"model": model, "messages": req.messages, "stream": True}
        if provider.options.get("stream_usage", True):
            body["stream_options"] = {"include_usage": True}
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.max_tokens is not None:
            body["max_tokens"] = req.max_tokens
        if req.tools:
            body["tools"] = req.tools
            if req.tool_choice is not None:
                body["tool_choice"] = req.tool_choice
        body.update(params or {})
        body.update(req.extra)
        timeout = httpx.Timeout(provider.timeout_s, connect=provider.connect_timeout_s)
        request = client.build_request("POST", f"{provider.base_url}/chat/completions",
                                       headers=self._headers(provider, key), json=body, timeout=timeout)
        try:
            resp = await client.send(request, stream=True)
        except httpx.TimeoutException as e:
            raise ProviderError("timeout", f"timeout: {type(e).__name__}") from None
        except httpx.HTTPError as e:
            raise ProviderError("server_error", f"transport: {type(e).__name__}") from None
        if resp.status_code >= 400:
            await resp.aread()
            await resp.aclose()
            raise classify_http_error(resp)
        return ChatStreamHandle(resp)

    async def list_models(self, provider: ProviderConfig, key: ApiKey, client: httpx.AsyncClient) -> list[str]:
        try:
            resp = await client.get(f"{provider.base_url}/models", headers=self._headers(provider, key),
                                    timeout=httpx.Timeout(provider.timeout_s, connect=provider.connect_timeout_s))
        except httpx.HTTPError as e:
            raise ProviderError("server_error", f"transport: {type(e).__name__}") from None
        if resp.status_code >= 400:
            raise classify_http_error(resp)
        data = resp.json()
        items = data.get("data", data if isinstance(data, list) else [])
        return sorted(str(m.get("id")) for m in items if isinstance(m, dict) and m.get("id"))


class JevAdapter:
    """Slot for the optional TypeSafe Jev decision model. Not a chat model.

    It is wired into the DecisionModel interface (CORE-13), not the chat router. Kept here so
    `type: jev` in providers.yaml resolves; any call fails clearly while the provider is disabled.
    """

    async def chat(self, *args: Any, **kwargs: Any) -> AdapterResponse:
        raise ProviderError("bad_request", "jev is a decision model, not a chat model")

    async def list_models(self, *args: Any, **kwargs: Any) -> list[str]:
        return []


def _anthropic() -> Any:
    from .anthropic_adapter import AnthropicAdapter   # imported lazily: the SDK is an optional extra

    return AnthropicAdapter()


_REGISTRY: dict[str, Callable[[], Any]] = {
    "openai_compatible": OpenAICompatibleAdapter,
    "anthropic": _anthropic,
    "jev": JevAdapter,
}


def register_adapter(type_name: str, factory: Callable[[], Any]) -> None:
    _REGISTRY[type_name] = factory


def get_adapter(type_name: str) -> Any:
    if type_name not in _REGISTRY:
        raise KeyError(f"no adapter registered for provider type '{type_name}'")
    return _REGISTRY[type_name]()
