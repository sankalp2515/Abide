"""CORE-3 Normalisation (library-first, D-016).

Thin wrapper over licensed libraries: Python's unicodedata (NFKC), ftfy (Apache-2.0),
confusable-homoglyphs (MIT) and anyascii (ISC). It returns a detection view of the text and
a record of what changed. The content given to the agent is never modified here.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

import ftfy
from anyascii import anyascii
from confusable_homoglyphs import confusables


@dataclass
class Normalised:
    original: str
    text: str                                   # NFKC + ftfy + format characters removed
    ascii_view: str                             # transliterated to ASCII (anyascii)
    changes: dict[str, int] = field(default_factory=dict)
    flags: set[str] = field(default_factory=set)
    skeleton: str | None = None                 # look-alike letters folded to the Latin letter they imitate

    def views(self) -> list[tuple[str, str]]:
        out = [("normalised", self.text)]
        if self.ascii_view != self.text:
            out.append(("ascii", self.ascii_view))
        if self.skeleton and self.skeleton not in (self.text, self.ascii_view):
            out.append(("skeleton", self.skeleton))
        return out


@lru_cache(maxsize=4096)
def _latin_lookalike(ch: str) -> str:
    """The ASCII letter a character visually imitates (Unicode confusables data), else its transliteration.
    anyascii transliterates by sound (Cyrillic 'р' -> 'r'); detection needs shape ('р' -> 'p')."""
    if ord(ch) < 128 or not ch.isalpha():
        return ch
    hit = confusables.is_confusable(ch, preferred_aliases=["latin"], greedy=True) or []
    for entry in hit:
        for h in entry.get("homoglyphs", []):
            if len(h["c"]) == 1 and ord(h["c"]) < 128:
                return h["c"]
    return anyascii(ch)


def latin_skeleton(text: str) -> str:
    return "".join(_latin_lookalike(c) for c in text)


def normalise(text: str) -> Normalised:
    changes: dict[str, int] = {}
    flags: set[str] = set()

    fixed = ftfy.fix_text(text, normalization="NFKC")
    if fixed != text:
        changes["ftfy_nfkc"] = 1

    fmt = [c for c in fixed if unicodedata.category(c) == "Cf"]
    if fmt:
        changes["format_chars_removed"] = len(fmt)
        flags.add("format_characters")
        fixed = "".join(c for c in fixed if unicodedata.category(c) != "Cf")

    folded = anyascii(fixed)
    if folded != fixed:
        changes["ascii_folded"] = sum(a != b for a, b in zip(folded, fixed)) or 1

    words = {w for w in fixed.split() if len(w) >= 3}
    for word in words:
        if confusables.is_mixed_script(word):
            flags.add("mixed_script")
            if confusables.is_dangerous(word):
                flags.add("confusable_characters")
            break
    if "confusable_characters" in flags:
        # Text written largely in look-alike letters is obfuscation: real multilingual text keeps each word in one script.
        lettered = [w for w in fixed.split() if sum(c.isalpha() for c in w) >= 3]
        dangerous = sum(1 for w in lettered if confusables.is_mixed_script(w) and confusables.is_dangerous(w))
        if dangerous >= 3 and dangerous >= 0.25 * len(lettered):
            flags.add("lookalike_obfuscation")

    skeleton = latin_skeleton(fixed) if flags & {"mixed_script", "confusable_characters"} else None
    return Normalised(original=text, text=fixed, ascii_view=folded, changes=changes, flags=flags, skeleton=skeleton)
