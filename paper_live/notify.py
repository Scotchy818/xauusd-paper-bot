"""
Telegram notifications for the paper trader.

Reuses the bot token and chat id already in .env. Every failure is swallowed
and logged — a Telegram outage must never take the trading loop down, which
is what happened to the earlier BTC bot (a 429 from the polling loop
crash-looped the whole process).
"""

from __future__ import annotations

import os
from pathlib import Path

import requests

_ENV = Path(__file__).resolve().parent.parent / ".env"


def _load_env():
    if not _ENV.exists():
        return {}
    out = {}
    for line in _ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


_E = _load_env()
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or _E.get("TELEGRAM_BOT_TOKEN")
CHAT = os.environ.get("TELEGRAM_CHAT_ID") or _E.get("TELEGRAM_CHAT_ID")


def enabled() -> bool:
    return bool(TOKEN and CHAT)


def send(text: str, logger=None) -> bool:
    if not enabled():
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15)
        if r.status_code != 200 and logger:
            logger.warning("telegram %s: %s", r.status_code, r.text[:200])
        return r.status_code == 200
    except Exception as e:                      # never let this kill the loop
        if logger:
            logger.warning("telegram send failed: %s", e)
        return False


def fmt_open(name, direction, price, signal_price, stop, size, equity) -> str:
    side = "LONG" if direction > 0 else "SHORT"
    slip = (price - signal_price) / signal_price * 10000 * (1 if direction > 0 else -1)
    return (f"🟢 <b>OPEN {side}</b>  {name}\n"
            f"entry <b>{price:.2f}</b>  (signal {signal_price:.2f}, "
            f"slip {slip:+.1f}bp)\n"
            f"stop {stop:.2f}   size {size:,.0f}\n"
            f"equity {equity:,.2f}")


def fmt_close(name, direction, entry, exit_px, reason, net_ret, pnl,
              equity, bars) -> str:
    side = "LONG" if direction > 0 else "SHORT"
    icon = "✅" if pnl > 0 else "❌"
    return (f"{icon} <b>CLOSE {side}</b>  {name}  ({reason})\n"
            f"{entry:.2f} → {exit_px:.2f}   {net_ret*100:+.2f}%  "
            f"held {bars}h\n"
            f"P/L <b>{pnl:+,.2f}</b>   equity <b>{equity:,.2f}</b>")
