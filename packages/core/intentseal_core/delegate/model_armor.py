"""Google Cloud Model Armor as a second screen in the content filter (step ④).

IntentSeal's engine inspects everything an agent reads first (rules, local classifier, hidden-text removal). Model
Armor then screens what the agent would actually receive. A prompt-injection / jailbreak or malicious-URL match holds
that content: the agent gets a notice instead, and the text stays tracked as untrusted so the gate can trace anything
it causes. Model Armor adds to the engine's decision and never relaxes it; if it cannot be reached, the engine's
decision stands and the receipt says Model Armor was unavailable.

Configure with the template's resource name:

    INTENTSEAL_MODEL_ARMOR_TEMPLATE=projects/<project>/locations/<location>/templates/<template>

Credentials are Google Application Default Credentials (the service's identity on Cloud Run), as for Vertex AI.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("intentseal.delegate.model_armor")
TEMPLATE_RE = re.compile(r"^projects/([^/]+)/locations/([^/]+)/templates/([^/]+)$")
BLOCKING = ("pi_and_jailbreak", "malicious_uris")      # what holds content; other filters are recorded only
MAX_CHARS = 50_000


@dataclass
class Screen:
    checked: bool
    match: bool = False
    filters: list[str] = field(default_factory=list)     # the blocking filters that matched
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"checked": self.checked, "match": self.match, "filters": self.filters, "error": self.error}


class ModelArmor:
    def __init__(self, template: str, timeout_s: float = 6.0, client: httpx.AsyncClient | None = None) -> None:
        m = TEMPLATE_RE.match(template.strip())
        if not m:
            raise ValueError("Model Armor template must be projects/<p>/locations/<l>/templates/<t>")
        self.template, self.project, location = template.strip(), m.group(1), m.group(2)
        self.url = f"https://modelarmor.{location}.rep.googleapis.com/v1/{self.template}:sanitizeUserPrompt"
        self.timeout_s, self._client = timeout_s, client

    @classmethod
    def from_env(cls) -> "ModelArmor | None":
        template = os.environ.get("INTENTSEAL_MODEL_ARMOR_TEMPLATE", "").strip()
        if not template:
            return None
        try:
            return cls(template)
        except ValueError as e:
            log.warning("Model Armor disabled: %s", e)
            return None

    async def _token(self) -> str:
        from ..llm.vertex_adapter import _TOKEN              # the same cached Google credentials as Vertex AI
        return await asyncio.to_thread(_TOKEN.get)

    async def screen(self, text: str) -> Screen:
        if not text.strip():
            return Screen(checked=True)
        try:
            token = await self._token()
            client = self._client or httpx.AsyncClient()
            try:
                resp = await client.post(self.url, timeout=self.timeout_s,
                                         headers={"Authorization": f"Bearer {token}",
                                                  "x-goog-user-project": self.project},
                                         json={"userPromptData": {"text": text[:MAX_CHARS]}})
            finally:
                if self._client is None:
                    await client.aclose()
            if resp.status_code >= 400:
                return Screen(checked=False, error=f"HTTP {resp.status_code}")
            result = resp.json().get("sanitizationResult") or {}
        except Exception as e:                               # network, credentials: the engine's decision stands
            return Screen(checked=False, error=type(e).__name__)
        found = []
        for name, r in (result.get("filterResults") or {}).items():
            inner = next((v for v in (r or {}).values() if isinstance(v, dict)), {})
            if name in BLOCKING and inner.get("matchState") == "MATCH_FOUND":
                found.append(name)
        return Screen(checked=True, match=bool(found), filters=sorted(found))
