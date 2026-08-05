"""Unit tests for binance_api.py — mocked Binance API responses."""

import sys
import os
import time
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bot.binance_api import (
    get_5min_windows,
    get_last_closed_4h_candle,
    get_btc_spot_price,
    get_window_direction,
    NEAR_FLAT_THRESHOLD,
)


# ── Helper: build a mock kline response ───────────────────────────────────────
# Binance kline format: [open_time, open, high, low, close, volume, close_time, ...]


def _make_kline(open_time_ms: int, open_px: float, close_px: float) -> list:
    """Build one 5m or 4h candle in Binance format."""
    return [
        open_time_ms,                    # 0: open time (ms)
        str(open_px),                    # 1: open
        str(max(open_px, close_px) + 5),# 2: high
        str(min(open_px, close_px) - 5),# 3: low
        str(close_px),                   # 4: close
        "100.0",                         # 5: volume
        open_time_ms + 299_999,          # 6: close time (ms)
        "5000.0",                        # 7: quote asset volume
        500,                             # 8: number of trades
        "50.0",                          # 9: taker buy base vol
        "2500.0",                        # 10: taker buy quote vol
        "0",                             # 11: ignore
    ]


def _make_kline_series(open_times_ms: list[int], opens: list[float], closes: list[float]) -> list:
    """Build a list of kline candles."""
    return [_make_kline(t, o, c) for t, o, c in zip(open_times_ms, opens, closes)]


# ── get_5min_windows ──────────────────────────────────────────────────────────


class TestGet5minWindows:
    """Tests for get_5min_windows() — 5-minute window history."""

    @patch("bot.binance_api.requests.get")
    def test_basic_17_windows_all_up(self, mock_get):
        """17 UP candles -> 16 windows, all UP, streak of 16."""
        base_ms = 1_785_600_000_000
        opens  = [62000.0 + i * 10 for i in range(17)]
        closes = [o + 50.0 for o in opens]  # all close above open → UP
        times  = [base_ms + i * 300_000 for i in range(17)]
        candles = _make_kline_series(times, opens, closes)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = candles
        mock_get.return_value = mock_resp

        result = get_5min_windows(16)
        assert result is not None
        assert len(result) == 16
        assert all(w["direction"] == "UP" for w in result)
        assert result[0]["ts"] == base_ms // 1000
        assert result[-1]["ts"] == (base_ms + 15 * 300_000) // 1000

    @patch("bot.binance_api.requests.get")
    def test_alternating_up_down(self, mock_get):
        """UP, DOWN, UP, DOWN... verify directions are correct."""
        base_ms = 1_785_600_000_000
        opens  = [62000.0] * 17
        closes = [62050.0, 61950.0] * 8 + [62050.0]  # UP, DOWN, UP, DOWN...
        times  = [base_ms + i * 300_000 for i in range(17)]
        candles = _make_kline_series(times, opens, closes)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = candles
        mock_get.return_value = mock_resp

        result = get_5min_windows(16)
        assert result is not None
        assert len(result) == 16
        expected = ["UP", "DOWN"] * 8
        for i, w in enumerate(result):
            assert w["direction"] == expected[i]

    @patch("bot.binance_api.requests.get")
    def test_streak_of_6_down(self, mock_get):
        """Last 6 windows all DOWN — perfect for fade signal testing."""
        base_ms = 1_785_600_000_000
        times  = [base_ms + i * 300_000 for i in range(17)]
        opens  = [62000.0] * 17
        closes = opens[:]
        # First 10: alternating, last 7: all DOWN (6 after dropping incomplete)
        for i in range(17):
            if i < 10:
                closes[i] = opens[i] + (50 if i % 2 == 0 else -50)
            else:
                closes[i] = opens[i] - 50  # all DOWN for last 7
        candles = _make_kline_series(times, opens, closes)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = candles
        mock_get.return_value = mock_resp

        result = get_5min_windows(16)
        assert result is not None
        assert len(result) == 16
        # Last 6 windows should be DOWN
        for w in result[-6:]:
            assert w["direction"] == "DOWN"

    @patch("bot.binance_api.requests.get")
    def test_price_values(self, mock_get):
        """Verify open and close prices are parsed correctly as floats."""
        base_ms = 1_785_600_000_000
        times  = [base_ms + i * 300_000 for i in range(17)]
        opens  = [62300.00 + i * 10 for i in range(17)]
        closes = [o + 20.00 for o in opens]  # exact round values
        candles = _make_kline_series(times, opens, closes)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = candles
        mock_get.return_value = mock_resp

        result = get_5min_windows(16)
        assert result is not None
        assert isinstance(result[0]["open"], float)
        assert isinstance(result[0]["close"], float)
        assert result[0]["open"] == 62300.00
        assert result[0]["close"] == 62320.00
        # Verify a later window too
        assert result[5]["open"] == 62350.00
        assert result[5]["close"] == 62370.00

    @patch("bot.binance_api.requests.get")
    def test_too_few_windows_returns_none(self, mock_get):
        """Only 3 candles -> less than 4 windows -> should return None."""
        base_ms = 1_785_600_000_000
        times  = [base_ms + i * 300_000 for i in range(4)]  # 4 candles -> 3 windows
        opens  = [62000.0] * 4
        closes = [62050.0] * 4
        candles = _make_kline_series(times, opens, closes)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = candles
        mock_get.return_value = mock_resp

        result = get_5min_windows(16)
        assert result is None

    @patch("bot.binance_api.time.sleep")  # skip retry delay
    @patch("bot.binance_api.requests.get")
    def test_network_error_returns_none(self, mock_get, mock_sleep):
        """Network error -> should return None after retries."""
        mock_get.side_effect = Exception("Connection refused")

        result = get_5min_windows(16)
        assert result is None

    @patch("bot.binance_api.requests.get")
    def test_bad_status_code_returns_none(self, mock_get):
        """HTTP 500 -> should return None."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_get.return_value = mock_resp

        result = get_5min_windows(16)
        assert result is None

    @patch("bot.binance_api.requests.get")
    def test_flat_candles_equal_up(self, mock_get):
        """open == close -> should be UP."""
        base_ms = 1_785_600_000_000
        times  = [base_ms + i * 300_000 for i in range(17)]
        opens  = [62000.0] * 17
        closes = [62000.0] * 17  # exact same -> UP (close >= open)
        candles = _make_kline_series(times, opens, closes)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = candles
        mock_get.return_value = mock_resp

        result = get_5min_windows(16)
        assert result is not None
        assert all(w["direction"] == "UP" for w in result)


# ── get_last_closed_4h_candle ─────────────────────────────────────────────────


class TestGetLastClosedFourHourCandle:
    """The trend cycle signals off the candle that finished, not the live one."""

    @staticmethod
    def _respond(mock_get, candles):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = candles
        mock_get.return_value = mock_resp

    @patch("bot.binance_api.requests.get")
    def test_returns_the_closed_candle_not_the_forming_one(self, mock_get):
        closed  = _make_kline(1_785_600_000_000, 63000.0, 63630.0)   # +1.0%
        forming = _make_kline(1_785_614_400_000, 63630.0, 63000.0)   # still open
        self._respond(mock_get, [closed, forming])

        result = get_last_closed_4h_candle()
        assert result is not None
        assert result["ts"] == 1_785_600_000
        assert result["direction"] == "UP"
        assert result["close"] == 63630.0

    @patch("bot.binance_api.requests.get")
    def test_strength_is_signed(self, mock_get):
        closed  = _make_kline(1_785_600_000_000, 63000.0, 62370.0)   # −1.0%
        forming = _make_kline(1_785_614_400_000, 62370.0, 62400.0)
        self._respond(mock_get, [closed, forming])

        result = get_last_closed_4h_candle()
        assert result["direction"] == "DOWN"
        assert result["strength"] == pytest.approx(-0.01)

    @patch("bot.binance_api.requests.get")
    def test_block_covers_the_four_hours_after_the_candle(self, mock_get):
        closed  = _make_kline(1_785_600_000_000, 63000.0, 63630.0)
        forming = _make_kline(1_785_614_400_000, 63630.0, 63700.0)
        self._respond(mock_get, [closed, forming])

        result = get_last_closed_4h_candle()
        assert result["block_start"] == 1_785_600_000 + 14400
        assert result["block_end"] == 1_785_600_000 + 28800

    @patch("bot.binance_api.requests.get")
    def test_single_candle_returns_none(self, mock_get):
        """Without a forming candle there is no way to know which one closed."""
        self._respond(mock_get, [_make_kline(1_785_600_000_000, 63000.0, 63500.0)])
        assert get_last_closed_4h_candle() is None

    @patch("bot.binance_api.time.sleep")
    @patch("bot.binance_api.requests.get")
    def test_network_error_returns_none(self, mock_get, mock_sleep):
        mock_get.side_effect = Exception("Timeout")
        assert get_last_closed_4h_candle() is None


# ── get_btc_spot_price ────────────────────────────────────────────────────────


class TestGetBtcSpotPrice:
    """Tests for get_btc_spot_price() — current BTC spot price."""

    @patch("bot.binance_api.requests.get")
    def test_spot_price_ok(self, mock_get):
        """Normal response with price."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"symbol": "BTCUSDT", "price": "62450.50"}
        mock_get.return_value = mock_resp

        result = get_btc_spot_price()
        assert result is not None
        assert result == 62450.50
        assert isinstance(result, float)

    @patch("bot.binance_api.requests.get")
    def test_spot_price_large(self, mock_get):
        """BTC at $100k+ — verify large numbers parse correctly."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"symbol": "BTCUSDT", "price": "123456.78"}
        mock_get.return_value = mock_resp

        result = get_btc_spot_price()
        assert result == 123456.78

    @patch("bot.binance_api.time.sleep")  # skip retry delay
    @patch("bot.binance_api.requests.get")
    def test_spot_price_network_error_returns_none(self, mock_get, mock_sleep):
        """Network error -> None."""
        mock_get.side_effect = Exception("Connection error")

        result = get_btc_spot_price()
        assert result is None

    @patch("bot.binance_api.requests.get")
    def test_spot_price_bad_status(self, mock_get):
        """HTTP 500 -> None."""
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_get.return_value = mock_resp

        result = get_btc_spot_price()
        assert result is None

    @patch("bot.binance_api.time.sleep")  # skip retry delay
    @patch("bot.binance_api.requests.get")
    def test_spot_price_missing_key(self, mock_get, mock_sleep):
        """Response missing 'price' key -> returns None after retries."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"symbol": "BTCUSDT"}  # no 'price'
        mock_get.return_value = mock_resp

        # get_btc_spot_price catches the KeyError internally and returns None
        result = get_btc_spot_price()
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# get_window_direction — settlement source for the martingale
# ═══════════════════════════════════════════════════════════════════════════════


class TestGetWindowDirection:
    WINDOW_TS = 1_785_600_000

    def _kline(self, ts, open_px, close_px, closed=True):
        open_ms = ts * 1000
        close_ms = open_ms + 300_000 - 1
        if not closed:
            close_ms = int(time.time() * 1000) + 60_000
        return [open_ms, str(open_px), "1", "1", str(close_px), "1", close_ms]

    def test_up_when_close_above_open(self):
        raw = [self._kline(self.WINDOW_TS, 62000.0, 62100.0)]
        with patch("bot.binance_api._get_klines", return_value=raw):
            assert get_window_direction(self.WINDOW_TS) == "UP"

    def test_down_when_close_below_open(self):
        raw = [self._kline(self.WINDOW_TS, 62000.0, 61900.0)]
        with patch("bot.binance_api._get_klines", return_value=raw):
            assert get_window_direction(self.WINDOW_TS) == "DOWN"

    def test_flat_candle_defers_to_gamma(self):
        """An exact tie is where our rule may not match Polymarket's."""
        raw = [self._kline(self.WINDOW_TS, 62000.0, 62000.0)]
        with patch("bot.binance_api._get_klines", return_value=raw):
            assert get_window_direction(self.WINDOW_TS) is None

    def test_near_flat_candle_defers_to_gamma(self):
        """Polymarket settles from Chainlink, so near-flat windows can split.

        Regression: an observed window moved $0.09 on $63,082 (0.00014%).
        Binance called it DOWN, Chainlink called it UP.
        """
        raw = [self._kline(self.WINDOW_TS, 63082.00, 63081.91)]
        with patch("bot.binance_api._get_klines", return_value=raw):
            assert get_window_direction(self.WINDOW_TS) is None

    def test_move_just_above_threshold_is_called(self):
        open_px = 63000.0
        close_px = open_px * (1 + NEAR_FLAT_THRESHOLD * 2)
        raw = [self._kline(self.WINDOW_TS, open_px, close_px)]
        with patch("bot.binance_api._get_klines", return_value=raw):
            assert get_window_direction(self.WINDOW_TS) == "UP"

    def test_threshold_is_the_calibrated_value(self):
        """Pin the measured optimum so a tweak has to be deliberate.

        5e-5 minimises over-sizing across 3.016 Gamma-labelled windows. Both
        directions cost real money: lower mis-settles more windows, higher
        defers so many that entries get sized with stale martingale state
        (docs/CHAINLINK_TWAP.md §11.1.b).
        """
        assert NEAR_FLAT_THRESHOLD == 5e-5

    def test_window_between_old_and_new_threshold_now_defers(self):
        """A 0.003% move: called under the old 1e-5, deferred under 5e-5.

        This band is where most of the mis-settlements lived — too flat for
        Binance to call reliably, but above the old cutoff.
        """
        open_px = 63000.0
        close_px = open_px * (1 + 3e-5)
        raw = [self._kline(self.WINDOW_TS, open_px, close_px)]
        with patch("bot.binance_api._get_klines", return_value=raw):
            assert get_window_direction(self.WINDOW_TS) is None

    def test_typical_window_is_called(self):
        """The median 5-min window moves ~0.031% — far above the threshold."""
        raw = [self._kline(self.WINDOW_TS, 63000.0, 63000.0 * 1.00031)]
        with patch("bot.binance_api._get_klines", return_value=raw):
            assert get_window_direction(self.WINDOW_TS) == "UP"

    def test_unclosed_candle_defers(self):
        raw = [self._kline(self.WINDOW_TS, 62000.0, 62100.0, closed=False)]
        with patch("bot.binance_api._get_klines", return_value=raw):
            assert get_window_direction(self.WINDOW_TS) is None

    def test_window_not_in_range_defers(self):
        raw = [self._kline(self.WINDOW_TS + 3000, 62000.0, 62100.0)]
        with patch("bot.binance_api._get_klines", return_value=raw):
            assert get_window_direction(self.WINDOW_TS) is None

    def test_picks_the_matching_candle(self):
        raw = [
            self._kline(self.WINDOW_TS - 300, 62000.0, 62500.0),
            self._kline(self.WINDOW_TS, 62500.0, 62100.0),
            self._kline(self.WINDOW_TS + 300, 62100.0, 62900.0),
        ]
        with patch("bot.binance_api._get_klines", return_value=raw):
            assert get_window_direction(self.WINDOW_TS) == "DOWN"

    def test_no_data_defers(self):
        with patch("bot.binance_api._get_klines", return_value=None):
            assert get_window_direction(self.WINDOW_TS) is None
