"""Real-money longshot trading on Kalshi, with hard safety limits.

Rule (the longshot paper rule): between 5pm and 9:59pm local the day before, buy NO on
every bracket whose best YES bid is 1-2c, at 1 - YES bid, 30 contracts, in six cities.
Backtest by hour (c/contract): 5pm +1.09, 7pm +1.02, 9pm +0.83. The wide window is there
because GitHub's scheduled runs can be hours late; each bracket is still bought only once.

Modes, set by the LIVE_MODE environment variable:
  off  (default) dry run: reads real prices, prints what it would buy, places nothing
  demo           Kalshi's demo exchange (fake money), keys KALSHI_DEMO_KEY_ID / KALSHI_DEMO_PRIVATE_KEY
  live           real money, keys KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY

Safety limits (all enforced here, not by settings):
  - 30 contracts per order max, NO price 98-99c only, limit orders that expire in 5 minutes
  - in each city the safest brackets (highest NO price) are taken first
  - at most 3 brackets per city per day (counting earlier runs), never a ticker already held
  - total money in open positions capped at $1,600 (two overlapping days: each batch is held
    ~38h, from 5pm the day before until the morning after the event, plus buy-agains)
  - stop: no new orders once equity (cash + open positions at cost) falls $50 below
    LIVE_START_EQUITY. Update LIVE_START_EQUITY after any deposit or withdrawal, and raise
    it by hand to re-arm after reviewing a stop.

Managing open positions (2026-11-15, every run, any hour; backtest +$381 vs +$241 holding):
  - late sell: from 6pm local on the event day, if the NO bid is below 70c (and the YES
    bid is at least 20c, so an empty book can't trigger it), sell the whole position
  - buy again: once per position, if the NO ask is 85-93c, buy 30 more NO (any time
    before the event day ends); counts against the open-money cap, cash and the stop
GitHub Actions logs on a public repo are public, so balances go to Telegram only.
"""
from __future__ import annotations

import base64
import os
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

from .config import CITIES, KALSHI_DEMO, KALSHI_PROD

LIVE_CITIES = ("nyc", "chi", "den", "lax", "aus", "phl")   # Miami stays on paper
LIVE_HOUR = 17                 # window start, local time
LIVE_LAST_HOUR = 21            # window end: trades up to 9:59pm local
CONTRACTS = 30                 # 2026-11-06: 30 contracts on the safest brackets only (was 20 at 96-99c)
MAX_ORDERS_PER_CITY_DAY = 3
MAX_OPEN_COST = 1600.0         # 2026-11-15: $1,600 account; worst case 2 days x 18 trades + 18 buy-agains
MIN_NO_CENTS, MAX_NO_CENTS = 98, 99   # YES bid 1-2c only: 0 losses in 533 upstream trades
STOP_LOSS = 50.0               # dollars below LIVE_START_EQUITY (~1.7 misses at 30 contracts)
ORDER_TTL = 300                # seconds a limit order may rest before it expires
STOP_NO_CENTS = 70             # late sell: NO bid below this...
STOP_FROM_HOUR = 18            # ...from 6pm local on the event day (its last 6 hours)
STOP_MIN_YES_BID = 0.20        # ...confirmed by a real YES bid, not an empty book
ADD_MIN_NO, ADD_MAX_NO = 85, 93   # buy again when the NO ask is in this band...
ADD_CONTRACTS = 30             # ...this many more, once per position


class LiveError(RuntimeError):
    pass


class AuthClient:
    """Signed Kalshi trade-api v2 calls: timestamp + METHOD + path, signed with Ed25519 or RSA-PSS."""

    def __init__(self, base_url: str, key_id: str, private_key_pem: str, session=None):
        from cryptography.hazmat.primitives import serialization
        self.base_url = base_url.rstrip("/")
        self.prefix = urlparse(self.base_url).path          # /trade-api/v2
        self.key_id = key_id
        pem = private_key_pem.replace("\\n", "\n").strip().encode()
        self.key = serialization.load_pem_private_key(pem, password=None)
        self.session = session or requests.Session()

    def headers(self, method: str, path: str) -> dict:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + self.prefix + path.split("?")[0]).encode()
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        if isinstance(self.key, Ed25519PrivateKey):      # newer Kalshi keys
            sig = self.key.sign(msg)
        else:                                            # RSA keys: PSS with SHA-256
            sig = self.key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                 salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
                "Content-Type": "application/json"}

    def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        r = self.session.request(method, self.base_url + path, params=params, json=body,
                                 headers=self.headers(method, path), timeout=20)
        if r.status_code >= 400:
            raise LiveError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    def balance(self) -> float:
        d = self.request("GET", "/portfolio/balance")
        if d.get("balance_dollars") not in (None, ""):
            return float(d["balance_dollars"])
        return float(d.get("balance", 0)) / 100.0

    def positions(self) -> list[dict]:
        out, cursor = [], None
        while True:
            params = {"limit": 200, **({"cursor": cursor} if cursor else {})}
            d = self.request("GET", "/portfolio/positions", params=params)
            out += d.get("market_positions") or d.get("positions") or []
            cursor = d.get("cursor")
            if not cursor:
                return out

    def _paged(self, path: str, key: str, params: dict | None = None) -> list[dict]:
        out, cursor = [], None
        while True:
            q = {"limit": 200, **(params or {}), **({"cursor": cursor} if cursor else {})}
            d = self.request("GET", path, params=q)
            out += d.get(key) or []
            cursor = d.get("cursor")
            if not cursor:
                return out

    def fills(self) -> list[dict]:
        return self._paged("/portfolio/fills", "fills")

    def settlements(self) -> list[dict]:
        return self._paged("/portfolio/settlements", "settlements")

    def resting_orders(self) -> list[dict]:
        return self.request("GET", "/portfolio/orders", params={"status": "resting", "limit": 200}).get("orders") or []

    def buy_no(self, ticker: str, no_cents: int, count: int) -> dict:
        """Buy NO at no_cents via Kalshi's V2 order endpoint.

        V2 quotes everything on the YES book: buying NO at P is selling YES (side "ask")
        at 1 - P. The limit order rests at most ORDER_TTL seconds.
        """
        body = {"ticker": ticker, "side": "ask", "count": str(int(count)),
                "price": f"{(100 - int(no_cents)) / 100:.2f}",
                "time_in_force": "good_till_canceled", "expiration_time": int(time.time()) + ORDER_TTL,
                "self_trade_prevention_type": "taker_at_cross",
                "client_order_id": f"kw-{uuid.uuid4().hex[:16]}"}
        d = self.request("POST", "/portfolio/events/orders", body=body)
        o = d.get("order") or d
        filled = _num(o, "fill_count", "fill_count_fp")
        left = _num(o, "remaining_count", "remaining_count_fp")
        o["status"] = o.get("status") or (f"filled {filled:g}" + (f", {left:g} resting" if left else ""))
        return o

    def sell_no(self, ticker: str, no_cents: int, count: int) -> dict:
        """Sell NO we hold at no_cents: on the V2 book that is buying YES (side "bid") at
        1 - P, which nets out the NO position."""
        body = {"ticker": ticker, "side": "bid", "count": str(int(count)),
                "price": f"{(100 - int(no_cents)) / 100:.2f}",
                "time_in_force": "good_till_canceled", "expiration_time": int(time.time()) + ORDER_TTL,
                "self_trade_prevention_type": "taker_at_cross",
                "client_order_id": f"kw-{uuid.uuid4().hex[:16]}"}
        d = self.request("POST", "/portfolio/events/orders", body=body)
        o = d.get("order") or d
        filled = _num(o, "fill_count", "fill_count_fp")
        left = _num(o, "remaining_count", "remaining_count_fp")
        o["status"] = o.get("status") or (f"filled {filled:g}" + (f", {left:g} resting" if left else ""))
        return o


def _cents(d: dict, name: str) -> float:
    """A money field in dollars from either `<name>_dollars` or cents `<name>`."""
    if d.get(f"{name}_dollars") not in (None, ""):
        return float(d[f"{name}_dollars"])
    return float(d.get(name) or 0) / 100.0


def open_cost(positions: list[dict]) -> float:
    """Dollars currently tied up in open positions (Kalshi's market exposure)."""
    tot = 0.0
    for p in positions:
        if not float(p.get("position") or p.get("position_fp") or 0):
            continue
        exp = _cents(p, "market_exposure")
        tot += exp if exp else _cents(p, "total_cost")
    return round(tot, 2)


def held_tickers(positions: list[dict], orders: list[dict]) -> set[str]:
    held = {p["ticker"] for p in positions
            if float(p.get("position") or p.get("position_fp") or 0) or int(p.get("resting_order_count") or 0)}
    return held | {o["ticker"] for o in orders if o.get("ticker")}


def mode_settings(mode: str) -> tuple[str, str, str]:
    """(base_url, key id, private key) for demo or live."""
    if mode == "demo":
        base, kid, pem = KALSHI_DEMO, "KALSHI_DEMO_KEY_ID", "KALSHI_DEMO_PRIVATE_KEY"
    elif mode == "live":
        base, kid, pem = KALSHI_PROD, "KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY"
    else:
        raise LiveError(f"unknown LIVE_MODE {mode!r}")
    if not os.environ.get(kid) or not os.environ.get(pem):
        raise LiveError(f"LIVE_MODE={mode} needs the {kid} and {pem} secrets")
    return base, os.environ[kid], os.environ[pem]


def telegram(text: str) -> None:
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      data={"chat_id": chat, "text": text, "disable_web_page_preview": "true"}, timeout=20)
    except requests.RequestException as e:
        print(f"telegram send failed: {type(e).__name__}")


def in_window(cities, now=None) -> list:
    """Cities where the local time is between LIVE_HOUR:00 and LIVE_LAST_HOUR:59."""
    now = now or datetime.now(timezone.utc)
    return [c for c in cities if LIVE_HOUR <= now.astimezone(ZoneInfo(c.tz)).hour <= LIVE_LAST_HOUR]


def event_of(ticker: str) -> str:
    """KXHIGHNY-26NOV05-B74.5 -> KXHIGHNY-26NOV05"""
    return ticker.rsplit("-", 1)[0]


def plan_orders(market, cities, now=None, log=print) -> list[dict]:
    """Longshot brackets to buy right now in the cities given (public data only)."""
    from .kalshi import event_date
    from .longshot import VARIANTS, longshot_signal
    cfg = replace(VARIANTS["longshot_5pm"], contracts=CONTRACTS)
    now = now or datetime.now(timezone.utc)
    plan = []
    for city in cities:
        target = now.astimezone(ZoneInfo(city.tz)).date() + timedelta(days=1)
        brackets = [b for b in market.markets(city.series, status="open") if event_date(b.event_ticker) == target]
        found = []
        for b in brackets:
            q = market.orderbook(b.ticker)
            sig = longshot_signal(b.ticker, b.event_ticker, q["yes_bid"], cfg, q["yes_bid_qty"],
                                  yes_ask=q.get("yes_ask"))
            if sig is None:
                continue
            cents = int(round(sig.price * 100))
            if not MIN_NO_CENTS <= cents <= MAX_NO_CENTS:
                continue
            found.append({"city": city.key, "ticker": b.ticker, "no_cents": cents,
                          "count": min(sig.contracts, CONTRACTS), "fee": sig.fee})
        found.sort(key=lambda p: -p["no_cents"])            # safest (cheapest YES) first
        plan += found[:MAX_ORDERS_PER_CITY_DAY]
        log(f"[{city.key}] {target}: {min(len(found), MAX_ORDERS_PER_CITY_DAY)} longshot bracket(s)")
    plan.sort(key=lambda p: -p["no_cents"])                 # if the cash cap binds, safest go first
    return plan


def _city_of(ticker: str):
    series = ticker.split("-", 1)[0]
    return next((CITIES[k] for k in LIVE_CITIES if CITIES[k].series == series), None)


def _is_no_buy(f: dict) -> bool:
    return f.get("book_side") == "ask" or (
        not f.get("book_side") and (f.get("outcome_side") or f.get("side") or "").lower() == "no"
        and (f.get("action") or "buy").lower() == "buy")


def _no_price(f: dict) -> float:
    p = _num(f, "no_price_dollars") or _num(f, "no_price") / 100.0
    if not p and (f.get("yes_price_dollars") or f.get("yes_price")):
        p = 1 - (_num(f, "yes_price_dollars") or _num(f, "yes_price") / 100.0)
    return p


def added_tickers(fills: list[dict]) -> set[str]:
    """Tickers already bought again: any NO buy below 97c (first entries are 98-99c)."""
    return {f.get("ticker") or f.get("market_ticker") for f in fills
            if _is_no_buy(f) and 0 < _no_price(f) < 0.97}


def manage(mode: str, market, auth: AuthClient, now=None, start_equity: float | None = None,
           log=print) -> dict:
    """Late sell and buy-again on open NO positions in the live cities. Runs any hour."""
    from .kalshi import event_date
    from .strategy import taker_fee
    now = now or datetime.now(timezone.utc)
    out = {"sold": [], "added": [], "skipped": []}
    positions, orders = auth.positions(), auth.resting_orders()
    resting = {o.get("ticker") for o in orders}
    cash, tied = auth.balance(), open_cost(positions)
    stopped = start_equity is not None and cash + tied < start_equity - STOP_LOSS
    added = None
    for p in positions:
        t = p.get("ticker") or ""
        n = -int(round(float(p.get("position") or p.get("position_fp") or 0)))
        city = _city_of(t)
        if n <= 0 or city is None or t in resting:
            continue
        d = event_date(event_of(t))
        local = now.astimezone(ZoneInfo(city.tz))
        if d is None or local.date() > d:
            continue                               # event day over: wait for settlement
        try:
            q = market.orderbook(t)
        except Exception as e:
            log(f"manage {t}: no quote ({type(e).__name__})")
            continue
        ya, yb = q.get("yes_ask"), q.get("yes_bid")
        if (local.date() == d and local.hour >= STOP_FROM_HOUR and ya is not None
                and yb is not None and yb >= STOP_MIN_YES_BID - 1e-9):
            no_bid = 100 - int(round(ya * 100))
            if no_bid < STOP_NO_CENTS:
                try:
                    o = auth.sell_no(t, no_bid, n)
                except LiveError as e:
                    out["skipped"].append((t, "sell error"))
                    log(f"SELL ERROR {t}: {e}")
                    continue
                out["sold"].append({"ticker": t, "count": n, "no_cents": no_bid, "status": o.get("status")})
                log(f"LATE SELL {mode.upper()} {n}x NO {t} @ {no_bid}c -> {o.get('status')}")
                continue
        if yb is None or stopped:
            continue
        no_ask = 100 - int(round(yb * 100))
        if not ADD_MIN_NO <= no_ask <= ADD_MAX_NO:
            continue
        if added is None:
            added = added_tickers(auth.fills())
        if t in added:
            continue
        count = min(ADD_CONTRACTS, int(q.get("yes_bid_qty") or 0))
        if count <= 0:
            continue
        cost = count * no_ask / 100.0 + taker_fee(no_ask / 100.0, count)
        why = ("open-cost cap" if tied + cost > MAX_OPEN_COST else
               "not enough cash" if cash < cost + 1.0 else None)
        if why:
            out["skipped"].append((t, why))
            log(f"skip buy-again {t}: {why}")
            continue
        try:
            o = auth.buy_no(t, no_ask, count)
        except LiveError as e:
            out["skipped"].append((t, "buy-again error"))
            log(f"BUY-AGAIN ERROR {t}: {e}")
            continue
        added.add(t)
        tied, cash = tied + cost, cash - cost
        out["added"].append({"ticker": t, "count": count, "no_cents": no_ask, "status": o.get("status")})
        log(f"BUY AGAIN {mode.upper()} {count}x NO {t} @ {no_ask}c -> {o.get('status')}")
    lines = ([f"SOLD {x['count']}x NO {x['ticker']} @ {x['no_cents']}c ({x['status']})" for x in out["sold"]]
             + [f"BOUGHT AGAIN {x['count']}x NO {x['ticker']} @ {x['no_cents']}c ({x['status']})"
                for x in out["added"]])
    if lines:
        telegram(f"Kalshi {mode} bot managed {len(lines)} position(s):\n" + "\n".join(lines))
    return out


def run(mode: str, market, auth: AuthClient | None = None, cities=None, now=None,
        start_equity: float | None = None, log=print) -> dict:
    """One pass: plan, apply every safety check, place orders (unless mode is off)."""
    cities = cities if cities is not None else [CITIES[k] for k in LIVE_CITIES]
    plan = plan_orders(market, cities, now=now, log=log)
    result = {"mode": mode, "planned": len(plan), "placed": [], "skipped": [], "stopped": None}
    if mode == "off":
        for p in plan:
            log(f"DRY RUN would buy {p['count']}x NO {p['ticker']} @ {p['no_cents']}c")
        return result
    if auth is None:
        raise LiveError("auth client required outside dry-run mode")
    if start_equity is None:
        raise LiveError("set the LIVE_START_EQUITY repository variable (e.g. 150)")
    positions, orders = auth.positions(), auth.resting_orders()
    cash, tied = auth.balance(), open_cost(positions)
    equity = cash + tied
    result.update(equity=round(equity, 2), cash=round(cash, 2), open_cost=tied)
    if equity < start_equity - STOP_LOSS:
        result["stopped"] = f"equity ${equity:.2f} is more than ${STOP_LOSS:.0f} below start ${start_equity:.2f}"
        log("STOP: loss limit reached; no new orders")
        if plan:
            telegram(f"Kalshi live bot STOPPED: {result['stopped']}. {len(plan)} order(s) skipped. "
                     f"Review, then raise LIVE_START_EQUITY to re-arm.")
        return result
    held = held_tickers(positions, orders)
    errors = []
    per_event = {}
    for t in held:
        per_event[event_of(t)] = per_event.get(event_of(t), 0) + 1
    for p in plan:
        cost = p["count"] * p["no_cents"] / 100.0 + p["fee"]
        ev = event_of(p["ticker"])
        why = ("already held" if p["ticker"] in held else
               "city-day cap" if per_event.get(ev, 0) >= MAX_ORDERS_PER_CITY_DAY else
               "open-cost cap" if tied + cost > MAX_OPEN_COST else
               "not enough cash" if cash < cost + 1.0 else None)
        if why:
            result["skipped"].append((p["ticker"], why))
            log(f"skip {p['ticker']}: {why}")
            continue
        try:
            o = auth.buy_no(p["ticker"], p["no_cents"], p["count"])
        except LiveError as e:
            result["skipped"].append((p["ticker"], "order error"))
            errors.append(f"{p['ticker']}: {str(e)[:160]}")
            log(f"ORDER ERROR {p['ticker']}: {e}")
            continue
        held.add(p["ticker"])
        per_event[ev] = per_event.get(ev, 0) + 1
        tied, cash = tied + cost, cash - cost
        result["placed"].append({**p, "status": o.get("status"), "order_id": o.get("order_id")})
        log(f"ORDER {mode.upper()} buy {p['count']}x NO {p['ticker']} @ {p['no_cents']}c -> {o.get('status')}")
    if result["placed"]:
        lines = [f"{x['count']}x NO {x['ticker']} @ {x['no_cents']}c ({x['status']})" for x in result["placed"]]
        telegram(f"Kalshi {mode} bot placed {len(lines)} order(s):\n" + "\n".join(lines))
    if errors:      # one message per run, not one per order
        telegram(f"Kalshi {mode} bot: {len(errors)} order(s) failed, nothing bought for them.\n"
                 + "\n".join(errors[:3]) + (f"\n...and {len(errors) - 3} more" if len(errors) > 3 else ""))
    return result


def _num(d: dict, *names) -> float:
    for n in names:
        if d.get(n) not in (None, ""):
            return float(d[n])
    return 0.0


def _when(d: dict, *names) -> datetime | None:
    for n in names:
        v = d.get(n)
        if v in (None, ""):
            continue
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v, timezone.utc)
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return None


def bot_series() -> tuple[str, ...]:
    """Ticker prefixes of the markets this bot trades, e.g. 'KXHIGHNY-'."""
    return tuple(CITIES[k].series + "-" for k in LIVE_CITIES)


def trade_results(fills: list[dict], settlements: list[dict], now=None, only=None) -> dict:
    """Per-trade live results from Kalshi fills (cost) and settlements (payout).

    One trade = one settled market we bought NO in. P&L = payout - price paid - taker fee.
    `only`: ticker prefixes to count; anything else in the account (manual bets) is ignored.
    """
    from .strategy import taker_fee
    now = now or datetime.now(timezone.utc)
    day_ago = now - timedelta(days=1)
    cost, bought, sold, sold_at = {}, {}, {}, {}
    new_24h = set()
    for f in fills:
        if only and not (f.get("ticker") or f.get("market_ticker") or "").startswith(only):
            continue                     # not a market this bot trades (e.g. a manual bet)
        t = f.get("ticker") or f.get("market_ticker")
        if f.get("book_side") == "bid":  # a late sell: bought YES to close NO
            n = _num(f, "count_fp", "count")
            yes = _num(f, "yes_price_dollars") or _num(f, "yes_price") / 100.0
            fee = _num(f, "fee_cost")
            cost[t] = cost.get(t, 0.0) - (n * (1 - yes) - fee)
            sold[t] = sold.get(t, 0) + n
            w = _when(f, "created_time", "ts")
            if w and (t not in sold_at or w > sold_at[t]):
                sold_at[t] = w
            continue
        # Buying NO: on the V2 book that is a YES "ask"; older fills say side "no".
        if not (f.get("book_side") == "ask" or (f.get("outcome_side") or f.get("side") or "").lower() == "no"):
            continue
        n = _num(f, "count_fp", "count")
        price = _num(f, "no_price_dollars") or _num(f, "no_price") / 100.0
        fee = _num(f, "fee_cost") if f.get("fee_cost") not in (None, "") else (taker_fee(price, int(n)) if n else 0.0)
        t = f.get("ticker") or f.get("market_ticker")
        cost[t] = cost.get(t, 0.0) + n * price + fee
        bought[t] = bought.get(t, 0) + n
        w = _when(f, "created_time", "ts")
        if w and w >= day_ago:
            new_24h.add(t)
    rows = []
    for s in settlements:
        t = s.get("ticker") or s.get("market_ticker")
        if t not in cost:
            continue                     # not a trade this bot made
        result = (s.get("market_result") or s.get("result") or "").lower()
        payout = _num(s, "revenue_dollars") or _num(s, "revenue") / 100.0
        rows.append({"ticker": t, "lost": result == "yes", "pnl": round(payout - cost[t], 2),
                     "at": _when(s, "settled_time", "ts")})
    settled = {r["ticker"] for r in rows}
    for t, n in sold.items():            # fully sold early: counted as a loss when it closes
        if t not in settled and t in bought and n >= bought[t] - 1e-9:
            rows.append({"ticker": t, "lost": True, "pnl": round(-cost[t], 2), "at": sold_at.get(t)})
    recent = [r for r in rows if r["at"] and r["at"] >= day_ago]
    pack = lambda rs: {"settled": len(rs), "losses": sum(r["lost"] for r in rs),
                       "pnl": round(sum(r["pnl"] for r in rs), 2)}
    return {"day": {**pack(recent), "new": len(new_24h)}, "all": pack(rows)}


def status(mode: str, auth: AuthClient, start_equity: float | None) -> dict:
    """Account snapshot plus per-trade results, for the daily summary."""
    positions = auth.positions()
    cash, tied = auth.balance(), open_cost(positions)
    try:
        results = trade_results(auth.fills(), auth.settlements(), only=bot_series())
    except LiveError:
        results = None
    eq = cash + tied
    return {"mode": mode, "cash": round(cash, 2), "open_cost": tied, "equity": round(eq, 2),
            "open_positions": sum(1 for p in positions if float(p.get("position") or p.get("position_fp") or 0)),
            "results": results,
            "pnl": None if start_equity is None else round(eq - start_equity, 2),
            "stopped": start_equity is not None and eq < start_equity - STOP_LOSS}


def from_env() -> tuple[str, AuthClient | None, float | None]:
    mode = (os.environ.get("LIVE_MODE") or "off").strip().lower()
    se = os.environ.get("LIVE_START_EQUITY")
    start = float(se) if se not in (None, "") else None
    if mode == "off":
        return mode, None, start
    base, kid, pem = mode_settings(mode)
    return mode, AuthClient(base, kid, pem), start
