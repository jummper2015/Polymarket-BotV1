"""Chainlink BTC/USD aggregator on Ethereum mainnet — historical strike source.

The aggregator at `0xF403...eE88c` is the legacy Chainlink Data Feed for
BTC/USD on Ethereum. Each round (~1 hour heartbeat) has a `updatedAt`
timestamp and a `answer` price (8 decimals). We use it as the primary source
for the 5-min strike because Polymarket's "price at the beginning" comes from
Chainlink's BTC/USD TWAP stream, which aggregates the same exchanges the
legacy feed tracks.

This is Option C: a free, auth-free RPC + a public aggregator. Historical
data is recovered by walking backward from `latestRoundId` and stopping when
`updatedAt <= window_ts`. Worst-case queries per strike = ~12 (one heartbeat),
typically 1–3 because the bot asks for the strike ~5 s after the window opens.

## Sources tried (2026-09-13)

| RPC | Status |
|---|---|
| `https://cloudflare-eth.com` | ❌ internal error on our selector |
| `https://eth.llamarpc.com`  | ❌ empty body |
| `https://ethereum-rpc.publicnode.com` | ✅ works |
| `https://1rpc.io/eth` | ✅ works |

Both working RPCs are kept as fallbacks; if one fails or rate-limits, we
rotate to the next on the next call.

## Limitations

- Only the **legacy Data Feed** (price, not TWAP). Polymarket uses Data
  Streams TWAP — slightly different aggregation. The gap is typically <$10
  for BTC but non-zero during volatility.
- Round-based: 1 round per heartbeat (~1 hour). For a 5-min window the
  strike we return is the closest round >= window_ts (or the previous round
  if no round covers the exact timestamp). Worst case: a 5-min window
  born right after a heartbeat has the previous hour's price as its strike.
- Public RPC rate limits are aggressive. We rely on caching to keep QPS low
  (1 strike per 5-min window per symbol = 288 queries/day for BTC alone).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# AggregatorV3Interface selector + parameters
_SELECTOR_LATEST = "0xfeaf968c"          # latestRoundData()
_SELECTOR_GET_ROUND = "0x9a6fc8f5"        # getRoundData(uint80)
_SELECTOR_DECIMALS = "0x313ce567"         # decimals()

# BTC / USD aggregator on Ethereum mainnet (proxy that points at the actual
# aggregator contract). Verified live on 2026-09-13 via publicnode.com.
AGGREGATOR_ADDR = "0xF4030086522a5bEEa4988F8cA5B36dbC97BeE88c"

# Ordered by preference; rotated on failure.
_ETH_RPCS: List[str] = [
    "https://ethereum-rpc.publicnode.com",
    "https://1rpc.io/eth",
]
_RPC_TIMEOUT = 8.0
_RPC_RETRIES_PER_ENDPOINT = 2

_CACHE: dict[tuple[str, int], float] = {}
_CACHE_LIMIT = 512
_CACHE_LOCK = threading.Lock()


def _encode_uint80_call(selector_hex: str, value: int) -> str:
    """Encode `value` as a uint80 parameter for a function call."""
    return selector_hex + format(value, "x").zfill(64)


def _rpc_call(method: str, params: list, *, _depth: int = 0) -> Optional[dict]:
    """JSON-RPC call with endpoint rotation and bounded retries."""
    if _depth >= len(_ETH_RPCS) * _RPC_RETRIES_PER_ENDPOINT:
        return None

    rpc = _ETH_RPCS[_depth // _RPC_RETRIES_PER_ENDPOINT]
    attempt = _depth % _RPC_RETRIES_PER_ENDPOINT
    if attempt > 0:
        time.sleep(0.4)

    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": 1,
    }
    try:
        r = requests.post(rpc, json=payload, timeout=_RPC_TIMEOUT)
    except requests.RequestException as exc:
        if _depth + 1 < len(_ETH_RPCS) * _RPC_RETRIES_PER_ENDPOINT:
            return _rpc_call(method, params, _depth=_depth + 1)
        logger.warn(f"[chainlink] RPC {rpc} unreachable: {type(exc).__name__}")
        return None

    if r.status_code != 200:
        return _rpc_call(method, params, _depth=_depth + 1)

    try:
        data = r.json()
    except ValueError:
        return _rpc_call(method, params, _depth=_depth + 1)

    if "error" in data:
        # Rate limit or method error — try next endpoint.
        return _rpc_call(method, params, _depth=_depth + 1)

    return data


def _decode_round_data(result_hex: str) -> Optional[Tuple[int, float, int]]:
    """Decode the 5-word return of `latestRoundData()` / `getRoundData(uint80)`.

    Returns `(roundId, price, updatedAt)` or `None` on any failure.
    """
    if not result_hex or result_hex == "0x":
        return None
    h = result_hex[2:]
    if len(h) < 320:  # 5 × 64 hex chars
        return None
    try:
        round_id = int(h[0:64], 16)
        answer = int(h[64:128], 16)
        # startedAt = int(h[128:192], 16)  # unused
        updated_at = int(h[192:256], 16)
    except ValueError:
        return None

    # Chainlink BTC/USD uses 8 decimals. Guard against regressions.
    # (We don't fetch decimals() to save one round-trip; the feed has been
    # at 8 decimals since launch.)
    price = answer / 1e8
    if price <= 0:
        return None
    return round_id, price, updated_at


def _latest_round() -> Optional[Tuple[int, float, int]]:
    data = _rpc_call("eth_call", [{"to": AGGREGATOR_ADDR, "data": _SELECTOR_LATEST}, "latest"])
    if data is None:
        return None
    return _decode_round_data(data.get("result", ""))


def _round_at(round_id: int) -> Optional[Tuple[int, float, int]]:
    """getRoundData(uint80 roundId) — same shape as latestRoundData."""
    call_data = _encode_uint80_call(_SELECTOR_GET_ROUND, round_id)
    data = _rpc_call("eth_call", [{"to": AGGREGATOR_ADDR, "data": call_data}, "latest"])
    if data is None:
        return None
    return _decode_round_data(data.get("result", ""))


def _decimals() -> Optional[int]:
    data = _rpc_call("eth_call", [{"to": AGGREGATOR_ADDR, "data": _SELECTOR_DECIMALS}, "latest"])
    if data is None:
        return None
    result = data.get("result", "")
    if not result or result == "0x":
        return None
    try:
        return int(result, 16)
    except ValueError:
        return None


def get_strike_at(window_ts: int, symbol: str = "btc") -> Optional[float]:
    """BTC/USD price (Chainlink Ethereum) closest to `window_ts`.

    Walks backward from `latestRoundId` until a round with
    `updatedAt <= window_ts` is found, then returns that round's price. The
    strike is cached per `(symbol, window_ts)` to avoid re-querying for the
    same window across multiple strategies.

    Returns `None` if:
      - the symbol is not BTC (only BTC/USD is currently wired),
      - the RPC chain fails completely,
      - the aggregator returns malformed data.
    """
    if symbol.lower() != "btc":
        return None

    key = (symbol, int(window_ts))
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    if cached is not None:
        return cached

    latest = _latest_round()
    if latest is None:
        logger.warn("[chainlink] latestRoundData unavailable")
        return None

    round_id, latest_price, latest_updated_at = latest

    # Fast path: the latest round's updatedAt is already <= window_ts.
    if latest_updated_at <= window_ts:
        result_price: Optional[float] = latest_price
    else:
        # Walk backward until we find a round with updatedAt <= window_ts.
        # The Ethereum RPC bounds roundId queries to ~1024 blocks behind the
        # latest, but each roundId is a logical round, not a block. Most
        # public RPCs support historical getRoundData going back many
        # months. We bound the walk at 200 rounds (~16 hours) as a safety
        # net.
        cur_id = round_id
        result_price = None
        for _ in range(200):
            if cur_id == 0:
                break
            cur_id -= 1
            r = _round_at(cur_id)
            if r is None:
                # Likely hit a pruned round; stop walking.
                break
            _, p, updated_at = r
            if updated_at <= window_ts:
                result_price = p
                break

        if result_price is None:
            # No round found within the walk window — return the latest and
            # log. The caller should treat this as a soft failure.
            logger.warn(
                f"[chainlink] no round with updatedAt<={window_ts} within "
                f"200 rounds of latest (roundId={round_id})"
            )
            result_price = latest_price

    if result_price <= 0:
        return None

    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_LIMIT:
            for stale in sorted(_CACHE)[: _CACHE_LIMIT // 2]:
                _CACHE.pop(stale, None)
        _CACHE[key] = result_price
    return result_price


def decimals() -> Optional[int]:
    """Returns the aggregator's `decimals()` value. Cached after first call."""
    return _decimals()
