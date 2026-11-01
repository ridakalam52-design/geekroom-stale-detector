"""Runtime settings, read from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    return value or default


@dataclass
class Settings:
    bifrost_url: str = field(default_factory=lambda: _env("BIFROST_URL", "http://localhost:8080"))
    bifrost_vk: str = field(default_factory=lambda: _env("BIFROST_VK"))
    # Optional: lets the CLI read Bifrost's own logs/budget counters for the run summary.
    bifrost_setup_token: str = field(default_factory=lambda: _env("BIFROST_SETUP_TOKEN"))
    model: str = field(default_factory=lambda: _env("STALE_MODEL", "otari/alibaba/qwen3-30b-a3b-instruct-2507"))
    # Judging a claim against sources is the hardest step, so it gets a stronger model by default.
    assess_model: str = field(default_factory=lambda: _env("STALE_ASSESS_MODEL", "otari/alibaba/qwen3-235b-a22b-instruct-2507"))
    fallbacks: list[str] = field(
        default_factory=lambda: [m.strip() for m in _env("STALE_FALLBACKS", "otari/mistralai/mistral-small-3").split(",") if m.strip()]
    )
    # Provider prefix that points at an unreachable upstream, used by --simulate-outage.
    outage_provider: str = field(default_factory=lambda: _env("STALE_OUTAGE_PROVIDER", "otari-down"))
    tavily_api_key: str = field(default_factory=lambda: _env("TAVILY_API_KEY"))
    tavily_url: str = field(default_factory=lambda: _env("TAVILY_URL", "https://api.tavily.com"))
    cache_dir: str = field(default_factory=lambda: _env("STALE_CACHE_DIR", ".cache"))
    timeout_s: float = 60.0
