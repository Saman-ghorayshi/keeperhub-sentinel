import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sentinel


# ── config loading & validation ──

def test_load_config_defaults(tmp_path):
    cfg = sentinel.load_config(str(tmp_path / "nonexistent.json"))
    assert cfg["threshold"] == 1.5
    assert cfg["network"] == "11155111"
    assert cfg["kill_switch"] is False


def test_load_config_from_file(tmp_path):
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps({"threshold": 1.2, "network": "1", "wallet_address": "0x" + "a" * 40}))
    cfg = sentinel.load_config(str(path))
    assert cfg["threshold"] == 1.2
    assert cfg["network"] == "1"
    assert cfg["wallet_address"] == "0x" + "a" * 40


def test_load_config_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("WALLET_ADDRESS", "0x" + "b" * 40)
    monkeypatch.setenv("NETWORK", "8453")
    cfg = sentinel.load_config(str(tmp_path / "nonexistent.json"))
    assert cfg["wallet_address"] == "0x" + "b" * 40
    assert cfg["network"] == "8453"


def test_validate_config_ok():
    sentinel.validate_config({"wallet_address": "0x" + "a" * 40, "network": "1", "threshold": 1.5, "max_retries": 3})


def test_validate_config_no_wallet():
    with pytest.raises(ValueError, match="wallet_address not set"):
        sentinel.validate_config({"wallet_address": "", "network": "1", "threshold": 1.5, "max_retries": 1})


def test_validate_config_bad_wallet():
    with pytest.raises(ValueError, match="bad wallet"):
        sentinel.validate_config({"wallet_address": "nope", "network": "1", "threshold": 1.5, "max_retries": 1})


def test_validate_config_bad_network():
    with pytest.raises(ValueError, match="unsupported network"):
        sentinel.validate_config({"wallet_address": "0x" + "a" * 40, "network": "999", "threshold": 1.5, "max_retries": 1})


def test_validate_config_zero_threshold():
    with pytest.raises(ValueError, match="threshold must be positive"):
        sentinel.validate_config({"wallet_address": "0x" + "a" * 40, "network": "1", "threshold": 0, "max_retries": 1})


# ── audit ──

def test_audit_writes(tmp_path):
    log = tmp_path / "audit.jsonl"
    sentinel.audit(log, {"outcome": "success", "tx_hash": "0xabc"})
    entry = json.loads(log.read_text().strip())
    assert entry["outcome"] == "success"
    assert entry["tx_hash"] == "0xabc"
    assert "id" in entry and "at" in entry


def test_audit_appends(tmp_path):
    log = tmp_path / "audit.jsonl"
    sentinel.audit(log, {"outcome": "noop"})
    sentinel.audit(log, {"outcome": "success"})
    lines = log.read_text().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["outcome"] == "noop"
    assert json.loads(lines[1])["outcome"] == "success"


def test_audit_creates_parent(tmp_path):
    log = tmp_path / "deep" / "dir" / "audit.jsonl"
    sentinel.audit(log, {"outcome": "blocked"})
    assert log.exists()


def test_audit_list_empty(tmp_path):
    assert sentinel.audit_list(tmp_path / "nope.jsonl") == []


def test_audit_list_limit(tmp_path):
    log = tmp_path / "audit.jsonl"
    for i in range(25):
        sentinel.audit(log, {"outcome": "noop", "i": i})
    entries = sentinel.audit_list(log, limit=5)
    assert len(entries) == 5
    assert entries[-1]["i"] == 24


def test_last_success_time(tmp_path):
    log = tmp_path / "audit.jsonl"
    sentinel.audit(log, {"outcome": "failed"})
    sentinel.audit(log, {"outcome": "success", "tx_hash": "0xabc"})
    sentinel.audit(log, {"outcome": "noop"})
    t = sentinel.last_success_time(log)
    assert t is not None


def test_last_success_time_none(tmp_path):
    log = tmp_path / "audit.jsonl"
    sentinel.audit(log, {"outcome": "failed"})
    assert sentinel.last_success_time(log) is None


# ── parse helpers ──

def test_parse_health_factor_normal():
    assert sentinel.parse_health_factor(int(1.5 * 1e18)) == pytest.approx(1.5)


def test_parse_health_factor_inf():
    assert sentinel.parse_health_factor(sentinel.MAX_UINT256) == float("inf")


def test_parse_health_factor_zero():
    assert sentinel.parse_health_factor(0) == 0.0


def test_parse_mcp_result_valid():
    result = MagicMock()
    item = MagicMock()
    item.text = '{"success": true}'
    result.content = [item]
    assert sentinel.parse_mcp_result(result)["success"] is True


def test_parse_mcp_result_empty():
    result = MagicMock()
    result.content = []
    with pytest.raises(RuntimeError, match="empty response"):
        sentinel.parse_mcp_result(result)


def test_parse_mcp_result_bad_json():
    result = MagicMock()
    item = MagicMock()
    item.text = "not json"
    result.content = [item]
    with pytest.raises(json.JSONDecodeError):
        sentinel.parse_mcp_result(result)


# ── observe ──

def test_observe_mock():
    obs = asyncio.run(sentinel.observe(None, {"wallet_address": "0x" + "a" * 40, "network": "1"}, mock=True))
    assert obs["health_factor"] == 1.3
    assert obs["wallet"] == "0x" + "a" * 40


def test_observe_live():
    session = AsyncMock()
    result = MagicMock()
    item = MagicMock()
    item.text = json.dumps({
        "success": True,
        "result": {
            "healthFactor": str(int(1.2 * 1e18)),
            "totalCollateralBase": str(int(2.0 * 1e18)),
            "totalDebtBase": str(int(1.5 * 1e18)),
        }
    })
    result.content = [item]
    session.call_tool = AsyncMock(return_value=result)
    obs = asyncio.run(sentinel.observe(session, {"wallet_address": "0x" + "a" * 40, "network": "1"}))
    assert obs["health_factor"] == pytest.approx(1.2)
    assert obs["total_collateral_eth"] == pytest.approx(2.0)
    assert obs["total_debt_eth"] == pytest.approx(1.5)


def test_observe_failed():
    session = AsyncMock()
    result = MagicMock()
    item = MagicMock()
    item.text = json.dumps({"success": False, "error": "rpc down"})
    result.content = [item]
    session.call_tool = AsyncMock(return_value=result)
    with pytest.raises(RuntimeError, match="keeperhub read failed"):
        asyncio.run(sentinel.observe(session, {"wallet_address": "0x" + "a" * 40, "network": "1"}))


# ── decide ──

def test_decide_supply():
    cfg = {"kill_switch": False, "threshold": 1.5, "supply_asset": "0x" + "b" * 40}
    obs = {"health_factor": 1.2}
    d = sentinel.decide(obs, cfg)
    assert d["action"] == "supply"
    assert "1.2" in d["rationale"]


def test_decide_withdraw():
    cfg = {"kill_switch": False, "threshold": 1.5, "supply_asset": "", "withdraw_asset": "0x" + "c" * 40}
    obs = {"health_factor": 1.1}
    d = sentinel.decide(obs, cfg)
    assert d["action"] == "withdraw"


def test_decide_transfer():
    cfg = {"kill_switch": False, "threshold": 1.5, "supply_asset": "", "withdraw_asset": "",
           "transfer_recipient": "0x" + "d" * 40}
    obs = {"health_factor": 0.9}
    d = sentinel.decide(obs, cfg)
    assert d["action"] == "transfer"


def test_decide_noop_safe():
    cfg = {"kill_switch": False, "threshold": 1.5, "supply_asset": "0x" + "b" * 40}
    obs = {"health_factor": 2.0}
    d = sentinel.decide(obs, cfg)
    assert d["action"] == "noop"


def test_decide_kill_switch():
    cfg = {"kill_switch": True, "threshold": 1.5, "supply_asset": "0x" + "b" * 40}
    obs = {"health_factor": 0.5}
    d = sentinel.decide(obs, cfg)
    # decide still proposes the action; policy blocks it
    assert d["action"] == "supply"


def test_decide_breach_no_action():
    cfg = {"kill_switch": False, "threshold": 1.5, "supply_asset": "", "withdraw_asset": "", "transfer_recipient": ""}
    obs = {"health_factor": 0.8}
    d = sentinel.decide(obs, cfg)
    assert d["action"] == "noop"


def test_decide_inf():
    cfg = {"kill_switch": False, "threshold": 1.5, "supply_asset": "0x" + "b" * 40}
    obs = {"health_factor": float("inf")}
    d = sentinel.decide(obs, cfg)
    assert d["action"] == "noop"


# ── policy ──

def test_policy_allows_supply(tmp_path):
    log = tmp_path / "audit.jsonl"
    cfg = {"kill_switch": False, "network": "1", "chain_allowlist": ["1", "8453"],
           "cooldown_seconds": 0, "supply_amount": "1000", "max_amount_wei": "1000000",
           "recipient_allowlist": []}
    decision = {"action": "supply"}
    p = sentinel.evaluate_policy(decision, cfg, log)
    assert p["allowed"] is True


def test_policy_blocks_kill_switch(tmp_path):
    log = tmp_path / "audit.jsonl"
    cfg = {"kill_switch": True, "network": "1", "chain_allowlist": ["1"],
           "cooldown_seconds": 0, "supply_amount": "1000", "max_amount_wei": "1000000",
           "recipient_allowlist": []}
    decision = {"action": "supply"}
    p = sentinel.evaluate_policy(decision, cfg, log)
    assert p["allowed"] is False
    assert any("kill switch" in r for r in p["reasons"])


def test_policy_blocks_off_chain(tmp_path):
    log = tmp_path / "audit.jsonl"
    cfg = {"kill_switch": False, "network": "999", "chain_allowlist": ["1"],
           "cooldown_seconds": 0, "supply_amount": "1000", "max_amount_wei": "1000000",
           "recipient_allowlist": []}
    decision = {"action": "supply"}
    p = sentinel.evaluate_policy(decision, cfg, log)
    assert p["allowed"] is False
    assert any("not allowlisted" in r for r in p["reasons"])


def test_policy_blocks_cooldown(tmp_path):
    log = tmp_path / "audit.jsonl"
    sentinel.audit(log, {"outcome": "success", "tx_hash": "0xabc"})
    cfg = {"kill_switch": False, "network": "1", "chain_allowlist": ["1"],
           "cooldown_seconds": 3600, "supply_amount": "1000", "max_amount_wei": "1000000",
           "recipient_allowlist": []}
    decision = {"action": "supply"}
    p = sentinel.evaluate_policy(decision, cfg, log)
    assert p["allowed"] is False
    assert any("cooldown" in r for r in p["reasons"])


def test_policy_blocks_amount_exceeds(tmp_path):
    log = tmp_path / "audit.jsonl"
    cfg = {"kill_switch": False, "network": "1", "chain_allowlist": ["1"],
           "cooldown_seconds": 0, "supply_amount": "99999999", "max_amount_wei": "1000",
           "recipient_allowlist": []}
    decision = {"action": "supply"}
    p = sentinel.evaluate_policy(decision, cfg, log)
    assert p["allowed"] is False
    assert any("exceeds" in r for r in p["reasons"])


def test_policy_blocks_bad_recipient(tmp_path):
    log = tmp_path / "audit.jsonl"
    cfg = {"kill_switch": False, "network": "1", "chain_allowlist": ["1"],
           "cooldown_seconds": 0, "supply_amount": "1000", "max_amount_wei": "1000000",
           "recipient_allowlist": ["0x" + "a" * 40]}
    decision = {"action": "transfer"}
    p = sentinel.evaluate_policy(decision, cfg, log)
    assert p["allowed"] is False
    assert any("recipient" in r for r in p["reasons"])


def test_policy_allows_noop(tmp_path):
    log = tmp_path / "audit.jsonl"
    cfg = {"kill_switch": True, "network": "999", "chain_allowlist": [],
           "cooldown_seconds": 3600, "recipient_allowlist": []}
    decision = {"action": "noop"}
    p = sentinel.evaluate_policy(decision, cfg, log)
    assert p["allowed"] is True


def test_policy_allows_recipient_in_allowlist(tmp_path):
    log = tmp_path / "audit.jsonl"
    recipient = "0x" + "a" * 40
    cfg = {"kill_switch": False, "network": "1", "chain_allowlist": ["1"],
           "cooldown_seconds": 0, "supply_amount": "1000", "max_amount_wei": "1000000",
           "recipient_allowlist": [recipient], "transfer_recipient": recipient}
    decision = {"action": "transfer"}
    p = sentinel.evaluate_policy(decision, cfg, log)
    assert p["allowed"] is True


# ── execute ──

def test_execute_noop():
    result = asyncio.run(sentinel.execute_action(None, {"action": "noop"}, {}, Path("data/audit.jsonl"), mock=True))
    assert result["outcome"] == "noop"


def test_execute_mock_supply():
    decision = {"action": "supply"}
    cfg = {"action": "supply"}
    result = asyncio.run(sentinel.execute_action(None, decision, cfg, Path("data/audit.jsonl"), mock=True))
    assert result["outcome"] == "success"
    assert result["tx_hash"].startswith("0xMOCK_")


def test_execute_mock_withdraw():
    decision = {"action": "withdraw"}
    result = asyncio.run(sentinel.execute_action(None, decision, {}, Path("data/audit.jsonl"), mock=True))
    assert result["outcome"] == "success"


def test_execute_mock_transfer():
    decision = {"action": "transfer"}
    result = asyncio.run(sentinel.execute_action(None, decision, {}, Path("data/audit.jsonl"), mock=True))
    assert result["outcome"] == "success"


def test_execute_live_supply_success():
    session = AsyncMock()
    result = MagicMock()
    item = MagicMock()
    item.text = json.dumps({"success": True, "transactionHash": "0xabc"})
    result.content = [item]
    session.call_tool = AsyncMock(return_value=result)
    cfg = {"network": "1", "wallet_address": "0x" + "a" * 40, "supply_asset": "0x" + "b" * 40,
           "supply_amount": "1000", "max_retries": 3}
    r = asyncio.run(sentinel.execute_action(session, {"action": "supply"}, cfg, Path("data/audit.jsonl")))
    assert r["outcome"] == "success"
    assert r["tx_hash"] == "0xabc"


def test_execute_live_supply_retries():
    session = AsyncMock()
    fail = MagicMock()
    fi = MagicMock()
    fi.text = json.dumps({"success": False, "error": "rpc down"})
    fail.content = [fi]
    ok = MagicMock()
    oi = MagicMock()
    oi.text = json.dumps({"success": True, "transactionHash": "0xwin"})
    ok.content = [oi]
    session.call_tool = AsyncMock(side_effect=[fail, ok])
    cfg = {"network": "1", "wallet_address": "0x" + "a" * 40, "supply_asset": "0x" + "b" * 40,
           "supply_amount": "1000", "max_retries": 3}
    with patch("sentinel.asyncio.sleep", new=AsyncMock()):
        r = asyncio.run(sentinel.execute_action(session, {"action": "supply"}, cfg, Path("data/audit.jsonl")))
    assert r["outcome"] == "success"
    assert r["tx_hash"] == "0xwin"
    assert session.call_tool.call_count == 2


def test_execute_live_supply_all_fail():
    session = AsyncMock()
    fail = MagicMock()
    fi = MagicMock()
    fi.text = json.dumps({"success": False, "error": "rpc down"})
    fail.content = [fi]
    session.call_tool = AsyncMock(return_value=fail)
    cfg = {"network": "1", "wallet_address": "0x" + "a" * 40, "supply_asset": "0x" + "b" * 40,
           "supply_amount": "1000", "max_retries": 2}
    with patch("sentinel.asyncio.sleep", new=AsyncMock()):
        r = asyncio.run(sentinel.execute_action(session, {"action": "supply"}, cfg, Path("data/audit.jsonl")))
    assert r["outcome"] == "failed"
    assert session.call_tool.call_count == 2


# ── run_cycle (integration) ──

def test_run_cycle_mock_rebalance(tmp_path):
    log = tmp_path / "audit.jsonl"
    cfg = {"network": "1", "wallet_address": "0x" + "a" * 40, "threshold": 1.5,
           "kill_switch": False, "supply_asset": "0x" + "b" * 40, "supply_amount": "1000",
           "max_amount_wei": "1000000", "cooldown_seconds": 0,
           "chain_allowlist": ["1"], "recipient_allowlist": [], "max_retries": 1,
           "withdraw_asset": "", "transfer_recipient": ""}
    record = asyncio.run(sentinel.run_cycle(None, cfg, log, trigger="manual", mock=True))
    assert record["outcome"] == "success"
    assert record["tx_hash"].startswith("0xMOCK_")
    assert record["decision"]["action"] == "supply"
    assert record["policy"]["allowed"] is True


def test_run_cycle_mock_hold(tmp_path):
    log = tmp_path / "audit.jsonl"
    cfg = {"network": "1", "wallet_address": "0x" + "a" * 40, "threshold": 1.5,
           "kill_switch": False, "supply_asset": "", "withdraw_asset": "",
           "transfer_recipient": "", "supply_amount": "0",
           "max_amount_wei": "1000000", "cooldown_seconds": 0,
           "chain_allowlist": ["1"], "recipient_allowlist": [], "max_retries": 1}
    with patch("sentinel.observe", new=AsyncMock(return_value={"health_factor": 2.0})):
        record = asyncio.run(sentinel.run_cycle(None, cfg, log, trigger="guardian", mock=True))
    assert record["outcome"] == "noop"
    assert record["decision"]["action"] == "noop"


def test_run_cycle_blocked_by_policy(tmp_path):
    log = tmp_path / "audit.jsonl"
    cfg = {"network": "1", "wallet_address": "0x" + "a" * 40, "threshold": 1.5,
           "kill_switch": True, "supply_asset": "0x" + "b" * 40, "supply_amount": "1000",
           "max_amount_wei": "1000000", "cooldown_seconds": 0,
           "chain_allowlist": ["1"], "recipient_allowlist": [], "max_retries": 1,
           "withdraw_asset": "", "transfer_recipient": ""}
    record = asyncio.run(sentinel.run_cycle(None, cfg, log, trigger="manual", mock=True))
    assert record["outcome"] == "blocked"
    assert record["policy"]["allowed"] is False


# ── event-driven mode ──

def test_observe_with_event():
    obs = asyncio.run(sentinel.observe(None, {"wallet_address": "0x" + "a" * 40, "network": "1"},
                                        mock=True, event={"name": "large_withdrawal", "tx_hash": "0xev"}))
    assert obs["recent_event"]["name"] == "large_withdrawal"
    assert obs["health_factor"] == 1.3  # still reads HF


def test_observe_without_event():
    obs = asyncio.run(sentinel.observe(None, {"wallet_address": "0x" + "a" * 40, "network": "1"}, mock=True))
    assert "recent_event" not in obs


def test_decide_event_triggers_supply():
    cfg = {"kill_switch": False, "threshold": 1.5, "supply_asset": "0x" + "c" * 40}
    obs = {"health_factor": 2.0, "recent_event": {"name": "large_withdrawal", "tx_hash": "0xev"}}
    d = sentinel.decide(obs, cfg)
    assert d["action"] == "supply"
    assert "event" in d["rationale"]


def test_decide_event_overrides_safe_hf():
    cfg = {"kill_switch": False, "threshold": 1.5, "supply_asset": "0x" + "c" * 40}
    obs = {"health_factor": 999.0, "recent_event": {"name": "collateral_removed"}}
    d = sentinel.decide(obs, cfg)
    assert d["action"] == "supply"  # event triggers even with safe HF


def test_decide_event_no_action():
    cfg = {"kill_switch": False, "threshold": 1.5, "supply_asset": "", "withdraw_asset": "", "transfer_recipient": ""}
    obs = {"health_factor": 999.0, "recent_event": {"name": "price_spike"}}
    d = sentinel.decide(obs, cfg)
    assert d["action"] == "noop"


def test_run_cycle_with_event(tmp_path):
    log = tmp_path / "audit.jsonl"
    cfg = {"network": "1", "wallet_address": "0x" + "a" * 40, "threshold": 1.5, "supply_asset": "0x" + "b" * 40,
           "kill_switch": False, "supply_amount": "1000", "max_amount_wei": "1000000", "cooldown_seconds": 0,
           "chain_allowlist": ["1"], "recipient_allowlist": [], "max_retries": 1,
           "withdraw_asset": "", "transfer_recipient": ""}
    obs = asyncio.run(sentinel.observe(None, cfg, mock=True,
        event={"name": "large_withdrawal", "tx_hash": "0xev1"}))
    decision = sentinel.decide(obs, cfg)
    policy = sentinel.evaluate_policy(decision, cfg, log)
    assert policy["allowed"] is True
    result = asyncio.run(sentinel.execute_action(None, decision, cfg, log, mock=True))
    assert result["outcome"] == "success"


# ── execution status polling ──

def test_poll_execution_success():
    session = AsyncMock()
    result = MagicMock()
    item = MagicMock()
    item.text = json.dumps({"status": "success", "transactionHash": "0xpoll"})
    result.content = [item]
    session.call_tool = AsyncMock(return_value=result)
    tx = asyncio.run(sentinel.poll_execution(session, "exec_123"))
    assert tx == "0xpoll"


def test_poll_execution_failed():
    session = AsyncMock()
    result = MagicMock()
    item = MagicMock()
    item.text = json.dumps({"status": "reverted"})
    result.content = [item]
    session.call_tool = AsyncMock(return_value=result)
    tx = asyncio.run(sentinel.poll_execution(session, "exec_456"))
    assert tx is None


def test_poll_execution_timeout():
    session = AsyncMock()
    result = MagicMock()
    item = MagicMock()
    item.text = json.dumps({"status": "pending"})
    result.content = [item]
    session.call_tool = AsyncMock(return_value=result)
    with patch("sentinel.asyncio.sleep", new=AsyncMock()):
        with patch("sentinel.time.time", side_effect=[0, 0, 70]):
            tx = asyncio.run(sentinel.poll_execution(session, "exec_789", timeout=60))
    assert tx is None


# ── gas-aware dynamic cooldown ──

def test_dynamic_cooldown_no_gas():
    assert sentinel.dynamic_cooldown(120, None) == 120
    assert sentinel.dynamic_cooldown(60, 0) == 60


def test_dynamic_cooldown_low_gas():
    # 10 gwei → 0.5x factor → 60
    assert sentinel.dynamic_cooldown(120, 10) == 60


def test_dynamic_cooldown_normal_gas():
    # 30 gwei → 1x factor → 120
    assert sentinel.dynamic_cooldown(120, 30) == 120


def test_dynamic_cooldown_high_gas():
    # 100 gwei → 3.33x factor → int(120*3.33) = 400
    assert sentinel.dynamic_cooldown(120, 100) == 400


def test_dynamic_cooldown_extreme_gas():
    # 1000 gwei → capped at 5x → 600
    assert sentinel.dynamic_cooldown(120, 1000) == 600
