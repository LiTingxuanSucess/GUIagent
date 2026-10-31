"""Small text repair helpers for OCR mojibake on Windows."""
from __future__ import annotations

import re


_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_HIGH_LATIN_RE = re.compile(r"[\u00c0-\u00ff]")
_REPLACEMENT = chr(0xFFFD)
_MOJIBAKE_MARKERS = "".join(
    map(
        chr,
        [
            0x00C3,
            0x00C2,
            0x00E6,
            0x00E7,
            0x00E9,
            0x00E8,
            0x00E5,
            0x00C1,
            0x00CD,
            0x00CE,
            0x00D0,
            0x00D8,
            0x00F5,
            0x00EA,
            0x00C8,
            0x00CB,
            0x00D1,
        ],
    )
)
_MOJIBAKE_RE = re.compile(f"[{re.escape(_REPLACEMENT + _MOJIBAKE_MARKERS)}]")


def _score_text(text: str) -> int:
    cjk = len(_CJK_RE.findall(text))
    high_latin = len(_HIGH_LATIN_RE.findall(text))
    mojibake = len(_MOJIBAKE_RE.findall(text))
    replacement = text.count(_REPLACEMENT)
    return cjk * 4 - high_latin * 2 - mojibake * 3 - replacement * 20


def repair_mojibake(text: str) -> str:
    """Repair common OCR service encoding mixups without touching normal text."""
    if not text or not _MOJIBAKE_RE.search(text):
        return text

    candidates = [text]
    for source_encoding in ("cp1252", "latin1"):
        for target_encoding in ("gbk", "utf-8"):
            try:
                candidates.append(text.encode(source_encoding).decode(target_encoding))
            except UnicodeError:
                pass

    best = max(candidates, key=_score_text)
    if _score_text(best) > _score_text(text):
        return best
    return text


def repair_text_list(items: list[str]) -> list[str]:
    return [repair_mojibake(item) for item in items]
