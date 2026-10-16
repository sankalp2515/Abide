"""Outbound scan: stop secrets, credentials and canaries from leaving through a tool call.

Uses detect-secrets (Apache-2.0) specific detectors (entropy detectors excluded: too noisy on prose),
plus literal canaries and values registered at runtime. Hits are reported by type only, never by value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from detect_secrets.core.scan import scan_line
from detect_secrets.settings import default_settings

import base64
import binascii
import re
from urllib.parse import unquote

_B64 = re.compile(r"[A-Za-z0-9+/_-]{12,}={0,2}")
_HEX = re.compile(r"(?:[0-9A-Fa-f]{2}){8,}")

_NOISY = ("High Entropy",)


@dataclass(frozen=True)
class OutboundHit:
    kind: str          # "canary" | "secret:<detector>" | "registered"
    field: str         # argument name where it was found


def _strings(args: Any, prefix: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(args, dict):
        for k, v in args.items():
            yield from _strings(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(args, (list, tuple)):
        for i, v in enumerate(args):
            yield from _strings(v, f"{prefix}[{i}]")
    elif args is not None:
        yield prefix or "value", str(args)


def _views(value: str) -> list[str]:
    """The value plus every plausible decoding of it. Unlike the inbound decoder this keeps decodings that do not read
    like prose: a secret is a token, not a sentence."""
    out = [value, value[::-1], unquote(value)]
    out.append("".join(chr(ord(c) - 0xE0000) for c in value if 0xE0020 <= ord(c) <= 0xE007E))   # tag characters
    for m in _B64.finditer(value):
        s = m.group(0)
        try:
            out.append(base64.b64decode(s.replace("-", "+").replace("_", "/") + "=" * (-len(s) % 4)).decode("utf-8", "ignore"))
        except (binascii.Error, ValueError):
            pass
    for m in _HEX.finditer(value):
        out.append(bytes.fromhex(m.group(0)).decode("utf-8", "ignore"))
    return [v for v in out if v]


def scan_args(args: Any, canaries: Iterable[str] = (), registered: Iterable[str] = ()) -> list[OutboundHit]:
    canaries = [c for c in canaries if c and len(c) >= 6]
    registered = [r for r in registered if r and len(r) >= 6]
    hits: list[OutboundHit] = []
    with default_settings():
        for field_name, value in _strings(args):
            # The value as sent and every decoded view of it (base64, hex, %-encoding, tag characters, rot13):
            # protected data must not leave encoded either (docs/13 L3).
            views = [v.lower() for v in _views(value)]
            if any(c.lower() in v for c in canaries for v in views):
                hits.append(OutboundHit("canary", field_name))
            if any(r.lower() in v for r in registered for v in views):
                hits.append(OutboundHit("registered", field_name))
            for line in value.splitlines() or [value]:
                for s in scan_line(line):
                    if not any(n in s.type for n in _NOISY):
                        hits.append(OutboundHit(f"secret:{s.type}", field_name))
    return sorted(set(hits), key=lambda h: (h.field, h.kind))
