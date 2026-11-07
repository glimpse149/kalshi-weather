"""SQLite paper-trading ledger."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

from .strategy import Signal, settle_pnl

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  opened_at TEXT NOT NULL,
  city TEXT NOT NULL,
  ticker TEXT NOT NULL,
  event_ticker TEXT NOT NULL,
  side TEXT NOT NULL,
  price REAL NOT NULL,
  contracts INTEGER NOT NULL,
  fee REAL NOT NULL,
  prob REAL NOT NULL,
  edge REAL NOT NULL,
  forecast REAL,
  status TEXT NOT NULL DEFAULT 'open',
  result TEXT,
  pnl REAL,
  settled_at TEXT,
  strategy TEXT NOT NULL DEFAULT 'model'
);
"""


# Rulebook R2/R3 (claude/kalshi_weather_rules.md), enforced for every paper strategy:
CHEAP_YES_BAN = 0.10     # R2: never buy YES at 10c or less ...
YES_EXCEPTION = (2.0, 0.08)   # ... unless model p >= 2x the price and edge >= 8c
MIN_EDGE_YES = 0.08      # R3: a YES buy needs at least 8c of edge
MAX_YES_SHARE = 0.40     # R3: once YES is over 40% of a day's trades, take only NO


def yes_guard(s: Signal, day_trades: int, day_yes: int) -> str | None:
    """Why a YES buy is blocked, or None if allowed. NO buys are never blocked here."""
    if s.side != "yes":
        return None
    mult, min_edge = YES_EXCEPTION
    if s.price <= CHEAP_YES_BAN and not (s.prob >= mult * s.price and s.edge >= min_edge):
        return f"R2: YES at {s.price:.2f} (10c or less)"
    if s.edge < MIN_EDGE_YES:
        return f"R3: YES edge {s.edge:.3f} below {MIN_EDGE_YES}"
    if day_trades and day_yes / day_trades > MAX_YES_SHARE:
        return f"R3: YES already over {MAX_YES_SHARE:.0%} of today's trades; NO only until it drops"
    return None


class Ledger:
    def __init__(self, path: str):
        if os.path.dirname(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(trades)")}
        if "strategy" not in cols:          # ledgers created before strategies existed
            self.db.execute("ALTER TABLE trades ADD COLUMN strategy TEXT NOT NULL DEFAULT 'model'")
            self.db.commit()

    def has_event(self, event_ticker: str, strategy: str = "model") -> bool:
        return self.db.execute("SELECT 1 FROM trades WHERE event_ticker=? AND strategy=?",
                               (event_ticker, strategy)).fetchone() is not None

    def has_ticker(self, ticker: str, strategy: str) -> bool:
        return self.db.execute("SELECT 1 FROM trades WHERE ticker=? AND strategy=?",
                               (ticker, strategy)).fetchone() is not None

    def record(self, city: str, s: Signal, forecast: float | None = None,
               strategy: str = "model") -> int | None:
        """Insert a paper trade; None (nothing recorded) if the R2/R3 guard blocks it."""
        if s.side == "yes":
            day = datetime.now(timezone.utc).date().isoformat()
            n, y = self.db.execute("SELECT COUNT(*), COALESCE(SUM(side='yes'), 0) FROM trades "
                                   "WHERE substr(opened_at, 1, 10) = ?", (day,)).fetchone()
            why = yes_guard(s, n, y)
            if why:
                print(f"blocked {strategy} YES {s.ticker}: {why}")
                return None
        cur = self.db.execute(
            "INSERT INTO trades (opened_at, city, ticker, event_ticker, side, price, contracts,"
            " fee, prob, edge, forecast, strategy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), city, s.ticker, s.event_ticker, s.side,
             s.price, s.contracts, s.fee, s.prob, s.edge, forecast, strategy))
        self.db.commit()
        return cur.lastrowid

    def open_trades(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM trades WHERE status='open'").fetchall()

    def settle(self, trade_id: int, result: str) -> float:
        t = self.db.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        pnl = settle_pnl(t["side"], t["price"], t["contracts"], t["fee"], result)
        self.db.execute("UPDATE trades SET status='settled', result=?, pnl=?, settled_at=? WHERE id=?",
                        (result, pnl, datetime.now(timezone.utc).isoformat(), trade_id))
        self.db.commit()
        return pnl

    def strategies(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT DISTINCT strategy FROM trades ORDER BY 1")]

    def summary(self, strategy: str | None = None) -> dict:
        rows = self.db.execute("SELECT * FROM trades WHERE status IN ('open','settled')"
                               + (" AND strategy=?" if strategy else ""),
                               (strategy,) if strategy else ()).fetchall()
        settled = [r for r in rows if r["status"] == "settled"]
        wins = sum(1 for r in settled if r["side"] == r["result"])
        cost = sum(r["price"] * r["contracts"] + r["fee"] for r in settled)
        pnl = sum(r["pnl"] for r in settled)
        exp = sum((r["prob"] - r["price"]) * r["contracts"] - r["fee"] for r in settled)
        return {
            "total_trades": len(rows), "open": len(rows) - len(settled), "settled": len(settled),
            "wins": wins, "win_rate": wins / len(settled) if settled else None,
            "pnl": round(pnl, 2), "cost": round(cost, 2),
            "roi": pnl / cost if cost else None, "expected_pnl": round(exp, 2),
            "avg_pred_win_prob": (sum(r["prob"] for r in settled) / len(settled)) if settled else None,
        }
