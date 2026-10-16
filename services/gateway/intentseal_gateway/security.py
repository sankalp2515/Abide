"""Gateway self-security (API-8, API-9, API-12, NFR §12): API keys (hashed at rest), scopes, organisations,
rate limits, request-size limits. Keys come from the environment only.

INTENTSEAL_API_KEYS     comma list of name:key:scope[:org[:agent|agent...]]   (scope: admin | project)
                        e.g. "ops:abc123:admin:*"  (org "*" = every organisation)
                             "acme-ci:def456:project:acme:support-bot|billing-bot"
                        Without an org a key belongs to the "default" organisation, like every policy without one.
INTENTSEAL_PUBLIC_KEY   optional read-only key for the public Console (scope: public, org default)
INTENTSEAL_PUBLIC_AGENTS   comma list: the only agents the public key can see (e.g. the demo agents)
INTENTSEAL_RATE_LIMIT_PER_MIN / INTENTSEAL_PUBLIC_RATE_LIMIT_PER_MIN   requests per minute per key (the public
                        key is limited per client address as well, so one visitor cannot use up everyone's quota)
INTENTSEAL_MAX_BODY_BYTES   max request body (default 2 MB)
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from starlette.responses import JSONResponse

SCOPES = {"public": 0, "project": 1, "admin": 2}
ALL_ORGS = "*"


@dataclass(frozen=True)
class Principal:
    name: str
    scope: str
    org: str = "default"
    agents: frozenset[str] | None = None     # None: every agent of the organisation

    @property
    def all_orgs(self) -> bool:
        return self.org == ALL_ORGS


def _h(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _agents(value: str) -> frozenset[str] | None:
    names = frozenset(a.strip() for a in value.split("|") if a.strip())
    return names or None


class KeyRing:
    def __init__(self) -> None:
        self._keys: dict[str, Principal] = {}
        for item in filter(None, (s.strip() for s in os.environ.get("INTENTSEAL_API_KEYS", "").split(","))):
            parts = item.split(":")
            if 3 <= len(parts) <= 5 and parts[2] in SCOPES and parts[0] and parts[1]:
                org = parts[3].strip() if len(parts) > 3 and parts[3].strip() else "default"
                agents = _agents(parts[4]) if len(parts) > 4 else None
                self._keys[_h(parts[1])] = Principal(parts[0], parts[2], org, agents)
        pub = os.environ.get("INTENTSEAL_PUBLIC_KEY")
        if pub:
            self._keys[_h(pub)] = Principal("public", "public", "default",
                                            _agents(os.environ.get("INTENTSEAL_PUBLIC_AGENTS", "").replace(",", "|")))
        self._buckets: dict[str, list[float]] = {}
        self.limit = int(os.environ.get("INTENTSEAL_RATE_LIMIT_PER_MIN") or "120")
        self.public_limit = int(os.environ.get("INTENTSEAL_PUBLIC_RATE_LIMIT_PER_MIN") or "20")

    def add(self, key: str, principal: Principal) -> None:
        """Register an in-process key (e.g. the demo runner's ephemeral key for calling its own proxy)."""
        self._keys[_h(key)] = principal

    def orgs(self) -> set[str]:
        return {p.org for p in self._keys.values()}

    def lookup(self, key: str | None) -> Principal | None:
        if not key:
            return None
        h = _h(key)
        for stored, p in self._keys.items():
            if hmac.compare_digest(stored, h):
                return p
        return None

    def allow(self, p: Principal, client: str = "") -> bool:
        now = time.time()
        bucket_id = f"{p.name}@{client}" if p.scope == "public" else p.name
        bucket = [t for t in self._buckets.get(bucket_id, []) if now - t < 60]
        limit = self.public_limit if p.scope == "public" else self.limit
        if len(bucket) >= limit:
            self._buckets[bucket_id] = bucket
            return False
        bucket.append(now)
        self._buckets[bucket_id] = bucket
        if len(self._buckets) > 10000:   # drop idle client buckets
            for k in [k for k, v in self._buckets.items() if not v or now - v[-1] >= 60]:
                del self._buckets[k]
        return True


def _key_from(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key") or request.headers.get("x-goog-api-key")   # Anthropic / Google SDKs


def require(scope: str):
    async def dep(request: Request) -> Principal:
        ring: KeyRing = request.app.state.keys
        p = ring.lookup(_key_from(request))
        if p is None:
            raise HTTPException(401, "missing or invalid API key")
        if SCOPES[p.scope] < SCOPES[scope]:
            raise HTTPException(403, f"this key cannot access {scope}-scoped endpoints")
        if not ring.allow(p, request.client.host if request.client else ""):
            raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": "60"})
        return p

    return Depends(dep)


class BodySizeLimit:
    """Reject bodies over the limit without buffering them: a declared Content-Length is checked up front, and a
    chunked body is counted as it streams in (413 as soon as it passes the limit). Runs before authentication."""

    def __init__(self, app, max_bytes: int | None = None) -> None:
        self.app = app
        self.max = max_bytes or int(os.environ.get("INTENTSEAL_MAX_BODY_BYTES") or 2 * 1024 * 1024)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        cl = dict(scope.get("headers") or []).get(b"content-length", b"").decode("latin-1")
        if cl.isdigit() and int(cl) > self.max:
            return await JSONResponse({"detail": "request body too large"}, status_code=413)(scope, receive, send)
        seen = 0

        async def limited_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max:
                    raise HTTPException(413, "request body too large")
            return message

        await self.app(scope, limited_receive, send)
