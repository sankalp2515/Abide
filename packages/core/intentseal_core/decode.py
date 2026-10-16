"""CORE-3 decoding: extra detection views for text hidden by common encodings (standard library only).

Requirement (CORE-3): encoded-instruction cases decode *before scoring*. Each decoder returns the decoded
text as an additional view for Tier 1/Tier 2; the content given to the agent is never changed here.

  base64 / base64url  runs of >= 24 base64 characters that decode to mostly printable UTF-8 text
  hex                 runs of >= 32 hex digits that decode to mostly printable UTF-8 text
  unicode tags        U+E0020..U+E007E "tag" characters, an invisible copy of ASCII (they are format
                      characters, so plain normalisation would delete the hidden text instead of reading it)
  url-encoding        text with several %XX escapes
  rot13               whole-text rot13 when the rotated text reads as English much better than the original

Binary payloads (images, keys, hashes) do not decode to readable text and are ignored, which keeps
ordinary encoded data (logos, tokens, checksums) from producing views.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import re
import urllib.parse
from dataclasses import dataclass

_B64 = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{24,}={0,2}(?![A-Za-z0-9+/_-])")
_HEX = re.compile(r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}){16,}(?![0-9A-Fa-f])")
_TAGS = re.compile("[\U000E0020-\U000E007E]{4,}")
_PCT = re.compile(r"(?:%[0-9A-Fa-f]{2}){1,}")
_WORDS = frozenset(
    "the be to of and a in that have i it for not on with he as you do at this but his by from they we say her "
    "she or an will my one all would there their what so up out if about who get which go me when make can like "
    "time no just him know take people into year your good some could them see other than then now look only "
    "come its over think also back after use two how our work first well way even new want because any these "
    "give day most us is are was were been has had should must please send email file all data user assistant "
    "instructions ignore previous system prompt reply include password key secret".split())


@dataclass(frozen=True)
class DecodedView:
    kind: str          # base64 | hex | unicode-tags | url | rot13
    text: str          # decoded text
    span: tuple[int, int]


def _readable(raw: bytes) -> str | None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if len(text.strip()) < 8:
        return None
    printable = sum(ch.isprintable() or ch in "\n\r\t" for ch in text)
    letters = sum(ch.isalpha() for ch in text)
    if printable / len(text) < 0.95 or letters / len(text) < 0.5 or " " not in text.strip():
        return None    # binary or a single opaque token, not hidden prose
    return text


def _english(text: str) -> float:
    words = re.findall(r"[a-z]+", text.lower())
    return sum(w in _WORDS for w in words) / len(words) if words else 0.0


def decode_views(text: str, limit: int = 8) -> list[DecodedView]:
    out: list[DecodedView] = []
    for m in _B64.finditer(text):
        token = m.group(0)
        for alt in (token, token.replace("-", "+").replace("_", "/")):
            try:
                raw = base64.b64decode(alt + "=" * (-len(alt) % 4), validate=True)
            except (binascii.Error, ValueError):
                continue
            decoded = _readable(raw)
            if decoded:
                out.append(DecodedView("base64", decoded, m.span()))
                break
    for m in _HEX.finditer(text):
        decoded = _readable(bytes.fromhex(m.group(0)))
        if decoded:
            out.append(DecodedView("hex", decoded, m.span()))
    for m in _TAGS.finditer(text):
        out.append(DecodedView("unicode-tags", "".join(chr(ord(c) - 0xE0000) for c in m.group(0)), m.span()))
    if len(_PCT.findall(text)) >= 3:
        decoded = urllib.parse.unquote(text)
        if decoded != text:
            out.append(DecodedView("url", decoded, (0, len(text))))
    if len(text) >= 24:
        rotated = codecs.decode(text, "rot13")
        if _english(rotated) >= 0.25 and _english(rotated) > 2 * _english(text) + 0.05:
            out.append(DecodedView("rot13", rotated, (0, len(text))))
    return out[:limit]
