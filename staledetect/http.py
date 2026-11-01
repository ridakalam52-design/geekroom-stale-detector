"""Tiny JSON-over-HTTP helper on the standard library (honours HTTP(S)_PROXY)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
# Loopback traffic (the local Bifrost gateway) must never go through an outbound HTTP proxy.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_DEFAULT = urllib.request.build_opener()


class HTTPError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} from {url}: {body[:300]}")
        self.status = status
        self.body = body
        self.url = url

    def json(self) -> dict[str, Any]:
        try:
            return json.loads(self.body)
        except ValueError:
            return {}


def request_json(method: str, url: str, payload: Any | None = None, headers: dict[str, str] | None = None,
                 timeout: float = 60.0) -> tuple[dict[str, Any], dict[str, str], float]:
    """Return (json_body, response_headers, elapsed_seconds). Raises HTTPError on non-2xx."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    for k, v in (headers or {}).items():
        if v:
            req.add_header(k, v)
    start = time.monotonic()
    try:
        opener = _DIRECT if (urlparse(url).hostname or "") in _LOCAL_HOSTS else _DEFAULT
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:  # non-2xx
        raise HTTPError(exc.code, exc.read().decode("utf-8", "replace"), url) from None
    elapsed = time.monotonic() - start
    return (json.loads(body) if body.strip() else {}), hdrs, elapsed
