"""End-to-end pipeline with fake Bifrost and fake Tavily clients (no network)."""

import json
from datetime import date

from staledetect.bifrost_client import BifrostClient, GatewayError
from staledetect.config import Settings
from staledetect.pipeline import Options, run
from staledetect.report import to_html, to_markdown
from staledetect.tavily_client import Source

ANSWER = ("The latest stable version of Python is 3.12. Python was created by Guido van Rossum. "
          "Acme Robotics raised a $40M Series B last month.")

EXTRACT = {"claims": [
    {"text": "The latest stable version of Python is 3.12.", "quote": "The latest stable version of Python is 3.12.",
     "category": "api_or_version", "time_sensitivity": "high", "why": "versions change", "search_query": "latest python"},
    {"text": "Python was created by Guido van Rossum.", "quote": "Python was created by Guido van Rossum.",
     "category": "other", "time_sensitivity": "low", "why": "", "search_query": "python creator"},
    {"text": "Acme Robotics raised a $40M Series B last month.", "quote": "Acme Robotics raised a $40M Series B last month.",
     "category": "event_or_status", "time_sensitivity": "high", "why": "funding news", "search_query": "Acme Robotics Series B"},
]}


class FakeLLM(BifrostClient):
    def __init__(self, fail_on=None):
        super().__init__(Settings(bifrost_vk="test"))
        self.fail_on = fail_on

    def chat(self, messages, purpose, **_kw):
        if self.fail_on and purpose.startswith(self.fail_on):
            raise GatewayError("budget exceeded", kind="budget_exceeded", status=402)
        user = messages[-1]["content"]
        if purpose == "extract":
            return json.dumps(EXTRACT)
        if purpose == "rewrite":
            return "The latest stable version of Python is 3.15 [1]."
        if "Python is 3.12" in user:
            return json.dumps({"verdict": "changed", "current_fact": "Python 3.15",
                               "evidence": [{"source": "S1", "quote": "Python 3.15.0 - Oct. 9, 2026", "stance": "contradicts_claim"},
                                            {"source": "S2", "quote": "this sentence is not on the page", "stance": "contradicts_claim"}],
                               "inference": "3.15 is newer than 3.12.", "confidence": 0.9})
        # The model claims support but invents its quote -> must be downgraded.
        return json.dumps({"verdict": "supported", "current_fact": None,
                           "evidence": [{"source": "S1", "quote": "Acme Robotics closed a $40M Series B", "stance": "supports_claim"}],
                           "inference": "Looks right.", "confidence": 0.8})


class FakeTavily:
    searches = 0
    cache_hits = 0
    credits_estimate = 0

    def search(self, query, **_kw):
        self.searches += 1
        if "python" in query.lower():
            return [Source("S1", "https://www.python.org/downloads/", "Python Releases",
                           "Python 3.15.0 - Oct. 9, 2026. Python 3.14.2 - Sept. 1, 2026.", "2026-10-21"),
                    Source("S2", "https://docs.python.org/3/whatsnew/", "What's New", "What's new in Python 3.15", None)]
        return [Source("S1", "https://example.com/acme", "Acme blog", "Acme Robotics hires a new head of sales.", None)]


def _run(llm, **opts):
    return run(ANSWER, llm=llm, tavily=FakeTavily(), question="What's new?",
               opts=Options(today=date(2026, 11, 2), workers=1, **opts))


def test_pipeline_end_to_end():
    result = _run(FakeLLM(), rewrite=True)
    claims = {c["id"]: c for c in result["claims"]}
    assert result["summary"]["claims_found"] == 3
    assert claims["c2"]["status"] == "not_checked"  # stable fact skipped
    py = claims["c1"]["result"]
    assert py["verdict"] == "changed" and py["current_fact"] == "Python 3.15"
    assert len(py["evidence"]) == 1 and py["evidence"][0]["url"].startswith("https://www.python.org")
    assert len(py["rejected_quotes"]) == 1  # invented quote dropped, not shown as evidence
    acme = claims["c3"]["result"]
    assert acme["verdict"] == "unverifiable"  # downgraded: no verifiable quote
    assert any("downgraded" in n for n in acme["checker_notes"])
    assert result["rewritten_answer"].startswith("The latest stable version of Python is 3.15")
    md, page = to_markdown(result), to_html(result)
    assert "CHANGED" in md and "Inference" in md and "python.org" in page


def test_gateway_refusal_is_reported_not_crashed():
    result = _run(FakeLLM(fail_on="assess"))
    errored = [c for c in result["claims"] if c.get("status") == "error"]
    assert len(errored) == 2
    assert all(c["result"]["verdict"] == "unverifiable" for c in errored)
    assert "budget exceeded" in errored[0]["error"]


def test_max_claims_limit():
    result = _run(FakeLLM(), max_claims=1)
    skipped = [c for c in result["claims"] if c.get("not_checked_reason", "").startswith("over the per-run limit")]
    assert len(skipped) == 1


def test_changed_without_a_new_fact_is_downgraded():
    class EchoLLM(FakeLLM):
        def chat(self, messages, purpose, **kw):
            if purpose.startswith("assess") and "Python is 3.12" in messages[-1]["content"]:
                return json.dumps({"verdict": "changed", "current_fact": "The latest stable version of Python is 3.12.",
                                   "evidence": [{"source": "S1", "quote": "Python 3.15.0 - Oct. 9, 2026", "stance": "contradicts_claim"}],
                                   "inference": "", "confidence": 0.9})
            return super().chat(messages, purpose, **kw)

    result = _run(EchoLLM())
    py = next(c for c in result["claims"] if c["id"] == "c1")["result"]
    assert py["verdict"] == "unverifiable"
