"""Infer newly appeared chat text from consecutive OCR snapshots."""
from __future__ import annotations

from difflib import SequenceMatcher

# Lines with ratio >= this threshold are treated as "same" despite OCR noise.
_LINE_MATCH_RATIO = 0.82


def _norm(s: str) -> str:
    """Collapse whitespace; used for fuzzy line comparison."""
    return " ".join(s.split())


def _lines_match(a: str, b: str) -> bool:
    """
    Two OCR lines are 'the same' when:
    - exact match after normalisation, OR
    - one is a substring of the other (a bubble re-OCR'd slightly truncated), OR
    - SequenceMatcher ratio >= _LINE_MATCH_RATIO (handles 1-2 char OCR drift).
    """
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return a == b
    if a == b:
        return True
    # Very short strings: require exact match to avoid false positives
    if min(len(a), len(b)) < 4:
        return False
    if a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= _LINE_MATCH_RATIO


def appended_content(prev: str, curr: str) -> str:
    """
    Return lines that newly appeared in `curr` vs `prev`.

    Strategy: walk the line lists from the top and skip all lines that fuzzy-
    match (absorbs 1-2-char OCR noise).  Everything after the last matched
    prefix line is considered new content.

    OCR bubbles are appended at the bottom, so a prefix-stable model is a
    good approximation; it breaks when the window scrolls, but that causes
    the whole hash to change anyway and the baseline gets resynced.
    """
    if not curr or not str(curr).strip():
        return ""
    if not prev or not str(prev).strip():
        return str(curr).strip()

    pl = [x for x in prev.splitlines() if x.strip()]
    cl = [x for x in curr.splitlines() if x.strip()]

    k = 0
    while k < len(pl) and k < len(cl) and _lines_match(pl[k], cl[k]):
        k += 1

    if k < len(cl):
        return "\n".join(cl[k:]).strip()
    return ""


def looks_like_echo(new_part: str, last_outgoing: str) -> bool:
    """Heuristic: ignore if new OCR chunk closely matches what we just sent."""
    if not last_outgoing or not new_part:
        return False
    a, b = _norm(new_part), _norm(last_outgoing)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 8 and (a in b or b in a):
        return True
    # Also catch partial OCR of our own sent message
    if min(len(a), len(b)) >= 8 and SequenceMatcher(None, a, b).ratio() >= 0.90:
        return True
    return False
