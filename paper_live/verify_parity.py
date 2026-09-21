"""
Parity check: live state machine vs the validated backtest engine.

This runs in CI before every trading cycle. If the live engine ever stops
reproducing exit_engine.simulate_exits() exactly, the paper results stop
meaning anything — so the run fails rather than quietly trading with a
changed engine.

It found a real bug during development: the live loop allowed a strategy to
close and re-open on the same bar, which the backtest forbids
(`if i <= last_exit: continue`). That produced ~10% more trades than the
system we actually validated.

    python -m paper_live.verify_parity
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exit_engine import simulate_exits
from strategies import STRATEGIES
from paper_live import config as C
from paper_live import feed
from paper_live.engine import open_position, update

TOL = 1e-9


def replay(df, sig, stop, rr, tk, style, max_walk):
    """Bar-by-bar through the LIVE state machine, mirroring run.py's loop."""
    h = df["h"].values
    l = df["l"].values
    c = df["c"].values
    s = sig.values
    n = len(df)
    out = []
    pos = None
    closed_bar = -1
    for i in range(n - 1):
        if pos is not None:
            reason, px, gross = update(pos, h[i], l[i], max_walk)
            if reason is not None:
                out.append(gross)
                pos = None
                closed_bar = i
        # no re-entry on the bar a position just exited (matches backtest)
        if pos is None and s[i] != 0 and i > closed_bar:
            pos = open_position("t", int(s[i]), str(i), c[i], c[i],
                                1.0, 0.02, stop, rr, tk, style)
    return out


def main() -> int:
    print(f"parity check — {len(C.STRATEGIES)} live strategies\n")
    print(f"  {'strategy':<14}{'tf':>4}  {'exit':<15}"
          f"{'backtest':>9}{'live':>7}{'maxdiff':>12}  result")
    print("  " + "-" * 74)

    cache: dict[str, object] = {}
    all_ok = True

    for (name, tf, fam, style, stop, rr, tk) in C.STRATEGIES:
        if tf not in cache:
            cache[tf] = feed.fetch_recent(C.SYMBOL, tf, C.HISTORY_BARS)
        df = cache[tf]
        fn = STRATEGIES.get(fam)
        if fn is None:
            print(f"  {fam:<14}{tf:>4}  MISSING STRATEGY")
            all_ok = False
            continue

        sig = fn(df).fillna(0).astype(int)
        target = stop * rr if rr > 0 else stop * 99
        bt, _, _, _ = simulate_exits(df, sig, stop, target, C.MAX_HOLD_BARS,
                                     0.0, style=style,
                                     trail_k=tk if tk > 0 else 1.0)
        lv = replay(df, sig, stop, rr, tk if tk > 0 else 1.0, style,
                    C.MAX_HOLD_BARS)

        if len(bt) == len(lv):
            diff = max((abs(a - b) for a, b in zip(bt, lv)), default=0.0)
            ok = diff < TOL
        else:
            diff = float("nan")
            ok = False
        all_ok &= ok
        print(f"  {fam:<14}{tf:>4}  {style:<15}{len(bt):>9}{len(lv):>7}"
              f"{diff:>12.2e}  {'MATCH' if ok else 'MISMATCH'}")

    print()
    if all_ok:
        print("PARITY OK — live engine reproduces the backtest exactly.")
        return 0
    print("PARITY FAILED — refusing to trade with a changed engine.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
