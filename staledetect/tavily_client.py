"""Live web research through Tavily's Search API, with an on-disk cache to save credits."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from .config import Settings
from .http import HTTPError, request_json


class ResearchError(RuntimeError):
    pass


@dataclass
class Source:
    sid: str
    url: str
    title: str
    content: str
    published_date: str | None = None
    score: float | None = None

    @property
    def domain(self) -> str:
        host = urlparse(self.url).netloc.lower()
        return host[4:] if host.startswith("www.") else host

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.sid, "url": self.url, "title": self.title, "published_date": self.published_date,
                "domain": self.domain, "score": self.score, "content": self.content}


@dataclass
class TavilyClient:
    settings: Settings
    use_cache: bool = True
    searches: int = 0
    cache_hits: int = 0
    credits_estimate: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _cache_path(self, payload: dict[str, Any]) -> str:
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
        return os.path.join(self.settings.cache_dir, "tavily", f"{key}.json")

    def search(self, query: str, *, topic: str = "general", depth: str = "advanced", max_results: int = 5,
               time_range: str | None = None) -> list[Source]:
        if not self.settings.tavily_api_key:
            raise ResearchError("TAVILY_API_KEY is not set.")
        payload: dict[str, Any] = {
            "query": query[:380],
            "topic": topic,
            "search_depth": depth,
            "max_results": max_results,
            "include_answer": False,  # we want raw evidence, not a pre-digested answer
            "include_raw_content": False,
        }
        if depth == "advanced":
            payload["chunks_per_source"] = 3
        if time_range:
            payload["time_range"] = time_range
        path = self._cache_path(payload)
        data: dict[str, Any] | None = None
        if self.use_cache and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            with self._lock:
                self.cache_hits += 1
        if data is None:
            try:
                data, _, _ = request_json("POST", f"{self.settings.tavily_url.rstrip('/')}/search", payload,
                                          headers={"Authorization": f"Bearer {self.settings.tavily_api_key}"},
                                          timeout=self.settings.timeout_s)
            except HTTPError as exc:
                raise ResearchError(f"Tavily search failed ({exc.status}): {exc.body[:200]}") from None
            except OSError as exc:
                raise ResearchError(f"Could not reach Tavily: {exc}") from None
            with self._lock:
                self.searches += 1
                self.credits_estimate += 2 if depth == "advanced" else 1
            if self.use_cache:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(data, fh)
        out = []
        for i, r in enumerate(data.get("results") or [], start=1):
            content = (r.get("content") or "").strip()
            if not content or not r.get("url"):
                continue
            out.append(Source(sid=f"S{i}", url=r["url"], title=(r.get("title") or "").strip(), content=content[:2500],
                              published_date=r.get("published_date") or None, score=r.get("score")))
        return out

    def usage(self) -> dict[str, Any] | None:
        if not self.settings.tavily_api_key:
            return None
        try:
            data, _, _ = request_json("GET", f"{self.settings.tavily_url.rstrip('/')}/usage",
                                      headers={"Authorization": f"Bearer {self.settings.tavily_api_key}"}, timeout=15)
        except (HTTPError, OSError):
            return None
        acct = data.get("account") or {}
        return {"plan": acct.get("current_plan"), "used": acct.get("plan_usage"), "limit": acct.get("plan_limit")}
