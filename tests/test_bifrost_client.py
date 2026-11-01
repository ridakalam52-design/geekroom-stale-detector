"""BifrostClient against a tiny local HTTP server that mimics Bifrost's responses."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from staledetect.bifrost_client import BifrostClient, GatewayError
from staledetect.config import Settings


class Handler(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *_a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Handler.seen.append((self.headers.get("x-bf-vk"), body))
        if self.headers.get("x-bf-vk") != "good-key":
            self._send(401, {"type": "virtual_key_required", "error": {"message": "virtual key is required"}})
        elif body["model"].startswith("otari-down/"):
            # Bifrost answered from the fallback model.
            self._send(200, {"choices": [{"message": {"content": '{"ok": true}'}}],
                             "usage": {"prompt_tokens": 5, "completion_tokens": 3},
                             "extra_fields": {"latency": 12, "routing_info": {
                                 "provider": "otari", "model": "mistralai/mistral-small-3", "is_fallback": True}}})
        else:
            self._send(200, {"choices": [{"message": {"content": "hello"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                             "extra_fields": {"routing_info": {"provider": "otari", "model": "m"}}})

    def _send(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture(scope="module")
def server():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_sends_virtual_key_and_fallbacks(server):
    c = BifrostClient(Settings(bifrost_url=server, bifrost_vk="good-key", model="otari/a/b", fallbacks=["otari/c/d"]))
    assert c.chat([{"role": "user", "content": "hi"}], purpose="t") == "hello"
    vk, body = Handler.seen[-1]
    assert vk == "good-key" and body["fallbacks"] == ["otari/c/d"] and body["temperature"] == 0.0


def test_simulated_outage_records_fallback(server):
    c = BifrostClient(Settings(bifrost_url=server, bifrost_vk="good-key", model="otari/a/b"), simulate_outage=True)
    assert c.chat_json([{"role": "user", "content": "hi"}], purpose="t") == {"ok": True}
    assert Handler.seen[-1][1]["model"] == "otari-down/a/b"
    assert c.usage_totals()["fallback_calls"] == 1


def test_refusal_raises_gateway_error(server):
    c = BifrostClient(Settings(bifrost_url=server, bifrost_vk="bad-key"))
    with pytest.raises(GatewayError) as exc:
        c.chat([{"role": "user", "content": "hi"}], purpose="t")
    assert exc.value.status == 401 and exc.value.kind == "virtual_key_required"


def test_missing_key_is_a_config_error():
    with pytest.raises(GatewayError) as exc:
        BifrostClient(Settings(bifrost_vk="")).chat([{"role": "user", "content": "hi"}], purpose="t")
    assert exc.value.kind == "config"
