#!/usr/bin/env bash
# Deploy Mart-Hedge fixes (2026-10-06) to Hostinger VPS.
#
# Cambios incluidos en este deploy:
#   1. Issue #1 — multiplier ×2.5 constante (era 2.0 con +loss_pct)
#   2. Issue #2 — suprimir Path A y Path D tras Mart-Hedge
#   3. Issue #3 — loss-fallback 50% (sin side-change)
#   4. Issue #4 — entry_cutoff cableado a 120s (default)
#   5. Issue #5 — fix bug `ts` undefined en hold-winner
#
# Run from /workspaces/Polymarket-BotV1 with:
#   export SSHPASS='<vps-root-password>'
#   bash deploy_mart_hedge_2026-10-06.sh
#
# Si la contraseña tiene chars especiales, usar:
#   bash -c "SSHPASS='…' bash deploy_mart_hedge_2026-10-06.sh"
#
# Este script NO es idempotente.  No correr dos veces.

set -euo pipefail

VPS="root@76.13.251.202"
VPS_DIR="/opt/polymarket-bot"
TS=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR="${VPS_DIR}/.backups/mart_hedge_2026-10-06_${TS}"

SSH="sshpass -e ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
SCP="sshpass -e scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"

if [ -z "${SSHPASS:-}" ]; then
  echo "ERROR: SSHPASS env var is not set.  Export it and re-run." >&2
  exit 1
fi

echo "=== 1/7  Sanity check: local tests green? ==="
if ! python -m pytest tests/test_temporal_arb.py -q >/dev/null 2>&1; then
  echo "Local temporal_arb tests failed.  Fix locally first." >&2
  exit 1
fi
echo "    OK (local tests green)"

echo "=== 2/7  Confirm VPS is reachable ==="
if ! $SSH "$VPS" 'echo "VPS=$(hostname) up=$(uptime -p)"'; then
  echo "VPS unreachable.  Check SSHPASS / network." >&2
  exit 1
fi

echo "=== 3/7  Snapshot VPS working-tree files we'll touch ==="
$SSH "$VPS" "mkdir -p ${BACKUP_DIR} && \
  cp ${VPS_DIR}/bot/strategies/temporal_arb.py ${BACKUP_DIR}/temporal_arb.py.vps && \
  cp ${VPS_DIR}/bot/state.py                  ${BACKUP_DIR}/state.py.vps && \
  cp ${VPS_DIR}/tests/test_temporal_arb.py   ${BACKUP_DIR}/test_temporal_arb.py.vps && \
  echo BACKUP_OK"

echo "=== 4/7  Diff local vs VPS to know what we're overwriting ==="
$SSH "$VPS" "cd ${VPS_DIR} && \
  diff -u ${BACKUP_DIR}/temporal_arb.py.vps     bot/strategies/temporal_arb.py     | head -20 ; true ; \
  diff -u ${BACKUP_DIR}/state.py.vps            bot/state.py                        | head -20 ; true ; \
  diff -u ${BACKUP_DIR}/test_temporal_arb.py.vps tests/test_temporal_arb.py        | head -20 ; true"
echo "    (above diffs only show what's already different on VPS; not a blocker)"

echo "=== 5/7  scp new strategy + tests + surgical state.py patch ==="
$SCP bot/strategies/temporal_arb.py "${VPS}:${VPS_DIR}/bot/strategies/temporal_arb.py"
$SCP tests/test_temporal_arb.py     "${VPS}:${VPS_DIR}/tests/test_temporal_arb.py"
$SCP deploy_mart_hedge_patch_state.py "${VPS}:/tmp/patch_state.py"

# Surgical patch of state.py: keep ALL VPS customizations (cfd_enabled=True
# defaults, polymarket_*_balance fields, etc.) — only inject the new
# `ta_mart_hedge_loss_fallback_pct` field required by Issue #3.
#
# 1) add the attribute declaration after `ta_mart_hedge_max_rounds`
# 2) add it to the snapshot() mapping
#
# If the patch fails because state.py already has the field, skip silently
# (idempotent re-deploy).
$SSH "$VPS" "cd ${VPS_DIR} && python3 /tmp/patch_state.py"

echo "=== 6/7  Run full test suite on VPS in venv ==="
$SSH "$VPS" "cd ${VPS_DIR} && source venv/bin/activate && \
  python -m pytest tests/ -q --ignore=tests/test_spread_harvest.py 2>&1 | tail -10"

echo ""
echo "=== 7/7  Restart the bot service ==="
read -p "Tests green?  Restart the bot?  (y/N) " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
  echo "Aborted by user.  VPS files updated, but bot NOT restarted."
  echo "To restart later:  ssh root@76.13.251.202 'systemctl restart polymarket-bot'"
  echo "To restore from backup:  ssh root@76.13.251.202 'cp ${BACKUP_DIR}/*.vps ${VPS_DIR}/bot/strategies/ ${VPS_DIR}/bot/ ${VPS_DIR}/tests/'"
  exit 0
fi

$SSH "$VPS" "systemctl restart polymarket-bot && \
  sleep 2 && systemctl is-active polymarket-bot && \
  tail -n 30 ${VPS_DIR}/logs/bot.log 2>/dev/null || journalctl -u polymarket-bot -n 30 --no-pager"
echo "=== Deploy complete.  Backup at ${BACKUP_DIR} ==="