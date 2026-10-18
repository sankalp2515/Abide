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
INTENTSEAL_TRIAL_KEYS   1 = anyone may create a 7-day project key for themselves (POST /v1/trial-keys), so a
                        visitor can try Delegate with their own agents without asking anyone. Off by default.
                        Trial keys are stored hashed in the evidence store; each is a separate person.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
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
    agent: bool = False                       # an agent key: acts for `name`, but can never confirm, change or approve

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
            if len(parts) == 3 and parts[2] == "agent" and parts[0] and parts[1]:   # "local:local-agent-key:agent"
                self._keys[_h(parts[1])] = Principal(parts[0], "project", agent=True)
                continue
            if 3 <= len(parts) <= 5 and parts[2] in SCOPES and parts[0] and parts[1]:
                org = parts[3].strip() if len(parts) > 3 and parts[3].strip() else "default"
                agents = _agents(parts[4]) if len(parts) > 4 else None
                self._keys[_h(parts[1])] = Principal(parts[0], parts[2], org, agents)
        pub = os.environ.get("INTENTSEAL_PUBLIC_KEY")
        if pub:
            self._keys[_h(pub)] = Principal("public", "public", "default",
                                            _agents(os.environ.get("INTENTSEAL_PUBLIC_AGENTS", "").replace(",", "|")))
        self._buckets: dict[str, list[float]] = {}
        self.trial_store = None                  # set by the app when INTENTSEAL_TRIAL_KEYS=1
        self.agent_store = None                  # set by the app: agent keys issued from the Console (POST /v1/agent-keys)
        self._trial_cache: dict[str, tuple[Principal | None, float]] = {}
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
        if self.trial_store is not None and key.startswith(TRIAL_PREFIX):
            return self._trial(h)
        if self.agent_store is not None and key.startswith(AGENT_PREFIX):
            return self._agent(h)
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


TRIAL_PREFIX = "isk_trial_"
TRIAL_DAYS = 7
TRIAL_PER_ADDRESS_PER_HOUR = int(os.environ.get("INTENTSEAL_TRIAL_PER_HOUR") or 30)   # judges often share one office network
TRIAL_SCHEMA = ["""CREATE TABLE IF NOT EXISTS trial_keys (hash TEXT PRIMARY KEY, name TEXT, address TEXT,
                   created REAL, expires REAL)"""]


def _trial(self: KeyRing, h: str) -> Principal | None:
    hit = self._trial_cache.get(h)
    if hit and time.time() - hit[1] < 30:
        return hit[0]
    rows = self.trial_store.query("SELECT name, expires FROM trial_keys WHERE hash = ?", (h,))
    p = Principal(rows[0]["name"], "project") if rows and rows[0]["expires"] > time.time() else None
    if len(self._trial_cache) > 5000:
        self._trial_cache.clear()
    self._trial_cache[h] = (p, time.time())
    return p


KeyRing._trial = _trial      # type: ignore[attr-defined]


def issue_trial_key(ring: KeyRing, address: str) -> dict[str, object]:
    """A new 7-day project key for one visitor. The key is shown once; only its hash is stored."""
    store = ring.trial_store
    if store is None:
        raise HTTPException(404, "trial keys are not enabled on this deployment")
    now = time.time()
    recent = store.query("SELECT hash FROM trial_keys WHERE address = ? AND created > ?", (address, now - 3600))
    if len(recent) >= TRIAL_PER_ADDRESS_PER_HOUR:
        raise HTTPException(429, "too many trial keys from this address; try again in an hour",
                            headers={"Retry-After": "3600"})
    key = TRIAL_PREFIX + secrets.token_urlsafe(24)
    name = "trial-" + secrets.token_hex(4)
    expires = now + TRIAL_DAYS * 86400
    store.execute("INSERT INTO trial_keys VALUES (?,?,?,?,?)", (_h(key), name, address, now, expires))
    return {"key": key, "name": name, "scope": "project", "expires": expires,
            "note": "Shown once. Your delegations and businesses are private to this key."}


# ---- agent keys: what an agent runtime holds instead of the employee's own key -------------------------------------
AGENT_PREFIX = "isk_agent_"
AGENT_DAYS = 30
AGENT_PER_OWNER = 20
AGENT_SCHEMA = ["""CREATE TABLE IF NOT EXISTS agent_keys (hash TEXT PRIMARY KEY, id TEXT, owner TEXT, label TEXT,
                   created REAL, expires REAL, revoked REAL)"""]


def _agent(self: KeyRing, h: str) -> Principal | None:
    hit = self._trial_cache.get("a:" + h)
    if hit and time.time() - hit[1] < 30:
        return hit[0]
    rows = self.agent_store.query("SELECT owner, expires, revoked FROM agent_keys WHERE hash = ?", (h,))
    ok = rows and rows[0]["expires"] > time.time() and not rows[0]["revoked"]
    p = Principal(rows[0]["owner"], "project", agent=True) if ok else None
    self._trial_cache["a:" + h] = (p, time.time())
    return p


KeyRing._agent = _agent      # type: ignore[attr-defined]


def issue_agent_key(ring: KeyRing, owner: Principal, label: str) -> dict[str, object]:
    """A key for one agent runtime, acting for `owner`: it may start tasks, have its tool calls checked, report what
    ran and read the receipt, but it can never confirm or widen a work order, approve its own questions, register a
    business or issue keys. Shown once; only its hash is stored."""
    store = ring.agent_store
    if store is None or owner.agent or owner.scope not in ("project", "admin"):
        raise HTTPException(403, "only a person's own key can issue agent keys")
    if len(store.query("SELECT id FROM agent_keys WHERE owner = ? AND revoked IS NULL", (owner.name,))) >= AGENT_PER_OWNER:
        raise HTTPException(429, "too many agent keys; revoke one first")
    key = AGENT_PREFIX + secrets.token_urlsafe(24)
    now = time.time()
    kid = "ak-" + secrets.token_hex(4)
    store.execute("INSERT INTO agent_keys VALUES (?,?,?,?,?,?,?)",
                  (_h(key), kid, owner.name, (label or "my agent")[:60], now, now + AGENT_DAYS * 86400, None))
    return {"key": key, "id": kid, "owner": owner.name, "label": (label or "my agent")[:60],
            "expires": now + AGENT_DAYS * 86400,
            "note": "Shown once. Give it to your agent. It can act inside your work orders, never approve them."}


def human_only(p: Principal) -> None:
    """Confirming, widening, approving, registering a business and issuing keys are for people, not agent keys."""
    if p.agent:
        raise HTTPException(403, "an agent key cannot do this; the person who delegated the task must")


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
