"""Freshness Guard MCP server (Bifrost challenge 04): protocol, tools, transports and the Bifrost config.
All offline: the live check uses the fake Bifrost and fake Tavily clients from test_pipeline."""

import io
import json
import threading
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from staledetect.config import Settings
from staledetect.mcp_server import (MAX_LIVE_CLAIMS, SUPPORTED_PROTOCOLS, FreshnessServer, handle_payload,
                                    make_http_handler, serve_stdio, split_sentences, triage)

from test_pipeline import ANSWER, FakeLLM, FakeTavily

ROOT = Path(__file__).resolve().parents[1]


def server(**settings):
    s = Settings(bifrost_vk=settings.get("vk", "test-vk"), tavily_api_key=settings.get("tavily", "tvly-test"))
    return FreshnessServer(s, llm_factory=lambda _s: FakeLLM(), tavily_factory=lambda _s: FakeTavily(),
                           today=lambda: date(2026, 11, 4))


def rpc(srv, method, params=None, msg_id=1):
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return srv.handle(msg)


def call(srv, name, arguments):
    return rpc(srv, "tools/call", {"name": name, "arguments": arguments})["result"]


# ---- protocol --------------------------------------------------------------------------------
@pytest.mark.parametrize("version", ["2025-06-18", "2025-11-25", "2026-07-28"])
def test_initialize_accepts_every_protocol_bifrost_speaks(version):
    res = rpc(server(), "initialize", {"protocolVersion": version, "capabilities": {},
                                       "clientInfo": {"name": "bifrost", "version": "2.4.0"}})["result"]
    assert res["protocolVersion"] == version
    assert res["capabilities"] == {"tools": {"listChanged": False}}
    assert res["serverInfo"]["name"] == "freshness-guard"
    assert "triage_freshness" in res["instructions"]


def test_initialize_with_unknown_version_offers_newest():
    res = rpc(server(), "initialize", {"protocolVersion": "1999-01-01"})["result"]
    assert res["protocolVersion"] == SUPPORTED_PROTOCOLS[0]


def test_notifications_get_no_reply_and_ping_works():
    srv = server()
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert rpc(srv, "ping")["result"] == {}


def test_unknown_method_and_invalid_request():
    srv = server()
    assert rpc(srv, "sampling/createMessage")["error"]["code"] == -32601
    assert srv.handle({"id": 3, "method": "tools/list"})["error"]["code"] == -32600  # no jsonrpc field
    assert srv.handle({"jsonrpc": "2.0", "id": 9, "result": {}}) is None  # stray response is ignored


def test_tools_list_schemas():
    tools = {t["name"]: t for t in rpc(server(), "tools/list")["result"]["tools"]}
    assert set(tools) == {"triage_freshness", "check_freshness"}
    for t in tools.values():
        assert t["inputSchema"]["required"] == ["answer"]
        assert t["annotations"]["readOnlyHint"] is True
    assert tools["triage_freshness"]["annotations"]["openWorldHint"] is False
    assert tools["check_freshness"]["inputSchema"]["properties"]["max_claims"]["maximum"] == MAX_LIVE_CLAIMS


def test_unknown_tool_is_a_protocol_error():
    assert rpc(server(), "tools/call", {"name": "rm_rf", "arguments": {}})["error"]["code"] == -32602


# ---- triage ----------------------------------------------------------------------------------
def test_split_sentences_strips_markdown():
    parts = split_sentences("1. **CEO of Intel**: Pat Gelsinger is CEO. Python was created by Guido van Rossum.\n- ok")
    assert parts == ["CEO of Intel: Pat Gelsinger is CEO.", "Python was created by Guido van Rossum."]


def test_triage_flags_time_sensitive_sentences_only():
    data = triage("GitHub Copilot costs $10 per month. Python was created by Guido van Rossum. "
                  "The CEO of Intel is Pat Gelsinger.")
    flagged = {r["sentence"] for r in data["results"] if r["flagged"]}
    assert flagged == {"GitHub Copilot costs $10 per month.", "The CEO of Intel is Pat Gelsinger."}
    assert data["recommend_live_check"] is True and data["flagged"] == 2


def test_triage_tool_on_stable_text_recommends_no_live_check():
    res = call(server(), "triage_freshness", {"answer": "Python was created by Guido van Rossum in the Netherlands."})
    assert res["isError"] is False
    assert res["structuredContent"]["recommend_live_check"] is False
    assert "not needed" in res["content"][0]["text"]


@pytest.mark.parametrize("args, message", [
    ({}, "'answer' is required"),
    ({"answer": "   "}, "'answer' is required"),
    ({"answer": "x" * 12001}, "longer than"),
    ({"answer": "Copilot costs $10.", "threshold": 2}, "threshold"),
])
def test_triage_bad_arguments_are_tool_errors(args, message):
    res = call(server(), "triage_freshness", args)
    assert res["isError"] is True and message in res["content"][0]["text"]


# ---- live check (fake Bifrost + fake Tavily) -------------------------------------------------
def test_check_freshness_runs_the_detector():
    res = call(server(), "check_freshness", {"answer": ANSWER, "question": "What's new?", "max_claims": 3,
                                             "as_of": "2026-11-02"})
    assert res["isError"] is False
    data = res["structuredContent"]
    assert data["as_of"] == "2026-11-02"
    by_id = {c["id"]: c for c in data["claims"]}
    assert by_id["c1"]["verdict"] == "changed" and by_id["c1"]["current_fact"] == "Python 3.15"
    assert by_id["c1"]["sources"] == ["https://www.python.org/downloads/"]
    assert by_id["c2"]["verdict"] == "not_checked"
    assert by_id["c3"]["verdict"] == "unverifiable"  # invented quote downgraded, as in the CLI
    assert data["usage"]["tavily_searches"] >= 2  # at least one search per re-checked claim
    assert data["usage"]["bifrost_fallback_calls"] == 0
    assert "CHANGED" in res["content"][0]["text"] and "now: Python 3.15" in res["content"][0]["text"]


@pytest.mark.parametrize("args, message", [
    ({"answer": ANSWER, "max_claims": 0}, "between 1 and"),
    ({"answer": ANSWER, "max_claims": 99}, "between 1 and"),
    ({"answer": ANSWER, "max_claims": "lots"}, "integer"),
    ({"answer": ANSWER, "as_of": "yesterday"}, "YYYY-MM-DD"),
])
def test_check_bad_arguments(args, message):
    res = call(server(), "check_freshness", args)
    assert res["isError"] is True and message in res["content"][0]["text"]


def test_check_without_keys_explains_what_is_missing():
    assert "BIFROST_VK" in call(server(vk=""), "check_freshness", {"answer": ANSWER})["content"][0]["text"]
    assert "TAVILY_API_KEY" in call(server(tavily=""), "check_freshness", {"answer": ANSWER})["content"][0]["text"]


def test_gateway_refusal_inside_check_is_reported_per_claim():
    srv = server()
    srv.llm_factory = lambda _s: FakeLLM(fail_on="assess")
    res = call(srv, "check_freshness", {"answer": ANSWER})
    errored = [c for c in res["structuredContent"]["claims"] if c.get("error")]
    assert errored and all("budget exceeded" in c["error"] for c in errored)


def test_extract_refusal_becomes_tool_error():
    srv = server()
    srv.llm_factory = lambda _s: FakeLLM(fail_on="extract")
    res = call(srv, "check_freshness", {"answer": ANSWER})
    assert res["isError"] is True and "Bifrost refused" in res["content"][0]["text"]


# ---- transports ------------------------------------------------------------------------------
def test_stdio_transport_round_trip():
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    stdin = io.StringIO("\n".join(json.dumps(m) for m in lines) + "\nnot json\n")
    stdout = io.StringIO()
    serve_stdio(server(), stdin, stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, 2, None]
    assert replies[2]["error"]["code"] == -32700


def test_batch_payload():
    out = handle_payload(server(), [{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                    {"jsonrpc": "2.0", "method": "notifications/initialized"}])
    assert out == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


def test_http_transport_round_trip():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_http_handler(server()))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/mcp"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        body = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                           "params": {"name": "triage_freshness", "arguments": {"answer": "Copilot costs $10 a month."}}})
        req = urllib.request.Request(url, data=body.encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=5) as resp:
            reply = json.loads(resp.read())
        assert reply["id"] == 7 and reply["result"]["structuredContent"]["flagged"] == 1
        note = urllib.request.Request(url, data=b'{"jsonrpc":"2.0","method":"notifications/initialized"}',
                                      method="POST", headers={"Content-Type": "application/json"})
        with opener.open(note, timeout=5) as resp:
            assert resp.status == 202
    finally:
        httpd.shutdown()


# ---- the shipped Bifrost config registers the server correctly -------------------------------
def test_bifrost_config_registers_freshness_guard():
    cfg = json.loads((ROOT / "bifrost" / "config.json").read_text())
    client = next(c for c in cfg["mcp"]["client_configs"] if c["name"] == "freshness_guard")
    assert client["connection_type"] == "stdio"
    assert client["stdio_config"]["command"] == "staledetect-mcp"
    # Every env var listed must be one the server needs: Bifrost refuses to start a stdio client
    # when a listed variable is unset (seen on v2.4.0 with STALE_MODEL).
    assert set(client["stdio_config"]["envs"]) == {"PATH", "HOME", "BIFROST_URL", "BIFROST_VK", "TAVILY_API_KEY"}
    assert client["tool_execution_timeout"] >= 120  # live checks take 30-60 s; Bifrost's default is 30 s
    vk = cfg["governance"]["virtual_keys"][0]
    assert {"mcp_client_name": "freshness_guard", "tools_to_execute": ["*"]} in vk["mcp_configs"]
    # The detector's own model calls go through the same gateway: they must not get MCP tools injected.
    assert cfg["client"]["mcp_disable_auto_tool_inject"] is True
