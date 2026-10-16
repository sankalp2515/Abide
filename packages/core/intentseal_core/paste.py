"""Material the user pasted into their own message: third-party content inside a trusted turn (D-060).

"Summarise this article: ---- <article>" carries two things: the user's request (trusted, the user's intent) and an
article someone else wrote (untrusted, like a web page). An instruction hidden in the article is an indirect prompt
injection even though it arrived in the user's turn, and must not count as something the user asked for. The split is
deliberately conservative: only an explicit boundary (a separator line or fence, or a short request line ending in a
colon) and a long block after it count as pasted material; anything else stays the user's own words.
"""

from __future__ import annotations

import re

MIN_PASTED = 400        # characters of material after the boundary
MAX_REQUEST = 400       # the user's own request before the boundary stays short

# A line of 3+ dashes, equals, underscores, asterisks or tildes; a code fence; triple quotes; <<< or >>> markers.
_SEPARATOR = re.compile(r"^[ \t]*(?:[-=_*~]{3,}|`{3}[\w+-]*|\"{3}|'{3}|<{3,}|>{3,})[ \t]*$", re.M)


def split_pasted(text: str) -> tuple[int, int] | None:
    """(start, end) of the pasted material in `text`, or None when the message is the user's own words only."""
    if len(text) < MIN_PASTED + 10:
        return None
    start = None
    m = _SEPARATOR.search(text)
    if m and m.start() <= MAX_REQUEST and text[:m.start()].strip():
        start = m.end()
    else:
        first = text.find("\n")
        line = text[:first].strip() if first > 0 else ""
        if line.endswith(":") and 0 < len(line) <= MAX_REQUEST:
            start = first + 1
    if start is None:
        return None
    end = len(text)
    closing = _SEPARATOR.search(text, start + 1)
    if closing and len(text) - closing.end() <= MAX_REQUEST:   # a closing marker followed by more of the request
        end = closing.start()
    if len(text[start:end].strip()) < MIN_PASTED:
        return None
    return start, end
