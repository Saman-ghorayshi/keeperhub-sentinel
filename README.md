# Sentinel

Watches an Aave V3 position and auto-supplies collateral when the health factor drops below a threshold. Talks to KeeperHub's MCP server for all onchain reads and writes.

## Setup

```bash
pip install mcp httpx
cp .env.example .env   # fill in your key and wallet
python sentinel.py
```

For a dry run without hitting the chain:

```bash
python sentinel.py --mock
```

## Config

All via env vars or `.env`:

| Var | Default | What |
|-----|---------|------|
| `KEEPERHUB_API_KEY` | (required) | Your kh_ key from app.keeperhub.com > Settings > API Keys |
| `WALLET_ADDRESS` | (required) | The wallet to monitor |
| `NETWORK` | `1` | Chain ID (1=mainnet, 8453=base, 11155111=sepolia) |
| `HEALTH_FACTOR_THRESHOLD` | `1.5` | Trigger rebalance below this |
| `POLL_INTERVAL` | `60` | Seconds between checks |
| `MAX_RETRIES` | `3` | Retry attempts on failed tx |
| `SUPPLY_ASSET` | | ERC20 token address to supply as collateral |
| `SUPPLY_AMOUNT` | `1000000` | Amount in smallest unit (6 decimals for USDC) |
| `ALL_PROXY` | | Proxy URL if needed |

## How it works

1. Reads Aave V3 `getUserAccountData` for the wallet via KeeperHub MCP (`execute_protocol_action`)
2. Checks if health factor < threshold
3. If yes, calls `aave-v3/supply` via KeeperHub to add collateral
4. Retries failed transactions with exponential backoff (2s, 4s, 8s)
5. Validates wallet address and network before connecting
6. Every action logged to `data/audit.jsonl` (append-only)

## Audit trail

```bash
cat data/audit.jsonl | jq .
```

## Requirements

- Python 3.10+
- mcp package (`pip install mcp`)
- httpx (`pip install httpx`)
- A KeeperHub account with an API key and wallet integration
