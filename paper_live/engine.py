"""
Live position state machine.

This MUST behave identically to exit_engine.simulate_exits(), otherwise the
paper test measures something other than what the backtest validated. The
rules, copied deliberately rather than approximated:

  * One position per strategy. A new signal is ignored while one is open.
  * Entry on the close of the signal bar (live: the first price we can get
    after that bar closes — the difference is recorded as slippage, not
    hidden).
  * On each subsequent bar, the STOP is checked BEFORE the target. If a bar's
    range covers both, the stop wins. That is the pessimistic assumption and
    it is what the backtest assumed.
  * be_then_trail : no fixed target. At +1R the stop moves to breakeven and
                    trailing arms. Trail distance = stop_pct * trail_k behind
                    the best price seen.
  * runner        : fixed target at stop*rr. When hit, half the position is
                    banked at the target, the stop moves to breakeven, and the
                    remaining half trails. Final return is
                    exit_ret * 0.5 + banked  (the half-size scaling is the bug
                    that made the first backtest show 127x).

Nothing in this module can place a real order.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass
class Position:
    strategy: str
    direction: int          # +1 long, -1 short
    entry_time: str
    signal_price: float     # close of the signal bar (what the backtest assumes)
    entry_price: float      # what we actually got
    size: float             # notional units
    stop_px: float
    target_px: float | None
    best_px: float
    moved_be: int
    armed_trail: int
    half_banked: int
    banked_ret: float
    bars_held: int
    bars_late: int
    stop_pct: float
    trail_k: float
    style: str

    def to_row(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_row(r: dict) -> "Position":
        return Position(**{k: r[k] for k in Position.__dataclass_fields__})


def open_position(name, direction, bar_time, signal_price, fill_price,
                  equity, risk_pct, stop_pct, rr, trail_k, style) -> Position:
    """Size so that a full stop costs exactly risk_pct of equity."""
    if direction > 0:
        stop_px = fill_price * (1 - stop_pct)
        target_px = fill_price * (1 + stop_pct * rr) if rr > 0 else None
    else:
        stop_px = fill_price * (1 + stop_pct)
        target_px = fill_price * (1 - stop_pct * rr) if rr > 0 else None

    notional = (equity * risk_pct) / stop_pct
    return Position(
        strategy=name, direction=direction, entry_time=str(bar_time),
        signal_price=float(signal_price), entry_price=float(fill_price),
        size=float(notional), stop_px=float(stop_px),
        target_px=float(target_px) if target_px else None,
        best_px=float(fill_price), moved_be=0,
        armed_trail=1 if style == "trail_pct" else 0,
        half_banked=0, banked_ret=0.0, bars_held=0, bars_late=0,
        stop_pct=float(stop_pct), trail_k=float(trail_k), style=style,
    )


def update(pos: Position, high: float, low: float, max_hold: int):
    """
    Advance one bar.

    Returns (exit_reason, exit_price, gross_return) or (None, None, None)
    if the position is still open. gross_return is the fraction of notional,
    already including any banked half for a runner, before costs.
    """
    pos.bars_held += 1
    d = pos.direction
    entry = pos.entry_price
    trail_dist = pos.stop_pct * pos.trail_k

    # ---- 1. stop first (pessimistic within-bar assumption) ----------
    hit_stop = (d > 0 and low <= pos.stop_px) or (d < 0 and high >= pos.stop_px)
    if hit_stop:
        px = pos.stop_px
        raw = (px - entry) / entry if d > 0 else (entry - px) / entry
        size_rem = 0.5 if pos.half_banked else 1.0
        reason = "stop" if raw < 0 else ("trail" if pos.armed_trail else "breakeven")
        return reason, px, raw * size_rem + pos.banked_ret

    # ---- 2. fixed target ---------------------------------------------
    if pos.style in ("fixed", "be_1r", "be_half") and pos.target_px is not None:
        if (d > 0 and high >= pos.target_px) or (d < 0 and low <= pos.target_px):
            raw = pos.stop_pct * (abs(pos.target_px - entry) / (entry * pos.stop_pct))
            raw = (pos.target_px - entry) / entry if d > 0 else (entry - pos.target_px) / entry
            return "target", pos.target_px, raw

    if pos.style == "runner" and not pos.half_banked and pos.target_px is not None:
        if (d > 0 and high >= pos.target_px) or (d < 0 and low <= pos.target_px):
            tgt_ret = ((pos.target_px - entry) / entry if d > 0
                       else (entry - pos.target_px) / entry)
            pos.banked_ret = tgt_ret * 0.5     # half off at the target
            pos.half_banked = 1
            pos.armed_trail = 1
            pos.stop_px = entry                # protect the runner
            pos.moved_be = 1

    # ---- 3. breakeven promotion --------------------------------------
    if not pos.moved_be and pos.style in ("be_1r", "be_then_trail"):
        r1 = entry * (1 + pos.stop_pct) if d > 0 else entry * (1 - pos.stop_pct)
        if (d > 0 and high >= r1) or (d < 0 and low <= r1):
            pos.stop_px = entry
            pos.moved_be = 1
            if pos.style == "be_then_trail":
                pos.armed_trail = 1
    if not pos.moved_be and pos.style == "be_half":
        rh = (entry * (1 + pos.stop_pct * 0.5) if d > 0
              else entry * (1 - pos.stop_pct * 0.5))
        if (d > 0 and high >= rh) or (d < 0 and low <= rh):
            pos.stop_px = entry
            pos.moved_be = 1

    # ---- 4. trailing ---------------------------------------------------
    if pos.armed_trail:
        if d > 0:
            pos.best_px = max(pos.best_px, high)
            ns = pos.best_px * (1 - trail_dist)
            if ns > pos.stop_px:
                pos.stop_px = ns
        else:
            pos.best_px = min(pos.best_px, low)
            ns = pos.best_px * (1 + trail_dist)
            if ns < pos.stop_px:
                pos.stop_px = ns

    # ---- 5. stuck-state valve -------------------------------------------
    if pos.bars_held >= max_hold:
        px = pos.best_px
        raw = (px - entry) / entry if d > 0 else (entry - px) / entry
        size_rem = 0.5 if pos.half_banked else 1.0
        return "max_hold", px, raw * size_rem + pos.banked_ret

    return None, None, None
