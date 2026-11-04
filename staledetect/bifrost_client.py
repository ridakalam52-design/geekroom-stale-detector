"""All model calls go through a Bifrost gateway (OpenAI-compatible /v1/chat/completions).

Bifrost gives the detector: one virtual key with a spend budget, rate limit and model allow-list;
automatic fallback to a second model when the primary provider fails; and per-request logs/cost.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .http import HTTPError, request_json


class GatewayError(RuntimeError):
    """Bifrost refused or failed the request (budget, rate limit, blocked model, upstream down)."""

    def __init__(self, message: str, kind: str = "error", status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


@dataclass
class CallRecord:
    purpose: str
    requested_model: str
    served_provider: str
    served_model: str
    is_fallback: bool
    latency_ms: float
    prompt_tokens: int
    completion_tokens: int


@dataclass
class BifrostClient:
    settings: Settings
    simulate_outage: bool = False
    calls: list[CallRecord] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def primary_model(self) -> str:
        return self.route(self.settings.model)

    def route(self, model: str) -> str:
        if self.simulate_outage:
            # Same model name, but routed to a provider entry whose upstream is unreachable,
            # so Bifrost has to fall back. Demonstrates gateway-level reliability.
            _, _, rest = model.partition("/")
            model = f"{self.settings.outage_provider}/{rest}"
        return model

    def chat(self, messages: list[dict[str, str]], purpose: str, max_tokens: int = 1200,
             temperature: float = 0.0, model: str | None = None, fallbacks: list[str] | None = None) -> str:
        if not self.settings.bifrost_vk:
            raise GatewayError("BIFROST_VK is not set (the Bifrost virtual key for this app).", kind="config")
        requested = self.route(model) if model else self.primary_model
        payload: dict[str, Any] = {
            "model": requested,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        fb = self.settings.fallbacks if fallbacks is None else fallbacks
        if fb:
            payload["fallbacks"] = fb
        try:
            body, _hdrs, elapsed = request_json(
                "POST", f"{self.settings.bifrost_url.rstrip('/')}/v1/chat/completions", payload,
                headers={"x-bf-vk": self.settings.bifrost_vk}, timeout=self.settings.timeout_s)
        except HTTPError as exc:
            err = exc.json()
            kind = err.get("type") or (err.get("extra_fields") or {}).get("error_type") or "http_error"
            msg = ((err.get("error") or {}).get("message")) or exc.body[:300]
            raise GatewayError(f"Bifrost refused the request ({exc.status}, {kind}): {msg}", kind=kind,
                               status=exc.status) from None
        except OSError as exc:
            raise GatewayError(f"Could not reach Bifrost at {self.settings.bifrost_url}: {exc}", kind="unreachable") from None

        extra = body.get("extra_fields") or {}
        routing = extra.get("routing_info") or {}
        usage = body.get("usage") or {}
        record = CallRecord(
            purpose=purpose,
            requested_model=requested,
            served_provider=routing.get("provider") or extra.get("provider") or "?",
            served_model=routing.get("model") or body.get("model") or "?",
            is_fallback=bool(routing.get("is_fallback")),
            latency_ms=float(extra.get("latency") or elapsed * 1000),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )
        with self._lock:
            self.calls.append(record)
        choices = body.get("choices") or []
        if not choices:
            raise GatewayError("Bifrost returned no choices", kind="empty")
        return (choices[0].get("message") or {}).get("content") or ""

    def chat_json(self, messages: list[dict[str, str]], purpose: str, max_tokens: int = 1500,
                  required_key: str | None = None, model: str | None = None,
                  fallbacks: list[str] | None = None) -> dict[str, Any]:
        text = self.chat(messages, purpose=purpose, max_tokens=max_tokens, model=model, fallbacks=fallbacks)
        try:
            obj = parse_json_object(text)
            if required_key and required_key not in obj:
                raise ValueError(f"missing key {required_key}")
            return obj
        except ValueError:
            # One repair attempt through the gateway, then give up.
            repair = self.chat(messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": "That was not valid JSON. Reply with only the JSON object, nothing else."},
            ], purpose=purpose + ":repair", max_tokens=max_tokens, model=model, fallbacks=fallbacks)
            return parse_json_object(repair)

    # ---- MCP gateway (Freshness Guard) ---------------------------------------------------
    def mcp_rpc(self, method: str, params: dict[str, Any] | None = None, timeout: float = 240.0) -> dict[str, Any]:
        """One JSON-RPC request to Bifrost's MCP gateway endpoint (POST /mcp) under this app's virtual key.
        Bifrost only lists and runs the tools that the virtual key's mcp_configs allow."""
        if not self.settings.bifrost_vk:
            raise GatewayError("BIFROST_VK is not set (the Bifrost virtual key for this app).", kind="config")
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": int(time.time() * 1000) % 10**9, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            body, _h, _t = request_json("POST", f"{self.settings.bifrost_url.rstrip('/')}/mcp", payload,
                                        headers={"x-bf-vk": self.settings.bifrost_vk}, timeout=timeout)
        except HTTPError as exc:
            err = exc.json()
            msg = ((err.get("error") or {}).get("message")) or exc.body[:300]
            raise GatewayError(f"Bifrost MCP gateway refused the request ({exc.status}): {msg}",
                               kind=str(err.get("type") or "mcp_error"), status=exc.status) from None
        except OSError as exc:
            raise GatewayError(f"Could not reach Bifrost at {self.settings.bifrost_url}: {exc}", kind="unreachable") from None
        if body.get("error"):
            raise GatewayError(f"MCP error from Bifrost: {body['error'].get('message')}", kind="mcp_error")
        return body.get("result") or {}

    def mcp_tools(self) -> list[str]:
        return [t.get("name", "") for t in self.mcp_rpc("tools/list").get("tools") or []]

    def call_mcp_tool(self, name: str, arguments: dict[str, Any], timeout: float = 240.0) -> dict[str, Any]:
        """Run an MCP tool through Bifrost (tools/call). Returns the MCP tool result
        ({content, structuredContent, isError})."""
        return self.mcp_rpc("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)

    # ---- observability -------------------------------------------------------------------
    def gateway_summary(self, since_iso: str | None = None) -> dict[str, Any] | None:
        """Read spend/latency from Bifrost's own logs and the virtual key's budget counters.

        Needs BIFROST_SETUP_TOKEN (the /api surface is locked without it). Returns None if unavailable.
        """
        token = self.settings.bifrost_setup_token
        if not token:
            return None
        time.sleep(3)  # Bifrost writes logs asynchronously
        base = self.settings.bifrost_url.rstrip("/")
        hdr = {"X-Bifrost-Setup-Token": token}
        out: dict[str, Any] = {}
        try:
            q = "limit=200" + (f"&start_time={since_iso}" if since_iso else "")
            logs, _, _ = request_json("GET", f"{base}/api/logs?{q}", headers=hdr, timeout=15)
            entries = logs.get("logs") or []
            out["logged_requests"] = len(entries)
            out["logged_errors"] = sum(1 for e in entries if e.get("status") != "success")
            out["logged_cost_usd"] = round(sum(float(e.get("cost") or 0) for e in entries), 6)
            vks, _, _ = request_json("GET", f"{base}/api/governance/virtual-keys", headers=hdr, timeout=15)
            for vk in vks.get("virtual_keys") or []:
                if vk.get("value") == self.settings.bifrost_vk or vk.get("name") == "stale-detector":
                    budgets = vk.get("budgets") or []
                    if budgets:
                        out["vk_budget_used_usd"] = budgets[0].get("current_usage")
                        out["vk_budget_limit_usd"] = budgets[0].get("max_limit")
                    rl = vk.get("rate_limit") or {}
                    out["vk_requests_used"] = rl.get("request_current_usage")
                    out["vk_requests_limit"] = rl.get("request_max_limit")
                    break
        except (HTTPError, OSError, ValueError):
            return out or None
        return out

    def usage_totals(self) -> dict[str, Any]:
        calls = list(self.calls)
        return {
            "calls": len(calls),
            "fallback_calls": sum(1 for c in calls if c.is_fallback),
            "prompt_tokens": sum(c.prompt_tokens for c in calls),
            "completion_tokens": sum(c.completion_tokens for c in calls),
            "served_by": sorted({f"{c.served_provider}/{c.served_model}" for c in calls}),
        }


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse the first JSON object in a model reply (tolerates code fences and chatter)."""
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    for cand in candidates:
        cand = cand.strip()
        start = cand.find("{")
        while start != -1:
            depth = 0
            in_str = False
            esc = False
            end = start + 1
            for i in range(start, len(cand)):
                ch = cand[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        try:
                            obj = json.loads(cand[start:end])
                        except ValueError:
                            obj = None
                        if isinstance(obj, dict):
                            return obj
                        break
            else:
                break  # unbalanced braces: truncated output, do not fall back to an inner object
            # Only consider top-level objects: continue after the one that failed.
            start = cand.find("{", end)
    raise ValueError("no JSON object found in model output")
