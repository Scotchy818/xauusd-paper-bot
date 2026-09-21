"""
Strategy signal functions — extracted verbatim from the validated backtest.

These were pulled programmatically out of gold_megatest.py with an AST walk
rather than retyped, so they cannot drift from the code that produced the
backtest results. verify_parity.py re-checks the whole chain on every CI run.

Each function takes a DataFrame with columns o/h/l/c/v and returns a Series of
-1 (short), 0 (flat) or +1 (long), aligned to the bar index.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s, p): return s.ewm(span=p, adjust=False).mean()

def sma(s, p): return s.rolling(p).mean()

def rsi(s, p=14):
    d = s.diff()
    g = d.where(d > 0, 0).ewm(alpha=1/p, adjust=False).mean()
    l = (-d.where(d < 0, 0)).ewm(alpha=1/p, adjust=False).mean()
    return 100 - 100 / (1 + g / (l + 1e-12))

def atr(df, p=14):
    tr = pd.concat([
        df["h"] - df["l"],
        (df["h"] - df["c"].shift()).abs(),
        (df["l"] - df["c"].shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(p).mean()

def s_donchian(df, p=20):
    hi = df["h"].rolling(p).max().shift(1); lo = df["l"].rolling(p).min().shift(1)
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[df["c"] > hi] = 1; sig[df["c"] < lo] = -1
    return sig

def s_ema_cross(df, fast=12, slow=26):
    f, s = ema(df["c"], fast), ema(df["c"], slow)
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[(f>s)&(f.shift(1)<=s.shift(1))] = 1
    sig[(f<s)&(f.shift(1)>=s.shift(1))] = -1
    return sig

def s_bb_breakout(df, p=20, k=2.0):
    m = sma(df["c"], p); sd = df["c"].rolling(p).std()
    up, lo = m+k*sd, m-k*sd
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[(df["c"]>up)&(df["c"].shift(1)<=up.shift(1))] = 1
    sig[(df["c"]<lo)&(df["c"].shift(1)>=lo.shift(1))] = -1
    return sig

def s_macd(df, fast=12, slow=26, sigp=9):
    m = ema(df["c"], fast) - ema(df["c"], slow)
    sl = ema(m, sigp)
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[(m>sl)&(m.shift(1)<=sl.shift(1))] = 1
    sig[(m<sl)&(m.shift(1)>=sl.shift(1))] = -1
    return sig

def s_orb(df, lookback=12):
    """Opening Range Breakout: break the high/low of the prior `lookback` candles."""
    hi = df["h"].rolling(lookback).max().shift(1)
    lo = df["l"].rolling(lookback).min().shift(1)
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[df["c"] > hi] = 1
    sig[df["c"] < lo] = -1
    return sig

def s_keltner(df, p=20, mult=2.0):
    m = ema(df["c"], p); a = atr(df, p)
    up, lo = m+mult*a, m-mult*a
    sig = pd.Series(0, index=df.index, dtype=int)
    sig[(df["c"]>up)&(df["c"].shift(1)<=up.shift(1))] = 1
    sig[(df["c"]<lo)&(df["c"].shift(1)>=lo.shift(1))] = -1
    return sig


# Only the families the live bot trades. Names match the backtest exactly.
STRATEGIES = {
    "donchian20":  lambda df: s_donchian(df, 20),
    "donchian50":  lambda df: s_donchian(df, 50),
    "ema_12_26":   lambda df: s_ema_cross(df, 12, 26),
    "bb_breakout": lambda df: s_bb_breakout(df, 20, 2.0),
    "macd":        lambda df: s_macd(df),
    "orb12":       lambda df: s_orb(df, 12),
    "orb24":       lambda df: s_orb(df, 24),
    "keltner":     lambda df: s_keltner(df, 20, 2.0),
}
