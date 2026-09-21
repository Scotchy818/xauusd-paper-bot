"""
Exit management engine — the gap in the megatest.

Every config tested so far used ONE exit style: fixed stop, fixed target,
whichever hits first. That is a real limitation, and it rules out by
construction the thing most discretionary traders actually do:

  - move the stop to breakeven once the trade is up
  - trail the stop behind price so winners can run
  - hold with no fixed target at all and let the trend decide

These change the SHAPE of the return distribution, not just its scale.
A fixed 1:3 bracket can only ever return -1R or +3R. A trailing exit can
return -1R, +0.4R, +7R. R:R tuning cannot reach those outcomes.

Exit styles implemented
-----------------------
fixed        : stop + fixed target (the original — baseline for comparison)
be_1r        : fixed target, but stop moves to entry once price reaches +1R
be_half      : same, triggered at +0.5R
trail_pct    : NO fixed target; stop trails `trail_k * stop` behind the best
               price reached. Winners run until the trend breaks.
be_then_trail: stop to breakeven at +1R, then trail behind the best price
runner       : half the position exits at the fixed target, the rest trails

All of them use intrabar high/low, and all of them resolve the stop BEFORE
the target within a bar (the pessimistic assumption) so we never flatter a
result by assuming a favourable intrabar path.
"""

from __future__ import annotations

import numpy as np

EXIT_STYLES = ["fixed", "be_1r", "be_half", "trail_pct", "be_then_trail", "runner"]


def simulate_exits(df, sig, stop, target, max_walk, fee, style="fixed", trail_k=1.0,
                   with_entries=False):
    """
    Returns (rets, holds, n_target, n_stop), or with `with_entries=True`,
    (rets, holds, n_target, n_stop, entry_idx) where entry_idx[k] is the bar
    index the k-th trade was opened on. The rotation test needs entry times so
    it can slice trades by calendar month without re-simulating.

    `stop` and `target` are fractions of entry price.
    `trail_k` scales the trailing distance in units of `stop`.
    Trades never overlap: a new signal is ignored until the previous exits.
    """
    h = df["h"].values
    l = df["l"].values
    c = df["c"].values
    s = sig.values
    n = len(df)

    idx = np.where(s != 0)[0]
    idx = idx[idx < n - 1]

    rets, holds, entries = [], [], []
    n_target = n_stop = 0
    last_exit = -1

    for i in idx:
        if i <= last_exit:
            continue

        d = s[i]
        entry = c[i]
        # absolute price levels
        stop_px = entry * (1 - stop) if d > 0 else entry * (1 + stop)
        tgt_px = entry * (1 + target) if d > 0 else entry * (1 - target)
        be_px = entry
        r1_px = entry * (1 + stop) if d > 0 else entry * (1 - stop)
        rhalf_px = entry * (1 + stop * 0.5) if d > 0 else entry * (1 - stop * 0.5)

        trail_dist = stop * trail_k
        best = entry                      # best price reached in our favour
        moved_be = False
        armed_trail = (style == "trail_pct")
        half_banked = False
        banked = 0.0

        end = min(n, i + 1 + max_walk)
        exit_ret = None
        exit_kind = None
        j = i

        for j in range(i + 1, end):
            hi, lo = h[j], l[j]

            # ---- 1. stop check FIRST (pessimistic intrabar assumption) ----
            if d > 0 and lo <= stop_px:
                exit_ret = (stop_px - entry) / entry
                exit_kind = "stop"
                break
            if d < 0 and hi >= stop_px:
                exit_ret = (entry - stop_px) / entry
                exit_kind = "stop"
                break

            # ---- 2. fixed target (styles that still have one) ----
            if style in ("fixed", "be_1r", "be_half"):
                if d > 0 and hi >= tgt_px:
                    exit_ret = target; exit_kind = "target"; break
                if d < 0 and lo <= tgt_px:
                    exit_ret = target; exit_kind = "target"; break

            if style == "runner" and not half_banked:
                if (d > 0 and hi >= tgt_px) or (d < 0 and lo <= tgt_px):
                    banked = target * 0.5      # half off at the target
                    half_banked = True
                    armed_trail = True
                    # remaining half now trails; protect it at breakeven
                    stop_px = be_px
                    moved_be = True

            # ---- 3. breakeven promotion ----
            if not moved_be and style in ("be_1r", "be_then_trail"):
                if (d > 0 and hi >= r1_px) or (d < 0 and lo <= r1_px):
                    stop_px = be_px
                    moved_be = True
                    if style == "be_then_trail":
                        armed_trail = True
            if not moved_be and style == "be_half":
                if (d > 0 and hi >= rhalf_px) or (d < 0 and lo <= rhalf_px):
                    stop_px = be_px
                    moved_be = True

            # ---- 4. trailing ----
            if armed_trail:
                if d > 0:
                    best = max(best, hi)
                    new_stop = best * (1 - trail_dist)
                    if new_stop > stop_px:
                        stop_px = new_stop
                else:
                    best = min(best, lo)
                    new_stop = best * (1 + trail_dist)
                    if new_stop < stop_px:
                        stop_px = new_stop

        # ---- resolve ----
        if exit_ret is None:
            continue          # unresolved inside the window; dropped, as before

        # `runner` banks half the position at the fixed target, so only HALF
        # the remaining price move accrues to us. Adding the full move on top
        # of the banked half double-counts every winner.
        size_remaining = 0.5 if half_banked else 1.0
        total = exit_ret * size_remaining + banked - fee
        rets.append(total)
        holds.append(j - i)
        entries.append(i)
        if total > 0:
            n_target += 1
        else:
            n_stop += 1
        last_exit = j

    if with_entries:
        return rets, holds, n_target, n_stop, entries
    return rets, holds, n_target, n_stop


def equity_and_dd(rets, risk_pct, stop_pct):
    """Position sized so a full stop loses `risk_pct` of equity."""
    eq = 1.0
    peak = 1.0
    dd = 0.0
    mult = risk_pct / stop_pct
    for r in rets:
        eq *= 1 + r * mult
        if eq <= 0:
            return 0.0, 1.0
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak)
    return eq, dd
