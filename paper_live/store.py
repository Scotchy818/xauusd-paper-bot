"""
SQLite persistence for the paper trader.

Two tables:

  positions  - one row per open position. The process can be killed and
               restarted at any time and it will pick these back up, which
               matters because the alternative is orphaned state that silently
               stops being managed.

  trades     - one row per CLOSED trade, with everything needed to audit the
               result later: signal price vs actual fill (so feed latency can
               be measured), the exit reason, and the realised return both
               gross and net of cost.

  equity     - running account value after each close.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    strategy     TEXT PRIMARY KEY,
    direction    INTEGER NOT NULL,
    entry_time   TEXT NOT NULL,
    signal_price REAL NOT NULL,
    entry_price  REAL NOT NULL,
    size         REAL NOT NULL,
    stop_px      REAL NOT NULL,
    target_px    REAL,
    best_px      REAL NOT NULL,
    moved_be     INTEGER NOT NULL DEFAULT 0,
    armed_trail  INTEGER NOT NULL DEFAULT 0,
    half_banked  INTEGER NOT NULL DEFAULT 0,
    banked_ret   REAL NOT NULL DEFAULT 0,
    bars_held    INTEGER NOT NULL DEFAULT 0,
    bars_late    INTEGER NOT NULL DEFAULT 0,
    stop_pct     REAL NOT NULL,
    trail_k      REAL NOT NULL,
    style        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy     TEXT NOT NULL,
    direction    INTEGER NOT NULL,
    entry_time   TEXT NOT NULL,
    exit_time    TEXT NOT NULL,
    signal_price REAL NOT NULL,
    entry_price  REAL NOT NULL,
    exit_price   REAL NOT NULL,
    slippage_bp  REAL NOT NULL,
    bars_late    INTEGER NOT NULL DEFAULT 0,
    bars_held    INTEGER NOT NULL,
    exit_reason  TEXT NOT NULL,
    gross_ret    REAL NOT NULL,
    net_ret      REAL NOT NULL,
    pnl          REAL NOT NULL,
    equity_after REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---- meta -------------------------------------------------------
    def get_meta(self, key: str, default: Any = None):
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_meta(self, key: str, value: Any):
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))
        self.db.commit()

    # ---- positions --------------------------------------------------
    def open_positions(self) -> dict:
        rows = self.db.execute("SELECT * FROM positions").fetchall()
        return {r["strategy"]: dict(r) for r in rows}

    def upsert_position(self, p: dict):
        cols = ",".join(p.keys())
        qs = ",".join("?" * len(p))
        upd = ",".join(f"{k}=excluded.{k}" for k in p if k != "strategy")
        self.db.execute(
            f"INSERT INTO positions({cols}) VALUES({qs}) "
            f"ON CONFLICT(strategy) DO UPDATE SET {upd}", tuple(p.values()))
        self.db.commit()

    def delete_position(self, strategy: str):
        self.db.execute("DELETE FROM positions WHERE strategy=?", (strategy,))
        self.db.commit()

    # ---- trades -----------------------------------------------------
    def record_trade(self, t: dict):
        cols = ",".join(t.keys())
        qs = ",".join("?" * len(t))
        self.db.execute(f"INSERT INTO trades({cols}) VALUES({qs})",
                        tuple(t.values()))
        self.db.commit()

    def trades(self, limit: int | None = None, strategy: str | None = None):
        q = "SELECT * FROM trades"
        args: list = []
        if strategy:
            q += " WHERE strategy=?"
            args.append(strategy)
        q += " ORDER BY id DESC"
        if limit:
            q += f" LIMIT {int(limit)}"
        return [dict(r) for r in self.db.execute(q, args).fetchall()]

    def equity(self, start: float) -> float:
        r = self.db.execute(
            "SELECT equity_after FROM trades ORDER BY id DESC LIMIT 1").fetchone()
        return r["equity_after"] if r else start

    def trade_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) c FROM trades").fetchone()["c"]
