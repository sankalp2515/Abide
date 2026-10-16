"""CORE-17 Origin/taint tracking through the agent's data flow.

Every piece of content the engine inspects in a session is remembered as a fingerprint (hashed word
5-shingles, the same fingerprint the attack memory uses; no text is kept) together with its origin: the
source channel, the event id of the inspection, whether it was trusted (the user's own words) and whether
it was flagged (any decision other than ALLOW, or a judge risk at the neutralise threshold).

When a tool call is checked, each string argument is traced back: the share of its shingles that came from
each origin. Text the user wrote is never tainted, even if a document repeats it. The action guard uses the
trace to stop data copied out of flagged content from reaching a high-risk tool, and records every traced
flow with the decision so a replay shows where an argument's text came from.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Iterable

from .memory import shingles


@dataclass
class Origin:
    id: str                  # event id of the inspection (or a generated id)
    source: str              # e.g. "web", "pdf", "tool:read_email", "user"
    trusted: bool
    flagged: bool
    fingerprint: set[int] = field(default_factory=set)


@dataclass(frozen=True)
class TaintHit:
    arg: str                 # argument path, e.g. "body" or "items[0].text"
    origin: str              # origin id (event id)
    source: str
    flagged: bool
    coverage: float          # share of the argument's shingles found in that origin

    def as_dict(self) -> dict[str, Any]:
        return {"arg": self.arg, "origin": self.origin, "source": self.source, "flagged": self.flagged,
                "coverage": round(self.coverage, 2)}


def _strings(value: Any, prefix: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            yield from _strings(v, f"{prefix}[{i}]")
    elif isinstance(value, str):
        yield prefix or "value", value


class TaintTracker:
    """Per-session origins, bounded: the oldest untrusted origins are dropped beyond `max_origins`."""

    def __init__(self, max_origins: int = 200, min_shingles: int = 3) -> None:
        self.origins: OrderedDict[str, Origin] = OrderedDict()
        self.trusted: set[int] = set()
        self.max_origins = max_origins
        self.min_shingles = min_shingles

    def observe(self, text: str, origin_id: str, source: str, trusted: bool, flagged: bool) -> None:
        fp = shingles(text or "")
        if not fp:
            return
        if trusted:
            self.trusted |= fp
            return
        self.origins[origin_id] = Origin(origin_id, source, False, flagged, fp)
        self.origins.move_to_end(origin_id)
        while len(self.origins) > self.max_origins:
            self.origins.popitem(last=False)

    def trace(self, args: Any, min_coverage: float = 0.3) -> list[TaintHit]:
        hits: list[TaintHit] = []
        for path, value in _strings(args):
            fp = shingles(value) - self.trusted          # the user's own words are never taint
            if len(fp) < self.min_shingles:
                continue
            for o in self.origins.values():
                n = len(fp & o.fingerprint)
                if n >= self.min_shingles and n / len(fp) >= min_coverage:
                    hits.append(TaintHit(path, o.id, o.source, o.flagged, n / len(fp)))
        return hits
