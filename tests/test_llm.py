"""
Unit tests for brain/core/llm.py
- Billing context set/clear
- Usage extraction writes to _write_usage
- BYO-key loading stashes key in context
- Credit cap raises HTTP 402
- BYO-key accounts bypass credit cap
"""
import pytest
import unittest.mock as mock


def _reset_ctx():
    from brain.core import llm
    llm._ctx.clear()


# ── Billing context ───────────────────────────────────────────────────────────

def test_set_billing_context_stores_values(monkeypatch):
    from brain.core import llm
    # Disable DB lookup for BYO key
    monkeypatch.setattr(llm, "_load_byo_key", lambda aid: None)
    _reset_ctx()
    llm.set_billing_context(account_id="acct_1", user_id="user_1", team_id="eng")
    assert llm._ctx["account_id"] == "acct_1"
    assert llm._ctx["user_id"]    == "user_1"
    assert llm._ctx["team_id"]    == "eng"
    _reset_ctx()


def test_clear_billing_context():
    from brain.core import llm
    llm._ctx["account_id"] = "acct_x"
    llm.clear_billing_context()
    assert llm._ctx == {}


# ── _extract_usage ─────────────────────────────────────────────────────────────

def test_extract_usage_calls_write_usage(monkeypatch):
    from brain.core import llm
    llm._ctx.update({"account_id": "acct_1", "user_id": "u1", "team_id": "t1"})

    written = []
    monkeypatch.setattr(llm, "_write_usage",
                        lambda task, model, pt, ct, cost: written.append((task, pt, ct, cost)))

    fake_usage = mock.MagicMock()
    fake_usage.prompt_tokens = 100
    fake_usage.completion_tokens = 50

    resp = mock.MagicMock()
    resp.usage = fake_usage
    llm._extract_usage(resp, "gpt-4o-mini", "test-task")

    assert len(written) == 1
    task, pt, ct, cost = written[0]
    assert task == "test-task"
    assert pt   == 100
    assert ct   == 50
    assert cost  > 0
    _reset_ctx()


def test_extract_usage_skips_on_no_usage(monkeypatch):
    from brain.core import llm
    written = []
    monkeypatch.setattr(llm, "_write_usage",
                        lambda *a: written.append(a))
    resp = mock.MagicMock()
    resp.usage = None
    llm._extract_usage(resp, "gpt-4o-mini", "task")
    assert written == []


# ── BYO-key ───────────────────────────────────────────────────────────────────

def test_load_byo_key_stashes_in_ctx(monkeypatch):
    from brain.core import llm
    _reset_ctx()
    monkeypatch.setattr("brain.core.db_pg.get_byo_keys",
                        lambda acct, provs: {"openai": "sk-openai-123"})
    llm._load_byo_key("acct_1")
    assert llm._ctx["byo_provider"] == "openai"
    assert llm._ctx["byo_key"]      == "sk-openai-123"
    _reset_ctx()


def test_load_byo_key_uses_one_query(monkeypatch):
    """Key lookup must take a single DB round-trip.

    Looping per provider took a pooled connection each time while the request
    handler already held one, which exhausted the pool under load.
    """
    from brain.core import llm
    _reset_ctx()
    calls = []

    def _batch(acct, provs):
        calls.append(acct)
        return {"gemini": "sk-gem-1"}

    monkeypatch.setattr("brain.core.db_pg.get_byo_keys", _batch)
    llm._load_byo_key("acct_1")
    assert len(calls) == 1, f"expected 1 lookup, made {len(calls)}"
    assert llm._ctx["byo_provider"] == "gemini"
    _reset_ctx()


def test_load_byo_key_prefers_openai_over_others(monkeypatch):
    """When several providers have keys, preference order must be preserved."""
    from brain.core import llm
    _reset_ctx()
    monkeypatch.setattr(
        "brain.core.db_pg.get_byo_keys",
        lambda acct, provs: {"gemini": "sk-gem", "openai": "sk-oai", "anthropic": "sk-ant"},
    )
    llm._load_byo_key("acct_1")
    assert llm._ctx["byo_provider"] == "openai"
    _reset_ctx()


def test_effective_api_key_returns_byo(monkeypatch):
    from brain.core import llm
    _reset_ctx()
    llm._ctx["byo_provider"] = "anthropic"
    llm._ctx["byo_key"]      = "sk-ant-test"
    provider, key = llm._effective_api_key()
    assert provider == "anthropic"
    assert key      == "sk-ant-test"
    _reset_ctx()


def test_effective_api_key_returns_none_without_byo():
    from brain.core import llm
    _reset_ctx()
    provider, key = llm._effective_api_key()
    assert provider is None
    assert key      is None


# ── Credit cap ────────────────────────────────────────────────────────────────

def test_credit_check_raises_402_when_exhausted(monkeypatch):
    from fastapi import HTTPException
    from brain.core import llm
    _reset_ctx()
    llm._ctx["account_id"] = "acct_over"
    monkeypatch.setattr("brain.core.db_pg.get_plan_for_account",
                        lambda acct: {"monthly_credits": 100, "max_seats": 5})
    monkeypatch.setattr("brain.core.db_pg.get_credits_used_this_month",
                        lambda acct: 100)  # exactly at limit
    with pytest.raises(HTTPException) as exc:
        llm._check_credit_allowance()
    assert exc.value.status_code == 402
    _reset_ctx()


def test_credit_check_passes_when_under_limit(monkeypatch):
    from brain.core import llm
    _reset_ctx()
    llm._ctx["account_id"] = "acct_ok"
    monkeypatch.setattr("brain.core.db_pg.get_plan_for_account",
                        lambda acct: {"monthly_credits": 500})
    monkeypatch.setattr("brain.core.db_pg.get_credits_used_this_month",
                        lambda acct: 10)
    llm._check_credit_allowance()  # must not raise


def test_credit_check_skips_with_byo_key(monkeypatch):
    """BYO-key accounts are never credit-capped."""
    from brain.core import llm
    _reset_ctx()
    llm._ctx["account_id"] = "acct_byo"
    llm._ctx["byo_key"]    = "sk-their-key"
    # Even if plan says 0 credits, should not raise
    monkeypatch.setattr("brain.core.db_pg.get_plan_for_account",
                        lambda acct: {"monthly_credits": 0})
    monkeypatch.setattr("brain.core.db_pg.get_credits_used_this_month",
                        lambda acct: 999)
    llm._check_credit_allowance()  # must not raise
    _reset_ctx()


def test_credit_check_skips_in_dev_mode():
    """No account_id → dev/bypass mode → never blocked."""
    from brain.core import llm
    _reset_ctx()
    # No account_id in ctx
    llm._check_credit_allowance()  # must not raise


def test_credit_check_skips_for_byo_key():
    """BYO-key accounts are never credit-capped."""
    from brain.core import llm
    _reset_ctx()
    llm._ctx["account_id"] = "acct_byo"
    llm._ctx["byo_key"]    = "sk-test-key"
    llm._ctx["byo_provider"] = "openai"
    llm._check_credit_allowance()  # must not raise
    _reset_ctx()


# ── MODEL_PRICES coverage ─────────────────────────────────────────────────────

def test_model_prices_has_default():
    from brain.config import MODEL_PRICES
    assert "default" in MODEL_PRICES
    p, c = MODEL_PRICES["default"]
    assert p > 0 and c > 0


def test_model_prices_known_models():
    from brain.config import MODEL_PRICES
    for model in ("gpt-4o-mini", "claude-haiku-4-5-20251001", "openai/grok-3-mini"):
        assert model in MODEL_PRICES, f"Missing price entry for {model}"
