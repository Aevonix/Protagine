"""The router's token estimator (candidate selection needs about 20% accuracy)."""

from __future__ import annotations

import pytest

from protagine.router.tokens import estimate_tokens


def test_estimate_empty():
    assert estimate_tokens("") == 0


def test_estimate_prose_ratio():
    text = "The quick brown fox jumps over the lazy dog. " * 100
    est = estimate_tokens(text)
    # ~4 chars/token for prose
    assert abs(est - len(text) / 4) < len(text) / 20


def test_estimate_code_denser():
    code = '{"key": [1, 2, 3], "nested": {"a": 1, "b": [4, 5]}}\n' * 100
    prose = "a plain english sentence about nothing in particular here " * 90
    # Symbol-dense text should estimate more tokens per char than prose
    assert estimate_tokens(code) / len(code) > estimate_tokens(prose) / len(prose)


def test_estimate_env_override(monkeypatch):
    monkeypatch.setenv("PROTAGINE_CONTEXT_CHARS_PER_TOKEN", "2.0")
    text = "hello world, this is plain prose without any symbols at all " * 10
    assert estimate_tokens(text) == pytest.approx(len(text) / 2, rel=0.01)
