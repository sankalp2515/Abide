"""Per-agent policy, loaded from policies/<agent>.yaml."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .config import repo_root


class ToolPolicy(BaseModel):
    risk: str = "low"                                   # low | medium | high
    failure: str = "open"                               # open | closed (SDK-5)
    pinned: list[str] = Field(default_factory=list)     # args always taken from trusted context
    escalate_if_uncertain: bool = False
    allowed_recipients: list[str] = Field(default_factory=list)   # "@domain" suffixes or exact addresses
    recipient_arg: str = "to"
    allowed_domains: list[str] = Field(default_factory=list)      # for URL-taking tools
    url_arg: str = "url"
    path_arg: str | None = None                         # argument holding a file path (sandbox check)
    # Explicit intent (docs/13 L1, confused deputy): when set, the user's own words in this session must contain one of
    # these terms (word prefix match: "send" matches "sending") or the call is escalated for approval.
    intent_terms: list[str] = Field(default_factory=list)
    # D-064: the capability this tool exercises (send, submit, purchase, delete, execute, write, read); unset: from its name
    capability: str | None = None


class Thresholds(BaseModel):
    neutralise: float = 0.6
    block: float = 0.85
    escalate_band: tuple[float, float] = (0.5, 0.85)
    # Untrusted third-party content (not user turns) goes to the Tier 2 judge when:
    #   "classifier" - Tier 1 scores it at or above escalate_band[0] (fast path for everything else), or
    #   "always"     - always (for agents with high-risk tools; Tier 1 alone misses paraphrased injections)
    judge_untrusted: str = "classifier"
    judge_min_chars: int = 40                           # shorter tool output (e.g. "sent") stays on the fast path


class SessionPolicy(BaseModel):
    risk_decay_per_turn: float = 0.2
    block_at: float = 1.5
    escalate_at: float = 0.8


class Budgets(BaseModel):
    daily_tokens_tier2: int | None = None
    on_exceeded: str = "rules_only_hold_high_risk"


class SensitiveData(BaseModel):
    patterns: list[str] = Field(default_factory=lambda: ["api_key", "credential"])
    canaries: list[str] = Field(default_factory=list)   # literal values; "from_env" reads INTENTSEAL_CANARIES
    drop_non_visible: bool = True                       # untrusted docs: the model sees what a human sees
    strip_role_markers: bool = True                     # remove forged role/tool-call markers (not from source code)
    drop_lookalike_text: bool = True                    # quarantine text written largely in look-alike letters


class MemoryPolicy(BaseModel):
    enabled: bool = True                                # reviewer-confirmed attacks / false positives (memory.py)
    match_threshold: float = 0.6                        # containment of a stored attack fingerprint


class TaintPolicy(BaseModel):
    enabled: bool = True                                # CORE-17: trace tool-argument text to its origin
    min_coverage: float = 0.3                           # share of an argument's shingles from one origin
    flagged_to_high_risk: str = "escalate"              # escalate | block | record: flagged content into a high-risk tool


class ContractPolicy(BaseModel):
    """D-064 Intent contracts: each session gets a versioned contract compiled from the user's own words."""
    mode: str = "off"                                   # off | enforce
    # capabilities the user must ask for (in words, as a verb); a call outside them is out of scope
    require_mention: list[str] = Field(default_factory=lambda: ["send", "submit", "purchase", "delete"])
    out_of_scope: str = "escalate"                      # escalate | block
    # a capability the user prohibited ("do not contact them") is always blocked


class UpstreamPolicy(BaseModel):
    """Where the LLM proxy forwards this agent's model calls (docs/INTEGRATION.md).

    provider: a provider name from config/providers.yaml (openai, anthropic, gemini, groq, azure_openai, ollama, ...).
              Unset: the gateway's own `proxy_upstream` role (free-tier models; demo and evaluation).
    model:    override the model the client asks for (unset: forward the client's model).
    key:      "gateway" - the gateway's configured key for that provider; "client" - the agent's own provider key,
              sent in the X-Upstream-Key header and never stored."""
    provider: str | None = None
    model: str | None = None
    key: str = "gateway"


class AgentPolicy(BaseModel):
    agent: str
    org: str = "default"                                # organisation (multi-tenant gateway, API-12)
    mode: str = "monitor"                               # monitor | enforce
    task_scope: str = ""
    tools: dict[str, ToolPolicy] = Field(default_factory=dict)
    unknown_tools: str = "block"                        # block | allow
    sensitive_data: SensitiveData = Field(default_factory=SensitiveData)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    session: SessionPolicy = Field(default_factory=SessionPolicy)
    budgets: Budgets = Field(default_factory=Budgets)
    memory: MemoryPolicy = Field(default_factory=MemoryPolicy)
    taint: TaintPolicy = Field(default_factory=TaintPolicy)
    contract: ContractPolicy = Field(default_factory=ContractPolicy)
    # "on": content is inspected. "observe": content is recorded for provenance (origins, named targets) but never
    # scored or judged - an evaluation switch that shows what the action layer stops with the detectors bypassed.
    content_checks: str = "on"
    upstream: UpstreamPolicy = Field(default_factory=UpstreamPolicy)
    decision_model: str = "generative_free"
    version: str = "1"

    def canary_values(self) -> list[str]:
        out: list[str] = []
        for c in self.sensitive_data.canaries:
            if c == "from_env":
                out += [v.strip() for v in os.environ.get("INTENTSEAL_CANARIES", "").split(",") if v.strip()]
            else:
                out.append(c)
        return out


AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def policy_path(agent: str, directory: Path | None = None) -> Path:
    """policies/<agent>.yaml; agent ids are restricted so a request can never name a file outside the directory."""
    if not AGENT_ID.match(agent or "") or ".." in agent:
        raise ValueError("invalid agent id")
    return policy_dir(directory) / f"{agent}.yaml"


def policy_dir(directory: Path | None = None) -> Path:
    return directory or Path(os.environ.get("INTENTSEAL_POLICY_DIR") or repo_root() / "policies")


def list_policies(directory: Path | None = None) -> list[str]:
    """Agent ids with a policy file."""
    d = policy_dir(directory)
    return sorted(f.stem for f in d.glob("*.yaml") if AGENT_ID.match(f.stem)) if d.exists() else []


def policy_exists(agent: str, directory: Path | None = None) -> bool:
    """Raises ValueError for an invalid agent id (never a path outside the policy directory)."""
    return policy_path(agent, directory).exists()


def save_policy(policy: AgentPolicy, directory: Path | None = None) -> Path:
    path = policy_path(policy.agent, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(policy.model_dump(mode="json"), sort_keys=False), encoding="utf-8")
    tmp.replace(path)   # atomic on the same volume
    return path


def load_policy(agent: str, directory: Path | None = None) -> AgentPolicy:
    path = policy_path(agent, directory)
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {"agent": agent}
    raw.setdefault("agent", agent)
    policy = AgentPolicy.model_validate(raw)
    # Operational override (12-factor): e.g. evaluation runs, which exceed a production daily cap by design.
    # "none" lifts the cap; the evaluation summary records the value used.
    override = os.environ.get("INTENTSEAL_BUDGET_TIER2_DAILY_TOKENS")
    if override:
        policy.budgets.daily_tokens_tier2 = None if override.lower() == "none" else int(override)
    return policy
