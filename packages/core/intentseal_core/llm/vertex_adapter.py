"""Gemini on Vertex AI through its OpenAI-compatible endpoint, authenticated with Google credentials (no API key).

    https://aiplatform.googleapis.com/v1/projects/{project}/locations/{location}/endpoints/openapi

Credentials are Application Default Credentials: the service's own identity on Cloud Run, `gcloud auth
application-default login` on a laptop. The project comes from GOOGLE_CLOUD_PROJECT and the location from
GOOGLE_CLOUD_LOCATION (default "global"). Usage is billed to that project: the quota-project header names it too, so a
laptop's default quota project is never charged. Models are named as in models.yaml ("gemini-3.5-flash"); the
"google/" publisher prefix is added here.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import replace
from typing import Any

import httpx

from ..config import ProviderConfig
from ..secrets import ApiKey
from .adapters import AdapterResponse, ChatRequest, ChatStreamHandle, OpenAICompatibleAdapter, ProviderError

SCOPE = "https://www.googleapis.com/auth/cloud-platform"
DEFAULT_BASE = "https://aiplatform.googleapis.com/v1/projects/{project}/locations/{location}/endpoints/openapi"


def vertex_project() -> str:
    return os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()


def vertex_location() -> str:
    return os.environ.get("GOOGLE_CLOUD_LOCATION", "").strip() or "global"


class _Token:
    """One cached access token per process, refreshed a few minutes before it expires."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._creds: Any = None

    def get(self) -> str:
        with self._lock:
            if self._creds is None:
                try:
                    import google.auth                       # optional dependency: google-auth
                    import google.auth.transport.requests  # noqa: F401
                except ImportError:
                    raise ProviderError("auth_failed", "google-auth is not installed") from None
                try:
                    self._creds, _ = google.auth.default(scopes=[SCOPE])
                except Exception as e:                       # DefaultCredentialsError and friends
                    raise ProviderError("auth_failed", f"no Google credentials: {type(e).__name__}") from None
            expiry = getattr(self._creds, "expiry", None)
            stale = not self._creds.token or expiry is None or expiry.timestamp() - time.time() < 300
            if stale:
                import google.auth.transport.requests
                try:
                    self._creds.refresh(google.auth.transport.requests.Request())
                except Exception as e:
                    raise ProviderError("auth_failed", f"Google credential refresh failed: {type(e).__name__}") from None
            return str(self._creds.token)


_TOKEN = _Token()


def warm() -> bool:
    """Fetch the first token ahead of the first request (it costs 1–3 s). Never raises."""
    if not vertex_project():
        return False
    try:
        _TOKEN.get()
        return True
    except ProviderError:
        return False


class VertexOpenAIAdapter(OpenAICompatibleAdapter):
    """OpenAI-format chat against Vertex AI, with a fresh Google access token as the bearer."""

    @staticmethod
    def _target(provider: ProviderConfig, model: str) -> tuple[ProviderConfig, str]:
        project = vertex_project()
        if not project:
            raise ProviderError("auth_failed", "GOOGLE_CLOUD_PROJECT is not set")
        base = (provider.base_url or DEFAULT_BASE).format(project=project, location=vertex_location())
        headers = {**provider.extra_headers, "x-goog-user-project": project}
        return replace(provider, base_url=base.rstrip("/"), extra_headers=headers), \
            (model if "/" in model else f"google/{model}")

    async def _key(self, key: ApiKey) -> ApiKey:
        return ApiKey(key.alias, await asyncio.to_thread(_TOKEN.get))

    async def chat(self, provider: ProviderConfig, key: ApiKey, model: str, req: ChatRequest,
                   client: httpx.AsyncClient, params: dict[str, Any] | None = None) -> AdapterResponse:
        p, m = self._target(provider, model)
        return await super().chat(p, await self._key(key), m, req, client, params)

    async def open_stream(self, provider: ProviderConfig, key: ApiKey, model: str, req: ChatRequest,
                          client: httpx.AsyncClient, params: dict[str, Any] | None = None) -> ChatStreamHandle:
        p, m = self._target(provider, model)
        return await super().open_stream(p, await self._key(key), m, req, client, params)

    async def list_models(self, provider: ProviderConfig, key: ApiKey, client: httpx.AsyncClient) -> list[str]:
        return []                                            # Vertex's OpenAI endpoint has no model listing
