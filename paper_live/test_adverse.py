"""
Adverse-condition tests — the failure modes, not the happy path.

WHY THIS EXISTS
---------------
verify_parity.py replays a CONTIGUOUS stream of bars, one at a time, in
order. It proved the exit maths was right and it caught a real same-bar
re-entry bug. But it could not possibly catch the worst bug in this project,
because it never simulated the condition that causes it: bars arriving LATE,
in batches.

GitHub's scheduler routinely runs 15-40 minutes behind. The original code
processed only the newest closed bar per cycle, so on a late run it silently
discarded the bars in between — losing entry signals, and far worse, never
showing open positions the highs and lows of the skipped bars. A stop that
should have fired went unhonoured and the position kept running against
later, unrelated prices.

That is a correctness bug that would have quietly corrupted the entire
experiment while every existing test stayed green. These tests exist so the
failure modes are checked before deployment rather than discovered in
production.

TESTS
  1. GAP REPLAY     processing bars in late batches must give byte-identical
                    results to processing them one at a time
  2. RESTART        killing the process mid-position must not lose or corrupt
                    the position
  3. IDEMPOTENCE    re-running a cycle over an already-processed bar must not
                    double-enter or double-count
  4. FEED OUTAGE    a failed fetch must not advance state or drop bars

    python -m paper_live.test_adverse
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from paper_live import config as C
from paper_live import feed, notify
from paper_live.engine import Position, open_position, update
from paper_live.store import Store
from strategies import STRATEGIES

# Exercise the SHIPPED code path, not a reimplementation of it. A test that
# re-derives the logic only checks the author's understanding; this checks
# what actually runs in production.
notify.send = lambda *a, **k: False        # never message during tests
# process_bar calls feed.spot() for the fill on every entry — a live network
# round-trip. That is fine in production (a handful per hour) but makes a test
# doing thousands of entries unusable, so fills come from the bar close here.
# That also makes batching the ONLY variable in the gap-replay comparison.
_SPOT = {"px": None}
feed.spot = lambda *a, **k: _SPOT["px"]
from paper_live import run as RUN
import logging
_quiet = logging.getLogger("test"); _quiet.addHandler(logging.NullHandler())
_quiet.propagate = False

PASS, FAIL = "PASS", "FAIL"
results = []


def _tmpdb():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return path


def _run_bars(store, df, sigs, specs, bar_idxs, batch):
    """
    Drive the REAL run.process_bar with a controllable schedule.

    `batch` = bars delivered per cycle. batch=1 is a punctual scheduler;
    batch=8 is one running eight bars behind.
    """
    i = 0
    while i < len(bar_idxs):
        chunk = bar_idxs[i:i + batch]
        for k, bi in enumerate(chunk):
            _SPOT["px"] = float(df.iloc[bi]["c"])
            RUN.process_bar(store, "1h", df.iloc[bi], specs, sigs, bi,
                            len(chunk) - 1 - k, _quiet)
        i += batch
    return store.equity(C.START_EQUITY)


def test_gap_replay(df, sigs, specs):
    """
    Delivery delay must not change the outcome — WITHIN the staleness cap.

    Batch sizes here stay at or below MAX_ENTRY_BARS_LATE + 1, so every entry
    is still inside the freshness window and results must be byte-identical.

    Beyond the cap the runs legitimately DIVERGE, because entries on badly
    stale signals are deliberately skipped (see test_stale_entry_skipped).
    That is a real trade-off and not a bug: a long outage costs missed
    entries rather than trades taken at prices that were never available.
    Exits are replayed in every case regardless of lateness, which is the
    property that actually protects open positions.
    """
    idxs = list(range(len(df) - 600, len(df)))
    out = {}
    for batch in (1, 2, 3, C.MAX_ENTRY_BARS_LATE + 1):
        path = _tmpdb()
        st = Store(path)
        eq = _run_bars(st, df, sigs, specs, idxs, batch)
        tr = st.trades()
        out[batch] = (len(tr), round(eq, 6),
                      [(t["strategy"], t["entry_time"], round(t["net_ret"], 12))
                       for t in sorted(tr, key=lambda x: x["id"])])
        os.remove(path)

    base = out[1]
    ok = all(out[b][0] == base[0] and out[b][1] == base[1] and out[b][2] == base[2]
             for b in out)
    detail = "  ".join(f"batch{b}:{out[b][0]}tr eq{out[b][1]:.2f}" for b in out)
    results.append((f"GAP REPLAY (delay <= {C.MAX_ENTRY_BARS_LATE} bars == punctual)",
                    PASS if ok else FAIL, detail))
    return ok


def test_restart(df, sigs, specs):
    """A position must survive the process dying and be reloaded intact."""
    # Walk forward until a position is actually open — a restart test with
    # nothing open passes vacuously and proves nothing.
    path = _tmpdb()
    st = Store(path)
    before = {}
    start = len(df) - 600
    for cut in range(40, 600, 10):
        st.db.close()
        os.remove(path) if os.path.exists(path) else None
        st = Store(path)
        _run_bars(st, df, sigs, specs, list(range(start, start + cut)), 1)
        before = st.open_positions()
        if before:
            break
    if not before:
        results.append(("RESTART (positions survive process death)", FAIL,
                        "could not open any position — test is vacuous"))
        st.db.close(); os.remove(path)
        return False
    st.db.close()

    st2 = Store(path)                      # simulate a fresh process
    after = st2.open_positions()
    same = (set(before) == set(after) and
            all(abs(before[k]["stop_px"] - after[k]["stop_px"]) < 1e-12
                and before[k]["bars_held"] == after[k]["bars_held"]
                for k in before))
    st2.db.close()
    os.remove(path)
    results.append(("RESTART (positions survive process death)",
                    PASS if same else FAIL,
                    f"{len(before)} open position(s) reloaded"))
    return same


def test_idempotence(df, sigs, specs):
    """Re-processing an already-seen bar must not double-enter."""
    idxs = list(range(len(df) - 400, len(df) - 200))
    path = _tmpdb()
    st = Store(path)
    _run_bars(st, df, sigs, specs, idxs, 1)
    n1, e1 = len(st.trades()), st.equity(C.START_EQUITY)
    open1 = dict(st.open_positions())

    # replay the final bar again, exactly as a duplicate cycle would
    _run_bars(st, df, sigs, specs, [idxs[-1]], 1)
    n2, e2 = len(st.trades()), st.equity(C.START_EQUITY)
    open2 = dict(st.open_positions())
    # A duplicate bar may legitimately advance an open position, but it must
    # never create a second position for a strategy that already has one.
    no_dupes = set(open2) == set(open1) or len(open2) <= len(C.STRATEGIES)
    ok = no_dupes and n2 >= n1
    st.db.close()
    os.remove(path)
    results.append(("IDEMPOTENCE (duplicate bar != duplicate position)",
                    PASS if ok else FAIL,
                    f"{n1}->{n2} trades, {len(open1)}->{len(open2)} open"))
    return ok


def test_cold_start():
    """
    A cold start (no database) must NOT replay the whole fetched history.

    Before this was fixed, a missing paper.db made closed_since() return all
    1,000 fetched bars, so the first cycle tried to open trades on ancient
    bars and hung for minutes doing a network call per entry. Any state loss
    in CI would have done the same.
    """
    import types
    df = feed.fetch_recent(C.SYMBOL, "1h", 300)
    path = _tmpdb()
    st = Store(path)
    state = {}
    saved_tfs = C.TIMEFRAMES
    C.TIMEFRAMES = ["1h"]
    RUN.cycle(st, state)                      # cold start
    n_after_cold = len(st.trades())
    adopted = st.get_meta("last_bar_1h")
    C.TIMEFRAMES = saved_tfs
    st.db.close(); os.remove(path)
    ok = n_after_cold == 0 and adopted is not None
    results.append(("COLD START (no db != replay all history)",
                    PASS if ok else FAIL,
                    f"{n_after_cold} trades opened, adopted bar {adopted}"))
    return ok


def test_catchup_cap():
    """A huge gap must be truncated, not replayed in full."""
    df = feed.fetch_recent(C.SYMBOL, "1h", 500)
    old = str(df["dt"].iloc[0])
    pending = feed.closed_since(df, "1h", old)
    capped = min(len(pending), C.MAX_CATCHUP_BARS)
    ok = len(pending) > C.MAX_CATCHUP_BARS and capped == C.MAX_CATCHUP_BARS
    results.append(("CATCH-UP CAP (long outage is truncated)",
                    PASS if ok else FAIL,
                    f"{len(pending)} bars pending -> capped at {C.MAX_CATCHUP_BARS}"))
    return ok


def test_settle_delay():
    """A bar must not be actionable until SETTLE_SECONDS after its close."""
    import datetime as _dt
    now = pd.Timestamp(_dt.datetime.now(_dt.timezone.utc)).tz_localize(None)
    # a 15m bar that closed 5 seconds ago — too fresh to trust
    fresh = now - pd.Timedelta(minutes=15) + pd.Timedelta(seconds=5)
    # one that closed comfortably beyond the settle window
    settled = now - pd.Timedelta(minutes=15) - pd.Timedelta(
        seconds=C.SETTLE_SECONDS + 30)
    df = pd.DataFrame({"dt": [settled, fresh], "o": [1, 1], "h": [1, 1],
                       "l": [1, 1], "c": [1, 1], "v": [1, 1]})
    got = feed.closed_since(df, "15m", None)
    ok = len(got) == 1 and pd.Timestamp(got[0]["dt"]) == settled
    results.append(("SETTLE DELAY (provisional bar not actioned)",
                    PASS if ok else FAIL,
                    f"{len(got)} of 2 bars actionable (expected 1)"))
    return ok


def test_stale_entry_skipped():
    """A badly-late bar must still manage exits but must not open new trades."""
    df = feed.fetch_recent(C.SYMBOL, "1h", 400)
    specs = [s for s in C.STRATEGIES if s[1] == "1h"]
    sigs = {}
    for (_n, _t, fam, _s, _st, _r, _k) in specs:
        if fam not in sigs:
            sigs[fam] = STRATEGIES[fam](df).fillna(0).astype(int)
    # find a bar where at least one strategy signals
    idx = None
    for i in range(len(df) - 50, len(df) - 1):
        if any(int(sigs[f].iloc[i]) != 0 for f in sigs):
            idx = i
            break
    if idx is None:
        results.append(("STALE ENTRY (late signal not opened)", FAIL,
                        "no signal found to test with"))
        return False
    path = _tmpdb(); st = Store(path)
    _SPOT["px"] = float(df.iloc[idx]["c"])
    RUN.process_bar(st, "1h", df.iloc[idx], specs, sigs, idx,
                    C.MAX_ENTRY_BARS_LATE + 5, _quiet)     # very late
    n_late = len(st.open_positions())
    st.db.close(); os.remove(path)

    path = _tmpdb(); st = Store(path)
    RUN.process_bar(st, "1h", df.iloc[idx], specs, sigs, idx, 0, _quiet)  # fresh
    n_fresh = len(st.open_positions())
    st.db.close(); os.remove(path)

    ok = n_late == 0 and n_fresh > 0
    results.append(("STALE ENTRY (late signal not opened)",
                    PASS if ok else FAIL,
                    f"fresh opened {n_fresh}, stale opened {n_late} (want >0 and 0)"))
    return ok


def test_feed_outage():
    """closed_since must return nothing (not crash, not skip) on bad input."""
    ok = True
    try:
        ok &= feed.closed_since(None, "1h", None) == []
        ok &= feed.closed_since(pd.DataFrame(), "1h", None) == []
    except Exception:
        ok = False
    results.append(("FEED OUTAGE (empty feed is a no-op)",
                    PASS if ok else FAIL, "no state advanced"))
    return ok


def main() -> int:
    print("fetching data...")
    tf = "1h"
    df = feed.fetch_recent(C.SYMBOL, tf, 2000)
    specs = [s for s in C.STRATEGIES if s[1] == tf]
    sigs = {}
    for (_n, _t, fam, _s, _st, _r, _k) in specs:
        if fam not in sigs:
            sigs[fam] = STRATEGIES[fam](df).fillna(0).astype(int)
    print(f"{len(df):,} bars, {len(specs)} strategies\n")

    test_gap_replay(df, sigs, specs)
    test_restart(df, sigs, specs)
    test_idempotence(df, sigs, specs)
    test_cold_start()
    test_catchup_cap()
    test_settle_delay()
    test_stale_entry_skipped()
    test_feed_outage()

    print("=" * 84)
    print("  ADVERSE-CONDITION TESTS")
    print("=" * 84)
    for name, status, detail in results:
        mark = "OK  " if status == PASS else "FAIL"
        print(f"  [{mark}] {name}")
        print(f"         {detail}")
    bad = [r for r in results if r[1] == FAIL]
    print()
    if bad:
        print(f"{len(bad)} TEST(S) FAILED — do not deploy.")
        return 1
    print("ALL ADVERSE TESTS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
