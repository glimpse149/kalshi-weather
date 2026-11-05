"""Daily Telegram summary: live (real money) on top, paper (fake money) below.

Each section has three lines: last 24 hours, all time, progress.
Run: python -m kalshi_weather.summary   (needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

LIVE_TRIAL_TRADES = 100      # first review point for the real-money trial
PAPER_TARGET = 1500          # trades needed to tell a ~0.5c edge from zero


def live_lines(s: dict | None, start: float | None, error: str | None = None) -> list[str]:
    head = "LIVE (real money)"
    if error:
        return [head, f"Unavailable: {error}"]
    if s is None:
        return [head, "Off (dry run, no orders)"]
    if s["mode"] == "demo":
        head = "DEMO (fake exchange)"
    r = s.get("results")
    out = [head]
    if s.get("stopped"):
        out.append("STOPPED: loss limit reached, no new orders")
    if r is None:
        out.append("Trade results unavailable right now")
    else:
        d, a = r["day"], r["all"]
        out.append(f"Last 24h: {d['new']} new trades, {d['settled']} settled, {d['losses']} losses, "
                   f"P&L ${d['pnl']:+.2f}")
        out.append(f"All time: {a['settled']} settled ({s['open_positions']} open), {a['losses']} losses, "
                   f"P&L ${a['pnl']:+.2f}")
        out.append(f"Progress: {a['settled']} of {LIVE_TRIAL_TRADES} trial trades "
                   f"({a['settled'] / LIVE_TRIAL_TRADES:.0%})")
    acct = f"Account: ${s['equity']:.2f}"
    if start is not None:
        acct += f" (start ${start:.2f}, {s['pnl']:+.2f})"
    out.append(acct)
    return out


def paper_lines(db_path: str, now=None) -> list[str]:
    out = ["PAPER (fake money)"]
    if not os.path.exists(db_path):
        return out + ["No trades recorded yet."]
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=1)).isoformat()
    db = sqlite3.connect(db_path)
    q = lambda sql, *a: db.execute(sql, a).fetchone()
    opened = q("SELECT COUNT(*) FROM trades WHERE opened_at >= ?", since)[0]
    n24, l24, p24 = q("SELECT COUNT(*), COALESCE(SUM(result != side), 0), COALESCE(SUM(pnl), 0) "
                      "FROM trades WHERE status='settled' AND settled_at >= ?", since)
    n, l, p = q("SELECT COUNT(*), COALESCE(SUM(result != side), 0), COALESCE(SUM(pnl), 0) "
                "FROM trades WHERE status='settled'")
    open_n = q("SELECT COUNT(*) FROM trades WHERE status='open'")[0]
    main = q("SELECT COUNT(*) FROM trades WHERE status='settled' AND strategy LIKE 'longshot\\_%' ESCAPE '\\'")[0]
    out.append(f"Last 24h: {opened} new trades, {n24} settled, {l24} losses, P&L ${p24:+.2f}")
    out.append(f"All time: {n:,} settled ({open_n} open), {l} losses, P&L ${p:+.2f}")
    out.append(f"Progress: main strategy {main:,} of {PAPER_TARGET:,} trades ({main / PAPER_TARGET:.0%})")
    return out


def build(db_path: str = "ledger/paper.db", dashboard: str = "", now=None) -> str:
    from .live import from_env, status
    s = start = err = None
    try:
        mode, auth, start = from_env()
        if auth is not None:
            s = status(mode, auth, start)
    except Exception as e:
        err = type(e).__name__
    now = now or datetime.now(timezone.utc)
    lines = [f"Kalshi weather - {now:%b %d}", ""] + live_lines(s, start, err) + [""] + paper_lines(db_path, now)
    if dashboard:
        lines += ["", dashboard]
    return "\n".join(lines)


def main() -> None:
    import requests
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise SystemExit("Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID as repository secrets.")
    text = build(dashboard=os.environ.get("DASHBOARD_URL", ""))
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": text, "disable_web_page_preview": "true"}, timeout=20)
    print(r.status_code)
    r.raise_for_status()


if __name__ == "__main__":
    main()
