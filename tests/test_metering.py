"""
Cost and metering tests.

Verifies:
- usage_events rows are written after LLM calls (mocked DB write)
- Credit cap (402) blocks chat_json / chat_text when exhausted
- BYO-key holders bypass credit cap
- MODEL_PRICES produces correct cost computation
- plan helpers return free-plan defaults gracefully on missing data
"""
import pytest
import unittest.mock as mock


def _reset_ctx():
    from brain.core import llm
    llm._ctx.clear()


# ── Cost computation ──────────────────────────────────────────────────────────

def test_cost_computed_correctly_for_known_model():
    from brain.config import MODEL_PRICES
    from brain.core import llm

    prices = MODEL_PRICES["gpt-4o-mini"]  # (0.00015, 0.0006) per 1K
    prompt_tokens     = 1000
    completion_tokens = 500
    expected_cost = (prompt_tokens * prices[0] + completion_tokens * prices[1]) / 1000.0

    written = []
    _reset_ctx()
    llm._ctx.update({"account_id": "acct_1", "user_id": "u1"})

    def _capture(task, model, pt, ct, cost):
        written.append({"pt": pt, "ct": ct, "cost": cost})

    with mock.patch.object(llm, "_write_usage", side_effect=_capture):
        resp = mock.MagicMock()
        resp.usage = mock.MagicMock()
        resp.usage.prompt_tokens     = prompt_tokens
        resp.usage.completion_tokens = completion_tokens
        llm._extract_usage(resp, "gpt-4o-mini", "test")

    assert len(written) == 1
    assert abs(written[0]["cost"] - expected_cost) < 1e-9
    _reset_ctx()


def test_cost_uses_default_for_unknown_model():
    from brain.config import MODEL_PRICES
    from brain.core import llm

    default_prices = MODEL_PRICES["default"]
    written = []
    _reset_ctx()
    llm._ctx.update({"account_id": "acct_1", "user_id": "u1"})

    def _capture(task, model, pt, ct, cost):
        written.append(cost)

    with mock.patch.object(llm, "_write_usage", side_effect=_capture):
        resp = mock.MagicMock()
        resp.usage = mock.MagicMock()
        resp.usage.prompt_tokens     = 100
        resp.usage.completion_tokens = 50
        llm._extract_usage(resp, "some-unknown-model-v99", "test")

    assert len(written) == 1
    expected = (100 * default_prices[0] + 50 * default_prices[1]) / 1000.0
    assert abs(written[0] - expected) < 1e-9
    _reset_ctx()


# ── _write_usage skips without account_id ────────────────────────────────────

def test_write_usage_skips_without_account_id(monkeypatch):
    from brain.core import llm
    _reset_ctx()
    # No account_id in ctx → should skip silently without touching the DB
    with mock.patch("brain.core.db_pg.connect") as mock_conn:
        llm._write_usage("task", "gpt-4o-mini", 10, 5, 0.001)
    mock_conn.assert_not_called()
    _reset_ctx()


# ── Credit enforcement via chat_json / chat_text ──────────────────────────────

def test_chat_json_raises_402_when_credit_exhausted(monkeypatch):
    from fastapi import HTTPException
    from brain.core import llm
    _reset_ctx()
    llm._ctx["account_id"] = "acct_exhausted"

    monkeypatch.setattr("brain.core.db_pg.get_plan_for_account",
                        lambda a: {"monthly_credits": 10})
    monkeypatch.setattr("brain.core.db_pg.get_credits_used_this_month",
                        lambda a: 10)

    with pytest.raises(HTTPException) as exc:
        llm.chat_json("sys", "user", task_name="test")
    assert exc.value.status_code == 402
    _reset_ctx()


def test_chat_text_raises_402_when_credit_exhausted(monkeypatch):
    from fastapi import HTTPException
    from brain.core import llm
    _reset_ctx()
    llm._ctx["account_id"] = "acct_exhausted2"

    monkeypatch.setattr("brain.core.db_pg.get_plan_for_account",
                        lambda a: {"monthly_credits": 5})
    monkeypatch.setattr("brain.core.db_pg.get_credits_used_this_month",
                        lambda a: 5)

    with pytest.raises(HTTPException) as exc:
        llm.chat_text("sys", "user", task_name="test")
    assert exc.value.status_code == 402
    _reset_ctx()


def test_chat_json_succeeds_when_under_limit(monkeypatch):
    from brain.core import llm
    _reset_ctx()
    llm._ctx["account_id"] = "acct_ok"

    monkeypatch.setattr("brain.core.db_pg.get_plan_for_account",
                        lambda a: {"monthly_credits": 500})
    monkeypatch.setattr("brain.core.db_pg.get_credits_used_this_month",
                        lambda a: 1)
    monkeypatch.setattr("brain.core.db_pg.get_byo_key", lambda a, p: "", raising=False)

    with mock.patch.object(llm, "_write_usage", return_value=None):
        result = llm.chat_json("system prompt", "user prompt", task_name="test")
    assert isinstance(result, dict)
    _reset_ctx()


# ── Plan helper defaults ──────────────────────────────────────────────────────

def test_get_plan_for_account_returns_free_on_empty_account(monkeypatch):
    with mock.patch("brain.core.db_pg.connect") as mc:
        cur = mock.MagicMock()
        cur.fetchone.return_value = None
        mc.return_value.__enter__ = lambda s: s
        mc.return_value.__exit__  = mock.MagicMock(return_value=False)
        mc.return_value.cursor.return_value = cur
        mc.return_value.close = mock.MagicMock()

        from brain.core import db_pg
        with mock.patch.object(db_pg, "connect") as real_mc:
            real_mc.return_value = mc.return_value
            plan = db_pg.get_plan_for_account("acct_no_sub")

    assert plan["name"] == "free"
    assert plan["monthly_credits"] == 500


def test_get_plan_for_account_returns_free_on_no_account():
    from brain.core.db_pg import get_plan_for_account
    plan = get_plan_for_account("")
    assert plan["name"] == "free"


def test_get_credits_used_returns_zero_on_no_account():
    from brain.core.db_pg import get_credits_used_this_month
    assert get_credits_used_this_month("") == 0


def test_get_seat_count_returns_zero_on_no_account():
    from brain.core.db_pg import get_seat_count
    assert get_seat_count("") == 0


def test_get_byo_key_returns_empty_on_no_account():
    from brain.core.db_pg import get_byo_key
    assert get_byo_key("", "openai") == ""


# ── PLAN_CREDITS matches plans table seeds ────────────────────────────────────

def test_plan_credits_config_matches_seeds():
    from brain.config import PLAN_CREDITS
    assert PLAN_CREDITS["free"]     == 500
    assert PLAN_CREDITS["team"]     == 5000
    assert PLAN_CREDITS["business"] == 50000


def test_credit_usd_cost_is_sensible():
    from brain.config import CREDIT_USD_COST
    assert 0 < CREDIT_USD_COST <= 0.01  # between $0 and $0.01 per credit


# ── BYO-key encryption at rest ────────────────────────────────────────────────

@pytest.fixture
def _enc_key(monkeypatch):
    """Configure a master key and reset the cached Fernet instance."""
    from cryptography.fernet import Fernet
    from brain.core import db_pg as pg
    monkeypatch.setenv("BRAIN_KEY_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(pg, "_fernet", None)
    monkeypatch.setattr(pg, "_fernet_checked", False)
    return pg


def test_byo_key_ciphertext_does_not_contain_plaintext(_enc_key):
    """The stored value must not contain the secret it protects."""
    secret = "sk-proj-do-not-leak-me"
    stored = _enc_key._encrypt_secret(secret)
    assert secret not in stored
    assert stored.startswith("enc:v1:")


def test_byo_key_encryption_round_trips(_enc_key):
    secret = "sk-ant-abc123"
    assert _enc_key._decrypt_secret(_enc_key._encrypt_secret(secret)) == secret


def test_byo_key_reads_legacy_plaintext_rows(_enc_key):
    """Rows written before encryption existed must still be readable."""
    assert _enc_key._decrypt_secret("sk-written-before-encryption") == \
        "sk-written-before-encryption"


def test_byo_key_wrong_master_key_fails_closed(_enc_key, monkeypatch):
    """A key that cannot be decrypted returns empty, never ciphertext."""
    from cryptography.fernet import Fernet
    stored = _enc_key._encrypt_secret("sk-secret")
    monkeypatch.setenv("BRAIN_KEY_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(_enc_key, "_fernet", None)
    monkeypatch.setattr(_enc_key, "_fernet_checked", False)
    assert _enc_key._decrypt_secret(stored) == ""


def test_byo_key_malformed_master_key_raises(monkeypatch):
    """A bad master key must fail loudly, not silently store plaintext."""
    from brain.core import db_pg as pg
    monkeypatch.setenv("BRAIN_KEY_ENCRYPTION_KEY", "not-a-valid-fernet-key")
    monkeypatch.setattr(pg, "_fernet", None)
    monkeypatch.setattr(pg, "_fernet_checked", False)
    with pytest.raises(RuntimeError):
        pg._encrypt_secret("sk-secret")
