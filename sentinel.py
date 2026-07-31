"""Sentinel — Aave V3 health factor guardian agent.

Observes an Aave V3 position via KeeperHub MCP, decides if the health factor
breached a threshold, enforces policy (kill switch, cooldown, chain/amount
limits), executes a rebalance action, and logs an append-only audit record.

Usage:
    python sentinel.py run [--mock]       # single cycle
    python sentinel.py watch [--mock]     # continuous loop
    python sentinel.py status             # show last audit entries
"""

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

# ── constants ──

MCP_URL = os.getenv("KEEPERHUB_MCP_URL", "https://app.keeperhub.com/mcp")
API_KEY = os.getenv("KEEPERHUB_API_KEY", "")
MOCK = "--mock" in sys.argv

AUDIT_PATH = Path(os.getenv("AUDIT_PATH", "data/audit.jsonl"))
CONFIG_PATH = os.getenv("CONFIG_PATH", "config.json")

MAX_UINT256 = 2**256 - 1
VALID_NETWORKS = {"1", "8453", "11155111"}

DEFAULT_CONFIG = {
    "network": "11155111",
    "wallet_address": "",
    "threshold": 1.5,
    "poll_interval": 60,
    "max_retries": 3,
    "cooldown_seconds": 120,
    "kill_switch": False,
    "max_amount_wei": "10000000000000000",
    "chain_allowlist": ["1", "8453", "11155111"],
    "recipient_allowlist": [],
    "supply_asset": "",
    "supply_amount": "1000000",
    "withdraw_asset": "",
    "withdraw_amount": "1000000",
    "transfer_recipient": "",
    "transfer_amount": "1000000000000000",
}


# ── config ──

def load_config(path=CONFIG_PATH):
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        with open(path) as f:
            cfg.update(json.load(f))
    # env overrides
    if os.getenv("WALLET_ADDRESS"):
        cfg["wallet_address"] = os.getenv("WALLET_ADDRESS")
    if os.getenv("NETWORK"):
        cfg["network"] = os.getenv("NETWORK")
    if os.getenv("HEALTH_FACTOR_THRESHOLD"):
        cfg["threshold"] = float(os.getenv("HEALTH_FACTOR_THRESHOLD"))
    return cfg


def validate_config(cfg):
    wallet = cfg["wallet_address"]
    if not wallet:
        raise ValueError("wallet_address not set")
    if not wallet.startswith("0x") or len(wallet) != 42:
        raise ValueError(f"bad wallet address: {wallet}")
    net = cfg["network"]
    if net not in VALID_NETWORKS:
        raise ValueError(f"unsupported network {net}")
    if cfg["threshold"] <= 0:
        raise ValueError("threshold must be positive")
    if cfg["max_retries"] < 1:
        raise ValueError("max_retries must be at least 1")


# ── audit ──

def audit(log_path, record):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if "id" not in record:
        record["id"] = str(uuid.uuid4())
    record["at"] = time.time()
    with open(log_path, "a") as f:
        f.write(json.dumps(record) + "\n")
    print(f"[audit] {record['outcome']} id={record['id'][:8]}")


def audit_list(log_path, limit=20):
    if not log_path.exists():
        return []
    lines = log_path.read_text().strip().split("\n")
    return [json.loads(l) for l in lines[-limit:]]


def last_success_time(log_path):
    for entry in reversed(audit_list(log_path, 1000)):
        if entry.get("outcome") == "success" and entry.get("tx_hash"):
            return entry["at"]
    return None


# ── mcp helpers ──

def parse_mcp_result(result):
    text = "".join(c.text for c in result.content if hasattr(c, "text"))
    if not text:
        raise RuntimeError("empty response from keeperhub")
    return json.loads(text)


def parse_health_factor(hf_raw):
    hf_raw = int(hf_raw)
    if hf_raw >= MAX_UINT256:
        return float("inf")
    return hf_raw / 1e18


# ── observe ──

async def observe(session, cfg, mock=False, event=None):
    """Read Aave V3 health factor and native balance for the wallet.
    
    If event is provided, it's merged into the observation (event-driven mode).
    Event shape: {"name": str, "tx_hash": str, "payload": dict}
    """
    wallet = cfg["wallet_address"]
    network = cfg["network"]

    if mock:
        obs = {"health_factor": 1.3, "native_balance_eth": 0.05, "network": network, "wallet": wallet}
        if event:
            obs["recent_event"] = event
        return obs

    result = await session.call_tool("execute_protocol_action", {
        "actionType": "aave-v3/get-user-account-data",
        "params": {"network": network, "user": wallet},
    })
    data = parse_mcp_result(result)
    if not data.get("success"):
        raise RuntimeError(f"keeperhub read failed: {data}")

    hf = parse_health_factor(data["result"]["healthFactor"])
    collateral = int(data["result"]["totalCollateralBase"]) / 1e18
    debt = int(data["result"]["totalDebtBase"]) / 1e18

    return {
        "health_factor": hf,
        "total_collateral_eth": collateral,
        "total_debt_eth": debt,
        "network": network,
        "wallet": wallet,
        **({"recent_event": event} if event else {}),
    }


# ── decide ──

def decide(observation, cfg):
    """Decide what action to take based on the observation."""
    hf = observation["health_factor"]
    threshold = cfg["threshold"]
    recent_event = observation.get("recent_event")

    # events always trigger a check — recent event overrides safe HF
    if recent_event:
        event_name = recent_event.get("name", "unknown")
        if cfg.get("supply_asset"):
            return {"action": "supply", "rationale": f"event: {event_name}"}
        if cfg.get("withdraw_asset"):
            return {"action": "withdraw", "rationale": f"event: {event_name}"}
        if cfg.get("transfer_recipient"):
            return {"action": "transfer", "rationale": f"event: {event_name}"}
        return {"action": "noop", "rationale": f"event: {event_name}, no action configured"}

    if hf < threshold:
        if cfg.get("supply_asset"):
            return {"action": "supply", "rationale": f"HF {hf:.4f} < {threshold}"}
        if cfg.get("withdraw_asset"):
            return {"action": "withdraw", "rationale": f"HF {hf:.4f} < {threshold}, withdrawing to repay"}
        if cfg.get("transfer_recipient"):
            return {"action": "transfer", "rationale": f"HF {hf:.4f} < {threshold}, topping up"}
        return {"action": "noop", "rationale": f"HF {hf:.4f} < {threshold} but no action configured"}

    return {"action": "noop", "rationale": f"HF {hf:.4f} >= {threshold}"}


# ── policy ──

def evaluate_policy(decision, cfg, log_path):
    """Check if the action is allowed by policy rules."""
    reasons = []

    if decision["action"] != "noop" and cfg["kill_switch"]:
        reasons.append("kill switch engaged")

    if decision["action"] != "noop":
        net = cfg["network"]
        if net not in cfg.get("chain_allowlist", []):
            reasons.append(f"chain {net} not allowlisted")

        # cooldown
        cooldown = cfg.get("cooldown_seconds", 0)
        if cooldown > 0:
            last = last_success_time(log_path)
            if last:
                elapsed = time.time() - last
                if elapsed < cooldown:
                    remaining = int(cooldown - elapsed)
                    reasons.append(f"cooldown active ({remaining}s left)")

    if decision["action"] == "transfer":
        recipient = cfg.get("transfer_recipient", "")
        allowlist = cfg.get("recipient_allowlist", [])
        if not recipient:
            reasons.append("transfer recipient not set")
        elif allowlist and recipient.lower() not in [a.lower() for a in allowlist]:
            reasons.append(f"recipient {recipient[:10]}... not allowlisted")

    if decision["action"] in ("supply", "withdraw"):
        amount_str = cfg.get("supply_amount" if decision["action"] == "supply" else "withdraw_amount", "0")
        max_str = cfg.get("max_amount_wei", "0")
        try:
            if int(amount_str) > int(max_str) and int(max_str) > 0:
                reasons.append(f"amount {amount_str} exceeds max {max_str}")
        except ValueError:
            reasons.append("invalid amount format")

    if not reasons:
        return {"allowed": True, "reasons": []}
    return {"allowed": False, "reasons": reasons}


# ── execute ──

async def execute_action(session, decision, cfg, log_path, mock=False):
    """Execute the decided action via KeeperHub MCP."""
    action = decision["action"]

    if action == "noop":
        return {"outcome": "noop", "tx_hash": None}

    if mock:
        tx = f"0xMOCK_{int(time.time()):x}"
        return {"outcome": "success", "tx_hash": tx, "action": action}

    network = cfg["network"]
    wallet = cfg["wallet_address"]

    if action == "supply":
        return await _execute_supply(session, cfg, log_path)
    elif action == "withdraw":
        return await _execute_withdraw(session, cfg, log_path)
    elif action == "transfer":
        return await _execute_transfer(session, cfg, log_path)
    return {"outcome": "failed", "error": f"unknown action {action}"}


async def poll_execution(session, exec_id, timeout=60):
    """Poll execution status until confirmed or timeout."""
    started = time.time()
    while time.time() - started < timeout:
        result = await session.call_tool("get_direct_execution_status", {
            "executionId": exec_id,
        })
        try:
            data = parse_mcp_result(result)
        except Exception:
            await asyncio.sleep(2)
            continue
        status = (data.get("status") or "").lower()
        if status in ("success", "confirmed"):
            return data.get("transactionHash") or data.get("tx_hash")
        if status in ("failed", "reverted"):
            return None
        await asyncio.sleep(2)
    return None  # timeout


async def _execute_supply(session, cfg, log_path):
    for attempt in range(1, cfg["max_retries"] + 1):
        try:
            result = await session.call_tool("execute_protocol_action", {
                "actionType": "aave-v3/supply",
                "params": {
                    "network": cfg["network"],
                    "asset": cfg["supply_asset"],
                    "amount": str(cfg["supply_amount"]),
                    "onBehalfOf": cfg["wallet_address"],
                },
            })
            data = parse_mcp_result(result)
            tx_hash = data.get("transactionHash")
            exec_id = data.get("executionId") or data.get("execution_id")

            # poll if we got an execution ID but no tx hash yet
            if exec_id and not tx_hash:
                print(f"[poll] {exec_id}")
                tx_hash = await poll_execution(session, exec_id)
                await asyncio.sleep(1)

            if tx_hash:
                return {"outcome": "success", "tx_hash": tx_hash, "attempt": attempt}
            err = data.get("error", "no tx hash")
            print(f"[retry {attempt}] {err}")
            if attempt < cfg["max_retries"]:
                await asyncio.sleep(2 ** attempt)
        except Exception as e:
            print(f"[retry {attempt}] {e}")
            if attempt < cfg["max_retries"]:
                await asyncio.sleep(2 ** attempt)
    return {"outcome": "failed", "error": "supply exhausted retries"}


async def _execute_withdraw(session, cfg, log_path):
    for attempt in range(1, cfg["max_retries"] + 1):
        try:
            result = await session.call_tool("execute_protocol_action", {
                "actionType": "aave-v3/withdraw",
                "params": {
                    "network": cfg["network"],
                    "asset": cfg["withdraw_asset"],
                    "amount": str(cfg["withdraw_amount"]),
                    "to": cfg["wallet_address"],
                },
            })
            data = parse_mcp_result(result)
            tx_hash = data.get("transactionHash")
            exec_id = data.get("executionId") or data.get("execution_id")

            # poll if we got an execution ID but no tx hash yet
            if exec_id and not tx_hash:
                print(f"[poll] {exec_id}")
                tx_hash = await poll_execution(session, exec_id)
                await asyncio.sleep(1)

            if tx_hash:
                return {"outcome": "success", "tx_hash": tx_hash, "attempt": attempt}
            err = data.get("error", "no tx hash")
            print(f"[retry {attempt}] {err}")
            if attempt < cfg["max_retries"]:
                await asyncio.sleep(2 ** attempt)
        except Exception as e:
            print(f"[retry {attempt}] {e}")
            if attempt < cfg["max_retries"]:
                await asyncio.sleep(2 ** attempt)
    return {"outcome": "failed", "error": "withdraw exhausted retries"}


async def _execute_transfer(session, cfg, log_path):
    for attempt in range(1, cfg["max_retries"] + 1):
        try:
            result = await session.call_tool("execute_transfer", {
                "chain_id": cfg["network"],
                "to": cfg["transfer_recipient"],
                "amount": str(cfg["transfer_amount"]),
            })
            data = parse_mcp_result(result)
            tx_hash = data.get("transactionHash")
            exec_id = data.get("executionId") or data.get("execution_id")

            # poll if we got an execution ID but no tx hash yet
            if exec_id and not tx_hash:
                print(f"[poll] {exec_id}")
                tx_hash = await poll_execution(session, exec_id)
                await asyncio.sleep(1)

            if tx_hash:
                return {"outcome": "success", "tx_hash": tx_hash, "attempt": attempt}
            err = data.get("error", "no tx hash")
            print(f"[retry {attempt}] {err}")
            if attempt < cfg["max_retries"]:
                await asyncio.sleep(2 ** attempt)
        except Exception as e:
            print(f"[retry {attempt}] {e}")
            if attempt < cfg["max_retries"]:
                await asyncio.sleep(2 ** attempt)
    return {"outcome": "failed", "error": "transfer exhausted retries"}


# ── gas-aware cooldown ──

def dynamic_cooldown(base_seconds, gas_price_gwei=None):
    """Scale cooldown with gas price. Higher gas = longer wait between actions."""
    if not gas_price_gwei or gas_price_gwei <= 0:
        return base_seconds
    # at 30 gwei = base, at 100 gwei = 3x base, at 10 gwei = 0.5x base
    factor = max(0.5, min(5.0, gas_price_gwei / 30.0))
    return int(base_seconds * factor)



async def run_cycle(session, cfg, log_path, trigger="manual", mock=False):
    """One full observe -> decide -> policy -> execute -> audit cycle."""
    obs = await observe(session, cfg, mock=mock)
    decision = decide(obs, cfg)
    policy = evaluate_policy(decision, cfg, log_path)

    print(f"[observe] HF={obs['health_factor']:.4f} collateral={obs.get('total_collateral_eth', 0):.4f} ETH")
    print(f"[decide] {decision['action']} — {decision['rationale']}")

    if not policy["allowed"]:
        print(f"[policy] blocked: {'; '.join(policy['reasons'])}")
        record = {
            "trigger": trigger,
            "observation": obs,
            "decision": decision,
            "policy": policy,
            "outcome": "blocked",
        }
        audit(log_path, record)
        return record

    if decision["action"] == "noop":
        record = {
            "trigger": trigger,
            "observation": obs,
            "decision": decision,
            "policy": policy,
            "outcome": "noop",
        }
        audit(log_path, record)
        return record

    result = await execute_action(session, decision, cfg, log_path, mock=mock)

    if result["outcome"] == "success":
        print(f"[execute] tx={result['tx_hash']}")
    else:
        print(f"[execute] failed: {result.get('error')}")

    record = {
        "trigger": trigger,
        "observation": obs,
        "decision": decision,
        "policy": policy,
        "outcome": result["outcome"],
        "tx_hash": result.get("tx_hash"),
        "error": result.get("error"),
    }
    audit(log_path, record)
    return record


# ── connection ──

async def connect_mcp(api_key, proxy_url=None):
    import httpx
    kwargs = {"headers": {"Authorization": f"Bearer {api_key}"}, "timeout": 30}
    if proxy_url:
        kwargs["proxy"] = proxy_url
    http_client = httpx.AsyncClient(**kwargs)
    transport_ctx = streamable_http_client(MCP_URL, http_client=http_client)
    transport = await transport_ctx.__aenter__()
    read, write = transport[0], transport[1]
    session = ClientSession(read, write)
    await session.__aenter__()
    await session.initialize()
    return session, transport_ctx


async def disconnect_mcp(session, transport_ctx):
    try:
        await session.__aexit__(None, None, None)
    except Exception:
        pass
    try:
        await transport_ctx.__aexit__(None, None, None)
    except Exception:
        pass


# ── CLI ──

async def cmd_run(cfg, mock=False):
    log_path = AUDIT_PATH
    if mock:
        record = await run_cycle(None, cfg, log_path, trigger="manual", mock=True)
        print(f"\nDone. outcome={record['outcome']}")
        return

    proxy = os.getenv("ALL_PROXY") or os.getenv("HTTPS_PROXY")
    session, ctx = await connect_mcp(API_KEY, proxy)
    print("Connected to KeeperHub MCP")
    try:
        await run_cycle(session, cfg, log_path, trigger="manual")
    finally:
        await disconnect_mcp(session, ctx)


async def cmd_watch(cfg, mock=False):
    log_path = AUDIT_PATH
    interval = cfg["poll_interval"]
    print(f"Watching... interval={interval}s kill_switch={'on' if cfg['kill_switch'] else 'off'}")

    if mock:
        while True:
            try:
                await run_cycle(None, cfg, log_path, trigger="guardian", mock=True)
                await asyncio.sleep(interval)
            except KeyboardInterrupt:
                print("\nStopped.")
                break
        return

    proxy = os.getenv("ALL_PROXY") or os.getenv("HTTPS_PROXY")
    session, ctx = await connect_mcp(API_KEY, proxy)
    print("Connected to KeeperHub MCP")
    try:
        while True:
            try:
                await run_cycle(session, cfg, log_path, trigger="guardian")
                await asyncio.sleep(interval)
            except KeyboardInterrupt:
                print("\nStopped.")
                audit(log_path, {"trigger": "shutdown", "outcome": "shutdown"})
                break
            except Exception as e:
                print(f"[error] {e}")
                audit(log_path, {"trigger": "error", "outcome": "error", "error": str(e)})
                await asyncio.sleep(interval)
    finally:
        await disconnect_mcp(session, ctx)


def cmd_status():
    entries = audit_list(AUDIT_PATH, 10)
    if not entries:
        print("No audit records yet.")
        return
    print(f"Last {len(entries)} audit records:")
    for e in reversed(entries):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["at"]))
        outcome = e["outcome"]
        tx = e.get("tx_hash", "")
        tx_short = tx[:16] + "..." if tx and len(tx) > 16 else tx or "-"
        print(f"  {ts} | {outcome:8s} | {e.get('trigger', '-'):8s} | tx={tx_short}")


async def main():
    if not sys.argv[1:] or sys.argv[1] == "--mock":
        print("Usage: python sentinel.py [run|watch|status] [--mock]")
        sys.exit(1)

    cmd = sys.argv[1]
    cfg = load_config()

    if cmd == "status":
        cmd_status()
        return

    if not MOCK:
        validate_config(cfg)
    if not API_KEY and not MOCK:
        print("Set KEEPERHUB_API_KEY or use --mock")
        sys.exit(1)

    print(f"Sentinel — network={'MOCK' if MOCK else cfg['network']} wallet={cfg['wallet_address'][:10]}... threshold={cfg['threshold']}")

    if cmd == "run":
        await cmd_run(cfg, mock=MOCK)
    elif cmd == "watch":
        await cmd_watch(cfg, mock=MOCK)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
