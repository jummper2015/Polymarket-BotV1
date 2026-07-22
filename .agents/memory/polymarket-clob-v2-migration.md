---
name: Polymarket CLOB V2 Migration
description: CLOB V2 went live April 28, 2026 — V1 SDK no longer works on production. Key breaking changes for the bot.
---

# Polymarket CLOB V2 Migration

**Why:** CLOB V2 went live April 28, 2026. V1 SDK (`py-clob-client`) is dead against production. Bot was upgraded to `py-clob-client-v2` (v1.1.0).

## Key SDK Changes (V1 → V2)

| What | V1 | V2 |
|------|----|-----|
| Package | `py-clob-client` | `py-clob-client-v2` |
| ClobClient init | positional args + `signature_type`, `funder` | keyword args only — no `signature_type` / `funder` |
| Derive creds | `create_or_derive_api_creds()` + `set_api_creds()` | `create_or_derive_api_key()`, pass `creds=` to constructor |
| Order placement | `create_order()` + `post_order()` | `create_and_post_order(order_args, options, order_type)` |
| Side constants | `from py_clob_client.order_builder.constants import BUY` | `from py_clob_client_v2 import Side; Side.BUY / Side.SELL` |
| Options | none required | `PartialCreateOrderOptions(tick_size="0.01")` |
| Collateral | USDC.e | **pUSD** (API auto-handles; wrap() needed for pure API users) |
| Order fields | `nonce`, `feeRateBps`, `taker` | `timestamp` (ms), `metadata`, `builder` — SDK handles automatically |
| EIP-712 domain | version "1" | version "2" |
| Fees | embedded in signed order | operator-set at match time |
| Builder auth | `POLY_BUILDER_*` headers | `builderCode` field on the order |

## What Did NOT Change
- WebSocket URLs: unchanged
- WebSocket message payloads: mostly unchanged
- L1/L2 authentication flow: identical
- CLOB host: `https://clob.polymarket.com`
- Gamma API (market discovery): unchanged
- Rate limits: unchanged

## Bot Files Updated
- `bot/trader.py` — `_build_client()`, `_place_real_order()`, `_place_real_sell_order()`, `_on_price()` signature
- `bot/strategy_mm.py` — `_build_client()`, `_place_real_order()`
- `requirements.txt` / `pyproject.toml` — `py-clob-client-v2>=1.1.0`

## How to Apply
When building the CLOB client in V2:
```python
from py_clob_client_v2 import ClobClient, OrderArgs, OrderType, PartialCreateOrderOptions, Side

seed = ClobClient(host=host, chain_id=chain_id, key=private_key)
creds = seed.create_or_derive_api_key()
client = ClobClient(host=host, chain_id=chain_id, key=private_key, creds=creds)

resp = client.create_and_post_order(
    order_args=OrderArgs(token_id=tid, price=price, size=shares, side=Side.BUY),
    options=PartialCreateOrderOptions(tick_size="0.01"),
    order_type=OrderType.GTC,
)
```
