"""Version 2 paper tests. Registered 2026-11-06, before any of their trades.

They are judged only on paper trades placed from now on, never on the data that suggested them.

Base rule, strategy "v2_taker":
  Buy NO on tomorrow's daily-high brackets whose best YES bid is 1-2c (NO at 98-99c)
  and whose YES ask is 10c or less (no ask, or a wider one, means no real market).
  Trade between 3pm and 9:59pm local on the day before, safest brackets first,
  at most 3 per city per day, 10 contracts each.
  Markets: every Kalshi daily-high series except Miami (5 of the 7 losses in the core
  cities came from Miami). No daily-low series (7 losses in 314 trades, net negative).

Variants from the rulebook (claude/kalshi_weather_rules.md); each is a separate
ledger strategy so it can be compared with v2_taker head to head:
  v2_maker  (A4)  same brackets, but a resting YES offer 1c under the ask instead
                  of selling into the bid. It fills only when a real trade later prints
                  at or through our price (mm.check_fills); unfilled orders expire at close.
  v2_early  (R8)  same price rule, but only in the first 3 hours after a market opens.
  v2_guard  (R7)  v2_taker, but skip a bracket if the forecast high has moved 1F or
                  more toward it since the reference forecast (the earliest snapshot
                  taken 2-24h ago). Forecasts are snapshotted for every city every run.
R2/R3 (no cheap YES, higher bar for YES) are enforced for every strategy in ledger.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import CITIES, City
from .kalshi import event_date
from .markets import MARKETS
from .mm import maker_fee, maker_order
from .model import bracket_bounds
from .strategy import Signal, taker_fee

TAKER, MAKER, EARLY, GUARD = "v2_taker", "v2_maker", "v2_early", "v2_guard"
STRATEGIES = (TAKER, MAKER, EARLY, GUARD)
# 2026-11-06: the paper bot runs only the base rule. The variants stay in the code
# (and their past trades in the ledger); add one back here to restart it.
ACTIVE = (TAKER,)

V2_MARKETS = tuple(k for k, m in MARKETS.items() if m.kind == "high" and k != "high_mia")
FIRST_HOUR, LAST_HOUR = 15, 21          # 3pm to 9:59pm local, the day before
MAX_PER_CITY_DAY = 3
MIN_YES_BID, MAX_YES_BID, MAX_YES_ASK = 0.01, 0.02, 0.10
EARLY_HOURS = 3
GUARD_F = 1.0
REF_MIN_AGE, REF_MAX_AGE = timedelta(hours=2), timedelta(hours=24)

# Forecast points for the guard. The original six come from config.CITIES. The rest
# are the main airports, close to each settlement station. The guard only uses the
# *change* in the forecast, so a few miles off the exact station doesn't matter.
_COORDS = {
    "atl": (33.6367, -84.4281), "bos": (42.3656, -71.0096), "dal": (32.8998, -97.0403),
    "dc": (38.8512, -77.0402), "ewr": (40.6895, -74.1745), "hou": (29.6454, -95.2789),
    "lv": (36.0840, -115.1537), "msp": (44.8848, -93.2223), "nola": (29.9934, -90.2580),
    "okc": (35.3931, -97.6007), "phx": (33.4342, -112.0116), "san": (32.7338, -117.1933),
    "satx": (29.5337, -98.4698), "sdf": (38.1744, -85.7360), "sea": (47.4502, -122.3088),
    "sfo": (37.6213, -122.3790), "ttn": (40.2767, -74.8135),
}

FORECAST_SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
  market TEXT NOT NULL,        -- e.g. high_nyc
  event_date TEXT NOT NULL,    -- the day the high is for
  ts TEXT NOT NULL,            -- when the forecast was taken (UTC ISO)
  high REAL NOT NULL,          -- forecast daily high, F
  PRIMARY KEY (market, event_date, ts)
);
"""


def forecast_city(key: str) -> City | None:
    """A City (lat/lon/tz) to ask Open-Meteo about, for market key like 'high_nyc'."""
    m = MARKETS[key]
    for c in CITIES.values():
        if c.series == m.series:
            return c
    short = key.removeprefix("high_")
    if short not in _COORDS:
        return None
    lat, lon = _COORDS[short]
    return City(short, m.series, short, lat, lon, m.tz, "")


def qualifies(yes_bid: float | None, yes_ask: float | None) -> bool:
    """YES bid 1-2c (NO 98-99c) with a real YES ask of 10c or less."""
    if yes_bid is None or yes_ask is None:
        return False
    return MIN_YES_BID - 1e-9 <= yes_bid <= MAX_YES_BID + 1e-9 and yes_ask <= MAX_YES_ASK + 1e-9


def in_window(tz: str, now: datetime) -> bool:
    return FIRST_HOUR <= now.astimezone(ZoneInfo(tz)).hour <= LAST_HOUR


def opened_recently(open_time: str | None, now: datetime, hours: int = EARLY_HOURS) -> bool:
    if not open_time:
        return False
    t = datetime.fromisoformat(open_time.replace("Z", "+00:00"))
    return timedelta(0) <= now - t <= timedelta(hours=hours)


def city_day_count(ledger, strategy: str, event_ticker: str) -> int:
    return ledger.db.execute("SELECT COUNT(*) FROM trades WHERE strategy=? AND event_ticker=? "
                             "AND status != 'expired'", (strategy, event_ticker)).fetchone()[0]


# ---- forecast snapshots (for the R7 guard) ----

def save_forecast(ledger, key: str, day, high: float, now: datetime) -> None:
    ledger.db.execute("INSERT OR REPLACE INTO forecasts VALUES (?,?,?,?)",
                      (key, day.isoformat(), now.isoformat(), round(high, 2)))
    ledger.db.commit()


def reference_forecast(ledger, key: str, day, now: datetime) -> float | None:
    """Earliest forecast for `day` taken between 24h and 2h before `now`."""
    lo, hi = (now - REF_MAX_AGE).isoformat(), (now - REF_MIN_AGE).isoformat()
    r = ledger.db.execute("SELECT high FROM forecasts WHERE market=? AND event_date=? AND ts BETWEEN ? AND ? "
                          "ORDER BY ts LIMIT 1", (key, day.isoformat(), lo, hi)).fetchone()
    return None if r is None else r[0]


def moved_toward(bracket, now_high: float | None, ref_high: float | None, step: float = GUARD_F) -> bool:
    """True if the forecast moved `step`F or more toward this bracket since the reference."""
    if now_high is None or ref_high is None:
        return False
    bounds = bracket_bounds(bracket)
    if bounds is None:
        return False
    lo, hi = bounds
    move = now_high - ref_high
    if lo > now_high:                 # bracket is above the forecast
        return move >= step
    if hi < now_high:                 # bracket is below the forecast
        return move <= -step
    return abs(move) >= step          # forecast sits inside the bracket


# ---- order helpers ----

def _taker_signal(b, q: dict, contracts: int) -> Signal | None:
    n = min(contracts, int(q.get("yes_bid_qty") or 0))
    if n < 1:
        return None
    price = round(1.0 - q["yes_bid"], 4)
    fee = taker_fee(price, n)
    return Signal(b.ticker, b.event_ticker, "no", price, prob=price, edge=round(-fee / n, 4),
                  contracts=n, fee=fee)


def _place(ledger, strategy: str, key: str, sig: Signal, forecast, log, note: str = "") -> None:
    if ledger.record(key, sig, forecast=forecast, strategy=strategy) is not None:
        log(f"[{key}] {strategy}: PAPER BUY {sig.contracts}x NO {sig.ticker} @ ${sig.price:.2f}{note}")


def _maker(ledger, key: str, b, q: dict, contracts: int, log) -> bool:
    x = maker_order(q.get("yes_bid"), q.get("yes_ask"), max_ask=MAX_YES_ASK)
    if x is None:
        return False
    price = round(1 - x, 4)
    sig = Signal(b.ticker, b.event_ticker, "no", price, prob=price, edge=0.0,
                 contracts=contracts, fee=maker_fee(price, contracts))
    tid = ledger.record(key, sig, strategy=MAKER)
    if tid is None:
        return False
    ledger.db.execute("UPDATE trades SET status='resting' WHERE id=?", (tid,))
    ledger.db.commit()
    log(f"[{key}] {MAKER}: RESTING offer YES @ ${x:.2f} (= NO @ ${price:.2f}) x{contracts} on {b.ticker}")
    return True


# ---- the hourly run ----

def run_v2(kalshi, ledger, weather, sizes: dict | None = None, now: datetime | None = None,
           markets=V2_MARKETS, log=print) -> dict:
    """Snapshot forecasts, then place every v2 paper trade due right now.

    `sizes`: contracts per strategy (0 = stopped by the brain). Returns placements per strategy.
    """
    ledger.db.executescript(FORECAST_SCHEMA)
    now = now or datetime.now(timezone.utc)
    sizes = {s: 10 for s in STRATEGIES} | (sizes or {})
    placed = {s: 0 for s in STRATEGIES}
    for key in markets:
        m = MARKETS[key]
        local = now.astimezone(ZoneInfo(m.tz))
        tomorrow = local.date() + timedelta(days=1)
        window = in_window(m.tz, now)

        highs = {}
        city = forecast_city(key)
        if city is not None and weather is not None:
            try:
                highs = weather.forecast_highs(city, days=3)
                for d, h in highs.items():
                    if d > local.date():
                        save_forecast(ledger, key, d, h, now)
            except Exception as e:        # a forecast outage must not stop the trading run
                log(f"[{key}] forecast failed: {repr(e)[:120]}")

        try:
            brackets = kalshi.markets(m.series, status="open")
        except Exception as e:
            log(f"[{key}] markets failed: {repr(e)[:120]}")
            continue

        quotes = {}

        def quote(b):
            if b.ticker not in quotes:
                quotes[b.ticker] = kalshi.orderbook(b.ticker)
            return quotes[b.ticker]

        # Pre-filter on the listing's own prices, then confirm on the order book.
        def candidates(pool):
            out = []
            for b in pool:
                if b.yes_bid is not None and b.yes_bid > MAX_YES_BID + 1e-9:
                    continue
                q = quote(b)
                if qualifies(q.get("yes_bid"), q.get("yes_ask")):
                    out.append((b, q))
            return sorted(out, key=lambda bq: (bq[1]["yes_bid"], bq[0].ticker))   # safest first

        if window:
            tom = [b for b in brackets if event_date(b.event_ticker) == tomorrow]
            cands = candidates(tom)
            now_high, ref = highs.get(tomorrow), reference_forecast(ledger, key, tomorrow, now)
            for strategy in (TAKER, GUARD, MAKER):
                size = sizes.get(strategy, 0)
                if not size:
                    continue
                for b, q in cands:
                    if city_day_count(ledger, strategy, b.event_ticker) >= MAX_PER_CITY_DAY:
                        break
                    if ledger.has_ticker(b.ticker, strategy):
                        continue
                    if strategy == MAKER:
                        placed[MAKER] += _maker(ledger, key, b, q, size, log)
                        continue
                    if strategy == GUARD and moved_toward(b, now_high, ref):
                        log(f"[{key}] {GUARD}: skip {b.ticker}, forecast moved {now_high - ref:+.1f}F toward it")
                        continue
                    sig = _taker_signal(b, q, size)
                    if sig:
                        _place(ledger, strategy, key, sig, now_high, log)
                        placed[strategy] += 1

        size = sizes.get(EARLY, 0)
        fresh = [b for b in brackets if opened_recently(b.open_time, now)]
        if size and fresh:
            for b, q in candidates(fresh):
                if city_day_count(ledger, EARLY, b.event_ticker) >= MAX_PER_CITY_DAY:
                    continue
                if ledger.has_ticker(b.ticker, EARLY):
                    continue
                sig = _taker_signal(b, q, size)
                if sig:
                    d = event_date(b.event_ticker)
                    _place(ledger, EARLY, key, sig, highs.get(d), log, note=" (market opened < 3h ago)")
                    placed[EARLY] += 1
    return placed
