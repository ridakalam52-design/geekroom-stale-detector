"""Stale-answer detector: find time-sensitive claims in an LLM answer and re-check them live.

LLM calls go through a self-hosted Bifrost gateway; live research goes through Tavily.
"""

__version__ = "0.2.0"
