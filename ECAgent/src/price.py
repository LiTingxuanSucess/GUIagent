from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation


PRICE_CURRENCY_RE = re.compile(r"(?:¥|￥)\s*([1-9]\d{0,4})\s*(?:\.\s*(\d{1,2}))?")
PRICE_WITH_LABEL_RE = re.compile(
    r"(?:到手价|券后价|秒杀价|促销价|活动价|价格)\s*([1-9]\d{0,4})\s*(?:\.\s*(\d{1,2}))?(?!\s*[万千+人])"
)
PRICE_FALLBACK_RE = re.compile(r"(?:^|[^\d])([1-9]\d{1,4}(?:\.\d{1,2})?)(?!\d)")
PIECE_PATTERNS = [
    re.compile(r"(\d{1,4})\s*(?:只|片|枚|个)\s*(?:装|/盒|每盒)?"),
    re.compile(r"(\d{1,3})\s*盒\s*(?:装)?\s*(?:共|合计)?\s*(\d{1,4})\s*(?:只|片|枚|个)"),
    re.compile(r"(\d{1,3})\s*[xX*]\s*(\d{1,4})\s*(?:只|片|枚|个)"),
]


def normalize_price(text: str) -> float | None:
    currency_candidates: list[Decimal] = []
    label_candidates: list[Decimal] = []
    fallback_candidates: list[Decimal] = []
    for integer, fraction in PRICE_CURRENCY_RE.findall(text or ""):
        raw = integer + (f".{fraction}" if fraction else "")
        try:
            val = Decimal(raw)
        except InvalidOperation:
            continue
        if Decimal("1") <= val <= Decimal("99999"):
            currency_candidates.append(val)
    for integer, fraction in PRICE_WITH_LABEL_RE.findall(text or ""):
        raw = integer + (f".{fraction}" if fraction else "")
        try:
            val = Decimal(raw)
        except InvalidOperation:
            continue
        if Decimal("1") <= val <= Decimal("99999"):
            label_candidates.append(val)
    if not currency_candidates and not label_candidates:
        for raw in PRICE_FALLBACK_RE.findall(text or ""):
            try:
                val = Decimal(raw)
            except InvalidOperation:
                continue
            # Avoid treating series names such as 001/003 as prices.
            if Decimal("5") <= val <= Decimal("99999"):
                fallback_candidates.append(val)
    candidates = currency_candidates or label_candidates or fallback_candidates
    if not candidates:
        return None
    # Prefer the lowest visible offer price for e-commerce pages containing
    # original price, coupon price, and final price together.
    return float(min(candidates))


def parse_piece_count(text: str) -> int | None:
    s = text or ""
    m = re.search(r"共\s*(\d{1,4})\s*(?:只|片|枚|个)", s)
    if m:
        total = int(m.group(1))
        if 1 <= total <= 2000:
            return total
    m = re.search(r"(\d{1,4})\s*(?:只|片|枚|个)?\s*[\u00d7xX*✖×]\s*(\d{1,3})", s)
    if m:
        total = int(m.group(1)) * int(m.group(2))
        if 1 <= total <= 2000:
            return total
    m = re.search(r"[【\[]\s*(\d{1,4})\s*(?:只|片|枚|个)\s*[】\]]", s)
    if m:
        total = int(m.group(1))
        if 1 <= total <= 2000:
            return total
    if "+" in s or "＋" in s:
        normalized = re.sub(r"\b00[13]\b", "", s)
        parts = [int(x) for x in re.findall(r"(?<!\d)(\d{1,3})(?=\s*(?:只|片|枚|个)?\s*[+＋]|$)", normalized)]
        if len(parts) >= 2:
            total = sum(x for x in parts if 0 < x <= 200)
            if 1 <= total <= 2000:
                return total
    values: list[int] = []
    for pattern in PIECE_PATTERNS:
        for match in pattern.finditer(s):
            groups = [int(x) for x in match.groups() if x and x.isdigit()]
            if not groups:
                continue
            if len(groups) == 2 and "盒" in match.group(0):
                values.append(groups[-1])
            elif len(groups) == 2:
                values.append(groups[0] * groups[1])
            else:
                values.append(groups[0])
    values = [x for x in values if 1 <= x <= 2000]
    if not values:
        return None
    # The largest count is usually the total pack count in product titles.
    return max(values)


def unit_price(price: float | None, pieces: int | None) -> float | None:
    if price is None or not pieces:
        return None
    return round(price / pieces, 4)
