"""Deterministic time-sensitivity signals. They back up (and can overrule) the model's own rating,
so a claim with a price, a version number or a job title is never silently skipped."""

from __future__ import annotations

import re

SIGNALS: list[tuple[str, re.Pattern[str], float]] = [
    ("price", re.compile(r"(?:[$€£₹]\s?\d|\b\d+(?:\.\d+)?\s?(?:usd|eur|dollars?|rupees?)\b|\bper (?:month|year|seat|user|token|1[mk])\b|\bpric(?:e|ed|ing)\b|\bcosts?\b|\bfree (?:tier|plan)\b|\bsubscription\b)", re.I), 0.9),
    ("version", re.compile(r"(?:\bv\d+(?:\.\d+)*\b|\b\d+\.\d+(?:\.\d+)?\b(?=.*\b(?:version|release|lts|stable)\b)|\b(?:latest|newest|current|stable|lts)\s+(?:version|release|model)\b|\bdeprecat\w*|\bsunset\w*|\bend[- ]of[- ]life\b|\bbeta\b|\bpreview\b)", re.I), 0.85),
    ("role", re.compile(r"\b(?:ceo|cto|cfo|chief\b|president|prime minister|chancellor|governor|minister|chair(?:man|woman|person)?|head of|leads?\b|founder and ceo|secretary[- ]general|director)\b", re.I), 0.85),
    ("law", re.compile(r"\b(?:act|law|regulation|bill|directive|ban(?:ned|s)?|legal(?:ly)?|court|ruling|compliance|gdpr|policy)\b", re.I), 0.6),
    ("recency", re.compile(r"\b(?:currently|current|now|today|latest|recent(?:ly)?|as of|this year|so far|upcoming|newly|just (?:released|launched|announced)|still|not yet|yet to)\b", re.I), 0.7),
    ("date", re.compile(r"\b(?:19|20)\d{2}\b"), 0.45),
    ("quantity", re.compile(r"\b\d[\d,.]*\s?(?:million|billion|trillion|k|m|bn|%|percent|users|customers|employees|downloads|tokens|requests|rpm|tpm)\b", re.I), 0.6),
    ("limits", re.compile(r"\b(?:rate limits?|context window|quota|max(?:imum)? (?:of )?\d|up to \d|limit(?:ed)? to)\b", re.I), 0.7),
    ("ranking", re.compile(r"\b(?:largest|biggest|most popular|leading|top|fastest|best[- ]selling|market share|number one|#1)\b", re.I), 0.6),
]

STABLE = re.compile(r"\b(?:was born|invented|founded in|created by|discovered|is a (?:programming )?language|speed of light|boiling point|capital of)\b", re.I)


def heuristic_score(text: str) -> tuple[float, list[str]]:
    """Return (score 0..1, matched signal names). Several signals add up, capped at 1."""
    hits: list[str] = []
    best = 0.0
    total = 0.0
    for name, pattern, weight in SIGNALS:
        if pattern.search(text):
            hits.append(name)
            best = max(best, weight)
            total += weight
    if not hits:
        return 0.0, []
    score = min(1.0, best + 0.1 * (len(hits) - 1))
    if STABLE.search(text) and not {"price", "role", "version"} & set(hits):
        score *= 0.5
    return round(score, 2), hits


LEVEL = {"high": 0.9, "medium": 0.6, "low": 0.2, "none": 0.0}


def combined_sensitivity(llm_level: str, text: str) -> tuple[float, list[str]]:
    h, hits = heuristic_score(text)
    llm = LEVEL.get((llm_level or "").lower(), 0.4)
    # Take the stronger signal, nudged up when both agree.
    score = max(llm, h)
    if llm >= 0.6 and h >= 0.6:
        score = min(1.0, score + 0.05)
    return round(score, 2), hits
