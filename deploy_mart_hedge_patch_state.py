"""Surgical patch for bot/state.py — adds ta_mart_hedge_loss_fallback_pct.

Spec 2026-10-06, Issue #3. Idempotent: if the field is already present,
exits cleanly with a SKIP message.

Run on VPS via:
    python3 /tmp/patch_state.py
"""
import pathlib
import sys

p = pathlib.Path("bot/state.py")
src = p.read_text()

# 1) Add new attribute declaration after `self.ta_mart_hedge_max_rounds`
old_attr = "self.ta_mart_hedge_max_rounds: int   = 3"
new_attr = (
    "self.ta_mart_hedge_max_rounds: int   = 3\n"
    "        # Loss-fallback for Mart-Hedge round 1 (spec 2026-10-06, Issue #3).\n"
    "        # Si la 1ª pata cae ≥ este %, dispara Mart-Hedge aunque BTC no\n"
    "        # haya cruzado el strike. Default 0.50 = 50%.\n"
    "        self.ta_mart_hedge_loss_fallback_pct: float = 0.50"
)
if "ta_mart_hedge_loss_fallback_pct" in src:
    print("SKIP: ta_mart_hedge_loss_fallback_pct already in state.py")
    sys.exit(0)

if old_attr not in src:
    print("ERROR: 'self.ta_mart_hedge_max_rounds: int   = 3' not found")
    sys.exit(1)

src = src.replace(old_attr, new_attr, 1)

# 2) Add to snapshot() mapping after the max_rounds key
old_snap = '"ta_mart_hedge_max_rounds": self.ta_mart_hedge_max_rounds,'
new_snap = (
    '"ta_mart_hedge_max_rounds": self.ta_mart_hedge_max_rounds,\n'
    '                "ta_mart_hedge_loss_fallback_pct": self.ta_mart_hedge_loss_fallback_pct,'
)

if old_snap not in src:
    print("ERROR: snapshot key 'ta_mart_hedge_max_rounds' not found")
    sys.exit(1)

src = src.replace(old_snap, new_snap, 1)

p.write_text(src)
print("OK: state.py patched — added ta_mart_hedge_loss_fallback_pct")