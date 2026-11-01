"""Render a run as Markdown and as a single self-contained HTML page."""

from __future__ import annotations

import html
from typing import Any

ICON = {"supported": "🟢", "changed": "🔴", "unverifiable": "⚪"}
LABEL = {"supported": "SUPPORTED", "changed": "CHANGED", "unverifiable": "UNVERIFIED"}


def _verdict(c: dict[str, Any]) -> str:
    return (c.get("result") or {}).get("verdict", "")


def to_markdown(run: dict[str, Any]) -> str:
    s = run["summary"]
    out = [f"# Stale-answer check ({run['as_of']})", ""]
    if run.get("question"):
        out += [f"**Question:** {run['question']}", ""]
    out += [f"**{s['claims_found']} claims found, {s['claims_checked']} re-checked live:** "
            f"🟢 {s['supported']} still supported · 🔴 {s['changed']} changed · ⚪ {s['unverifiable']} could not be verified"
            f" · {s['not_checked']} not re-checked (stable or over limit)", ""]
    fb = run["gateway"]["llm_calls"]["fallback_calls"]
    if fb:
        out += [f"> ⚠️ The primary model was unavailable for {fb} call(s); Bifrost answered them with a fallback model "
                f"({', '.join(run['gateway']['llm_calls']['served_by'])}).", ""]
    out += ["| | Claim | Verdict | Confidence | Newest evidence | Sources |", "|---|---|---|---|---|---|"]
    for c in run["claims"]:
        if c.get("status") not in {"checked", "error"}:
            continue
        r = c["result"]
        ind = r["indicator"]
        links = " ".join(f"[{e['domain']}]({e['url']})" for e in r.get("evidence", [])[:3]) or "-"
        newest = ind.get("newest_evidence_date") or "undated"
        out.append(f"| {ICON[r['verdict']]} | {c['text']} | {LABEL[r['verdict']]} | {ind['confidence_level']} "
                   f"({ind['confidence']:.2f}) | {newest} | {links} |")
    out.append("")
    for c in run["claims"]:
        if c.get("status") not in {"checked", "error"}:
            continue
        r = c["result"]
        out += [f"## {ICON[r['verdict']]} {c['id']}: {c['text']}", "",
                f"- In the answer: \"{c['quote']}\"",
                f"- Why it can go stale: {c.get('why_time_sensitive') or '-'} (category {c['category']}, "
                f"sensitivity {c['sensitivity_score']}, signals: {', '.join(c['heuristic_signals']) or 'none'})",
                f"- **Verdict: {LABEL[r['verdict']]}**, confidence {r['indicator']['confidence_level']} "
                f"({r['indicator']['confidence']:.2f})"]
        if r.get("current_fact"):
            out.append(f"- **Current fact:** {r['current_fact']}")
        out += ["", "**Evidence** (quotes verified verbatim against the retrieved page text):"]
        if r.get("evidence"):
            for e in r["evidence"]:
                date = f", {e['published_date']}" if e.get("published_date") else ""
                out.append(f"> \"{e['quote']}\"  \n> - {e.get('stance', '')} · [{e.get('title') or e['domain']}]({e['url']}){date}")
                out.append("")
        else:
            out.append("_No verified evidence._")
            out.append("")
        if r.get("inference"):
            out += [f"**Inference** (model reasoning, not evidence): {r['inference']}", ""]
        for note in r.get("checker_notes") or []:
            out.append(f"- ⚠️ {note}")
        if r.get("rejected_quotes"):
            out.append(f"- {len(r['rejected_quotes'])} model quote(s) rejected because they were not in the source text.")
        out.append("")
    skipped = [c for c in run["claims"] if c.get("status") == "not_checked"]
    if skipped:
        out += ["## Not re-checked", ""]
        out += [f"- {c['text']} - _{c['not_checked_reason']}_" for c in skipped]
        out.append("")
    if run.get("rewritten_answer"):
        out += ["## Corrected answer", "", run["rewritten_answer"], ""]
    g = run["gateway"]
    calls = g["llm_calls"]
    out += ["## How this run used Bifrost and Tavily", "",
            f"- Bifrost: {calls['calls']} model calls ({calls['prompt_tokens']} prompt + {calls['completion_tokens']} "
            f"completion tokens), served by {', '.join(calls['served_by'])}; {calls['fallback_calls']} answered by a fallback model."]
    b = g.get("bifrost")
    if b:
        out.append(f"- Bifrost logs for this run: {b.get('logged_requests')} requests "
                   f"({b.get('logged_errors')} upstream errors), cost ${b.get('logged_cost_usd', 0):.6f}; "
                   f"virtual-key budget used ${float(b.get('vk_budget_used_usd') or 0):.6f} of "
                   f"${float(b.get('vk_budget_limit_usd') or 0):.2f}, requests {b.get('vk_requests_used')}/{b.get('vk_requests_limit')} today.")
    rs = run["research"]
    out.append(f"- Tavily: {rs['tavily_searches']} live searches (~{rs['tavily_credits_estimate']} credits), "
               f"{rs['tavily_cache_hits']} served from cache.")
    out.append("")
    return "\n".join(out)


CSS = """body{font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:980px;margin:32px auto;padding:0 16px;color:#1c1c1c;background:#fafaf7}
h1{font-size:26px;margin-bottom:4px}.muted{color:#666}.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-weight:600;font-size:13px}
.supported{background:#dcf5e3;color:#145a2a}.changed{background:#fde0dc;color:#8a1c10}.unverifiable{background:#ececec;color:#444}
.card{background:#fff;border:1px solid #e3e3dc;border-radius:12px;padding:16px 18px;margin:14px 0}
.card h3{margin:0 0 6px;font-size:17px}.quote{border-left:3px solid #c9c9bf;padding:4px 10px;margin:8px 0;background:#f6f6f1}
.inf{background:#fff8e6;border-radius:8px;padding:8px 10px;margin-top:8px}.bar{height:8px;background:#eee;border-radius:9px;overflow:hidden;width:140px;display:inline-block;vertical-align:middle}
.bar span{display:block;height:100%;background:#3b7d4f}.stats span{margin-right:16px}a{color:#1a4f8b}pre{white-space:pre-wrap;background:#fff;border:1px solid #e3e3dc;padding:12px;border-radius:8px}"""


def to_html(run: dict[str, Any]) -> str:
    e = html.escape
    s = run["summary"]
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>Stale-answer check</title><style>{CSS}</style></head><body>",
             f"<h1>Stale-answer check</h1><div class='muted'>As of {e(run['as_of'])} · re-checked live with Tavily · models via Bifrost</div>"]
    if run.get("question"):
        parts.append(f"<p><b>Question:</b> {e(run['question'])}</p>")
    parts.append(f"<p class='stats'><span class='pill supported'>{s['supported']} supported</span> "
                 f"<span class='pill changed'>{s['changed']} changed</span> <span class='pill unverifiable'>{s['unverifiable']} unverified</span> "
                 f"<span class='muted'>{s['claims_found']} claims found · {s['not_checked']} stable / not re-checked</span></p>")
    fb = run["gateway"]["llm_calls"]["fallback_calls"]
    if fb:
        parts.append(f"<p class='inf'>⚠️ The primary model was unavailable for {fb} call(s); Bifrost answered them with a "
                     f"fallback model ({e(', '.join(run['gateway']['llm_calls']['served_by']))}).</p>")
    parts.append(f"<details><summary>Original answer</summary><pre>{e(run['answer'])}</pre></details>")
    for c in run["claims"]:
        if c.get("status") not in {"checked", "error"}:
            continue
        r = c["result"]
        ind = r["indicator"]
        v = r["verdict"]
        parts.append(f"<div class='card'><h3><span class='pill {v}'>{ICON[v]} {LABEL[v]}</span> {e(c['text'])}</h3>")
        parts.append(f"<div class='muted'>Confidence <span class='bar'><span style='width:{int(ind['confidence'] * 100)}%'></span></span> "
                     f"{ind['confidence_level']} ({ind['confidence']:.2f}) · newest evidence {e(ind.get('newest_evidence_date') or 'undated')}"
                     f" · {ind['corroborating_domains']} independent site(s) · category {e(c['category'])}</div>")
        if r.get("current_fact"):
            parts.append(f"<p><b>Current fact:</b> {e(str(r['current_fact']))}</p>")
        for ev in r.get("evidence") or []:
            d = f" · {e(ev['published_date'])}" if ev.get("published_date") else ""
            parts.append(f"<div class='quote'>“{e(ev['quote'])}”<br><small>{e(ev.get('stance') or '')} · "
                         f"<a href='{e(ev['url'])}'>{e(ev.get('title') or ev['domain'])}</a>{d}</small></div>")
        if not r.get("evidence"):
            parts.append("<p class='muted'>No verified evidence.</p>")
        if r.get("inference"):
            parts.append(f"<div class='inf'><b>Inference</b> (model reasoning, not evidence): {e(r['inference'])}</div>")
        for note in r.get("checker_notes") or []:
            parts.append(f"<p class='muted'>⚠️ {e(note)}</p>")
        parts.append("</div>")
    skipped = [c for c in run["claims"] if c.get("status") == "not_checked"]
    if skipped:
        parts.append("<h2>Not re-checked</h2><ul>" + "".join(
            f"<li>{e(c['text'])} <span class='muted'>({e(c['not_checked_reason'])})</span></li>" for c in skipped) + "</ul>")
    if run.get("rewritten_answer"):
        parts.append(f"<h2>Corrected answer</h2><pre>{e(run['rewritten_answer'])}</pre>")
    calls = run["gateway"]["llm_calls"]
    b = run["gateway"].get("bifrost") or {}
    parts.append("<h2>Under the hood</h2><ul>"
                 f"<li>Bifrost: {calls['calls']} model calls, {calls['prompt_tokens'] + calls['completion_tokens']} tokens, "
                 f"{calls['fallback_calls']} served by fallback; models {e(', '.join(calls['served_by']))}</li>"
                 + (f"<li>Bifrost logged cost ${b.get('logged_cost_usd', 0):.6f}; virtual-key budget used "
                    f"${float(b.get('vk_budget_used_usd') or 0):.6f} of ${float(b.get('vk_budget_limit_usd') or 0):.2f}</li>" if b else "")
                 + f"<li>Tavily: {run['research']['tavily_searches']} live searches, {run['research']['tavily_cache_hits']} cached</li></ul>")
    parts.append("</body></html>")
    return "\n".join(parts)
