import pytest

from staledetect.bifrost_client import parse_json_object
from staledetect.heuristics import combined_sensitivity, heuristic_score


@pytest.mark.parametrize("text,signal", [
    ("GitHub Copilot for individuals costs $10 per month.", "price"),
    ("Linda Yaccarino is the CEO of X.", "role"),
    ("The latest stable version of Python is 3.12.", "version"),
    ("The EU AI Act has not yet been adopted.", "recency"),
])
def test_time_sensitive_signals(text, signal):
    score, hits = heuristic_score(text)
    assert signal in hits
    assert score >= 0.6


def test_stable_fact_scores_low():
    score, _ = heuristic_score("Python was created by Guido van Rossum and first released in 1991.")
    assert score < 0.55


def test_heuristic_overrules_a_low_model_rating():
    score, _ = combined_sensitivity("low", "The Pro plan costs $20 per month.")
    assert score >= 0.85


def test_parse_json_with_fence_and_chatter():
    assert parse_json_object('Sure!\n```json\n{"verdict": "supported"}\n```') == {"verdict": "supported"}
    assert parse_json_object('{"a": "brace } inside"} trailing') == {"a": "brace } inside"}


def test_parse_json_does_not_return_inner_object_of_truncated_output():
    with pytest.raises(ValueError):
        parse_json_object('{"claims": [{"text": "a"}, {"text": "b"')
