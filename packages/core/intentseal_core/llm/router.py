"""LLM router (CORE-11): role -> ordered {provider, model} list, with per-key health,
rate-limit backoff, circuit breaking, failover, response cache, budgets and usage records.

Policy on rate limits: a 429 cools down the *provider* (honouring retry-after) and the router
moves on to the next provider in the role list. Extra keys of the same provider are used only
when a key is rejected (401/403), so we never multiply a provider's published limits.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import ModelRef, ProviderConfig, RoleConfig, Settings, load_settings
from ..secrets import ApiKey, install_log_redaction, registry
from ..store import Store
from .adapters import AdapterResponse, ChatRequest, ProviderError, get_adapter

log = logging.getLogger("intentseal.router")


@dataclass
class KeyHealth:
    disabled: bool = False
    disabled_reason: str | None = None
    last_status: str | None = None
    last_used: float | None = None


@dataclass
class ProviderHealth:
    cooldown_until: float = 0.0          # set by 429 / retry-after
    circuit_open_until: float = 0.0      # set after repeated 5xx / timeouts
    consecutive_failures: int = 0
    rate_limit_streak: int = 0
    next_slot: float = 0.0               # client-side throttle (rpm)
    unavailable_models: dict[str, float] = field(default_factory=dict)  # model -> until
    last_error: str | None = None


@dataclass
class Attempt:
    provider: str
    model: str
    key_alias: str | None
    outcome: str
    latency_ms: float = 0.0
    detail: str | None = None


@dataclass
class ChatResult:
    content: str | None
    tool_calls: list[dict[str, Any]]
    finish_reason: str | None
    provider: str
    model: str
    key_alias: str | None
    input_tokens: int
    output_tokens: int
    latency_ms: float
    est_cost_usd: float
    cached: bool
    attempts: list[Attempt]

    @property
    def failed_over(self) -> bool:
        return any(a.outcome != "ok" for a in self.attempts)


class ChatStream:
    """A streamed completion from one provider: iterate `chunks()`; usage is recorded once, when it ends."""

    def __init__(self, router: "Router", role: str, provider: ProviderConfig, ref: ModelRef, key_alias: str,
                 handle: Any, t0: float, attempts: list[Attempt]) -> None:
        self.router, self.role, self.p, self.ref, self.key_alias = router, role, provider, ref, key_alias
        self.handle, self.t0, self.attempts = handle, t0, attempts
        self.provider, self.model = provider.name, ref.model

    async def chunks(self):
        status, error = "ok", None
        try:
            async for chunk in self.handle.chunks():
                yield chunk
        except ProviderError as e:
            status, error = e.kind, str(e)
            raise
        finally:
            h = self.handle
            cost = (h.input_tokens * self.ref.price_in + h.output_tokens * self.ref.price_out) / 1_000_000
            self.router._record_tokens(self.p, self.ref.model, h.input_tokens + h.output_tokens)
            self.router.store.record_usage(role=self.role, provider=self.p.name, key_alias=self.key_alias,
                                           model=self.ref.model, status=status, http_status=200,
                                           latency_ms=(time.perf_counter() - self.t0) * 1000,
                                           input_tokens=h.input_tokens, output_tokens=h.output_tokens,
                                           est_cost_usd=cost, error=error)

    async def aclose(self) -> None:
        await self.handle.aclose()


class AllProvidersFailed(Exception):
    def __init__(self, role: str, attempts: list[Attempt]) -> None:
        summary = "; ".join(f"{a.provider}/{a.model}:{a.outcome}" for a in attempts) or "no eligible provider"
        super().__init__(f"all providers failed for role '{role}': {summary}")
        self.role = role
        self.attempts = attempts


def _cache_key(role: str, req: ChatRequest) -> str:
    blob = json.dumps(
        {"role": role, "m": req.messages, "t": req.tools, "tc": req.tool_choice, "temp": req.temperature,
         "max": req.max_tokens, "rf": req.response_format, "x": req.extra},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


class Router:
    def __init__(self, settings: Settings | None = None, store: Store | None = None,
                 client: httpx.AsyncClient | None = None, clock=time.time, sleep=asyncio.sleep) -> None:
        install_log_redaction()
        self.settings = settings or load_settings()
        self.store = store or Store(self.settings.db_path)
        self._client = client
        self._clock = clock
        self._sleep = sleep
        self.key_health: dict[str, KeyHealth] = {}
        self.provider_health: dict[str, ProviderHealth] = {
            name: ProviderHealth() for name in self.settings.providers
        }
        self._throttle_locks: dict[str, asyncio.Lock] = {}
        self._token_log: dict[str, list[tuple[float, int]]] = {}   # "provider/model" -> [(ts, tokens)]

    # ---- plumbing ------------------------------------------------------------------------
    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _kh(self, key: ApiKey) -> KeyHealth:
        return self.key_health.setdefault(key.alias, KeyHealth())

    def healthy_keys(self, provider: ProviderConfig) -> list[ApiKey]:
        return [k for k in provider.keys if not self._kh(k).disabled]

    def provider_status(self, name: str) -> str:
        p = self.settings.providers[name]
        if not p.active:
            return f"disabled ({p.disabled_reason})"
        h = self.provider_health[name]
        now = self._clock()
        if not self.healthy_keys(p):
            return "no healthy keys"
        if h.circuit_open_until > now:
            return f"circuit open ({h.circuit_open_until - now:.0f}s)"
        if h.cooldown_until > now:
            return f"rate limited ({h.cooldown_until - now:.0f}s)"
        return "healthy"

    def _skip_reason(self, ref: ModelRef) -> str | None:
        p = self.settings.providers.get(ref.provider)
        if p is None:
            return "unknown_provider"
        if not p.active:
            return "disabled"
        h = self.provider_health[ref.provider]
        now = self._clock()
        if h.circuit_open_until > now:
            return "circuit_open"
        if h.cooldown_until > now:
            return "cooling_down"
        if h.unavailable_models.get(ref.model, 0) > now:
            return "model_unavailable"
        if not self.healthy_keys(p):
            return "no_healthy_keys"
        if p.daily_token_budget is not None and self.store.tokens_today(p.name) >= p.daily_token_budget:
            return "budget_exceeded"
        return None

    async def _throttle(self, p: ProviderConfig) -> None:
        if not p.rpm:
            return
        lock = self._throttle_locks.setdefault(p.name, asyncio.Lock())
        async with lock:
            h = self.provider_health[p.name]
            now = self._clock()
            wait = h.next_slot - now
            if wait > 0:
                await self._sleep(wait)
            h.next_slot = max(now, h.next_slot) + 60.0 / float(p.rpm)

    async def _throttle_tokens(self, p: ProviderConfig, model: str, req: ChatRequest,
                               max_wait_s: float | None = None) -> bool:
        """Stay under a tokens-per-minute limit: wait until the last 60 s of usage leaves room.
        Returns False (without waiting) when the wait would exceed max_wait_s: the caller fails over."""
        if not p.tpm:
            return True
        est = len(json.dumps(req.messages, default=str)) // 4 + (req.max_tokens or 512)
        est = min(est, int(p.tpm))
        key = f"{p.name}/{model}"
        lock = self._throttle_locks.setdefault("tpm:" + key, asyncio.Lock())
        async with lock:
            window = self._token_log.setdefault(key, [])
            while True:
                now = self._clock()
                window[:] = [(t, n) for t, n in window if now - t < 60.0]
                if sum(n for _, n in window) + est <= p.tpm or not window:
                    break
                wait = max(0.5, 60.0 - (now - window[0][0]))
                if max_wait_s is not None and wait > max_wait_s:
                    return False
                await self._sleep(wait)
            window.append((self._clock(), est))
            return True

    def _record_tokens(self, p: ProviderConfig, model: str, used: int) -> None:
        window = self._token_log.get(f"{p.name}/{model}")
        if window:   # replace the estimate with the real count
            t, _ = window[-1]
            window[-1] = (t, used)

    def _shortest_cooldown(self, rc: RoleConfig) -> float | None:
        now = self._clock()
        waits = [self.provider_health[m.provider].cooldown_until - now for m in rc.models
                 if m.provider in self.provider_health and self.settings.providers[m.provider].active]
        waits = [w for w in waits if w > 0]
        return min(waits) if waits else None

    def _backoff(self, p: ProviderConfig, n: int) -> float:
        return min(p.backoff_max_s, p.backoff_base_s * (2 ** max(0, n - 1))) * (0.8 + 0.4 * random.random())

    # ---- public API ----------------------------------------------------------------------
    async def chat(self, role: str, req: ChatRequest, use_cache: bool | None = None) -> ChatResult:
        rc: RoleConfig | None = self.settings.roles.get(role)
        if rc is None:
            raise KeyError(f"unknown role '{role}' (config/models.yaml)")
        for k, v in rc.defaults.items():
            if getattr(req, k, None) is None:
                setattr(req, k, v)
        use_cache = rc.cache if use_cache is None else use_cache
        ck = _cache_key(role, req)
        if use_cache:
            hit = self.store.cache_get(ck)
            if hit:
                self.store.record_usage(role=role, provider=hit["provider"], model=hit["model"], status="cached",
                                        cached=1, latency_ms=0.0)
                return ChatResult(**{**hit, "latency_ms": 0.0, "cached": True, "attempts": []})

        result = await self._route(role, rc, req, stream=False)
        if use_cache:
            payload = {k: v for k, v in result.__dict__.items() if k not in ("attempts", "cached", "latency_ms")}
            self.store.cache_put(ck, payload, rc.cache_ttl_s)
        return result

    async def stream_chat(self, role: str, req: ChatRequest) -> "ChatStream":
        """Streamed completion (PRX-6). Fails over like chat() until a provider accepts the request; once
        chunks flow there is no failover. Never cached. Usage is recorded when the stream ends."""
        rc: RoleConfig | None = self.settings.roles.get(role)
        if rc is None:
            raise KeyError(f"unknown role '{role}' (config/models.yaml)")
        for k, v in rc.defaults.items():
            if getattr(req, k, None) is None:
                setattr(req, k, v)
        return await self._route(role, rc, req, stream=True)

    async def _route(self, role: str, rc: RoleConfig, req: ChatRequest, stream: bool) -> Any:
        attempts: list[Attempt] = []
        waited = 0.0
        while True:
            for ref in rc.models:
                reason = self._skip_reason(ref)
                if reason:
                    attempts.append(Attempt(ref.provider, ref.model, None, f"skipped:{reason}"))
                    continue
                result = await self._try_provider(role, ref, req, attempts, stream=stream)
                if result is not None:
                    return result
            # Every candidate failed. If the role allows it, wait out the shortest provider cooldown.
            wait = self._shortest_cooldown(rc)
            if wait is None or waited + wait > rc.wait_on_rate_limit_s:
                raise AllProvidersFailed(role, attempts)
            log.info("role %s: all providers cooling down; waiting %.1fs", role, wait)
            await self._sleep(wait + 0.5)
            waited += wait + 0.5

    async def _try_provider(self, role: str, ref: ModelRef, req: ChatRequest,
                            attempts: list[Attempt], stream: bool = False) -> Any:
        p = self.settings.providers[ref.provider]
        h = self.provider_health[p.name]
        adapter = get_adapter(p.type)
        tries = 0
        while True:
            keys = self.healthy_keys(p)
            if not keys:
                return None
            key = keys[0]
            await self._throttle(p)
            rc = self.settings.roles.get(role)
            if not await self._throttle_tokens(p, ref.model, req, rc.max_queue_s if rc else None):
                attempts.append(Attempt(p.name, ref.model, key.alias, "skipped:throttled"))
                return None   # would exceed this role's latency budget: try the next provider
            t0 = time.perf_counter()
            try:
                if stream:
                    handle = await adapter.open_stream(p, key, ref.model, req, self.client, ref.params)
                else:
                    r: AdapterResponse = await adapter.chat(p, key, ref.model, req, self.client, ref.params)
            except ProviderError as e:
                ms = (time.perf_counter() - t0) * 1000
                attempts.append(Attempt(p.name, ref.model, key.alias, e.kind, ms, str(e)[:200]))
                self._kh(key).last_status = e.kind
                h.last_error = e.kind
                self.store.record_usage(role=role, provider=p.name, key_alias=key.alias, model=ref.model,
                                        status=e.kind, http_status=e.http_status, latency_ms=ms, error=str(e))
                if e.kind == "auth_failed":
                    kh = self._kh(key)
                    kh.disabled, kh.disabled_reason = True, f"auth failed (HTTP {e.http_status})"
                    log.warning("ALERT key %s disabled: authentication failed (HTTP %s)", key.alias, e.http_status)
                    continue  # next key of the same provider
                if e.kind == "rate_limited":
                    h.rate_limit_streak += 1
                    wait = e.retry_after_s if e.retry_after_s is not None else self._backoff(p, h.rate_limit_streak)
                    h.cooldown_until = self._clock() + wait
                    log.info("provider %s rate limited; cooling down %.1fs", p.name, wait)
                    return None  # fail over to next provider; never rotate keys to dodge a limit
                if e.kind in ("not_found",):
                    h.unavailable_models[ref.model] = self._clock() + 3600
                    return None
                if e.kind == "bad_request":
                    return None  # request/model incompatibility; not a health problem
                # server_error / timeout / error: retry, then circuit
                h.consecutive_failures += 1
                if h.consecutive_failures >= p.circuit_failures:
                    h.circuit_open_until = self._clock() + p.circuit_open_s
                    log.warning("provider %s circuit opened for %.0fs", p.name, p.circuit_open_s)
                    return None
                tries += 1
                if tries > p.retries:
                    return None
                await self._sleep(e.retry_after_s or self._backoff(p, tries))
                continue
            ms = (time.perf_counter() - t0) * 1000
            h.consecutive_failures = 0
            h.rate_limit_streak = 0
            kh = self._kh(key)
            kh.last_status, kh.last_used = "ok", self._clock()
            if stream:
                attempts.append(Attempt(p.name, ref.model, key.alias, "ok", ms))
                return ChatStream(self, role, p, ref, key.alias, handle, t0, attempts)
            self._record_tokens(p, ref.model, r.input_tokens + r.output_tokens)
            cost = (r.input_tokens * ref.price_in + r.output_tokens * ref.price_out) / 1_000_000
            attempts.append(Attempt(p.name, ref.model, key.alias, "ok", ms))
            self.store.record_usage(role=role, provider=p.name, key_alias=key.alias, model=ref.model, status="ok",
                                    http_status=200, latency_ms=ms, input_tokens=r.input_tokens,
                                    output_tokens=r.output_tokens, est_cost_usd=cost)
            return ChatResult(content=r.content, tool_calls=r.tool_calls, finish_reason=r.finish_reason,
                              provider=p.name, model=ref.model, key_alias=key.alias,
                              input_tokens=r.input_tokens, output_tokens=r.output_tokens, latency_ms=ms,
                              est_cost_usd=cost, cached=False, attempts=attempts)

    # ---- the proxy's per-agent upstream (the agent's own provider and model) -------------------
    async def call_upstream(self, provider: str, model: str, req: ChatRequest, client_key: str | None = None,
                            stream: bool = False, role: str = "proxy_upstream") -> Any:
        """One provider and one model, chosen by the agent's policy rather than a role list: no failover (the
        customer chose the provider) and no shared cooldown or circuit state (a client key's rate limit is that
        client's). Retries transient errors only. Errors propagate as ProviderError so the proxy can answer with
        the provider's own status. `client_key`: the agent's own provider key (never stored, redacted in logs)."""
        p = self.settings.providers.get(provider)
        if p is None or not p.enabled:
            raise ProviderError("bad_request", f"upstream provider '{provider}' is not configured or disabled")
        if client_key:
            registry.register(client_key)
            key = ApiKey(f"{p.name}#client-{hashlib.sha256(client_key.encode()).hexdigest()[:8]}", client_key)
        else:
            keys = self.healthy_keys(p)
            if not keys:
                raise ProviderError("auth_failed", f"no key configured on the gateway for upstream '{provider}'")
            key = keys[0]
        adapter = get_adapter(p.type)
        ref = ModelRef(p.name, model)
        for attempt in range(1 + max(0, p.retries)):
            t0 = time.perf_counter()
            try:
                if stream:
                    handle = await adapter.open_stream(p, key, model, req, self.client, None)
                    return ChatStream(self, role, p, ref, key.alias, handle, t0, [])
                r: AdapterResponse = await adapter.chat(p, key, model, req, self.client, None)
            except ProviderError as e:
                self.store.record_usage(role=role, provider=p.name, key_alias=key.alias, model=model, status=e.kind,
                                        http_status=e.http_status, latency_ms=(time.perf_counter() - t0) * 1000, error=str(e))
                if e.kind in ("server_error", "timeout") and attempt < p.retries:
                    await self._sleep(self._backoff(p, attempt + 1))
                    continue
                raise
            ms = (time.perf_counter() - t0) * 1000
            self.store.record_usage(role=role, provider=p.name, key_alias=key.alias, model=model, status="ok",
                                    http_status=200, latency_ms=ms, input_tokens=r.input_tokens, output_tokens=r.output_tokens)
            return ChatResult(content=r.content, tool_calls=r.tool_calls, finish_reason=r.finish_reason, provider=p.name,
                              model=model, key_alias=key.alias, input_tokens=r.input_tokens, output_tokens=r.output_tokens,
                              latency_ms=ms, est_cost_usd=0.0, cached=False, attempts=[])
        raise ProviderError("server_error", "upstream unavailable")   # pragma: no cover - loop always returns or raises

    # ---- direct calls (used by `providers check` and validation) ---------------------------
    async def call_direct(self, provider: str, model: str, req: ChatRequest, key: ApiKey | None = None,
                          role: str = "check") -> ChatResult:
        """One call to a specific provider/model/key, bypassing role lists and cache."""
        p = self.settings.providers[provider]
        key = key or self.healthy_keys(p)[0]
        adapter = get_adapter(p.type)
        await self._throttle(p)
        await self._throttle_tokens(p, model, req)
        t0 = time.perf_counter()
        try:
            r = await adapter.chat(p, key, model, req, self.client)
        except ProviderError as e:
            ms = (time.perf_counter() - t0) * 1000
            self.store.record_usage(role=role, provider=provider, key_alias=key.alias, model=model, status=e.kind,
                                    http_status=e.http_status, latency_ms=ms, error=str(e))
            if e.kind == "auth_failed":
                kh = self._kh(key)
                kh.disabled, kh.disabled_reason = True, f"auth failed (HTTP {e.http_status})"
            raise
        ms = (time.perf_counter() - t0) * 1000
        self.store.record_usage(role=role, provider=provider, key_alias=key.alias, model=model, status="ok",
                                http_status=200, latency_ms=ms, input_tokens=r.input_tokens,
                                output_tokens=r.output_tokens)
        return ChatResult(content=r.content, tool_calls=r.tool_calls, finish_reason=r.finish_reason,
                          provider=provider, model=model, key_alias=key.alias, input_tokens=r.input_tokens,
                          output_tokens=r.output_tokens, latency_ms=ms, est_cost_usd=0.0, cached=False,
                          attempts=[Attempt(provider, model, key.alias, "ok", ms)])

    async def list_models(self, provider: str) -> list[str]:
        p = self.settings.providers[provider]
        return await get_adapter(p.type).list_models(p, self.healthy_keys(p)[0], self.client)
