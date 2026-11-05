"""Real-money longshot trading on Kalshi, with hard safety limits.

Rule (same as the longshot_5pm paper variant): at 5pm local the day before, buy NO on
every bracket whose best YES bid is 1-4c, at 1 - YES bid, 10 contracts, in six cities.

Modes, set by the LIVE_MODE environment variable:
  off  (default) dry run: reads real prices, prints what it would buy, places nothing
  demo           Kalshi's demo exchange (fake money), keys KALSHI_DEMO_KEY_ID / KALSHI_DEMO_PRIVATE_KEY
  live           real money, keys KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY

Safety limits (all enforced here, not by settings):
  - 10 contracts per order max, NO price 96-99c only, limit orders that expire in 5 minutes
  - at most 3 orders per city per day, never a second order on a ticker already held
  - total money in open positions capped at $130
  - stop: no new orders once equity (cash + open positions at cost) falls $25 below
    LIVE_START_EQUITY. Raise LIVE_START_EQUITY by hand to re-arm after reviewing.
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
LIVE_HOUR = 17
CONTRACTS = 10
MAX_ORDERS_PER_CITY_DAY = 3
MAX_OPEN_COST = 130.0          # dollars tied up in open positions
MIN_NO_CENTS, MAX_NO_CENTS = 96, 99
STOP_LOSS = 25.0               # dollars below LIVE_START_EQUITY
ORDER_TTL = 300                # seconds a limit order may rest before it expires


class LiveError(RuntimeError):
    pass


class AuthClient:
    """Signed Kalshi trade-api v2 calls (RSA-PSS SHA256 over timestamp + METHOD + path)."""

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

    def resting_orders(self) -> list[dict]:
        return self.request("GET", "/portfolio/orders", params={"status": "resting", "limit": 200}).get("orders") or []

    def buy_no(self, ticker: str, no_cents: int, count: int) -> dict:
        body = {"ticker": ticker, "action": "buy", "side": "no", "type": "limit", "count": int(count),
                "no_price": int(no_cents), "client_order_id": f"kw-{uuid.uuid4().hex[:16]}",
                "expiration_ts": int(time.time()) + ORDER_TTL}
        return self.request("POST", "/portfolio/orders", body=body).get("order") or {}


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
        n = 0
        for b in brackets:
            q = market.orderbook(b.ticker)
            sig = longshot_signal(b.ticker, b.event_ticker, q["yes_bid"], cfg, q["yes_bid_qty"],
                                  yes_ask=q.get("yes_ask"))
            if sig is None:
                continue
            cents = int(round(sig.price * 100))
            if not MIN_NO_CENTS <= cents <= MAX_NO_CENTS or n >= MAX_ORDERS_PER_CITY_DAY:
                continue
            plan.append({"city": city.key, "ticker": b.ticker, "no_cents": cents,
                         "count": min(sig.contracts, CONTRACTS), "fee": sig.fee})
            n += 1
        log(f"[{city.key}] {target}: {n} longshot bracket(s)")
    return plan


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
    for p in plan:
        cost = p["count"] * p["no_cents"] / 100.0 + p["fee"]
        why = ("already held" if p["ticker"] in held else
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
            log(f"ORDER ERROR {p['ticker']}: {e}")
            telegram(f"Kalshi live bot order error on {p['ticker']}: {str(e)[:200]}")
            continue
        held.add(p["ticker"])
        tied, cash = tied + cost, cash - cost
        result["placed"].append({**p, "status": o.get("status"), "order_id": o.get("order_id")})
        log(f"ORDER {mode.upper()} buy {p['count']}x NO {p['ticker']} @ {p['no_cents']}c -> {o.get('status')}")
    if result["placed"]:
        lines = [f"{x['count']}x NO {x['ticker']} @ {x['no_cents']}c ({x['status']})" for x in result["placed"]]
        telegram(f"Kalshi {mode} bot placed {len(lines)} order(s):\n" + "\n".join(lines))
    return result


def status(mode: str, auth: AuthClient, start_equity: float | None) -> dict:
    """Account snapshot for the daily summary."""
    positions = auth.positions()
    cash, tied = auth.balance(), open_cost(positions)
    since = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp())
    try:
        settled = auth.request("GET", "/portfolio/settlements", params={"min_ts": since, "limit": 200}).get("settlements") or []
    except LiveError:
        settled = []
    eq = cash + tied
    return {"mode": mode, "cash": round(cash, 2), "open_cost": tied, "equity": round(eq, 2),
            "open_positions": sum(1 for p in positions if float(p.get("position") or p.get("position_fp") or 0)),
            "settled_24h": len(settled),
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
