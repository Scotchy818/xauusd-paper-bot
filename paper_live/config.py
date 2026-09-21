"""
Live paper-trading configuration.

STRATEGY SELECTION
------------------
Measured feed latency (Dukascopy): a closed 1m bar is available ~1.5 minutes
later, a 5m bar ~3.5 minutes. That is effectively real time, so both 1h and
15m strategies are tradeable. We poll every 60s and act on a bar within about
two minutes of its close; the gap between the bar-close price the backtest
assumed and the price we actually get is recorded per trade as slippage_bp so
its real cost is measured rather than assumed.

These are the survivors from strategy_report.py, which ranked on the FULL
period rather than the recent window:

  1h
    donchian20  be_then_trail   full 7.22x  ·  5M 2.35x   <- strong in both
    orb24       be_then_trail   full 7.00x  ·  5M 2.51x   <- strong in both
    orb12       be_then_trail   full 6.50x  ·  5M 2.15x
    macd        runner          full 4.55x  ·  5M 2.11x
    ema_12_26   be_then_trail   full 3.91x  ·  5M 1.11x   <- lowest drawdown

  15m
    bb_breakout be_then_trail   full 7.27x  ·  5M 0.97x   <- best record,
                                                             currently losing
    donchian50  runner          full 6.24x  ·  5M 3.22x
    keltner     runner          full 6.01x  ·  5M 1.43x

bb_breakout is included deliberately despite being down over 5 months: it has
the strongest full-period record, and running it is how we find out whether
the recent weakness is noise or decay. Excluding it would only guarantee we
never learn that.

RISK
----
Each strategy risks RISK_PCT of the TOTAL account per trade and holds at most
one position. With five strategies that is at most 5% of the account at risk
simultaneously. The backtest sized each config at 2% of its own capital slice;
1% of the whole account is close to that and is easier to reason about.

Nothing here can place a real order. There are no broker credentials and no
order-placing code anywhere in this package.
"""

from __future__ import annotations

SYMBOL = "XAUUSD"
TIMEFRAMES = ["1h", "15m"]

START_EQUITY = 10_000.0
RISK_PCT = 0.01            # 1% of total account per trade, per strategy
                           # 8 strategies x 1 position each => at most 8% of
                           # the account at risk simultaneously. Lower this
                           # first if the swings feel too big.

# Round-trip cost applied to every trade, as a fraction of notional.
# 0.02% matches what the backtest charged for XAUUSD.
COST = 0.00020

# How often to check for a newly closed bar (seconds).
# 60s so a 15m bar is picked up within ~2 min of closing, given the ~1.5 min
# feed latency measured above.
POLL_SECONDS = 60

# Wait this long after a bar's close before acting on it, to let the feed
# finalise. A spot check found a 15m bar already final 14s after close, but
# one sample does not prove it always is — and acting on a provisional high
# or low would trigger stops at prices that never really traded. 30s is well
# inside the 60s poll interval, so it costs no meaningful responsiveness.
SETTLE_SECONDS = 30

# Most bars a single cycle will replay. A cold start (no database) or a long
# outage must NOT replay the entire fetched history: that would open trades on
# ancient bars and, before this cap existed, made the first run hang for
# minutes doing a network call per entry.
MAX_CATCHUP_BARS = 50

# Bars of history kept for signal computation. Must comfortably exceed the
# longest lookback any strategy uses (premium/discount uses 100).
HISTORY_BARS = 1000

# Safety valve: if a position somehow stays open this long, close it at market
# and flag it. The backtest let trades run to resolution, so this should never
# fire in normal operation - it exists to catch stuck state, not to manage risk.
MAX_HOLD_BARS = 1000


# (name, timeframe, strategy_family, exit_style, stop_pct, rr, trail_k)
STRATEGIES = [
    ("donchian20_1h",  "1h",  "donchian20", "be_then_trail", 0.0035, 0.0, 0.5),
    ("orb24_1h",       "1h",  "orb24",      "be_then_trail", 0.0035, 0.0, 0.5),
    ("orb12_1h",       "1h",  "orb12",      "be_then_trail", 0.0035, 0.0, 0.5),
    ("macd_1h_run",    "1h",  "macd",       "runner",        0.0050, 3.0, 1.0),
    ("ema1226_1h",     "1h",  "ema_12_26",  "be_then_trail", 0.0035, 0.0, 0.5),
    ("bbbreak_15m",    "15m", "bb_breakout", "be_then_trail", 0.0035, 0.0, 0.5),
    ("donchian50_15m", "15m", "donchian50", "runner",        0.0035, 3.0, 0.5),
    ("keltner_15m",    "15m", "keltner",    "runner",        0.0050, 2.0, 0.5),
]

DB_PATH = "paper_live/paper.db"
LOG_PATH = "paper_live/paper.log"
