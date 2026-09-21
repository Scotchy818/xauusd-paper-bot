"""
Live candle feed (Dukascopy) — the same source the backtest used.

Using the identical data source matters: if the paper test ran on a different
feed, any difference in results could be the feed rather than the strategies,
and we would not be able to tell which.

Measured latency: a closed 1m bar appears ~1.5 min later, a 5m bar ~3.5 min.
`latest_closed()` only ever returns bars that have actually finished, so a
partially-formed bar can never be traded on.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

# Silence ONLY the dukascopy loggers. A global logging.disable(INFO) here —
# which is what fx_data.py does — also suppresses the paper trader's own logs,
# leaving an empty paper.log and no visibility into what the bot is doing.
for _n in ("dukascopy_python", "urllib3", "requests"):
    logging.getLogger(_n).setLevel(logging.WARNING)

import pandas as pd
import dukascopy_python
from dukascopy_python.instruments import (
    INSTRUMENT_FX_METALS_XAU_USD,
    INSTRUMENT_FX_MAJORS_EUR_USD,
    INSTRUMENT_FX_MAJORS_GBP_USD,
    INSTRUMENT_FX_MAJORS_USD_JPY,
)

INSTRUMENTS = {
    "XAUUSD": INSTRUMENT_FX_METALS_XAU_USD,
    "EURUSD": INSTRUMENT_FX_MAJORS_EUR_USD,
    "GBPUSD": INSTRUMENT_FX_MAJORS_GBP_USD,
    "USDJPY": INSTRUMENT_FX_MAJORS_USD_JPY,
}

INTERVALS = {
    "1m": (dukascopy_python.INTERVAL_MIN_1, 1),
    "5m": (dukascopy_python.INTERVAL_MIN_5, 5),
    "15m": (dukascopy_python.INTERVAL_MIN_15, 15),
    "30m": (dukascopy_python.INTERVAL_MIN_30, 30),
    "1h": (dukascopy_python.INTERVAL_HOUR_1, 60),
}


def fetch_recent(symbol: str, tf: str, bars: int = 1000) -> pd.DataFrame:
    """Fetch enough recent history to compute signals. Returns o/h/l/c/v + dt."""
    interval, minutes = INTERVALS[tf]
    # ask for generous extra span: weekends and holidays have no ticks
    span = timedelta(minutes=minutes * bars * 2.2)
    now = datetime.now(timezone.utc)
    df = dukascopy_python.fetch(
        INSTRUMENTS[symbol], interval, dukascopy_python.OFFER_SIDE_BID,
        now - span, now)
    df = df.reset_index().rename(columns={
        "timestamp": "dt", "open": "o", "high": "h",
        "low": "l", "close": "c", "volume": "v"})
    df = df[["dt", "o", "h", "l", "c", "v"]].dropna().reset_index(drop=True)
    df["dt"] = pd.to_datetime(df["dt"])
    try:
        df["dt"] = df["dt"].dt.tz_convert(None)
    except (TypeError, AttributeError):
        pass
    return df.tail(bars).reset_index(drop=True)


def latest_closed(df: pd.DataFrame, tf: str) -> pd.Series | None:
    """
    The most recent bar that has definitely finished.

    Dukascopy does not emit an in-progress bar, but we verify anyway rather
    than trusting that: trading a partial bar would use a 'close' that is
    really just the current price, which the backtest never did.
    """
    if df is None or df.empty:
        return None
    minutes = INTERVALS[tf][1]
    last = df.iloc[-1]
    close_time = pd.Timestamp(last["dt"]) + pd.Timedelta(minutes=minutes)
    now = pd.Timestamp(datetime.now(timezone.utc)).tz_localize(None)
    if close_time > now:
        return df.iloc[-2] if len(df) >= 2 else None
    return last


def closed_since(df: pd.DataFrame, tf: str, last_seen: str | None) -> list:
    """
    Every bar that has CLOSED since `last_seen`, oldest first.

    Processing only the newest bar silently drops the ones in between. That
    loses entry signals, but much worse, it means open positions never see
    the skipped bars' highs and lows — so a stop that should have triggered
    two bars ago goes unhonoured and the position keeps running against
    later, unrelated prices. Any scheduler delay longer than one bar
    corrupts the run.

    Returns a list of bar rows, so the caller can replay them in order.
    """
    if df is None or df.empty:
        return []
    from paper_live import config as _C
    minutes = INTERVALS[tf][1]
    now = pd.Timestamp(datetime.now(timezone.utc)).tz_localize(None)
    # Require the bar to be closed AND settled (see config.SETTLE_SECONDS).
    cutoff = now - pd.Timedelta(seconds=getattr(_C, "SETTLE_SECONDS", 0))

    closed = df[pd.to_datetime(df["dt"]) + pd.Timedelta(minutes=minutes) <= cutoff]
    if closed.empty:
        return []
    if last_seen:
        closed = closed[pd.to_datetime(closed["dt"]) > pd.Timestamp(last_seen)]
    return [closed.iloc[i] for i in range(len(closed))]


def spot(symbol: str) -> float | None:
    """Current price, from the newest 1m bar. Used as the live fill price."""
    try:
        df = fetch_recent(symbol, "1m", bars=5)
        if df.empty:
            return None
        return float(df["c"].iloc[-1])
    except Exception:
        return None
