# XAUUSD Paper Trader

Paper-trades eight trend/breakout strategies on gold. **No broker is
connected and there is no order-placing code in this repository.** It cannot
touch money.

Runs on GitHub Actions every 15 minutes. State (open positions, closed
trades, equity) lives in `paper_live/paper.db`, committed back after each
cycle so the bot survives restarts.

## Why this exists

These eight configurations came out of a large backtest search over ~10,000
strategy/parameter combinations on XAUUSD, EURUSD, GBPUSD and USDJPY. Most of
that search found nothing: the gross per-trade edge across the whole universe
was 0.00 bp — a coin flip — and the net loss was exactly the spread.

What did survive was trend-following on gold, which worked across every
lookback tested and is a recognised strategy class (the same thing managed
futures funds run). It is also regime-dependent: it makes money when markets
trend and bleeds when they chop, and the backtest period covered gold's
strongest trending run in a decade.

So the backtested numbers are optimistic by construction. This paper test
exists because forward testing on data that does not exist yet is the only
check that cannot be accidentally biased by the person running it.

## Strategies

| Name | TF | Family | Exit | Stop |
|---|---|---|---|---|
| donchian20_1h | 1h | Donchian 20 | breakeven-then-trail | 0.35% |
| orb24_1h | 1h | Opening range 24 | breakeven-then-trail | 0.35% |
| orb12_1h | 1h | Opening range 12 | breakeven-then-trail | 0.35% |
| macd_1h_run | 1h | MACD | runner (half at 3R, rest trails) | 0.50% |
| ema1226_1h | 1h | EMA 12/26 | breakeven-then-trail | 0.35% |
| bbbreak_15m | 15m | Bollinger breakout | breakeven-then-trail | 0.35% |
| donchian50_15m | 15m | Donchian 50 | runner | 0.35% |
| keltner_15m | 15m | Keltner | runner | 0.50% |

Risk is 1% of the account per trade, one position per strategy, so at most 8%
is at risk at once.

## Integrity checks

Two things are verified so the paper test measures what the backtest measured:

- `strategies.py` was extracted from the backtest source with an AST walk
  rather than retyped. All 16 signal sets (8 families x 2 timeframes) are
  bit-identical to the originals.
- `paper_live/verify_parity.py` replays historical bars through the live
  state machine and compares trade-for-trade against the backtest engine.
  **It runs in CI before every cycle** and fails the run on any mismatch.

This caught a real bug during development: the live loop allowed a strategy
to close and re-open on the same bar, which the backtest forbids. That alone
produced ~10% more trades than the validated system.

## Local use

```bash
pip install -r requirements.txt
python -m paper_live.verify_parity     # engine matches backtest?
python -m paper_live.run --once        # one cycle
python -m paper_live.run --status      # current state
python -m paper_live.run               # continuous loop
```

## Expectations

Losing streaks of 8-17 trades in a row are normal here; the runner strategies
win only ~28-35% of the time and make it back on size. A bad first month means
very little. `bbbreak_15m` is included despite being down over the last five
months because it has the best full-period record — running it is how we find
out whether that weakness is noise or decay.
