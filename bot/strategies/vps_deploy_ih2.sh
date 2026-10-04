cd /opt/polymarket-bot
echo "=== Deploy strategies files ==="
ls bot/strategies/temporal_arb.py bot/strategies/impulse_hedge.py bot/strategies/__init__.py 2>&1 | head -3
echo
echo "=== Restart bot ==="
systemctl restart polymarket-bot
sleep 10
systemctl is-active polymarket-bot
echo
echo "=== Verify registry ==="
source venv/bin/activate
python3 -c "
from bot import strategies
ids = strategies.ids()
print(f'Registered: {ids}')
print(f'impulse_hedge: {\"impulse_hedge\" in ids}')
"
echo
echo "=== Recent log ==="
grep -aE "impulse_hedge|Streak Snapper" /opt/polymarket-bot/logs/bot.log | tail -3