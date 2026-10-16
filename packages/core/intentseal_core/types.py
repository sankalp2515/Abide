"""Core Engine data types shared by every surface (SDK, API, proxy, Console)."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

ATTACK_TYPES = [
    "Instruction Override", "Role Change", "Secret Extraction", "Tool Abuse", "Credential Theft",
    "Context Poisoning", "Multi-Step Jailbreaks", "Encoded Instructions", "Indirect Prompt Injection",
]


class Decision(str, Enum):
    ALLOW = "ALLOW"
    NEUTRALISE = "NEUTRALISE"
    BLOCK = "BLOCK"
    ESCALATE = "ESCALATE"


class Finding(BaseModel):
    """One suspicious span (or structural signal) inside a piece of content."""

    span: tuple[int, int] | None = None        # offsets in the extracted text
    preview: str = ""                          # redacted, truncated
    origin: str = ""                           # extractor segment origin, e.g. "pdf:page1:invisible"
    signal: str                                # e.g. "non_visible_content", "classifier", "judge"
    labels: list[str] = Field(default_factory=list)
    score: float = 0.0
    tier: int = 0


class InspectResult(BaseModel):
    decision: Decision
    enforced: Decision                         # what was actually applied (monitor mode => ALLOW)
    cleaned_content: str
    findings: list[Finding] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    risk: float = 0.0
    session_risk: float = 0.0
    policy_rule: str = ""
    reason: str = ""
    tier: int = 0
    degraded: bool = False
    budget_degraded: bool = False
    latency_ms: float = 0.0
    event_id: str | None = None


class ActionResult(BaseModel):
    decision: Decision
    enforced: Decision
    final_args: dict[str, Any] | None = None
    pinned_overrides: dict[str, Any] = Field(default_factory=dict)
    taint: list[dict[str, Any]] = Field(default_factory=list)   # CORE-17: where argument text came from
    contract: dict[str, Any] | None = None     # D-064: contract version/hash and the clause that ruled on the call
    labels: list[str] = Field(default_factory=list)
    policy_rule: str = ""
    reason: str = ""
    notice: str | None = None                  # what the agent is told when not allowed
    degraded: bool = False
    latency_ms: float = 0.0
    event_id: str | None = None
