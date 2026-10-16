"""Shared inspection and action boundary (CORE-1, 4–8, 10, 12, 14, 15).

Structural rules select short classifier inputs; classifiers only advise escalation.
Cached scans/assessments never cache enforcement or session risk.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect as inspectlib
import json
import math
import os
import re
import time
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable

from . import action_guard
from . import contract as contracts
from .classifier import local_classifier
from .decision_model import Assessment, DecisionModel, make_decision_model
from . import markers
from .decode import decode_views
from .extract import ExtractedDoc, extract
from .llm.router import Router
from .logger import DecisionLogger
from .memory import AttackMemory, Match
from .normalise import normalise
from .outbound import scan_args
from .paste import split_pasted
from .policy import AgentPolicy, load_policy, policy_exists
from .secrets import registry
from .session import Session, SessionStore
from .store import Store
from .types import ATTACK_TYPES, ActionResult, Decision, Finding, InspectResult



# Third-party content needs at least this much risk of its own before session risk can hold it (D-063).
SESSION_GATE_FLOOR = 0.3

_TAG_RUN = re.compile("[\U000E0000-\U000E007F]+")


def _emoji_flag(text: str, start: int, end: int) -> bool:
    """A subdivision flag (e.g. England): black flag U+1F3F4, then tag letters, then CANCEL TAG U+E007F."""
    return start > 0 and text[start - 1] == "\U0001F3F4" and text[end - 1] == "\U000E007F" and end - start <= 7

@dataclass
class _Scan:
    doc: ExtractedDoc
    view: str
    findings: list[Finding]
    chunks: list[tuple[int, int, str, str]]
    all_chunks: list[tuple[int, int, str, str]] | None = None   # every chunk, for Tier 1 on unflagged content


def _probability(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("invalid probability")
    return value


def _sync(call: Callable[[], Any]) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(call())
    raise RuntimeError("An event loop is running; await the async Engine method instead")


class Engine:
    def __init__(self, *, policy_loader: Callable[[str], AgentPolicy] = load_policy,
                 decision_model: DecisionModel | None = None, classifiers: Iterable[Any] | None = None,
                 router: Router | None = None, store: Store | None = None,
                 logger: DecisionLogger | None = None, sessions: SessionStore | None = None,
                 memory: AttackMemory | None = None, long_decision_model: DecisionModel | None = None,
                 policy_directory: Path | None = None, tier0_timeout_s: float = 2.0,
                 tier1_timeout_s: float = 1.0, tier2_timeout_s: float = 15.0,
                 segment_chars: int = 1000, encoded_min_chars: int = 48,
                 judge_window_chars: int = 6000, judge_max_windows: int | None = None, max_text_chars: int | None = None,
                 cache_size: int = 256, cache_ttl_s: float = 3600,
                 clock: Callable[[], float] = time.time) -> None:
        if min(tier0_timeout_s, tier1_timeout_s, tier2_timeout_s, segment_chars, encoded_min_chars) <= 0:
            raise ValueError("time budgets and scan sizes must be positive")
        if cache_size < 0 or cache_ttl_s < 0:
            raise ValueError("cache limits must be non-negative")
        if logger is not None and store is not None and logger.store is not store:
            raise ValueError("logger and engine must share a store")
        self.router = router
        self.store = logger.store if logger else (store or (router.store if router else Store(":memory:")))
        self.logger = logger or DecisionLogger(self.store)
        self.memory = memory or AttackMemory(self.store)
        # Recent extracted text by event id (in process only, never persisted) so a reviewer can confirm an
        # attack or mark a false positive on a fresh decision; the memory itself keeps fingerprints only.
        self._recent: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self.recent_size = 500
        self.sessions = sessions or SessionStore()
        self.policy_loader = (lambda agent: load_policy(agent, policy_directory)) if policy_directory else policy_loader
        self._policy_files = (policy_directory, policy_directory is not None or policy_loader is load_policy)
        self.logger.org_of = self._org_of
        self.decision_model = decision_model
        # Documents the judge windows cannot cover are assessed whole by a long-context model (role `judge_long`)
        self.long_decision_model = long_decision_model
        self.long_judge_chars = int(os.environ.get("INTENTSEAL_LONG_JUDGE_MAX_CHARS") or 600_000)
        self.long_judge_timeout_s = float(os.environ.get("INTENTSEAL_LONG_JUDGE_TIMEOUT_S") or 90)
        self.long_judge_min_windows = int(os.environ.get("INTENTSEAL_LONG_JUDGE_MIN_WINDOWS") or 3)
        self.classifiers = None if classifiers is None else list(classifiers)
        self.tier0_timeout_s, self.tier1_timeout_s, self.tier2_timeout_s = (
            tier0_timeout_s, tier1_timeout_s, tier2_timeout_s)
        self.segment_chars = segment_chars
        self.judge_window_chars = judge_window_chars
        self.judge_max_windows = judge_max_windows or int(os.environ.get("INTENTSEAL_JUDGE_MAX_WINDOWS") or 8)
        self.tier1_per_chunk_s = float(os.environ.get("INTENTSEAL_TIER1_PER_CHUNK_S") or 0.75)
        self.tier1_max_s = float(os.environ.get("INTENTSEAL_TIER1_MAX_S") or 30)
        self.max_text_chars = max_text_chars or int(os.environ.get("INTENTSEAL_MAX_TEXT_CHARS") or 1_000_000)
        self._encoded = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{" + str(encoded_min_chars) + r",}={0,2}")
        self.cache_size, self.cache_ttl_s, self.clock = cache_size, cache_ttl_s, clock
        self._cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._models: dict[str, DecisionModel] = {}
        self._secrets: list[str] = []
        self._agent_locks: dict[str, asyncio.Lock] = {}

    def _policy(self, agent_id: str) -> AgentPolicy:
        policy = self.policy_loader(agent_id)
        self.register_secrets(policy.canary_values())
        return policy

    def _org_of(self, agent_id: str) -> str | None:
        try:
            return self.policy_loader(agent_id).org
        except Exception:
            return None

    def has_policy(self, agent_id: str) -> bool:
        """Is this agent configured? Policies come from files unless a custom loader was given."""
        directory, from_files = self._policy_files
        return policy_exists(agent_id, directory) if from_files else True

    def register_secrets(self, values: Iterable[str]) -> None:
        for value in values:
            if value and value not in self._secrets:
                self._secrets.append(value)
                registry.register(value)
                self.logger.add_secrets([value])

    def start_session(self, agent_id: str, user_ref: str | None = None, task: str = "",
                      trusted: dict[str, Any] | None = None, session_id: str | None = None) -> Session:
        self._policy(agent_id)
        session = self.sessions.create(agent_id, user_ref=user_ref, task=task, trusted=dict(trusted or {}),
                                       session_id=session_id)
        self.logger.session(session.session_id, agent_id, session.user_ref, task)
        self._contract(session, self._policy(agent_id))
        return session

    def _contract(self, session: Session, policy: AgentPolicy) -> Any:
        """D-064: the session's intent contract, compiled from the policy and the task on first use."""
        if policy.contract.mode == "off":
            return None
        if session.contract is None or session.contract.agent_id != session.agent_id:
            session.contract = contracts.compile_contract(session.agent_id, session.session_id, policy, session.task)
            self.logger.contract(session.contract)
        return session.contract

    def _amend_contract(self, session: Session, policy: AgentPolicy, text: str, origin: str) -> None:
        """Only the user's own words reach this (the task, user turns that were not held)."""
        current = self._contract(session, policy)
        if current is None:
            return
        session.contract = contracts.amend(current, text, origin)
        if session.contract.version != current.version:
            self.logger.contract(session.contract)

    def contract(self, agent_id: str, session_id: str) -> dict[str, Any] | None:
        """The current contract of a live session (None when contracts are off for the agent)."""
        policy = self._policy(agent_id)
        c = self._contract(self._session(agent_id, session_id), policy)
        return c.as_dict() if c is not None else None

    def _session(self, agent_id: str, session_id: str | None) -> Session:
        session = self.sessions.get(session_id, agent_id)
        if session.agent_id != agent_id:
            raise ValueError("session belongs to a different agent")
        self.logger.session(session.session_id, agent_id, session.user_ref, session.task)
        return session

    def _get(self, key: str) -> Any:
        hit = self._cache.get(key)
        if hit is None:
            return None
        if self.clock() >= hit[0]:
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return hit[1]

    def _put(self, key: str, value: Any) -> None:
        if not self.cache_size or not self.cache_ttl_s:
            return
        self._cache[key] = (self.clock() + self.cache_ttl_s, value)
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)

    @staticmethod
    def _key(*parts: Any) -> str:
        return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()

    def _scan(self, content: str | bytes, source: str, filename: str | None, fmt: str | None) -> _Scan:
        doc = extract(content, fmt=fmt, filename=filename, source=source)
        findings, chunks, views, all_chunks = [], [], [], []
        for segment in doc.segments:
            seg_norm = normalise(segment.text)
            views.append(seg_norm.text)
            if seg_norm.flags & {"mixed_script", "confusable_characters"}:
                views.append("[ascii view] " + (seg_norm.skeleton or seg_norm.ascii_view))   # look-alikes folded by shape
            encoded = list(self._encoded.finditer(segment.text))
            forged = markers.find(segment.text)   # forged role / tool-call markers (docs/13 L4)
            # Unicode tag characters are an invisible copy of ASCII: outside an emoji flag sequence they only ever carry
            # text a reader cannot see (ASCII smuggling), so they are quarantined whatever the judge says (D-060).
            for m in _TAG_RUN.finditer(segment.text):
                if not _emoji_flag(segment.text, m.start(), m.end()):
                    findings.append(Finding(span=(segment.start + m.start(), segment.start + m.end()), preview="",
                                            origin=segment.origin, signal="invisible-characters",
                                            labels=["Encoded Instructions"]))
            for m in forged:
                findings.append(Finding(span=(segment.start + m.start(), segment.start + m.end()), preview=m.group(0),
                                        origin=segment.origin, signal="role-marker", labels=["Role Change"]))
            for offset in range(0, len(segment.text), self.segment_chars):
                text = segment.text[offset:offset + self.segment_chars]
                norm = normalise(text)
                span = (segment.start + offset, segment.start + offset + len(text))
                signals = sorted(norm.flags)
                if not segment.visible:
                    signals.append("non-visible-content")
                if any(m.start() < offset + len(text) and m.end() > offset for m in encoded):
                    signals.append("encoded-blob")
                if any(m.start() < offset + len(text) and m.end() > offset for m in forged):
                    signals.append("role-markers")
                for signal in signals:
                    labels = {"non-visible-content": ["Indirect Prompt Injection"],
                              "role-markers": ["Role Change"]}.get(signal, ["Encoded Instructions"])
                    findings.append(Finding(span=span, preview=text, origin=segment.origin,
                                            signal=signal, labels=labels))
                if norm.changes:
                    findings.append(Finding(span=span, preview=text, origin=segment.origin,
                                            signal="normalisation:" + json.dumps(norm.changes, sort_keys=True),
                                            labels=["Encoded Instructions"]))
                for _, view in norm.views():
                    for start in range(0, len(view), self.segment_chars):
                        chunk = (*span, segment.origin, view[start:start + self.segment_chars])
                        all_chunks.append(chunk)
                        if signals:
                            chunks.append(chunk)
            # CORE-3: decode before scoring. Decoded text is an extra view (always escalated); spans point at the
            # encoded run in the original, so a neutralise quarantines the encoded text itself.
            for d in decode_views(segment.text):
                span = (segment.start + d.span[0], segment.start + d.span[1])
                findings.append(Finding(span=span, preview=d.text, origin=segment.origin, signal="decoded:" + d.kind,
                                        labels=["Encoded Instructions"]))
                views.append(f"[decoded {d.kind}] {normalise(d.text).text}")
                for start in range(0, len(d.text), self.segment_chars):
                    chunk = (*span, segment.origin, normalise(d.text[start:start + self.segment_chars]).ascii_view)
                    chunks.append(chunk)
                    all_chunks.append(chunk)
        return _Scan(doc, "\n".join(views), findings, chunks, all_chunks)

    async def _scores(self, chunks: list[tuple[int, int, str, str]]) -> list[Finding]:
        scorers = self.classifiers
        if scorers is None:
            scorer = await asyncio.to_thread(local_classifier)
            if scorer is None:
                raise RuntimeError("local classifier unavailable")
            scorers = [scorer]
        findings = []
        for start, end, origin, text in chunks:
            for scorer in scorers:
                if inspectlib.iscoroutinefunction(scorer.score):
                    value = await scorer.score(text)
                else:
                    value = await asyncio.to_thread(scorer.score, text)
                    if inspectlib.isawaitable(value):
                        value = await value
                if value is None:
                    raise RuntimeError("classifier unavailable")
                findings.append(Finding(span=(start, end), origin=origin, signal="classifier",
                                        labels=["Indirect Prompt Injection"], score=_probability(value), tier=1))
        return findings

    def _tokens_today(self, agent_id: str) -> int:
        now = self.clock()
        start = now - now % 86400
        rows = self.store.query("SELECT COALESCE(SUM(input_tokens), 0) AS tokens FROM decisions "
                                "WHERE agent_id = ? AND created_at >= ?", (agent_id, start))
        return int(rows[0]["tokens"])

    def _judge_windows(self, scan: _Scan, priority: dict[tuple[int, int], float] | None = None
                       ) -> tuple[list[tuple[str, list[tuple[int, int]]]], list[tuple[int, int]]]:
        """CORE-16: what the judge reads. A document whose detection view fits one window is judged whole (with its
        context). A longer one is judged as windows of its hot chunks (structurally flagged, or scored at or above the
        policy's threshold), each window carrying the original spans it covers; at most `judge_max_windows` windows,
        the most suspicious chunks packed first. Returns the windows and the spans that did not fit (never silently
        dropped: the caller decides on them)."""
        if len(scan.view) <= self.judge_window_chars:
            return [(scan.view, [])], []   # judged whole: no span list means "the whole document"
        views: dict[tuple[int, int], list[str]] = {}
        for start, end, _, text in scan.chunks:
            views.setdefault((start, end), []).append(text)
        # The most suspicious chunks are packed first: unscored (structural) ones, then by Tier 1 score, so a few
        # flagged parts spread over a long page share windows instead of each claiming one (D-051). Document order
        # breaks ties.
        rank = lambda s: (priority or {}).get(s, 1.0)
        ordered = sorted(views, key=lambda s: (-rank(s), s))
        windows: list[tuple[str, list[tuple[int, int]]]] = []
        text, spans = "", []
        for i, span in enumerate(ordered):
            piece = "\n".join(dict.fromkeys(views[span]))[: self.judge_window_chars]
            if text and len(text) + len(piece) + 1 > self.judge_window_chars:
                windows.append((text, spans))
                text, spans = "", []
                if len(windows) == self.judge_max_windows:
                    return windows, ordered[i:]
            text, spans = (text + "\n" + piece if text else piece), spans + [span]
        if text:
            windows.append((text, spans))
        return windows, []

    async def _judge(self, scan: _Scan, source: str, task: str, policy: AgentPolicy, agent_id: str,
                     priority: dict[tuple[int, int], float] | None = None
                     ) -> tuple[Assessment, bool, list[tuple[int, int]]]:
        """Tier 2 over the judge windows. Returns the combined assessment (highest risk wins; usage summed; degraded
        if any window could not be assessed), whether the daily budget stopped it, and the spans left unjudged."""
        windows, unjudged = self._judge_windows(scan, priority)
        cap = policy.budgets.daily_tokens_tier2
        many = bool(unjudged) or len(windows) >= self.long_judge_min_windows
        long_model = self._long_decision_model(policy) if many and len(scan.view) <= self.long_judge_chars else None
        if long_model is not None:
            # A long document: one whole-document assessment by a long-context model (one call instead of many
            # windows; measured 3-7 s for 30K-300K characters). If it is unavailable, the windows below run and
            # whatever they cannot cover is held (CORE-16).
            key = self._key("judge-long", scan.view, source, task, agent_id, policy.model_dump())
            cached = self._get(key)
            if cached is None and cap is not None and self._tokens_today(agent_id) >= cap:
                return Assessment(0, degraded=True), True, unjudged
            whole = (Assessment(**{**cached.__dict__, "input_tokens": 0, "output_tokens": 0, "est_cost": 0.0})
                     if cached is not None else
                     await self._assess(long_model, key, scan.view, source, task, timeout=self.long_judge_timeout_s))
            if not whole.degraded:
                whole.spans = []   # judged whole: evidence is searched in the whole document
                return whole, False, []
        results: list[Assessment | None] = []
        pending: list[tuple[int, str, str]] = []
        for i, (text, _) in enumerate(windows):
            # Agent/policy/task/source isolate assessments; state and enforcement are recomputed.
            key = self._key("judge", text, source, task, agent_id, policy.model_dump())
            cached = self._get(key)
            results.append(None if cached is None else
                           Assessment(**{**cached.__dict__, "input_tokens": 0, "output_tokens": 0, "est_cost": 0.0}))
            if cached is None:
                pending.append((i, key, text))
        if pending and cap is not None and self._tokens_today(agent_id) >= cap:
            return Assessment(0, degraded=True), True, unjudged
        if pending:
            model = self._decision_model(policy)
            sem = asyncio.Semaphore(3)

            async def one(i: int, key: str, text: str) -> None:
                async with sem:
                    results[i] = await self._assess(model, key, text, source, task)

            await asyncio.gather(*(one(*p) for p in pending))
        combined = Assessment(0.0)
        for (_, spans), a in zip(windows, results):
            assert a is not None
            combined.degraded |= a.degraded
            combined.input_tokens += a.input_tokens
            combined.output_tokens += a.output_tokens
            combined.est_cost += a.est_cost
            combined.model = combined.model or a.model
            if a.degraded:
                continue
            for k, v in a.categories.items():
                combined.categories[k] = max(v, combined.categories.get(k, 0.0))
            if a.malicious > 0:
                combined.evidence += [q for q in a.evidence if q not in combined.evidence]
                combined.spans += [s for s in spans if s not in combined.spans]
            combined.malicious = max(combined.malicious, a.malicious)
        return combined, False, unjudged

    def _decision_model(self, policy: AgentPolicy) -> DecisionModel:
        model = self.decision_model
        if model is None:
            model = self._models.get(policy.decision_model)
            if model is None:
                if policy.decision_model == "generative_free" and self.router is None:
                    self.router = Router(store=self.store)
                model = make_decision_model(policy.decision_model, self.router)
                self._models[policy.decision_model] = model
        return model

    def _long_decision_model(self, policy: AgentPolicy) -> DecisionModel | None:
        if self.long_decision_model is not None or self.decision_model is not None:
            return self.long_decision_model   # injected models (tests, custom decision models): only if given
        if policy.decision_model != "generative_free" or self.router is None or "judge_long" not in self.router.settings.roles:
            return None
        if "judge_long" not in self._models:
            from .decision_model import GenerativeJudge
            self._models["judge_long"] = GenerativeJudge(self.router, role="judge_long", max_chars=self.long_judge_chars)
        return self._models["judge_long"]

    async def _assess(self, model: DecisionModel, key: str, text: str, source: str, task: str,
                      timeout: float | None = None) -> Assessment:
        try:
            assessment = await asyncio.wait_for(model.assess(text, source, task), timeout or self.tier2_timeout_s)
            _probability(assessment.malicious)
            for value in assessment.categories.values():
                _probability(value)
            if assessment.input_tokens < 0 or not math.isfinite(assessment.est_cost) or assessment.est_cost < 0:
                raise ValueError("invalid judge usage")
        except Exception:
            return Assessment(0, degraded=True)
        if not assessment.degraded:
            self._put(key, deepcopy(assessment))
        return assessment

    @staticmethod
    def _judge_findings(scan: _Scan, assessment: Assessment, labels: list[str]) -> list[Finding]:
        # Evidence quotes are located in the original text: anywhere when the document was judged whole, otherwise
        # inside the windows the judge called malicious. Never detection-view offsets.
        text = scan.doc.text
        regions = sorted(assessment.spans) or [(0, len(text))]
        spans: set[tuple[int, int]] = set()
        for quote in assessment.evidence:
            if not quote:
                continue
            for lo, hi in regions:
                start = lo
                while (pos := text.find(quote, start, min(len(text), hi + len(quote)))) >= 0:
                    spans.add((pos, pos + len(quote)))
                    start = pos + len(quote)
        if not spans:   # quote not found verbatim (normalised/decoded view): the windows' own chunk spans
            spans = set(assessment.spans) or {(start, end) for start, end, _, _ in scan.chunks}
        return [Finding(span=span, preview=scan.doc.text[span[0]:span[1]],
                        origin=next((s.origin for s in scan.doc.segments if s.start <= span[0] < s.end), ""),
                        signal="judge", labels=labels, score=assessment.malicious, tier=2)
                for span in sorted(spans)]

    @staticmethod
    def _quarantine(text: str, spans: list[tuple[int, int]]) -> str:
        merged: list[tuple[int, int]] = []
        for start, end in sorted(set(spans)):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        for start, end in reversed(merged):
            text = text[:start] + "[QUARANTINED]" + text[end:]
        # A fresh boundary cannot be closed by a marker already present in the data.
        import secrets

        boundary = "UNTRUSTED-" + secrets.token_hex(8)
        return f"<<{boundary}>>\n{text}\n<</{boundary}>>"

    def _remember_recent(self, event_id: str | None, agent_id: str, text: str) -> None:
        if event_id and self.recent_size:
            self._recent[event_id] = (agent_id, text)
            while len(self._recent) > self.recent_size:
                self._recent.popitem(last=False)

    def feedback(self, verdict: str, *, event_id: str | None = None, content: str | bytes | None = None,
                 agent_id: str = "*", labels: Iterable[str] = (), origin: str = "reviewer", note: str = "",
                 source: str = "", filename: str | None = None, fmt: str | None = None) -> str | None:
        """Record a reviewer verdict (CON-8) or an adversary-loop miss (EVAL-4) in the memory.

        verdict: "attack" (confirmed attack) or "false_positive". Content comes from `content` (extracted the same
        way inspect() extracts it) or from the recent in-process text of `event_id`; only fingerprints are stored."""
        if content is not None:
            content = extract(content, fmt=fmt, filename=filename, source=source).text
        else:
            hit = self._recent.get(event_id or "")
            if hit is None:
                raise KeyError("event not in recent memory; pass the content instead")
            agent_id = hit[0] if agent_id == "*" and verdict == "false_positive" else agent_id
            content = hit[1]
        kind = {"attack": "attack", "false_positive": "benign"}[verdict]
        return self.memory.add(content, kind, labels, agent_id=agent_id, origin=origin, note=note)

    async def inspect(self, content: str | bytes, source: str, agent_id: str, session_id: str | None = None,
                      filename: str | None = None, fmt: str | None = None, task: str | None = None) -> InspectResult:
        # Serialise agent decisions through accounting/logging, including concurrent sessions.
        async with self._agent_locks.setdefault(agent_id, asyncio.Lock()):
            cut = split_pasted(content) if source == "user" and isinstance(content, str) and not (filename or fmt) else None
            if cut:
                return await self._inspect_pasted(content, cut, agent_id, session_id, task)
            return await self._inspect(content, source, agent_id, session_id, filename, fmt, task)

    async def _inspect_pasted(self, content: str, cut: tuple[int, int], agent_id: str, session_id: str | None,
                              task: str | None) -> InspectResult:
        """A user turn carrying pasted material (D-060): the user's own words are inspected as the user's, the pasted
        block as third-party content. A poisoned paste is quarantined in place; the user's request still goes through."""
        start, end = cut
        before, pasted, after = content[:start], content[start:end], content[end:]
        own = await self._inspect(before + after, "user", agent_id, session_id, None, None, task)
        material = await self._inspect(pasted, "pasted", agent_id, session_id, None, None, None, new_turn=False)
        severity = [Decision.ALLOW, Decision.NEUTRALISE, Decision.ESCALATE, Decision.BLOCK]
        if severity.index(own.decision) >= severity.index(Decision.ESCALATE) or material.decision == Decision.ALLOW:
            decision, rule, reason = own.decision, own.policy_rule, own.reason
        elif material.decision == Decision.NEUTRALISE:
            decision, rule, reason = Decision.NEUTRALISE, material.policy_rule, "In the pasted material: " + material.reason
        else:
            decision, rule, reason = (Decision.NEUTRALISE, "pasted-content-quarantined",
                                      "Pasted material held by policy (" + material.policy_rule + "); the request is kept")
        if own.enforced in (Decision.BLOCK, Decision.ESCALATE):
            cleaned = own.cleaned_content
        else:
            body = material.cleaned_content if material.enforced != Decision.ALLOW else pasted
            head = own.cleaned_content[:len(before)] if own.enforced == Decision.NEUTRALISE else before
            cleaned = head + body + (own.cleaned_content[len(before):] if own.enforced == Decision.NEUTRALISE else after)
        enforced = decision if self._policy(agent_id).mode == "enforce" else Decision.ALLOW
        findings = own.findings + [f.model_copy(update={"span": (f.span[0] + start, f.span[1] + start)} if f.span else {})
                                   for f in material.findings]
        lead = material if material.decision != Decision.ALLOW else own
        return InspectResult(decision=decision, enforced=enforced, cleaned_content=cleaned, findings=findings,
                             labels=sorted(set(own.labels) | set(material.labels)), risk=max(own.risk, material.risk),
                             session_risk=material.session_risk, policy_rule=rule, reason=reason,
                             tier=max(own.tier, material.tier), degraded=own.degraded or material.degraded,
                             budget_degraded=own.budget_degraded or material.budget_degraded,
                             latency_ms=round(own.latency_ms + material.latency_ms, 2), event_id=lead.event_id)

    async def _inspect(self, content, source, agent_id, session_id, filename, fmt, task,
                       new_turn: bool = True) -> InspectResult:
        t0 = time.perf_counter()
        policy = self._policy(agent_id)
        session = self._session(agent_id, session_id)
        if new_turn:   # pasted material is part of the same turn as the request it came with
            session.new_turn(policy.session.risk_decay_per_turn)
        if task is not None:
            session.task = task
            session.note_trusted(task)
            self._amend_contract(session, policy, task, "task")
        assessment, tier, degraded, budget_degraded = Assessment(0), 0, False, False
        unjudged: list[tuple[int, int]] = []
        oversize_hold = False
        key = self._key("scan", hashlib.sha256(content if isinstance(content, bytes) else content.encode()).hexdigest(),
                        source, filename, fmt)
        scan = self._get(key)
        too_large = len(content) > 4 * self.max_text_chars   # CORE-16 input limit (raw size, before any parsing)
        if too_large:
            scan = _Scan(ExtractedDoc(fmt or "text"), "", [], [])
            scan.doc.warnings.append("content too large to inspect")
        if scan is None:
            try:
                scan = await asyncio.wait_for(asyncio.to_thread(self._scan, content, source, filename, fmt),
                                              self.tier0_timeout_s)
                if not scan.doc.warnings:
                    self._put(key, scan)
            except Exception:
                raw = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
                scan = _Scan(ExtractedDoc(fmt or "text", text=raw), "", [], [])
                scan.doc.warnings.append("inspection unavailable")
        too_large = too_large or len(scan.doc.text) > self.max_text_chars
        findings = [f.model_copy(deep=True) for f in scan.findings]
        structural = bool(scan.chunks)   # hidden text, encodings, look-alikes, forged markers (before judge selection)
        if scan.doc.warnings:
            degraded = True
            findings.append(Finding(signal="extraction-unavailable", labels=["Indirect Prompt Injection"]))
        if source == "user":
            session.note_trusted(scan.doc.text)
        else:
            session.note_untrusted(scan.doc.text)
            session.flagged_untrusted |= bool(scan.chunks or scan.doc.warnings)
        if policy.content_checks == "observe":
            return self._observe(session, policy, agent_id, source, content, scan, t0)
        known: Match | None = None
        if policy.memory.enabled and source != "system" and scan.doc.text:
            known = self.memory.match(scan.doc.text, agent_id, policy.memory.match_threshold)
        if known is not None and known.kind == "attack":
            # Reviewer-confirmed attack: decided by rules at Tier 0, no AI call needed.
            findings.append(Finding(signal="known-attack:" + known.id, labels=known.labels or (
                ["Instruction Override"] if source == "user" else ["Indirect Prompt Injection"]), score=known.score))
            if source != "user":
                session.flagged_untrusted = True
        elif known is not None:
            pass   # reviewer-marked false positive: skip the AI tiers; structural rules below still apply
        elif scan.chunks:
            # Structural suspicion (hidden prose, encodings, look-alikes, forged markers) always goes to the judge and
            # low classifier scores cannot clear it. The rest of the document is scored by Tier 1 like any other
            # content, so a structural flag never shields the visible text from inspection (D-051).
            tier = 1
            flagged = {((c[0], c[1]), c[2]) for c in scan.chunks}
            flagged_spans = {s for s, _ in flagged}
            score_key = self._key("scores-all", key)
            scored = self._get(score_key)
            tier1_failed = False
            if scored is None:
                try:
                    budget = min(self.tier1_max_s, self.tier1_timeout_s * 4 + self.tier1_per_chunk_s * len(scan.all_chunks))
                    scored = await asyncio.wait_for(self._scores(scan.all_chunks), budget)
                    self._put(score_key, scored)
                except Exception:
                    degraded = tier1_failed = True
                    scored = []
            low = policy.thresholds.escalate_band[0]
            if (policy.thresholds.judge_untrusted == "always" and source != "user"
                    and len(scan.doc.text.strip()) >= policy.thresholds.judge_min_chars):
                low = 0.0
            hot = flagged | {(f.span, f.origin) for f in scored if f.score >= low}
            if tier1_failed:
                hot = {((c[0], c[1]), c[2]) for c in scan.all_chunks}
            findings.extend(f.model_copy(deep=True) for f in scored if f.score >= low or f.span in flagged_spans)
            scan = replace(scan, chunks=[c for c in scan.all_chunks if ((c[0], c[1]), c[2]) in hot])
            tier = 2
            # Structurally flagged spans carry no score here, so they rank first (priority 1.0) for the judge windows.
            priority = {f.span: f.score for f in scored if f.span and f.span not in flagged_spans}
            assessment, budget_degraded, unjudged = await self._judge(scan, source, session.task or policy.task_scope,
                                                                      policy, agent_id, priority)
            degraded |= assessment.degraded
            band = policy.thresholds.escalate_band[0]
            # Beyond the judge's window cap: structurally flagged chunks are never cleared unjudged; other chunks are
            # cleared by a Tier 1 score below the escalation band (CORE-16).
            unjudged_hot = [s for s in unjudged if tier1_failed or s in flagged_spans or priority.get(s, 1.0) >= band]
            if unjudged:
                findings.append(Finding(signal=f"judge-coverage-partial:{len(unjudged)}-chunks",
                                        labels=["Indirect Prompt Injection"] if source != "user" else ["Instruction Override"]))
                oversize_hold = bool(unjudged_hot)
        elif scan.all_chunks and source != "system":
            # No structural signal: Tier 1 scores every chunk; only chunks at or above the policy's
            # escalation threshold go to the Tier 2 judge (D-011: the classifier never decides alone).
            tier = 1
            score_key = self._key("scores-all", key)
            scored = self._get(score_key)
            tier1_failed = False
            if scored is None:
                try:
                    # CORE-16: the budget grows with the document (measured ~0.7 s per 1,000-character chunk on CPU)
                    budget = min(self.tier1_max_s, self.tier1_timeout_s * 4 + self.tier1_per_chunk_s * len(scan.all_chunks))
                    scored = await asyncio.wait_for(self._scores(scan.all_chunks), budget)
                    self._put(score_key, scored)
                except Exception:
                    degraded = tier1_failed = True
                    scored = []
            low = policy.thresholds.escalate_band[0]
            if (policy.thresholds.judge_untrusted == "always" and source != "user"
                    and len(scan.doc.text.strip()) >= policy.thresholds.judge_min_chars):
                low = 0.0   # policy: every untrusted document is assessed by the judge (scores still logged)
            hot_spans = {(f.span, f.origin) for f in scored if f.score >= low}
            if tier1_failed:
                # Without Tier 1 nothing can clear the content on the fast path: every chunk goes to the judge, and
                # an unavailable judge then holds it (judge-unavailable-hold / budget-hold) instead of allowing it.
                hot_spans = {((c[0], c[1]), c[2]) for c in scan.all_chunks}
            if hot_spans:
                findings.extend(f.model_copy(deep=True) for f in scored if f.score >= low)
                # a copy: the scan object is cached and must keep its structural chunks only
                scan = replace(scan, chunks=[c for c in scan.all_chunks if ((c[0], c[1]), c[2]) in hot_spans])
                if source != "user":
                    session.flagged_untrusted = True
                tier = 2
                priority = {f.span: f.score for f in scored if f.span}
                assessment, budget_degraded, unjudged = await self._judge(
                    scan, source, session.task or policy.task_scope, policy, agent_id, priority)
                degraded |= assessment.degraded
                # Chunks beyond the judge's window cap: cleared only by a Tier 1 score below the escalation band
                # (the `classifier` rule); anything else there is held (CORE-16).
                band = policy.thresholds.escalate_band[0]
                unjudged_hot = [s for s in unjudged if tier1_failed or priority.get(s, 1.0) >= band]
                if unjudged:
                    findings.append(Finding(signal=f"judge-coverage-partial:{len(unjudged)}-chunks",
                                            labels=["Indirect Prompt Injection"] if source != "user" else ["Instruction Override"]))
                    oversize_hold = bool(unjudged_hot)
        judge_labels = sorted(k for k, score in assessment.categories.items() if k in ATTACK_TYPES and score > 0)
        judge_labels = judge_labels or (["Instruction Override"] if source == "user" else ["Indirect Prompt Injection"])
        if assessment.malicious > 0:   # a degraded window scores 0; the others still count
            findings.extend(self._judge_findings(scan, assessment, judge_labels))
        # Hidden text in web formats (HTML, HTML email, Markdown) is ordinary web UI as often as a hiding place: cookie
        # banners, collapsed menus, link-preview descriptions, licence comments. It always goes to the judge and is
        # quarantined unless the judge read it and cleared the document. Hidden text in PDF and Word files (white,
        # tiny or vanished runs) is almost always deliberate and is quarantined regardless (D-051).
        unjudged_spans = set(unjudged)
        web_cleared = tier == 2 and not assessment.degraded and assessment.malicious < policy.thresholds.neutralise
        cleared = [f for f in findings if f.signal == "non-visible-content" and web_cleared and f.span not in unjudged_spans
                   and f.origin.startswith(("html:", "email:html:", "markdown:"))]
        hidden = [(f.span[0], f.span[1]) for f in findings
                  if f.signal == "non-visible-content" and f.span and policy.sensitive_data.drop_non_visible
                  and not any(f is c for c in cleared)]
        if cleared:   # read and cleared: not an attack signal, and recorded as such
            for f in cleared:
                f.labels = []
                f.signal = "non-visible-content-cleared"
        # Forged role / tool-call markers are removed by policy, except from source code, where they are legitimate.
        hidden += [(f.span[0], f.span[1]) for f in findings
                   if f.signal == "role-marker" and f.span and policy.sensitive_data.strip_role_markers
                   and scan.doc.format != "code"]
        # Text written largely in look-alike letters (mixed-script words) is obfuscation, whatever the judge says.
        lookalike = [(f.span[0], f.span[1]) for f in findings
                     if f.signal == "lookalike_obfuscation" and f.span and policy.sensitive_data.drop_lookalike_text]
        hidden += [s for s in lookalike if s not in hidden]
        invisible = [(f.span[0], f.span[1]) for f in findings if f.signal == "invisible-characters" and f.span]
        hidden += [s for s in invisible if s not in hidden]
        risk = max(assessment.malicious,
                   policy.thresholds.neutralise if hidden else 0.0,
                   policy.thresholds.block if known is not None and known.kind == "attack" else 0.0)
        labels = sorted({label for f in findings for label in f.labels})
        session.add_risk(risk, labels, user=source == "user",
                         key=hashlib.sha256(scan.doc.text.encode("utf-8", "ignore")).hexdigest()[:24])
        # A poisoned document raises the session's risk for actions and further documents, but the user's own words are
        # held only for risk the user's own turns built up (multi-step attacks).
        gate = session.user_risk if source == "user" else session.risk
        # Third-party content is held for session risk only when it carries a signal of its own (a judge score, a
        # structural flag, an incomplete inspection): one poisoned email must not stop the agent reading the rest of
        # the inbox. Actions stay gated by session risk in the action guard, whatever the content (D-063).
        if source != "user" and not (risk >= SESSION_GATE_FLOOR or structural or degraded or scan.doc.warnings):
            gate = 0.0
        if too_large:
            decision, rule, reason = (Decision.BLOCK, "content-too-large",
                                      f"Content exceeds the inspection limit ({self.max_text_chars} characters)")
        elif scan.doc.warnings:
            decision, rule, reason = Decision.BLOCK, "extraction-failed", "Content could not be fully inspected"
        elif known is not None and known.kind == "attack":
            decision, rule, reason = (Decision.BLOCK, "known-attack-match",
                                      f"Matches a confirmed attack in memory (similarity {known.score:.2f})")
        elif risk >= policy.thresholds.block:
            decision, rule, reason = Decision.BLOCK, "content-risk-block", "Content risk reached the policy block threshold"
        elif gate >= policy.session.block_at:
            decision, rule, reason = Decision.BLOCK, "session-risk-block", "Accumulated session risk reached the block threshold"
        elif assessment.degraded and scan.chunks:
            decision, rule, reason = (Decision.BLOCK, "budget-hold" if budget_degraded else "judge-unavailable-hold",
                                      "Flagged content is held until it can be assessed")
        elif oversize_hold:
            decision, rule, reason = (Decision.BLOCK, "oversize-hold",
                                      "Flagged parts of a long document exceed what the judge can assess; held")
        elif gate >= policy.session.escalate_at:
            decision, rule, reason = Decision.ESCALATE, "session-risk-escalate", "Accumulated session risk requires review"
        elif hidden:
            kinds = {f.signal for f in findings if f.span in hidden
                     and f.signal in ("non-visible-content", "role-marker", "lookalike_obfuscation", "invisible-characters")}
            if "non-visible-content" in kinds:
                decision, rule, reason = Decision.NEUTRALISE, "drop-non-visible", "Non-visible content quarantined by policy"
            elif "invisible-characters" in kinds:
                decision, rule, reason = (Decision.NEUTRALISE, "drop-invisible-characters",
                                          "Invisible tag characters (hidden ASCII) quarantined by policy")
            elif "lookalike_obfuscation" in kinds:
                decision, rule, reason = (Decision.NEUTRALISE, "drop-lookalike-text",
                                          "Text written in look-alike letters quarantined by policy")
            else:
                decision, rule, reason = (Decision.NEUTRALISE, "strip-role-markers",
                                          "Forged role/tool-call markers removed by policy")
        elif risk >= policy.thresholds.neutralise:
            decision, rule, reason = Decision.NEUTRALISE, "judge-neutralise", "Assessed spans reached the policy neutralise threshold"
        else:
            decision, rule, reason = Decision.ALLOW, "allow", "Content is within policy"
        enforced = decision if policy.mode == "enforce" else Decision.ALLOW
        original = content if isinstance(content, str) and scan.doc.format == "text" else scan.doc.text
        cleaned = original
        if enforced in (Decision.BLOCK, Decision.ESCALATE):
            cleaned = "[Content held by the security policy: " + rule + "]"
        elif enforced == Decision.NEUTRALISE:
            spans = hidden + [f.span for f in findings if f.signal == "judge" and f.span
                              and f.score >= policy.thresholds.neutralise]
            offset = original.find(scan.doc.text) if scan.doc.format == "text" else 0
            cleaned = self._quarantine(original, [(start + offset, end + offset) for start, end in spans])
        for finding in findings:
            if finding.span:
                finding.preview = self.logger.scrub(scan.doc.text[finding.span[0]:finding.span[1]])
        result = InspectResult(decision=decision, enforced=enforced, cleaned_content=cleaned, findings=findings,
                               labels=labels, risk=risk, session_risk=session.risk, policy_rule=rule, reason=reason,
                               tier=tier, degraded=degraded, budget_degraded=budget_degraded,
                               latency_ms=round((time.perf_counter() - t0) * 1000, 2))
        result.event_id = self.logger.inspection(session.session_id, agent_id, source, scan.doc.text, result,
                                                  assessment.model, assessment.input_tokens, assessment.est_cost,
                                                  assessment.output_tokens)
        self._remember_recent(result.event_id, agent_id, scan.doc.text)
        if source == "user" and decision not in (Decision.BLOCK, Decision.ESCALATE):
            self._amend_contract(session, policy, scan.doc.text, f"user turn {session.turns}")
        elif source != "user":
            session.note_untrusted(scan.doc.text, result.event_id or "", source)
        if policy.taint.enabled and source != "user":   # CORE-17: where this content came from (fingerprints only;
            # the user's own words were recorded as trusted above)
            session.taint.observe(scan.doc.text, result.event_id or "", source, trusted=False,
                                  flagged=decision != Decision.ALLOW or risk >= policy.thresholds.neutralise)
        return result

    def _observe(self, session: Session, policy: AgentPolicy, agent_id: str, source: str, content: Any, scan: Any,
                 t0: float) -> InspectResult:
        """content_checks: observe. Recorded for provenance and the contract, never scored or judged (evaluation of
        the action layer with the detectors bypassed)."""
        original = content if isinstance(content, str) and scan.doc.format == "text" else scan.doc.text
        result = InspectResult(decision=Decision.ALLOW, enforced=Decision.ALLOW, cleaned_content=original,
                               session_risk=session.risk, policy_rule="content-checks-off",
                               reason="Recorded for provenance; content checks are off for this agent",
                               latency_ms=round((time.perf_counter() - t0) * 1000, 2))
        result.event_id = self.logger.inspection(session.session_id, agent_id, source, scan.doc.text, result)
        if source == "user":
            self._amend_contract(session, policy, scan.doc.text, f"user turn {session.turns}")
        else:
            session.note_untrusted(scan.doc.text, result.event_id or "", source)
            if policy.taint.enabled:
                session.taint.observe(scan.doc.text, result.event_id or "", source, trusted=False, flagged=False)
        return result

    def record_execution(self, event_id: str, executed: bool, detail: str = "") -> bool:
        """The application reports whether an allowed action actually ran (execution receipt)."""
        return self.logger.receipt(event_id, executed, detail)

    async def check_action(self, tool: str, args: dict[str, Any], agent_id: str,
                           session_id: str | None = None) -> ActionResult:
        async with self._agent_locks.setdefault(agent_id, asyncio.Lock()):
            policy, session = self._policy(agent_id), self._session(agent_id, session_id)
            self._contract(session, policy)
            result = action_guard.check_action(tool, args, policy, session, self._secrets)
            if result.decision == Decision.ESCALATE:
                approved = self.logger.take_approval(session.session_id, tool, args)
                if approved:   # a human approved this exact call (CON-9); approvals never override a BLOCK
                    pinned = action_guard.check_action(tool, args, policy.model_copy(update={"mode": "monitor"}),
                                                       Session(agent_id=agent_id, trusted=dict(session.trusted)),
                                                       self._secrets)
                    result = result.model_copy(update={
                        "decision": Decision.ALLOW, "enforced": Decision.ALLOW, "final_args": pinned.final_args,
                        "policy_rule": "human-approved", "notice": None,
                        "reason": f"approved by a reviewer (approval {approved}); was: {result.reason}"})
            result.event_id = self.logger.action(session.session_id, agent_id, tool, args, result, session.risk)
            self.store.execute("UPDATE sessions SET risk = ? WHERE id = ?", (session.risk, session.session_id))
            return result

    def output_hits(self, text: str, agent_id: str) -> bool:
        """Does an answer contain protected data (canaries, registered secrets, known secret formats; plain or
        encoded)? Fast and unlogged: for streamed answers, checked as they are sent."""
        return bool(text) and bool(scan_args({"answer": text}, self._policy(agent_id).canary_values(), self._secrets))

    async def check_output(self, text: str, agent_id: str, session_id: str | None = None) -> InspectResult:
        """The model's final answer, before the user (or anyone the answer reaches) sees it: protected values are
        redacted, and an answer with protected data that cannot be redacted (encoded, or a recognised secret format)
        is withheld. Only answers with findings are logged (source "answer")."""
        t0 = time.perf_counter()
        policy = self._policy(agent_id)
        canaries = policy.canary_values()
        hits = scan_args({"answer": text or ""}, canaries, self._secrets)
        if not hits:
            return InspectResult(decision=Decision.ALLOW, enforced=Decision.ALLOW, cleaned_content=text or "",
                                 policy_rule="allow", reason="no protected data in the answer",
                                 latency_ms=round((time.perf_counter() - t0) * 1000, 2))
        cleaned = text
        for value in sorted({*canaries, *self._secrets}, key=len, reverse=True):
            if value:
                cleaned = cleaned.replace(value, "[REDACTED]")
        rest = scan_args({"answer": cleaned}, canaries, self._secrets)
        decision = Decision.BLOCK if rest else Decision.NEUTRALISE
        if rest:
            cleaned = "[The answer was withheld by the security policy: it contained protected data]"
        kinds = sorted({h.kind for h in hits})
        async with self._agent_locks.setdefault(agent_id, asyncio.Lock()):
            session = self._session(agent_id, session_id)
            labels = ["Credential Theft", "Secret Extraction"]
            session.add_risk(policy.thresholds.block if rest else policy.thresholds.neutralise, labels)
            enforced = decision if policy.mode == "enforce" else Decision.ALLOW
            result = InspectResult(
                decision=decision, enforced=enforced, cleaned_content=cleaned if enforced != Decision.ALLOW else text,
                findings=[Finding(signal="outbound-secret:" + k, labels=labels) for k in kinds], labels=labels,
                risk=1.0, session_risk=session.risk, policy_rule="answer-protected-data",
                reason=f"the answer contains protected data ({', '.join(kinds)})",
                latency_ms=round((time.perf_counter() - t0) * 1000, 2))
            result.event_id = self.logger.inspection(session.session_id, agent_id, "answer", cleaned, result, None, 0, 0.0, 0)
        return result

    def inspect_sync(self, *args: Any, **kwargs: Any) -> InspectResult:
        return _sync(lambda: self.inspect(*args, **kwargs))

    def check_action_sync(self, *args: Any, **kwargs: Any) -> ActionResult:
        return _sync(lambda: self.check_action(*args, **kwargs))
