"""Exit-rule backtest on hourly Kalshi prices. Research only: it places no orders.

Entry (the live rule): best YES bid 1-2c at 5pm local the day before, so buy NO at
1 - bid, 30 contracts. From there, hourly candle closes until the event day ends
(local midnight) are used to test what an hourly check could have done:

  hold          keep every position to settlement (what the bots do today)
  stop T        sell the whole position the first hour the NO bid (1 - YES ask) is below T
  add L-H, Xh   buy 30 more NO the first hour its ask (1 - YES bid) is within L-H,
                during the last X hours of the event day ("any" = any time after entry)
  stop + add    both rules together; a stop sells every contract held at that point
  late stop     stop T, but only during the last X hours of the event day

Every buy and sell pays the taker fee. Prices are hourly closes, so this matches a bot
that checks once an hour; anything that happens inside an hour is not seen.
"""
from __future__ import annotations

import gzip
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .kalshi import KALSHI_PROD, KalshiClient, event_date
from .longshot import quote_from_candles
from .markets import MARKETS
from .strategy import taker_fee

N = 30
ENTRY_HOUR = 17
MIN_BID, MAX_BID = 0.01, 0.02
LIVE_SIX = ("high_nyc", "high_chi", "high_den", "high_lax", "high_aus", "high_phl")
STOPS = (0.90, 0.85, 0.80, 0.75, 0.70, 0.60, 0.50)
ADDS = ((0.85, 0.93, None), (0.85, 0.93, 12), (0.85, 0.93, 6), (0.85, 0.93, 3),
        (0.88, 0.95, 6), (0.80, 0.90, 6))
COMBOS = ((0.80, (0.85, 0.93, 6)), (0.75, (0.85, 0.93, 6)), (0.70, (0.88, 0.95, 6)))
LATE_STOPS = tuple((s, h) for s in (0.80, 0.70, 0.60) for h in (3, 6, 9, 12))


def groups() -> dict[str, tuple[str, ...]]:
    return {
        "Whole market (48 markets: highs + lows)": tuple(MARKETS),
        "All daily highs (24 markets)": tuple(k for k, m in MARKETS.items() if m.kind == "high"),
        "Daily highs without Miami (23)": tuple(k for k, m in MARKETS.items()
                                               if m.kind == "high" and k != "high_mia"),
        "Daily lows (24 markets)": tuple(k for k, m in MARKETS.items() if m.kind == "low"),
        "Your 6 live cities": LIVE_SIX,
    }


# ---------- data ----------

def _window(m, d: date) -> tuple[int, int, int, int]:
    """(candle start, entry ts, event-day end, candle end) as unix seconds."""
    tz = ZoneInfo(m.tz)
    prev = d - timedelta(days=1)
    start = datetime.combine(prev, time(11), tz)
    entry = datetime.combine(prev, time(ENTRY_HOUR), tz)
    day_end = datetime.combine(d + timedelta(days=1), time(0), tz)
    return int(start.timestamp()), int(entry.timestamp()), int(day_end.timestamp()), int(day_end.timestamp()) + 3 * 3600


def collect_market(key: str, start: date, end: date, cache_dir: str | None, log=print) -> list[dict]:
    """Every settled bracket of one market that met the entry rule, with its hourly path."""
    m = MARKETS[key]
    k = KalshiClient(KALSHI_PROD, cache_dir=cache_dir)
    lo = int(datetime.combine(start, time(0), timezone.utc).timestamp())
    hi = int(datetime.combine(end + timedelta(days=2), time(0), timezone.utc).timestamp())
    out = []
    try:
        brackets = k.settled_markets(m.series, min_close_ts=lo, max_close_ts=hi, cache=True)
    except Exception as e:
        log(f"[{key}] markets failed: {repr(e)[:120]}")
        return out
    for b in brackets:
        d = event_date(b.event_ticker)
        if not d or not (start <= d <= end) or b.result not in ("yes", "no"):
            continue
        s, entry, day_end, e = _window(m, d)
        try:
            candles = k.candles(m.series, b.ticker, s, e, period=60, close_time=b.close_time)
        except Exception as ex:
            log(f"[{key}] candles failed for {b.ticker}: {repr(ex)[:80]}")
            continue
        bid, _ask = quote_from_candles(candles, entry)
        if bid is None or not (MIN_BID - 1e-9 <= bid <= MAX_BID + 1e-9):
            continue
        path, yb, ya = [], None, None
        for c in candles:
            if c["ts"] <= entry or c["ts"] > day_end:
                continue
            yb = c["yes_bid"] if c["yes_bid"] is not None else yb
            ya = c["yes_ask"] if c["yes_ask"] is not None else ya
            path.append((c["ts"], yb, ya))
        out.append({"market": key, "ticker": b.ticker, "date": d.isoformat(), "entry_bid": bid,
                    "result": b.result, "day_end": day_end, "path": path})
    log(f"[{key}] {len(out)} qualifying trades")
    return out


def collect(keys, days: int, cache_dir: str | None, workers: int = 3, log=print) -> list[dict]:
    end = datetime.now(timezone.utc).date() - timedelta(days=2)
    start = end - timedelta(days=days)
    with ThreadPoolExecutor(workers) as ex:
        parts = list(ex.map(lambda key: collect_market(key, start, end, cache_dir, log), keys))
    return [t for p in parts for t in p]


# ---------- simulation ----------

def simulate(t: dict, stop: float | None = None, add: tuple | None = None,
             stop_last: int | None = None) -> dict:
    """P&L of one trade under the rules, plus what happened (stopped, added).
    stop_last: the stop only acts during the last N hours of the event day."""
    won = t["result"] == "no"
    p0 = round(1 - t["entry_bid"], 4)
    legs = [(p0, N)]
    cash = -(p0 * N) - taker_fee(p0, N)
    stopped_at = added_at = None
    for ts, yb, ya in t["path"]:
        if add and added_at is None and yb is not None:
            lo, hi, last = add
            no_ask = round(1 - yb, 4)
            late = last is None or t["day_end"] - ts <= last * 3600
            if late and lo - 1e-9 <= no_ask <= hi + 1e-9:
                legs.append((no_ask, N))
                cash -= no_ask * N + taker_fee(no_ask, N)
                added_at = no_ask
        if stop and ya is not None and (stop_last is None or t["day_end"] - ts <= stop_last * 3600):
            no_bid = round(1 - ya, 4)
            if no_bid < stop - 1e-9:
                held = sum(n for _, n in legs)
                cash += no_bid * held - taker_fee(no_bid, held)
                stopped_at = no_bid
                return {"pnl": round(cash, 2), "won": won, "stopped": stopped_at, "added": added_at}
    held = sum(n for _, n in legs)
    cash += held if won else 0.0
    return {"pnl": round(cash, 2), "won": won, "stopped": stopped_at, "added": added_at}


def rule_rows(trades: list[dict]) -> list[str]:
    def run(name, stop=None, add=None, last=None):
        r = [simulate(t, stop, add, last) for t in trades]
        pnl = sum(x["pnl"] for x in r)
        st = [x for x in r if x["stopped"] is not None]
        ad = [x for x in r if x["added"] is not None]
        note = []
        if stop:
            note.append(f"sold {len(st)}, {sum(x['won'] for x in st)} of them would have won")
        if add:
            note.append(f"added {len(ad)}, {sum(x['won'] for x in ad)} won")
        return f"| {name} | ${pnl:+,.2f} | {'; '.join(note) or '—'} |"

    rows = ["| Rule | Total P&L (30 contracts) | What it did |", "|---|---|---|", run("Hold to settlement")]
    rows += [run(f"Sell if NO bid < {s * 100:.0f}c", stop=s) for s in STOPS]
    rows += [run(f"Sell if NO bid < {s * 100:.0f}c, last {h}h only", stop=s, last=h) for s, h in LATE_STOPS]
    for lo, hi, last in ADDS:
        when = "any time" if last is None else f"last {last}h"
        rows.append(run(f"Buy again at {lo * 100:.0f}-{hi * 100:.0f}c, {when}", add=(lo, hi, last)))
    for s, (lo, hi, last) in COMBOS:
        rows.append(run(f"Sell < {s * 100:.0f}c + buy again {lo * 100:.0f}-{hi * 100:.0f}c last {last}h",
                        stop=s, add=(lo, hi, last)))
    return rows


def report(trades: list[dict], days: int, keys=None) -> str:
    dates = sorted(t["date"] for t in trades)
    out = ["# Exit-rule backtest (hourly Kalshi prices)", "",
           f"Entry: YES bid 1-2c at 5pm local the day before, buy 30 NO. Event days {dates[0] if dates else '-'} "
           f"to {dates[-1] if dates else '-'} (last {days} days). Checks once an hour; taker fees on every buy and sell.", ""]
    for name, gkeys in groups().items():
        if keys is not None and not set(gkeys) <= set(keys):
            continue
        g = [t for t in trades if t["market"] in gkeys]
        losses = [t for t in g if t["result"] != "no"]
        out += [f"## {name}", "", f"{len(g)} trades, {len(losses)} losses "
                f"({len(losses) / len(g):.2%} loss rate)" if g else "No trades.", ""]
        if g:
            out += rule_rows(g) + [""]
        if name == "Your 6 live cities" and losses:
            out += ["Losses, hourly NO bid after entry:", ""]
            for t in losses:
                bids = [f"{100 * (1 - ya):.0f}" for _, _, ya in t["path"] if ya is not None]
                out.append(f"- {t['ticker']} (bought at {100 * (1 - t['entry_bid']):.0f}c): " + " ".join(bids))
            out.append("")
    return "\n".join(out)


def main(days: int = 120, out_dir: str = "lab", cache_dir: str | None = None, log=print,
         keys=None) -> str:
    keys = tuple(keys or MARKETS)
    trades = collect(keys, days, cache_dir, log=log)
    os.makedirs(out_dir, exist_ok=True)
    with gzip.open(os.path.join(out_dir, "exit_paths.json.gz"), "wt") as f:
        json.dump(trades, f)
    text = report(trades, days, keys)
    with open(os.path.join(out_dir, "exits_report.md"), "w") as f:
        f.write(text + "\n")
    return text
