"""Freshness Guard: an MCP server that plugs the stale-answer detector into Bifrost's MCP gateway.

Bifrost (challenge 04, "the missing gateway feature"): a gateway operator registers this server once in
Bifrost's `mcp.client_configs` (stdio or HTTP). From then on every agent, IDE or app behind the gateway can
call the freshness tools through Bifrost's own MCP endpoint (`POST /mcp`) or tool-execution API
(`POST /v1/mcp/tool/execute`), scoped and governed by the caller's Bifrost virtual key.

Tools
-----
* ``triage_freshness``  - offline, zero-cost. Splits an answer into sentences and scores how likely each one is
  to go out of date (prices, versions, job titles, laws, "currently/latest", quantities, limits). Lets an agent
  or operator decide whether a full check is worth paying for.
* ``check_freshness``   - the full detector: claim extraction and judging go through Bifrost (back through the
  same gateway, under the server's own virtual key, so budget, rate limit, allow-list, fallbacks and logs all
  apply), and every selected claim is re-checked live with Tavily. Returns supported / changed / unverifiable
  per claim with a confidence indicator, the current fact and source links.

Transports: stdio (newline-delimited JSON-RPC 2.0, the MCP stdio transport) and a minimal Streamable-HTTP
endpoint (``--http PORT``; POST JSON-RPC, JSON responses). Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from . import __version__
from .bifrost_client import BifrostClient, GatewayError
from .config import Settings
from .heuristics import heuristic_score
from .pipeline import Options, run
from .tavily_client import TavilyClient

# Newest first. Bifrost speaks 2025-06-18, 2025-11-25 and 2026-07-28; older clients still get an answer.
SUPPORTED_PROTOCOLS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
SERVER_INFO = {"name": "freshness-guard", "title": "Freshness Guard (stale-answer detector)", "version": __version__}

INSTRUCTIONS = (
    "Freshness Guard checks LLM answers for facts that may be out of date (prices, API/software versions, "
    "laws, who holds a role, company facts). Call triage_freshness first: it is free and offline. Call "
    "check_freshness only when triage finds time-sensitive sentences and the answer matters; it re-checks claims "
    "live with Tavily and costs model calls and search credits. Treat 'changed' claims as wrong until corrected "
    "and 'unverifiable' as unknown, never as confirmed."
)

MAX_ANSWER_CHARS = 12000
MAX_LIVE_CLAIMS = 8

TOOLS: list[dict[str, Any]] = [
    {
        "name": "triage_freshness",
        "title": "Triage an answer for time-sensitive claims (free, offline)",
        "description": (
            "Score each sentence of an LLM answer for how likely it is to go stale (prices, versions, roles, "
            "laws, 'currently/latest', quantities, limits). No network calls, no model calls, no cost. Use it to "
            "decide whether check_freshness is worth running."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "answer": {"type": "string", "description": "The LLM-generated answer to triage."},
                "threshold": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.55,
                              "description": "Sentences scoring at or above this are flagged (default 0.55)."},
            },
            "required": ["answer"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True,
                        "openWorldHint": False},
    },
    {
        "name": "check_freshness",
        "title": "Re-check an answer's time-sensitive claims against live sources",
        "description": (
            "Extract the factual claims in an LLM answer, pick the time-sensitive ones, re-check each against live "
            "web sources (Tavily), and return a verdict per claim: supported, changed (with the current fact) or "
            "unverifiable, plus a 0-1 confidence, the newest evidence date and source URLs. Model calls go through "
            "the Bifrost gateway under the server's virtual key. Costs a few model calls and 2 searches per claim."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "answer": {"type": "string", "description": "The LLM-generated answer to check."},
                "question": {"type": "string", "description": "The question it answers (optional, helps extraction)."},
                "max_claims": {"type": "integer", "minimum": 1, "maximum": MAX_LIVE_CLAIMS, "default": 4,
                               "description": f"Most claims to re-check live (1-{MAX_LIVE_CLAIMS}, default 4)."},
                "as_of": {"type": "string", "description": "Treat this date (YYYY-MM-DD) as today (optional)."},
            },
            "required": ["answer"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False,
                        "openWorldHint": True},
    },
]

_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[*-])|\n+")


class ToolError(Exception):
    """A tool ran but could not do its job; reported to the caller as an isError tool result."""


def split_sentences(text: str) -> list[str]:
    parts = [p.replace("**", "").replace("__", "").strip(" \t-*•#>") for p in _SENTENCE.split(text or "")]
    return [p for p in parts if len(p) >= 12 and re.search(r"[A-Za-z]", p)]


def triage(answer: str, threshold: float = 0.55) -> dict[str, Any]:
    sentences = split_sentences(answer)
    scored = []
    for i, s in enumerate(sentences, start=1):
        score, signals = heuristic_score(s)
        scored.append({"n": i, "sentence": s, "score": score, "signals": signals, "flagged": score >= threshold})
    flagged = [s for s in scored if s["flagged"]]
    if not sentences:
        advice = "No checkable sentences found."
    elif flagged:
        advice = (f"{len(flagged)} of {len(sentences)} sentences look time-sensitive; run check_freshness "
                  f"(suggested max_claims={min(len(flagged), MAX_LIVE_CLAIMS)}).")
    else:
        advice = "No time-sensitive sentences found; a live check is probably not needed."
    return {"sentences": len(sentences), "flagged": len(flagged), "threshold": threshold,
            "recommend_live_check": bool(flagged), "advice": advice,
            "results": sorted(scored, key=lambda s: s["score"], reverse=True)}


def summarise_check(result: dict[str, Any]) -> dict[str, Any]:
    """The compact, agent-friendly view of a full detector run."""
    claims = []
    for c in result["claims"]:
        item: dict[str, Any] = {"id": c["id"], "claim": c["text"], "category": c.get("category")}
        if c.get("status") in {"checked", "error"}:
            r = c["result"]
            item.update({
                "verdict": r["verdict"],
                "confidence": r["indicator"]["confidence"],
                "level": r["indicator"].get("confidence_level"),
                "current_fact": r.get("current_fact"),
                "newest_evidence": r["indicator"].get("newest_evidence_date"),
                "sources": list(dict.fromkeys(e.get("url") for e in r.get("evidence", []) if e.get("url")))[:4],
            })
            if c.get("error"):
                item["error"] = c["error"]
        else:
            item.update({"verdict": "not_checked", "reason": c.get("not_checked_reason")})
        claims.append(item)
    calls = result["gateway"]["llm_calls"]
    return {"as_of": result["as_of"], "summary": result["summary"], "claims": claims,
            "usage": {"bifrost_calls": calls.get("calls"), "bifrost_fallback_calls": calls.get("fallback_calls"),
                      "tavily_searches": result["research"]["tavily_searches"]}}


def _text_report(summary: dict[str, Any]) -> str:
    s = summary["summary"]
    lines = [f"{s['claims_found']} claims, {s['claims_checked']} re-checked live: {s['supported']} supported, "
             f"{s['changed']} changed, {s['unverifiable']} unverifiable (as of {summary['as_of']})."]
    for c in summary["claims"]:
        if c["verdict"] == "not_checked":
            continue
        line = f"- {c['verdict'].upper()} ({c['confidence']:.2f}): {c['claim']}"
        if c.get("current_fact"):
            line += f" -> now: {c['current_fact']}"
        if c.get("sources"):
            line += f" [{', '.join(c['sources'][:2])}]"
        lines.append(line)
    return "\n".join(lines)


class FreshnessServer:
    """Transport-independent MCP request handler."""

    def __init__(self, settings: Settings | None = None,
                 llm_factory: Callable[[Settings], Any] | None = None,
                 tavily_factory: Callable[[Settings], Any] | None = None,
                 today: Callable[[], date] = date.today):
        self.settings = settings or Settings()
        self.llm_factory = llm_factory or (lambda s: BifrostClient(s))
        self.tavily_factory = tavily_factory or (lambda s: TavilyClient(s))
        self.today = today
        self.protocol_version: str | None = None

    # ---- JSON-RPC ------------------------------------------------------------------------
    def handle(self, msg: Any) -> dict[str, Any] | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                return None  # a response to something we never sent; ignore
            return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
        method, msg_id, params = msg["method"], msg.get("id"), msg.get("params") or {}
        is_notification = "id" not in msg
        try:
            result = self._dispatch(method, params)
        except _RpcError as exc:
            return None if is_notification else _error(msg_id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - never let one request kill the server
            return None if is_notification else _error(msg_id, -32603, f"Internal error: {exc}")
        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            requested = str(params.get("protocolVersion") or "")
            self.protocol_version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
            return {"protocolVersion": self.protocol_version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}
        if method.startswith("notifications/"):
            return None
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            return self.call_tool(str(params.get("name") or ""), params.get("arguments") or {})
        if method in {"resources/list", "prompts/list"}:
            return {method.split("/")[0]: []}
        raise _RpcError(-32601, f"Method not found: {method}")

    # ---- tools ---------------------------------------------------------------------------
    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name not in {t["name"] for t in TOOLS}:
            raise _RpcError(-32602, f"Unknown tool: {name}")
        try:
            answer = _answer_arg(args)
            if name == "triage_freshness":
                threshold = float(args.get("threshold", 0.55))
                if not 0 <= threshold <= 1:
                    raise ToolError("threshold must be between 0 and 1")
                data = triage(answer, threshold)
                text = data["advice"] + "\n" + "\n".join(
                    f"- {r['score']:.2f} {','.join(r['signals']) or '-'}: {r['sentence'][:160]}"
                    for r in data["results"] if r["flagged"])
            else:
                data = self._check(answer, args)
                text = _text_report(data)
        except ToolError as exc:
            return {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True}
        return {"content": [{"type": "text", "text": text.strip()}], "structuredContent": data, "isError": False}

    def _check(self, answer: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            max_claims = int(args.get("max_claims", 4))
        except (TypeError, ValueError):
            raise ToolError("max_claims must be an integer") from None
        if not 1 <= max_claims <= MAX_LIVE_CLAIMS:
            raise ToolError(f"max_claims must be between 1 and {MAX_LIVE_CLAIMS}")
        today = self.today()
        if args.get("as_of"):
            try:
                today = date.fromisoformat(str(args["as_of"]))
            except ValueError:
                raise ToolError("as_of must be a date in YYYY-MM-DD form") from None
        if not self.settings.bifrost_vk:
            raise ToolError("the server has no Bifrost virtual key (set BIFROST_VK in its environment)")
        if not self.settings.tavily_api_key:
            raise ToolError("the server has no Tavily key (set TAVILY_API_KEY in its environment)")
        question = args.get("question")
        try:
            result = run(answer, llm=self.llm_factory(self.settings), tavily=self.tavily_factory(self.settings),
                         question=str(question) if question else None,
                         opts=Options(max_claims=max_claims, today=today))
        except GatewayError as exc:
            raise ToolError(f"Bifrost refused the detector's model call: {exc}") from None
        return summarise_check(result)


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _answer_arg(args: dict[str, Any]) -> str:
    answer = args.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise ToolError("'answer' is required and must be non-empty text")
    if len(answer) > MAX_ANSWER_CHARS:
        raise ToolError(f"'answer' is longer than {MAX_ANSWER_CHARS} characters; split it up")
    return answer


# ---- transports ------------------------------------------------------------------------------
def handle_payload(server: FreshnessServer, payload: Any) -> Any:
    """One JSON-RPC message or a batch; returns what should be written back (None for nothing)."""
    if isinstance(payload, list):
        out = [r for r in (server.handle(m) for m in payload) if r is not None]
        return out or None
    return server.handle(payload)


def serve_stdio(server: FreshnessServer, stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    lock = threading.Lock()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            reply: Any = _error(None, -32700, "Parse error")
        else:
            reply = handle_payload(server, payload)
        if reply is not None:
            with lock:
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()


def make_http_handler(server: FreshnessServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # keep stdout clean; log to stderr
            sys.stderr.write("freshness-guard http: " + fmt % args + "\n")

        def _send(self, status: int, body: Any | None) -> None:
            data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)

        def do_POST(self):  # noqa: N802
            if self.path.rstrip("/") not in {"", "/mcp"}:
                return self._send(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"null")
            except json.JSONDecodeError:
                return self._send(400, _error(None, -32700, "Parse error"))
            reply = handle_payload(server, payload)
            if reply is None:
                return self._send(202, None)
            return self._send(200, reply)

        def do_GET(self):  # noqa: N802 - no server-initiated stream
            if self.path.rstrip("/") == "/health":
                return self._send(200, {"status": "ok", "server": SERVER_INFO})
            self._send(405, {"error": "SSE stream not offered; use POST"})

        def do_DELETE(self):  # noqa: N802 - stateless server, nothing to end
            self._send(200, {})

    return Handler


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="staledetect-mcp", description="Freshness Guard MCP server for Bifrost.")
    p.add_argument("--http", type=int, metavar="PORT", help="serve Streamable HTTP on this port instead of stdio")
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args(argv)
    server = FreshnessServer()
    if args.http:
        httpd = ThreadingHTTPServer((args.host, args.http), make_http_handler(server))
        sys.stderr.write(f"freshness-guard listening on http://{args.host}:{args.http}/mcp\n")
        httpd.serve_forever()
    else:
        serve_stdio(server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
