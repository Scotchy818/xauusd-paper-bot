"""
Paper trading loop — XAUUSD, 8 strategies, no real money.

    python -m paper_live.run              run the loop
    python -m paper_live.run --once       one cycle then exit (for testing)
    python -m paper_live.run --status     print current state and exit

What it does each cycle, per timeframe:

  1. Pull recent candles.
  2. If a new bar has closed since last cycle:
       a. advance every OPEN position through that bar's high/low, using the
          exact state machine from the backtest (stop before target, breakeven
          promotion, trailing, runner half-bank)
       b. evaluate every strategy's signal on the closed bar and open a
          position if it fires and that strategy is flat
  3. Persist everything, notify Telegram.

Positions survive restarts — they live in SQLite, not memory. The earlier BTC
bot orphaned positions on restart and that is how it lost track of trades.

There is no broker connection and no order-placing code in this package. It
cannot touch money.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from strategies import STRATEGIES as FAMILIES
from paper_live import config as C
from paper_live import feed, notify
from paper_live.engine import Position, open_position, update
from paper_live.store import Store


def setup_log():
    Path(C.LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(
        level=logging.INFO, format=fmt,
        handlers=[logging.FileHandler(C.LOG_PATH),
                  logging.StreamHandler(sys.stdout)])
    return logging.getLogger("paper")


log = setup_log()


def signal_for(family: str, df: pd.DataFrame) -> int:
    """Signal on the last row of df. Same computation as the backtest."""
    fn = FAMILIES.get(family)
    if fn is None:
        return 0
    try:
        s = fn(df).fillna(0).astype(int)
        return int(s.iloc[-1])
    except Exception as e:
        log.warning("signal %s failed: %s", family, e)
        return 0


def cycle(store: Store, state: dict) -> None:
    equity = store.equity(C.START_EQUITY)
    positions = {k: Position.from_row(v) for k, v in store.open_positions().items()}

    for tf in C.TIMEFRAMES:
        try:
            df = feed.fetch_recent(C.SYMBOL, tf, C.HISTORY_BARS)
        except Exception as e:
            log.warning("feed %s failed: %s", tf, e)
            continue
        bar = feed.latest_closed(df, tf)
        if bar is None:
            continue

        bar_key = f"last_bar_{tf}"
        seen = state.get(bar_key) or store.get_meta(bar_key)
        bar_time = str(bar["dt"])
        if seen == bar_time:
            continue                      # nothing new on this timeframe
        state[bar_key] = bar_time
        store.set_meta(bar_key, bar_time)
        log.info("new %s bar %s  o=%.2f h=%.2f l=%.2f c=%.2f",
                 tf, bar_time, bar["o"], bar["h"], bar["l"], bar["c"])

        specs = [s for s in C.STRATEGIES if s[1] == tf]
        # A strategy that exits on this bar may NOT re-enter on the same bar.
        # The backtest enforced this (`if i <= last_exit: continue`) because
        # neither it nor we can know whether the exit happened before or after
        # the signal within the bar. Allowing same-bar re-entry made the live
        # engine produce ~10% more trades than the backtest it is meant to
        # reproduce.
        closed_this_bar: set[str] = set()

        # ---- 1. advance open positions through this bar ----------------
        for (name, _tf, fam, style, stop, rr, tk) in specs:
            pos = positions.get(name)
            if pos is None:
                continue
            reason, px, gross = update(pos, float(bar["h"]), float(bar["l"]),
                                       C.MAX_HOLD_BARS)
            if reason is None:
                store.upsert_position(pos.to_row())
                continue

            net = gross - C.COST
            pnl = net * pos.size
            equity += pnl
            slip = ((pos.entry_price - pos.signal_price) / pos.signal_price
                    * 10000 * pos.direction)
            store.record_trade({
                "strategy": name, "direction": pos.direction,
                "entry_time": pos.entry_time, "exit_time": bar_time,
                "signal_price": pos.signal_price, "entry_price": pos.entry_price,
                "exit_price": float(px), "slippage_bp": float(slip),
                "bars_held": pos.bars_held, "exit_reason": reason,
                "gross_ret": float(gross), "net_ret": float(net),
                "pnl": float(pnl), "equity_after": float(equity),
            })
            store.delete_position(name)
            positions.pop(name, None)
            closed_this_bar.add(name)
            log.info("CLOSE %s %s @%.2f  %s  net %+.3f%%  pnl %+.2f  eq %.2f",
                     name, "LONG" if pos.direction > 0 else "SHORT", px,
                     reason, net * 100, pnl, equity)
            notify.send(notify.fmt_close(
                name, pos.direction, pos.entry_price, float(px), reason,
                net, pnl, equity, pos.bars_held), log)

        # ---- 2. look for new entries ------------------------------------
        for (name, _tf, fam, style, stop, rr, tk) in specs:
            if name in positions or name in closed_this_bar:
                continue                  # one position per strategy, and no
                                          # re-entry on the bar it just exited
            d = signal_for(fam, df)
            if d == 0:
                continue
            fill = feed.spot(C.SYMBOL) or float(bar["c"])
            pos = open_position(name, d, bar_time, float(bar["c"]), fill,
                                equity, C.RISK_PCT, stop, rr, tk, style)
            positions[name] = pos
            store.upsert_position(pos.to_row())
            log.info("OPEN  %s %s @%.2f (signal %.2f) stop %.2f size %.0f",
                     name, "LONG" if d > 0 else "SHORT", fill,
                     float(bar["c"]), pos.stop_px, pos.size)
            notify.send(notify.fmt_open(
                name, d, fill, float(bar["c"]), pos.stop_px, pos.size,
                equity), log)


def print_status(store: Store):
    eq = store.equity(C.START_EQUITY)
    n = store.trade_count()
    pos = store.open_positions()
    ret = (eq / C.START_EQUITY - 1) * 100
    print(f"\n  PAPER ACCOUNT — {C.SYMBOL}")
    print(f"  equity   : {eq:,.2f}   ({ret:+.2f}% from {C.START_EQUITY:,.0f})")
    print(f"  trades   : {n}")
    print(f"  open     : {len(pos)}/{len(C.STRATEGIES)}")
    for k, p in pos.items():
        side = "LONG" if p["direction"] > 0 else "SHORT"
        print(f"     {k:<16}{side:<6} entry {p['entry_price']:.2f}  "
              f"stop {p['stop_px']:.2f}  held {p['bars_held']} bars"
              + ("  [BE]" if p["moved_be"] else ""))
    if n:
        print(f"\n  last 10 trades:")
        for t in store.trades(limit=10):
            print(f"     {t['exit_time'][:16]}  {t['strategy']:<16}"
                  f"{'L' if t['direction']>0 else 'S'}  {t['exit_reason']:<10}"
                  f"{t['net_ret']*100:>+7.2f}%  {t['pnl']:>+9.2f}  "
                  f"eq {t['equity_after']:,.2f}")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="one cycle then exit")
    ap.add_argument("--status", action="store_true", help="print state and exit")
    args = ap.parse_args()

    store = Store(C.DB_PATH)

    if args.status:
        print_status(store)
        return

    log.info("paper trader starting — %s, %d strategies, equity %.2f",
             C.SYMBOL, len(C.STRATEGIES), store.equity(C.START_EQUITY))
    if notify.enabled():
        notify.send(
            f"📄 <b>Paper trader started</b>\n{C.SYMBOL} · "
            f"{len(C.STRATEGIES)} strategies\n"
            f"equity {store.equity(C.START_EQUITY):,.2f} · "
            f"risk {C.RISK_PCT*100:.1f}%/trade\n"
            f"<i>paper only — no broker connected</i>", log)
    else:
        log.warning("telegram not configured — running without notifications")

    state: dict = {}
    if args.once:
        cycle(store, state)
        print_status(store)
        return

    while True:
        try:
            cycle(store, state)
        except KeyboardInterrupt:
            log.info("stopped by user")
            break
        except Exception as e:                  # never die on a transient error
            log.exception("cycle error: %s", e)
        time.sleep(C.POLL_SECONDS)


if __name__ == "__main__":
    main()
