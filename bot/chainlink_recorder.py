"""Tape recorder for Chainlink TWAP ticks.

Chainlink offers no history and no replay after a disconnect, so any tick not
written down while it streams is unrecoverable. That makes recording urgent in a
way most logging isn't: strategies that need weeks of TWAP tape can only be
backtested weeks after the recorder starts (docs/CHAINLINK_TWAP.md §6).

Writes are batched. At ~2 ticks/s a commit per tick would hammer the DB the
trader shares, so ticks buffer in memory and flush on size or age.
"""

from __future__ import annotations

import threading
import time
from decimal import Decimal
from typing import List, Optional, Tuple

from . import logger
from .db import ChainlinkTickModel, db, db_context, purge_old_ticks


FLUSH_EVERY = 50            # ticks
FLUSH_SECONDS = 10.0
PURGE_EVERY_SECONDS = 3600.0

E18 = Decimal(10) ** 18


class ChainlinkRecorder:
    """Buffers TWAP ticks and persists them in batches.

    Wire it to a feed by passing `record` as the feed's `on_tick`.
    """

    def __init__(self, *, retention_days: int = 30):
        self.retention_days = retention_days

        self._lock = threading.Lock()
        self._buffer: List[Tuple[str, int, str, int, int]] = []
        self._last_flush = time.time()
        self._last_purge = time.time()
        self._written = 0
        self._dropped = 0

    def record(
        self,
        symbol: str,
        window_s: int,
        value: Decimal,
        observed_at: int,
        received_at: int,
    ) -> None:
        """Feed callback. Buffers one tick; flushes when the batch is due.

        Stores the exact E18 integer as text. Converting back through float
        here would discard the precision the payload went to the trouble of
        encoding.
        """
        value_e18 = str(int(value * E18))
        # Store seconds — the model indexes observed_at and the backtest queries
        # it by second-resolution ranges.
        with self._lock:
            self._buffer.append(
                (symbol, window_s, value_e18, observed_at // 1000, received_at // 1000)
            )
            due = (
                len(self._buffer) >= FLUSH_EVERY
                or (time.time() - self._last_flush) >= FLUSH_SECONDS
            )
        if due:
            self.flush()

    def flush(self) -> int:
        """Persist buffered ticks. Returns rows written."""
        with self._lock:
            batch = self._buffer
            self._buffer = []
            self._last_flush = time.time()

        if not batch:
            return 0

        try:
            with db_context():
                db.session.bulk_save_objects([
                    ChainlinkTickModel(
                        symbol=symbol,
                        window_s=window_s,
                        value_e18=value_e18,
                        observed_at=observed_at,
                        received_at=received_at,
                    )
                    for symbol, window_s, value_e18, observed_at, received_at in batch
                ])
                db.session.commit()
        except Exception as exc:
            # A tape gap is bad but not worth stopping the bot for. Drop the
            # batch, count it, and keep going — the trader owns this thread's DB.
            self._dropped += len(batch)
            logger.warn(f"[chainlink] no se pudieron guardar {len(batch)} ticks: {exc}")
            return 0

        self._written += len(batch)
        self._maybe_purge()
        return len(batch)

    def _maybe_purge(self) -> None:
        """Trim old ticks once an hour.

        ~172.800 rows/day fills a VPS disk in weeks if nothing prunes it, and
        the failure mode — a full disk — takes the trader down with it.
        """
        if (time.time() - self._last_purge) < PURGE_EVERY_SECONDS:
            return
        self._last_purge = time.time()

        try:
            with db_context():
                removed = purge_old_ticks(self.retention_days)
            if removed:
                logger.info(
                    f"[chainlink] purgados {removed} ticks "
                    f"(>{self.retention_days} días)", icon="🧹"
                )
        except Exception as exc:
            logger.warn(f"[chainlink] purga falló: {exc}")

    def stats(self) -> dict:
        with self._lock:
            pending = len(self._buffer)
        return {
            "written": self._written,
            "dropped": self._dropped,
            "pending": pending,
            "retention_days": self.retention_days,
        }
