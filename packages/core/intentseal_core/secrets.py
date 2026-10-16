"""Secret handling: keys are loaded from the environment, referred to by alias, and
scrubbed from every log record and string we persist."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ApiKey:
    """A provider key. `repr`/`str` never reveal the value."""

    alias: str
    value: str = field(repr=False)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.alias


class _Registry:
    def __init__(self) -> None:
        self._values: set[str] = set()

    def register(self, value: str) -> None:
        if value and len(value) >= 8:
            self._values.add(value)

    def redact(self, text: str) -> str:
        if not text:
            return text
        for v in self._values:
            if v in text:
                text = text.replace(v, "[REDACTED]")
        return _GENERIC_KEY_RE.sub("[REDACTED]", text)


# Common provider key shapes, as a backstop when a value was never registered.
_GENERIC_KEY_RE = re.compile(
    r"(gsk_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_\-]{30,}|sk-or-v1-[A-Za-z0-9]{20,}|"
    r"nvapi-[A-Za-z0-9_\-]{20,}|sk-[A-Za-z0-9]{32,})"
)

registry = _Registry()


def redact(text: str) -> str:
    return registry.redact(text)


def load_keys(provider: str, env_prefix: str | None, env_list: str | None) -> list[ApiKey]:
    """Read any number of keys: PREFIX, PREFIX_1..N (any gaps tolerated) and a comma list."""
    values: list[str] = []
    if env_prefix:
        if os.environ.get(env_prefix):
            values.append(os.environ[env_prefix])
        pattern = re.compile(rf"^{re.escape(env_prefix)}_(\d+)$")
        numbered = sorted(
            (int(m.group(1)), name)
            for name in os.environ
            if (m := pattern.match(name))
        )
        values.extend(os.environ[name] for _, name in numbered)
    if env_list and os.environ.get(env_list):
        values.extend(v.strip() for v in os.environ[env_list].split(","))
    seen: set[str] = set()
    keys: list[ApiKey] = []
    for v in values:
        v = v.strip().strip('"').strip("'")
        if not v or v in seen:
            continue
        seen.add(v)
        registry.register(v)
        keys.append(ApiKey(alias=f"{provider}#{len(keys) + 1}", value=v))
    return keys


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed record: still never leak
            message = str(record.msg)
        record.msg, record.args = redact(message), None
        return True


def install_log_redaction() -> None:
    """Attach the redacting filter to every root handler (idempotent). A library must not configure the host app's
    logging: when it has none, one WARNING handler (what Python would print anyway) is added, with redaction."""
    root = logging.getLogger()
    if not root.handlers:
        h = logging.StreamHandler()
        h.setLevel(logging.WARNING)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(h)
    for h in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in h.filters):
            h.addFilter(RedactingFilter())
