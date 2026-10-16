"""Router contract + fault-injection tests (CORE-11). No network: httpx is mocked with respx."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
import respx

from intentseal_core.config import ModelRef, ProviderConfig, RoleConfig, Settings
from intentseal_core.llm.adapters import ChatRequest
from intentseal_core.llm.router import AllProvidersFailed, Router
from intentseal_core.secrets import ApiKey, install_log_redaction, load_keys, redact, registry
from intentseal_core.store import Store

OK = {"choices": [{"message": {"content": "hi", "tool_calls": []}, "finish_reason": "stop"}],
      "usage": {"prompt_tokens": 10, "completion_tokens": 2}}


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def no_sleep(_s: float) -> None:
    return None


def prov(name: str, n_keys: int = 1, **kw) -> ProviderConfig:
    keys = [ApiKey(f"{name}#{i + 1}", f"secret-{name}-{i + 1}-xxxxxxxx") for i in range(n_keys)]
    for k in keys:
        registry.register(k.value)
    return ProviderConfig(name=name, type="openai_compatible", enabled=True,
                          base_url=f"https://{name}.test/v1", keys=keys, retries=1, **kw)


def make_router(providers: list[ProviderConfig], cache: bool = False) -> tuple[Router, Clock]:
    clock = Clock()
    settings = Settings(
        providers={p.name: p for p in providers},
        roles={"judge": RoleConfig("judge", [ModelRef(p.name, "m", price_in=1.0) for p in providers], cache=cache)},
        db_path=":memory:",  # type: ignore[arg-type]
    )
    r = Router(settings, store=Store(":memory:"), client=httpx.AsyncClient(), clock=clock, sleep=no_sleep)
    return r, clock


def req() -> ChatRequest:
    return ChatRequest(messages=[{"role": "user", "content": "x"}])


@respx.mock
async def test_success_records_usage_and_cost():
    r, _ = make_router([prov("a")])
    respx.post("https://a.test/v1/chat/completions").respond(200, json=OK)
    res = await r.chat("judge", req())
    assert res.provider == "a" and res.content == "hi" and res.key_alias == "a#1"
    assert res.est_cost_usd == pytest.approx(10 / 1_000_000)
    rows = r.store.query("SELECT status, input_tokens FROM llm_usage")
    assert rows == [{"status": "ok", "input_tokens": 10}]


@respx.mock
async def test_429_honours_retry_after_and_fails_over_to_next_provider_not_next_key():
    r, clock = make_router([prov("a", n_keys=2), prov("b")])
    a = respx.post("https://a.test/v1/chat/completions").respond(429, headers={"retry-after": "30"}, text="slow down")
    respx.post("https://b.test/v1/chat/completions").respond(200, json=OK)
    res = await r.chat("judge", req())
    assert res.provider == "b"
    assert a.call_count == 1                      # spare key a#2 NOT used to dodge the limit
    assert r.provider_status("a").startswith("rate limited")
    clock.t += 31
    assert r.provider_status("a") == "healthy"


@respx.mock
async def test_401_disables_key_with_alert_and_uses_spare(caplog):
    install_log_redaction()
    r, _ = make_router([prov("a", n_keys=2)])
    route = respx.post("https://a.test/v1/chat/completions")
    route.side_effect = [httpx.Response(401), httpx.Response(200, json=OK)]
    with caplog.at_level(logging.WARNING):
        res = await r.chat("judge", req())
    assert res.key_alias == "a#2"
    assert r.key_health["a#1"].disabled
    assert "a#1" in caplog.text and "secret-a-1" not in caplog.text


@respx.mock
async def test_5xx_retries_then_opens_circuit_and_fails_over():
    p = prov("a", circuit_failures=2)
    r, _ = make_router([p, prov("b")])
    respx.post("https://a.test/v1/chat/completions").respond(503)
    respx.post("https://b.test/v1/chat/completions").respond(200, json=OK)
    res = await r.chat("judge", req())
    assert res.provider == "b" and res.failed_over
    assert r.provider_status("a").startswith("circuit open")


@respx.mock
async def test_timeout_counts_as_failure_and_fails_over():
    r, _ = make_router([prov("a"), prov("b")])
    respx.post("https://a.test/v1/chat/completions").mock(side_effect=httpx.ReadTimeout("t"))
    respx.post("https://b.test/v1/chat/completions").respond(200, json=OK)
    assert (await r.chat("judge", req())).provider == "b"


@respx.mock
async def test_all_down_raises_with_attempt_summary():
    r, _ = make_router([prov("a"), prov("b")])
    respx.post("https://a.test/v1/chat/completions").respond(500)
    respx.post("https://b.test/v1/chat/completions").respond(429)
    with pytest.raises(AllProvidersFailed) as e:
        await r.chat("judge", req())
    assert "a/m" in str(e.value) and "b/m:rate_limited" in str(e.value)


async def test_zero_keys_auto_disables_provider():
    p = prov("a", n_keys=0)
    r, _ = make_router([p])
    assert not p.active and "no keys" in r.provider_status("a")
    with pytest.raises(AllProvidersFailed):
        await r.chat("judge", req())


@respx.mock
async def test_budget_exceeded_skips_provider():
    r, _ = make_router([prov("a", daily_token_budget=5), prov("b")])
    respx.post("https://a.test/v1/chat/completions").respond(200, json=OK)
    respx.post("https://b.test/v1/chat/completions").respond(200, json=OK)
    assert (await r.chat("judge", req())).provider == "a"   # 12 tokens used, over the 5 budget
    assert (await r.chat("judge", ChatRequest(messages=[{"role": "user", "content": "y"}]))).provider == "b"


@respx.mock
async def test_cache_hit_avoids_second_call():
    r, _ = make_router([prov("a")], cache=True)
    route = respx.post("https://a.test/v1/chat/completions").respond(200, json=OK)
    first = await r.chat("judge", req())
    second = await r.chat("judge", req())
    assert not first.cached and second.cached and route.call_count == 1


@respx.mock
async def test_bad_request_moves_to_next_model_without_health_penalty():
    r, _ = make_router([prov("a"), prov("b")])
    respx.post("https://a.test/v1/chat/completions").respond(400, text="tools unsupported")
    respx.post("https://b.test/v1/chat/completions").respond(200, json=OK)
    assert (await r.chat("judge", req())).provider == "b"
    assert r.provider_status("a") == "healthy"


def test_load_keys_any_count_and_aliases(monkeypatch):
    monkeypatch.setenv("ZZ_API_KEY", "k-one-aaaaaaaa")
    monkeypatch.setenv("ZZ_API_KEY_3", "k-three-aaaaaa")
    monkeypatch.setenv("ZZ_API_KEY_2", "k-two-aaaaaaaa")
    monkeypatch.setenv("ZZ_API_KEYS", "k-four-aaaaaaa, k-one-aaaaaaaa")
    keys = load_keys("zz", "ZZ_API_KEY", "ZZ_API_KEYS")
    assert [k.alias for k in keys] == ["zz#1", "zz#2", "zz#3", "zz#4"]
    assert [k.value for k in keys][:3] == ["k-one-aaaaaaaa", "k-two-aaaaaaaa", "k-three-aaaaaa"]
    assert "k-one" not in repr(keys[0])


def test_redact_removes_registered_and_shaped_keys():
    registry.register("my-registered-secret-123")
    s = redact("a my-registered-secret-123 b gsk_" + "A" * 30)
    assert "my-registered" not in s and "gsk_" not in s


@respx.mock
async def test_tpm_throttle_waits_for_the_window():
    p = prov("a", tpm=1000)
    r, clock = make_router([p])
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)
        clock.t += s

    r._sleep = fake_sleep
    respx.post("https://a.test/v1/chat/completions").respond(200, json=OK)
    big = ChatRequest(messages=[{"role": "user", "content": "x" * 2400}], max_tokens=300)   # ~900 tokens
    await r.chat("judge", big)
    await r.chat("judge", ChatRequest(messages=[{"role": "user", "content": "y" * 2400}], max_tokens=300))
    assert not slept   # first call recorded its real usage (12 tokens), so the second fits
    r._token_log["a/m"][-1] = (clock.t, 950)
    await r.chat("judge", ChatRequest(messages=[{"role": "user", "content": "z" * 2400}], max_tokens=300))
    assert slept and slept[0] > 50


@respx.mock
async def test_wait_on_rate_limit_retries_single_model_role():
    p = prov("a")
    r, clock = make_router([p])
    r.settings.roles["judge"].wait_on_rate_limit_s = 60

    async def fake_sleep(s: float) -> None:
        clock.t += s

    r._sleep = fake_sleep
    route = respx.post("https://a.test/v1/chat/completions")
    route.side_effect = [httpx.Response(429, headers={"retry-after": "20"}), httpx.Response(200, json=OK)]
    res = await r.chat("judge", req())
    assert res.provider == "a" and route.call_count == 2


@respx.mock
async def test_wait_on_rate_limit_gives_up_beyond_cap():
    r, clock = make_router([prov("a")])
    r.settings.roles["judge"].wait_on_rate_limit_s = 10
    respx.post("https://a.test/v1/chat/completions").respond(429, headers={"retry-after": "30"})
    with pytest.raises(AllProvidersFailed):
        await r.chat("judge", req())


@respx.mock
async def test_max_queue_fails_over_instead_of_waiting():
    a, b = prov("a", tpm=1000), prov("b")
    r, clock = make_router([a, b])
    r.settings.roles["judge"].max_queue_s = 2
    r._token_log["a/m"] = [(clock.t, 990)]
    respx.post("https://b.test/v1/chat/completions").respond(200, json=OK)
    res = await r.chat("judge", req())
    assert res.provider == "b" and any(x.outcome == "skipped:throttled" for x in res.attempts)


def _sse(*objs) -> bytes:
    lines = [f"data: {json.dumps(o)}" for o in objs] + ["data: [DONE]"]
    return "".join(line + "\n\n" for line in lines).encode()


@respx.mock
async def test_stream_fails_over_before_first_byte_and_records_usage_at_end():
    r, _ = make_router([prov("a"), prov("b")])
    respx.post("https://a.test/v1/chat/completions").respond(429, headers={"retry-after": "30"}, text="slow down")
    body = _sse({"choices": [{"index": 0, "delta": {"content": "he"}}]},
                {"choices": [{"index": 0, "delta": {"content": "llo"}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 3}})
    b = respx.post("https://b.test/v1/chat/completions").respond(200, content=body,
                                                                headers={"content-type": "text/event-stream"})
    s = await r.stream_chat("judge", req())
    assert s.provider == "b" and json.loads(b.calls[0].request.content)["stream"] is True
    assert r.store.query("SELECT status FROM llm_usage WHERE provider = 'b'") == []   # recorded when it ends
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in [x async for x in s.chunks()] if c["choices"])
    assert text == "hello"
    rows = r.store.query("SELECT status, input_tokens, output_tokens FROM llm_usage WHERE provider = 'b'")
    assert rows == [{"status": "ok", "input_tokens": 9, "output_tokens": 3}]


@respx.mock
async def test_stream_all_providers_down_raises_before_streaming():
    r, _ = make_router([prov("a")])
    respx.post("https://a.test/v1/chat/completions").respond(503, text="down")
    with pytest.raises(AllProvidersFailed):
        await r.stream_chat("judge", req())
