"""Gemini on Vertex AI: Google credentials instead of a key, the project's endpoint, billed to that project."""

from __future__ import annotations

import json

import httpx
import pytest

from intentseal_core.config import load_providers
from intentseal_core.llm import vertex_adapter
from intentseal_core.llm.adapters import ChatRequest, ProviderError, get_adapter


def _provider(monkeypatch, project: str | None = "my-proj"):
    if project:
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", project)
    else:
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    return load_providers()["vertex"]


def test_vertex_is_active_only_with_a_project(monkeypatch):
    assert not _provider(monkeypatch, None).active
    p = _provider(monkeypatch)
    assert p.active and p.type == "vertex_openai" and p.daily_token_budget


async def test_chat_uses_project_endpoint_token_and_quota_project(monkeypatch):
    p = _provider(monkeypatch)
    monkeypatch.setattr(vertex_adapter._TOKEN, "get", lambda: "ya29.test-token")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["headers"], seen["body"] = str(request.url), request.headers, json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 7, "completion_tokens": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = await get_adapter(p.type).chat(p, p.keys[0], "gemini-3.5-flash",
                                             ChatRequest(messages=[{"role": "user", "content": "hi"}]), client)
    assert out.content == "OK" and out.input_tokens == 7
    assert seen["url"] == ("https://aiplatform.googleapis.com/v1/projects/my-proj/locations/global/endpoints/openapi"
                           "/chat/completions")
    assert seen["headers"]["authorization"] == "Bearer ya29.test-token"
    assert seen["headers"]["x-goog-user-project"] == "my-proj"
    assert seen["body"]["model"] == "google/gemini-3.5-flash"


async def test_missing_credentials_fail_over_as_auth_failure(monkeypatch):
    p = _provider(monkeypatch)

    def no_creds() -> str:
        raise ProviderError("auth_failed", "no Google credentials")

    monkeypatch.setattr(vertex_adapter._TOKEN, "get", no_creds)
    async with httpx.AsyncClient() as client:
        with pytest.raises(ProviderError) as e:
            await get_adapter(p.type).chat(p, p.keys[0], "gemini-3.5-flash",
                                           ChatRequest(messages=[{"role": "user", "content": "hi"}]), client)
    assert e.value.kind == "auth_failed"
