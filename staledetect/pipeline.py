"""The detector pipeline: extract claims -> pick time-sensitive ones -> research live -> assess -> score."""

from __future__ import annotations

import concurrent.futures as cf
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable

from . import prompts
from .bifrost_client import BifrostClient, GatewayError
from .evidence import date_in_text, freshness_indicator, parse_date, quote_in_source
from .heuristics import combined_sensitivity
from .tavily_client import ResearchError, Source, TavilyClient

NEWS_CATEGORIES = {"leadership_or_role", "event_or_status", "law_or_policy"}


@dataclass
class Options:
    max_claims: int = 8
    threshold: float = 0.55
    workers: int = 4
    rewrite: bool = False
    today: date = field(default_factory=date.today)


def extract_claims(llm: BifrostClient, answer: str, question: str | None, today: date) -> list[dict[str, Any]]:
    qblock = f"QUESTION THE ANSWER RESPONDS TO: {question}\n" if question else ""
    data = llm.chat_json([
        {"role": "system", "content": prompts.EXTRACT_SYSTEM},
        {"role": "user", "content": prompts.EXTRACT_USER.format(today=today.isoformat(), question_block=qblock,
                                                                answer=answer)},
    ], purpose="extract", max_tokens=3500, required_key="claims")
    claims = []
    for i, raw in enumerate(data.get("claims") or [], start=1):
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        quote = str(raw.get("quote") or text).strip()
        level = str(raw.get("time_sensitivity") or "medium").lower()
        score, signals = combined_sensitivity(level, f"{text} {quote}")
        category = raw.get("category") if raw.get("category") in prompts.CATEGORIES else "other"
        claims.append({
            "id": f"c{i}",
            "text": text,
            "quote": quote,
            "quote_found_in_answer": quote.lower() in answer.lower(),
            "category": category,
            "model_time_sensitivity": level,
            "heuristic_signals": signals,
            "sensitivity_score": score,
            "why_time_sensitive": str(raw.get("why") or "").strip(),
            "search_query": str(raw.get("search_query") or text).strip(),
            "change_query": str(raw.get("change_query") or "").strip(),
        })
    return claims


def select_claims(claims: list[dict[str, Any]], opts: Options) -> list[dict[str, Any]]:
    ranked = sorted(claims, key=lambda c: c["sensitivity_score"], reverse=True)
    selected = []
    for c in ranked:
        if c["sensitivity_score"] < opts.threshold:
            c["status"] = "not_checked"
            c["not_checked_reason"] = "low time-sensitivity (stable fact)"
        elif len(selected) >= opts.max_claims:
            c["status"] = "not_checked"
            c["not_checked_reason"] = f"over the per-run limit of {opts.max_claims} live checks"
        else:
            c["status"] = "selected"
            selected.append(c)
    return selected


def research(tavily: TavilyClient, claim: dict[str, Any], max_sources: int = 8) -> list[Source]:
    """Two live searches per claim: one for the current state of the fact, one that looks for news
    that it changed (disconfirming evidence). Results are merged, de-duplicated and sorted newest first."""
    news = claim["category"] in NEWS_CATEGORIES
    sources = tavily.search(claim["search_query"], topic="news" if news else "general", depth="advanced",
                            max_results=5, time_range="year" if news else None)
    if claim.get("change_query"):
        sources += tavily.search(claim["change_query"], topic="news", depth="advanced", max_results=4,
                                 time_range="year")
    if len(sources) < 2 and news:
        sources += tavily.search(claim["search_query"], topic="general", depth="advanced", max_results=5)
    seen, unique = set(), []
    for s in sources:
        if s.url in seen:
            continue
        seen.add(s.url)
        s.content = s.content[:1500]
        unique.append(s)
    unique.sort(key=lambda s: parse_date(s.published_date) or date.min, reverse=True)
    for i, s in enumerate(unique[:max_sources], start=1):
        s.sid = f"S{i}"
    return unique[:max_sources]


def _sources_block(sources: list[Source]) -> str:
    parts = []
    for s in sources:
        parts.append(f"[{s.sid}] {s.title}\nURL: {s.url}\nPublished: {s.published_date or 'unknown'}\n"
                     f"Text: {s.content}")
    return "\n\n".join(parts) if parts else "(no sources found)"


def _adds_new_information(current: str, claim: str) -> bool:
    """A 'changed' verdict must name something the claim did not already say."""
    tok = lambda t: set(re.findall(r"[a-z0-9][a-z0-9.$%-]*", t.lower().replace(",", "")))  # noqa: E731
    new = {t.rstrip(".") for t in tok(current)} - {t.rstrip(".") for t in tok(claim)}
    return bool(new - {"the", "a", "an", "is", "now", "still", "currently"})


def assess_claim(llm: BifrostClient, claim: dict[str, Any], sources: list[Source], today: date) -> dict[str, Any]:
    if not sources:
        return {"verdict": "unverifiable", "current_fact": None, "evidence": [], "rejected_quotes": [],
                "inference": "Live search returned no usable sources.", "model_confidence": 0.0,
                "indicator": freshness_indicator("unverifiable", 0, 0, 0, None, today), "sources": []}
    data = llm.chat_json([
        {"role": "system", "content": prompts.ASSESS_SYSTEM},
        {"role": "user", "content": prompts.ASSESS_USER.format(today=today.isoformat(), claim=claim["text"],
                                                               quote=claim["quote"], sources=_sources_block(sources))},
    ], purpose=f"assess:{claim['id']}", max_tokens=1000, required_key="verdict",
       model=llm.settings.assess_model, fallbacks=[llm.settings.model] + llm.settings.fallbacks)
    by_id = {s.sid: s for s in sources}
    verdict = str(data.get("verdict") or "unverifiable").lower()
    if verdict not in {"supported", "changed", "unverifiable"}:
        verdict = "unverifiable"
    try:
        conf = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    evidence, rejected = [], []
    for ev in data.get("evidence") or []:
        src = by_id.get(str(ev.get("source") or "").strip().strip("[]"))
        quote = str(ev.get("quote") or "").strip()
        stance = str(ev.get("stance") or "").lower()
        stance = "contradicts" if "contradict" in stance else "supports" if "support" in stance else stance
        item = {"source": ev.get("source"), "quote": quote, "stance": stance}
        if src and quote_in_source(quote, f"{src.title} {src.content}"):
            item.update({"url": src.url, "title": src.title, "published_date": src.published_date,
                         "domain": src.domain})
            evidence.append(item)
        else:
            item["why_rejected"] = "quote not found verbatim in the cited source" if src else "unknown source id"
            rejected.append(item)
    notes = []
    if verdict in {"supported", "changed"} and not evidence:
        notes.append(f"Model said '{verdict}' but none of its quotes could be found in the sources, "
                     "so the verdict was downgraded to 'unverifiable'.")
        verdict = "unverifiable"
    current = str(data.get("current_fact") or "").strip()
    if verdict == "changed" and not _adds_new_information(current, claim["text"]):
        notes.append("Model said 'changed' but gave no different current fact, so the verdict was downgraded to "
                     "'unverifiable'.")
        verdict = "unverifiable"
    if verdict == "changed" and not any(e.get("stance") == "contradicts" for e in evidence):
        notes.append("No verified quote contradicts the claim; treat the change as weakly supported.")
        conf = min(conf, 0.5)
    domains = {e["domain"] for e in evidence}
    if verdict in {"supported", "changed"} and len(domains) == 1:
        notes.append(f"Only one site ({next(iter(domains))}) backs this verdict; confirm with a second source.")
        conf = min(conf, 0.6)
    # A date written inside the quote ("As of January 2025") is the fact's date; otherwise the page date.
    for e in evidence:
        stated = date_in_text(e["quote"], not_after=today)
        e["fact_date"] = (stated or parse_date(e.get("published_date")) or None)
        e["fact_date"] = e["fact_date"].isoformat() if e["fact_date"] else None
    dates = [d for d in (parse_date(e.get("fact_date")) for e in evidence) if d]
    newest = max(dates) if dates else None
    indicator = freshness_indicator(verdict, conf, len(evidence), len(domains), newest, today)
    return {
        "verdict": verdict,
        "current_fact": data.get("current_fact") if verdict == "changed" else None,
        "evidence": evidence,
        "rejected_quotes": rejected,
        "inference": str(data.get("inference") or "").strip(),
        "checker_notes": notes,
        "model_confidence": conf,
        "indicator": indicator,
        "sources": [s.to_dict() for s in sources],
    }


def rewrite_answer(llm: BifrostClient, answer: str, checked: list[dict[str, Any]], today: date) -> str:
    numbered: dict[str, int] = {}
    lines = []
    for c in checked:
        res = c.get("result") or {}
        cites = []
        if res.get("verdict") in {"supported", "changed"}:  # unverifiable claims get no citations
            for e in res.get("evidence") or []:
                n = numbered.setdefault(e["url"], len(numbered) + 1)
                cites.append(f"[{n}]")
        cur = f" Current fact: {res.get('current_fact')}." if res.get("current_fact") else ""
        lines.append(f"- {res.get('verdict', '?').upper()}: \"{c['quote']}\".{cur} Sources: {' '.join(cites) or 'none'}")
    sources = "\n".join(f"[{n}] {url}" for url, n in numbered.items()) or "(none)"
    text = llm.chat([
        {"role": "system", "content": prompts.REWRITE_SYSTEM.format(today=today.isoformat())},
        {"role": "user", "content": prompts.REWRITE_USER.format(today=today.isoformat(), answer=answer,
                                                                report="\n".join(lines), sources=sources)},
    ], purpose="rewrite", max_tokens=1800)
    text = text.replace("<<<", "").replace(">>>", "").strip()
    # Replace whatever source list the model wrote with the real numbered URLs.
    text = re.split(r"\n\s*(?:\*\*|#+\s*)?Sources?\b.*", text, maxsplit=1, flags=re.I)[0].rstrip()
    if numbered:
        text += "\n\nSources:\n" + "\n".join(f"[{n}] {url}" for url, n in numbered.items())
    return text


def run(answer: str, *, llm: BifrostClient, tavily: TavilyClient, question: str | None = None,
        opts: Options | None = None, log: Callable[[str], None] = lambda _m: None) -> dict[str, Any]:
    opts = opts or Options()
    started = datetime.now(timezone.utc)
    log("Extracting claims through Bifrost...")
    claims = extract_claims(llm, answer, question, opts.today)
    selected = select_claims(claims, opts)
    log(f"{len(claims)} claims found; {len(selected)} are time-sensitive enough to re-check live.")

    def work(claim: dict[str, Any]) -> None:
        try:
            sources = research(tavily, claim)
            claim["result"] = assess_claim(llm, claim, sources, opts.today)
            claim["status"] = "checked"
        except (ResearchError, GatewayError, ValueError) as exc:
            claim["status"] = "error"
            claim["error"] = str(exc)
            claim["result"] = {"verdict": "unverifiable", "evidence": [], "rejected_quotes": [], "sources": [],
                               "inference": "", "checker_notes": [f"Check failed: {exc}"], "model_confidence": 0.0,
                               "indicator": freshness_indicator("unverifiable", 0, 0, 0, None, opts.today)}
        log(f"  [{claim['id']}] {claim['result']['verdict']:<12} {claim['text'][:80]}")

    with cf.ThreadPoolExecutor(max_workers=max(1, opts.workers)) as pool:
        list(pool.map(work, selected))

    checked = [c for c in claims if c.get("status") in {"checked", "error"}]
    rewritten = None
    if opts.rewrite and checked:
        log("Rewriting the answer with corrections through Bifrost...")
        try:
            rewritten = rewrite_answer(llm, answer, checked, opts.today)
        except GatewayError as exc:
            rewritten = f"(rewrite skipped: {exc})"

    counts = {k: sum(1 for c in checked if c["result"]["verdict"] == k) for k in ("supported", "changed", "unverifiable")}
    return {
        "tool": "stale-answer-detector",
        "checked_at": started.isoformat(timespec="seconds"),
        "as_of": opts.today.isoformat(),
        "question": question,
        "answer": answer,
        "summary": {"claims_found": len(claims), "claims_checked": len(checked),
                    "not_checked": len(claims) - len(checked), **counts},
        "claims": sorted(claims, key=lambda c: int(c["id"][1:])),
        "rewritten_answer": rewritten,
        "gateway": {"llm_calls": llm.usage_totals(),
                    "bifrost": llm.gateway_summary(started.strftime("%Y-%m-%dT%H:%M:%SZ"))},
        "research": {"tavily_searches": tavily.searches, "tavily_cache_hits": tavily.cache_hits,
                     "tavily_credits_estimate": tavily.credits_estimate},
    }
