"""Evidence checks that do not trust the model: verbatim quote matching, source recency and the
final freshness/confidence indicator."""

from __future__ import annotations

import difflib
import re
from datetime import date, datetime
from typing import Any

_WS = re.compile(r"\s+")
_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-",
                         "\u00a0": " "})


def normalise(text: str) -> str:
    return _WS.sub(" ", (text or "").translate(_QUOTES)).strip().lower()


def quote_in_source(quote: str, source_text: str, threshold: float = 0.9) -> bool:
    """True if the quote appears in the source verbatim (after whitespace/quote-mark normalisation)
    or as a near-exact match (>= threshold similarity over a same-length window).
    A quote shortened with an ellipsis passes only if every piece is found."""
    pieces = [p for p in re.split(r"\.\.\.|…|\[\.\.\.\]", quote or "") if len(normalise(p).strip(" .\"'")) >= 8]
    if len(pieces) > 1:
        return all(_one_quote_in_source(p, source_text, threshold) for p in pieces)
    return _one_quote_in_source(quote, source_text, threshold)


def _one_quote_in_source(quote: str, source_text: str, threshold: float) -> bool:
    q = normalise(quote).strip(" .\"'…")
    s = normalise(source_text)
    if len(q) < 8 or not s:
        return False
    if q in s:
        return True
    # Model may drop an ellipsis or a stray character; slide a window of the quote's length.
    n = len(q)
    best = 0.0
    step = max(1, n // 8)
    anchor = q[: min(12, n)]
    positions = [m.start() for m in re.finditer(re.escape(anchor), s)] or range(0, max(1, len(s) - n + 1), step)
    for start in positions:
        window = s[start:start + n]
        ratio = difflib.SequenceMatcher(None, q, window, autojunk=False).ratio()
        best = max(best, ratio)
        if best >= threshold:
            return True
    return False


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d %b %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", value)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def recency_score(newest: date | None, today: date) -> float:
    """1.0 for evidence from the last 90 days, sliding to 0.2 at 3+ years. Undated evidence gets 0.4."""
    if newest is None:
        return 0.4
    age = (today - newest).days
    if age <= 90:
        return 1.0
    if age <= 365:
        return 0.8
    if age <= 730:
        return 0.55
    if age <= 1095:
        return 0.35
    return 0.2


def freshness_indicator(verdict: str, llm_confidence: float, verified_quotes: int, distinct_domains: int,
                        newest: date | None, today: date) -> dict[str, Any]:
    """Combine model confidence with checks we can do ourselves into one indicator per claim."""
    rec = recency_score(newest, today)
    corroboration = min(distinct_domains, 3) / 3
    if verdict == "unverifiable":
        score = 0.15
    else:
        quote_factor = 1.0 if verified_quotes >= 2 else (0.75 if verified_quotes == 1 else 0.0)
        score = (0.45 * max(0.0, min(1.0, llm_confidence)) + 0.2 * corroboration + 0.2 * rec + 0.15) * quote_factor
    score = round(max(0.0, min(1.0, score)), 2)
    level = "high" if score >= 0.75 else "medium" if score >= 0.5 else "low"
    label = {
        "supported": "Still supported",
        "changed": "Changed - answer is stale",
        "unverifiable": "Could not verify",
    }.get(verdict, verdict)
    return {
        "label": label,
        "confidence": score,
        "confidence_level": level,
        "newest_evidence_date": newest.isoformat() if newest else None,
        "evidence_age_days": (today - newest).days if newest else None,
        "corroborating_domains": distinct_domains,
    }


_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                       "september", "october", "november", "december"], start=1)}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_IN_TEXT = re.compile(r"\b(?:(\d{1,2})\s+)?(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
                      r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+(?:(\d{1,2}),?\s+)?((?:19|20)\d{2})\b",
                      re.I)
_ISO = re.compile(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b")


def date_in_text(text: str, not_after: date | None = None) -> date | None:
    """The latest explicit date written in a quote (e.g. 'As of January 2025', 'Oct. 9, 2026', '2026-03-27').
    Dates after `not_after` (schedules such as 'supported until 2029') are ignored."""
    found: list[date] = []
    for m in _IN_TEXT.finditer(text or ""):
        month = _MONTHS.get(m.group(2).lower()[:3])
        day = int(m.group(1) or m.group(3) or 1)
        try:
            found.append(date(int(m.group(4)), month, min(day, 28) if day > 28 else day))
        except (TypeError, ValueError):
            continue
    for m in _ISO.finditer(text or ""):
        try:
            found.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            continue
    if not_after:
        found = [d for d in found if d <= not_after]
    return max(found) if found else None
