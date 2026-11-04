"""`staledetect guard`: the client side of Freshness Guard, talking only to Bifrost (gateway faked)."""

import json

from staledetect import cli
from staledetect.bifrost_client import BifrostClient, GatewayError

TRIAGE_YES = {"content": [{"type": "text", "text": "1 of 1 sentences look time-sensitive"}], "isError": False,
              "structuredContent": {"advice": "1 of 1 sentences look time-sensitive", "recommend_live_check": True,
                                    "results": [{"sentence": "Copilot costs $10 per month.", "score": 0.9,
                                                 "signals": ["price"], "flagged": True}]}}
TRIAGE_NO = {"content": [{"type": "text", "text": "no"}], "isError": False,
             "structuredContent": {"advice": "No time-sensitive sentences found", "recommend_live_check": False,
                                   "results": []}}
CHECK = {"content": [{"type": "text", "text": "1 claims, 1 re-checked live: 0 supported, 1 changed"}], "isError": False,
         "structuredContent": {"as_of": "2026-11-04",
                               "summary": {"claims_found": 1, "claims_checked": 1, "not_checked": 0, "supported": 0,
                                           "changed": 1, "unverifiable": 0},
                               "claims": [{"id": "c1", "claim": "Copilot costs $10 per month.", "verdict": "changed",
                                           "confidence": 0.8, "current_fact": "usage-based credits",
                                           "sources": ["https://github.com/features/copilot/plans"]}],
                               "usage": {"bifrost_calls": 2, "bifrost_fallback_calls": 0, "tavily_searches": 2}}}


def fake_gateway(monkeypatch, triage, tools=("freshness_guard-check_freshness", "freshness_guard-triage_freshness")):
    calls = []
    monkeypatch.setattr(BifrostClient, "mcp_tools", lambda self: list(tools))

    def call_tool(self, name, arguments, timeout=240.0):
        calls.append((name, arguments))
        return triage if name.endswith("triage_freshness") else CHECK

    monkeypatch.setattr(BifrostClient, "call_mcp_tool", call_tool)
    monkeypatch.setattr(BifrostClient, "gateway_summary", lambda self, since_iso=None: None)
    return calls


def write_answer(tmp_path, text="Copilot costs $10 per month."):
    f = tmp_path / "answer.md"
    f.write_text(text)
    return str(f)


def test_guard_runs_live_check_when_triage_recommends(monkeypatch, tmp_path, capsys):
    calls = fake_gateway(monkeypatch, TRIAGE_YES)
    rc = cli.main(["guard", "--answer-file", write_answer(tmp_path), "--out", str(tmp_path / "out"),
                   "--max-claims", "2", "--as-of", "2026-11-04"])
    assert rc == 0
    assert [c[0] for c in calls] == ["freshness_guard-triage_freshness", "freshness_guard-check_freshness"]
    assert calls[1][1] == {"answer": "Copilot costs $10 per month.", "max_claims": 2, "as_of": "2026-11-04"}
    record = json.loads((tmp_path / "out" / "guard.json").read_text())
    assert record["check"]["summary"]["changed"] == 1
    md = (tmp_path / "out" / "guard.md").read_text()
    assert "CHANGED" in md and "usage-based credits" in md and "github.com/features/copilot/plans" in md
    assert "freshness_guard-triage_freshness" in capsys.readouterr().out


def test_guard_skips_paid_check_when_triage_says_no(monkeypatch, tmp_path):
    calls = fake_gateway(monkeypatch, TRIAGE_NO)
    rc = cli.main(["guard", "--answer-file", write_answer(tmp_path, "Python was created by Guido."),
                   "--out", str(tmp_path / "out")])
    assert rc == 0 and [c[0] for c in calls] == ["freshness_guard-triage_freshness"]
    assert json.loads((tmp_path / "out" / "guard.json").read_text())["check"] is None


def test_guard_force_runs_check_anyway(monkeypatch, tmp_path):
    calls = fake_gateway(monkeypatch, TRIAGE_NO)
    cli.main(["guard", "--answer-file", write_answer(tmp_path), "--out", str(tmp_path / "out"), "--force"])
    assert len(calls) == 2


def test_guard_reports_when_gateway_has_no_freshness_tools(monkeypatch, tmp_path, capsys):
    fake_gateway(monkeypatch, TRIAGE_YES, tools=("filesystem-read_file",))
    rc = cli.main(["guard", "--answer-file", write_answer(tmp_path), "--out", str(tmp_path / "out")])
    assert rc == 2 and "not registered" in capsys.readouterr().err


def test_guard_reports_gateway_refusal(monkeypatch, tmp_path, capsys):
    def refuse(self):
        raise GatewayError("Bifrost MCP gateway refused the request (401): virtual key required", status=401)

    monkeypatch.setattr(BifrostClient, "mcp_tools", refuse)
    rc = cli.main(["guard", "--answer-file", write_answer(tmp_path), "--out", str(tmp_path / "out")])
    assert rc == 2 and "virtual key required" in capsys.readouterr().err


def test_mcp_rpc_needs_a_virtual_key():
    from staledetect.config import Settings
    try:
        BifrostClient(Settings(bifrost_vk="")).mcp_tools()
    except GatewayError as exc:
        assert exc.kind == "config"
    else:
        raise AssertionError("expected GatewayError")
