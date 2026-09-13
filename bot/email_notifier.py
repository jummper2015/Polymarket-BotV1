"""Email notification system for Polymarket Bot.

Sends alerts for:
- Trade executions (TA entries, pairs, LPT, hedge)
- Daily summary (P&L, win rate, trades count)
- Critical errors (crashes, connection loss)
- P&L alerts (thresholds exceeded)
"""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from typing import Optional


class EmailNotifier:
    """Gmail-based email notifier for bot events."""

    def __init__(self):
        self.smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("SMTP_PORT", "587"))
        self.smtp_user = os.getenv("SMTP_USER", "")
        self.smtp_password = os.getenv("SMTP_PASSWORD", "")
        self.notify_email = os.getenv("NOTIFY_EMAIL", "")
        self.enabled = bool(self.smtp_user and self.smtp_password and self.notify_email)

        # Thresholds
        self.pnl_alert_loss = float(os.getenv("PNL_ALERT_LOSS", "-50"))
        self.pnl_alert_gain = float(os.getenv("PNL_ALERT_GAIN", "100"))

    def send_email(self, subject: str, body: str, html: bool = False) -> bool:
        """Send email notification."""
        if not self.enabled:
            return False

        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = f"[PolyBot] {subject}"
            msg['From'] = self.smtp_user
            msg['To'] = self.notify_email

            if html:
                msg.attach(MIMEText(body, 'html'))
            else:
                msg.attach(MIMEText(body, 'plain'))

            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                server.login(self.smtp_user, self.smtp_password)
                server.send_message(msg)

            return True
        except Exception as e:
            print(f"[EmailNotifier] Error sending email: {e}")
            return False

    def notify_trade_executed(self, trade_data: dict):
        """Notify when TA executes a trade."""
        side = trade_data.get('direction', 'N/A')
        price = trade_data.get('entry_price', 0)
        shares = trade_data.get('shares', 0)
        cost = trade_data.get('cost', 0)
        window = trade_data.get('window_slug', 'N/A')
        strategy = trade_data.get('strategy', 'temporal_arb')

        subject = f"🎯 Trade ejecutado: {side} @ ${price:.3f}"

        body = f"""
Temporal Arb ha ejecutado una operación:

📊 DETALLES
Estrategia: {strategy}
Lado: {side}
Precio: ${price:.3f}
Shares: {shares}
Costo: ${cost:.2f}
Ventana: {window}
Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}

🔗 Ver dashboard: https://polytradebot.cloud
"""
        self.send_email(subject, body)

    def notify_pair_completed(self, first_side: str, first_price: float,
                              second_side: str, second_price: float,
                              locked_profit: float):
        """Notify when TA completes a pair."""
        subject = f"📦 Par completo - Profit locked: ${locked_profit:.3f}"

        body = f"""
¡Temporal Arb completó un par exitosamente!

📦 PAR COMPLETO
Primera pata: {first_side} @ ${first_price:.3f}
Segunda pata: {second_side} @ ${second_price:.3f}
Costo total: ${first_price + second_price:.3f}
Profit locked: ${locked_profit:.3f}/share

🔗 Ver dashboard: https://polytradebot.cloud
"""
        self.send_email(subject, body)

    def notify_daily_summary(self, stats: dict):
        """Send daily summary email."""
        subject = f"📈 Resumen diario - P&L: ${stats.get('daily_pnl', 0):.2f}"

        body = f"""
Resumen de las últimas 24 horas:

📊 ESTADÍSTICAS
P&L del día: ${stats.get('daily_pnl', 0):.2f}
P&L total: ${stats.get('total_pnl', 0):.2f}
Win rate: {stats.get('win_rate_pct', 0):.1f}%
Trades hoy: {stats.get('daily_trades', 0)}
Trades totales: {stats.get('total_trades', 0)}
Wins/Losses: {stats.get('wins', 0)}W / {stats.get('losses', 0)}L

💰 SALDO
Bankroll actual: ${stats.get('current_bankroll', 0):.2f}
Disponible: ${stats.get('available_balance', 0):.2f}
Trades abiertas: {stats.get('open_trades', 0)}

🔗 Ver dashboard: https://polytradebot.cloud
"""
        self.send_email(subject, body)

    def notify_error(self, error_type: str, error_msg: str, traceback: Optional[str] = None):
        """Notify critical errors."""
        subject = f"❌ ERROR: {error_type}"

        body = f"""
⚠️ El bot encontró un error crítico:

ERROR: {error_type}
Mensaje: {error_msg}
Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}

"""
        if traceback:
            body += f"\nTraceback:\n{traceback}\n"

        body += "\n🔗 Revisar logs: ssh root@2.25.128.147\n"
        body += "tail -100 /opt/polymarket-bot/logs/bot.error.log\n"

        self.send_email(subject, body)

    def notify_pnl_alert(self, daily_pnl: float, total_pnl: float):
        """Notify when P&L crosses thresholds."""
        if daily_pnl <= self.pnl_alert_loss:
            subject = f"⚠️ Alerta: Pérdida diaria ${daily_pnl:.2f}"
            emoji = "📉"
        elif daily_pnl >= self.pnl_alert_gain:
            subject = f"🎉 Alerta: Ganancia diaria ${daily_pnl:.2f}"
            emoji = "📈"
        else:
            return

        body = f"""
{emoji} ALERTA DE P&L

P&L del día: ${daily_pnl:.2f}
P&L total: ${total_pnl:.2f}
Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}

🔗 Ver dashboard: https://polytradebot.cloud
"""
        self.send_email(subject, body)

    def notify_connection_loss(self, duration_minutes: int):
        """Notify when bot loses connection for extended period."""
        subject = f"⚠️ Conexión perdida por {duration_minutes} minutos"

        body = f"""
El bot perdió conexión con Polymarket:

⏱️ Duración: {duration_minutes} minutos
Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}

El bot intentará reconectarse automáticamente.

🔗 Revisar estado: https://polytradebot.cloud
"""
        self.send_email(subject, body)


# Singleton instance
_notifier = None

def get_notifier() -> EmailNotifier:
    """Get or create the global notifier instance."""
    global _notifier
    if _notifier is None:
        _notifier = EmailNotifier()
    return _notifier
