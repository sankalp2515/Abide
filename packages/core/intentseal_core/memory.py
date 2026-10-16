"""Trainability (EVAL-4, CON-8): a memory of reviewer-confirmed attacks and false positives.

Confirmed attacks (adversary-loop misses, reviewer "confirm attack") are matched on later content so a known
attack, or the same attack embedded in a larger document, is caught by rules at Tier 0, without a judge call.
Reviewer "false positive" marks let the exact same content pass the AI tiers next time; structural policy
rules (non-visible content) and the action guard still apply, so a mark can never wave through an action.

Privacy: only fingerprints are stored, never content. A fingerprint is the set of hashed word
5-shingles of the normalised ASCII view (keyed BLAKE2b, 64-bit), plus a SHA-256 of the whole view for exact
false-positive marks. Matching is containment: |stored ∩ new| / |stored|, so an attack quoted inside a long,
otherwise benign document still matches.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Iterable

from anyascii import anyascii

from .store import Store

SHINGLE = 5
_WORD = re.compile(r"[a-z0-9]+")
_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS memory (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, agent_id TEXT NOT NULL, labels TEXT, view_hash TEXT,
        shingles TEXT NOT NULL, n INTEGER NOT NULL, origin TEXT, note TEXT, created_at REAL)""",
    "CREATE INDEX IF NOT EXISTS ix_memory_hash ON memory(view_hash)",
]


def view_of(text: str) -> str:
    # Same folding as the detection view (NFKC, format characters dropped, ASCII transliteration), lower-cased.
    t = "".join(c for c in unicodedata.normalize("NFKC", text) if unicodedata.category(c) != "Cf")
    return anyascii(t).lower()


def view_hash(text: str) -> str:
    return hashlib.sha256(" ".join(_WORD.findall(view_of(text))).encode()).hexdigest()


def shingles(text: str) -> set[int]:
    words = _WORD.findall(view_of(text))
    if len(words) < SHINGLE:
        words = words + [""] * (SHINGLE - len(words)) if words else []
    out = set()
    for i in range(max(0, len(words) - SHINGLE + 1)):
        h = hashlib.blake2b(" ".join(words[i:i + SHINGLE]).encode(), digest_size=8, key=b"asg-memory-v1")   # fixed key: changing it invalidates stored fingerprints
        out.add(int.from_bytes(h.digest(), "big") >> 1)   # fits SQLite/JSON integers
    return out


@dataclass
class Match:
    id: str
    kind: str                       # attack | benign
    score: float                    # containment (attack) or 1.0 (exact benign)
    labels: list[str] = field(default_factory=list)
    origin: str = ""


class AttackMemory:
    def __init__(self, store: Store, min_shingles: int = 4) -> None:
        self.store = store
        self.store.ensure_schema(_SCHEMA)
        self.min_shingles = min_shingles
        self._index: dict[int, set[str]] | None = None
        self._version: tuple | None = None
        self._sizes: dict[str, tuple[int, str, list[str], str]] = {}

    # ---- writes ----------------------------------------------------------------------------
    def add(self, text: str, kind: str, labels: Iterable[str] = (), agent_id: str = "*", origin: str = "",
            note: str = "") -> str | None:
        """Remember `text` as a confirmed attack or a false positive. Returns the entry id (None if too short)."""
        if kind not in ("attack", "benign"):
            raise ValueError("kind must be 'attack' or 'benign'")
        sh = shingles(text)
        if kind == "attack" and len(sh) < self.min_shingles:
            return None
        vh = view_hash(text)
        dup = self.store.query("SELECT id FROM memory WHERE view_hash = ? AND kind = ? AND agent_id = ?",
                               (vh, kind, agent_id))
        if dup:
            return dup[0]["id"]
        mid = uuid.uuid4().hex[:16]
        self.store.execute("INSERT INTO memory VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (mid, kind, agent_id, json.dumps(sorted(set(labels))), vh, json.dumps(sorted(sh)), len(sh),
                            origin[:40], note[:200], time.time()))
        self._index = None
        return mid

    def remove(self, mid: str) -> bool:
        cur = self.store.execute("DELETE FROM memory WHERE id = ?", (mid,))
        self._index = None
        return bool(cur.rowcount)

    # ---- reads -----------------------------------------------------------------------------
    def entries(self, kind: str | None = None, limit: int = 200) -> list[dict]:
        q, p = "SELECT id, kind, agent_id, labels, n, origin, note, created_at FROM memory", []
        if kind:
            q += " WHERE kind = ?"; p.append(kind)
        q += " ORDER BY created_at DESC LIMIT ?"; p.append(limit)
        rows = self.store.query(q, tuple(p))
        for r in rows:
            r["labels"] = json.loads(r["labels"] or "[]")
        return rows

    def stats(self) -> dict[str, int]:
        rows = self.store.query("SELECT kind, COUNT(*) AS n FROM memory GROUP BY kind")
        return {r["kind"]: r["n"] for r in rows}

    def _load(self) -> dict[int, set[str]]:
        # Another process (adversary loop, reviewer API) may have changed the table: reload when it does.
        version = tuple(self.store.query("SELECT COUNT(*) AS n, MAX(created_at) AS t FROM memory")[0].values())
        if version != self._version:
            self._index, self._version = None, version
        if self._index is None:
            index: dict[int, set[str]] = {}
            self._sizes = {}
            for r in self.store.query("SELECT id, agent_id, labels, shingles, n, origin FROM memory WHERE kind = 'attack'"):
                self._sizes[r["id"]] = (r["n"], r["agent_id"], json.loads(r["labels"] or "[]"), r["origin"] or "")
                for s in json.loads(r["shingles"]):
                    index.setdefault(s, set()).add(r["id"])
            self._index = index
        return self._index

    def match(self, text: str, agent_id: str, threshold: float = 0.6) -> Match | None:
        """Exact false-positive mark for this agent first, then the best attack containment >= threshold."""
        index = self._load()
        if self._version and not self._version[0]:
            return None   # empty memory: no cost on the fast path
        benign = self.store.query("SELECT id FROM memory WHERE kind = 'benign' AND view_hash = ? AND agent_id IN (?, '*')",
                                  (view_hash(text), agent_id))
        if benign:
            return Match(benign[0]["id"], "benign", 1.0)
        if not index:
            return None
        hits: dict[str, int] = {}
        for s in shingles(text):
            for mid in index.get(s, ()):
                hits[mid] = hits.get(mid, 0) + 1
        best: Match | None = None
        for mid, k in hits.items():
            n, owner, labels, origin = self._sizes[mid]
            if owner not in ("*", agent_id):
                continue
            score = k / n
            if score >= threshold and (best is None or score > best.score):
                best = Match(mid, "attack", round(score, 3), labels, origin)
        return best
