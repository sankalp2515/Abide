"""Forged role and tool-call markers (Tier 0 structural signal).

Chat models are driven by control tokens that delimit roles, reasoning channels and tool calls (for example
`<|im_start|>`, `<|start|>…<|channel|>`, `<|start_header_id|>`, `[INST]`, `<start_of_turn>`, `<tool_call>`).
These tokens have no business inside a user message, a web page, an email or a tool result: when they appear there,
someone is trying to make the model read data as a new system, assistant, reasoning or tool turn. Both 2025–2026
Kaggle agent-security competitions saw this as a primary technique (docs/13).

This module only recognises the published, generic shapes of such markers; it never decides on its own. The engine
sends any chunk carrying them to the judge and, by policy (`sensitive_data.strip_role_markers`), quarantines the
marker tokens themselves so the model cannot act on them (source code is exempt: tokenizer and template code
legitimately contains them).
"""

from __future__ import annotations

import re

MARKERS = re.compile(
    r"<\|[A-Za-z][A-Za-z0-9_]{0,30}\|>"                       # <|im_start|>, <|start|>, <|channel|>, <|eot_id|>, ...
    r"|<\|(?:tool_call|tool_response|tool_result)>|<(?:tool_call|tool_response|tool_result)\|>"   # Gemma-style
    r"|</?(?:tool_call|tool_response|tool_result|function_call|start_of_turn|end_of_turn)>"
    r"|<(?:start_of_turn|end_of_turn)>(?:user|model|system)?"
    r"|\[/?INST\]|<</?SYS>>",
)


def find(text: str) -> list[re.Match[str]]:
    return list(MARKERS.finditer(text or ""))
