"""Live trading: signing and every safety check, with fake exchanges (no network)."""
import base64
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_weather import live
from kalshi_weather.config import CITIES
from kalshi_weather.kalshi import Bracket

NOW = datetime(2026, 11, 4, 22, 5, tzinfo=timezone.utc)   # 5:05pm in New York
EV = "KXHIGHNY-26NOV05"


class FakeMarket:
    def __init__(self, books):
        self.books = books     # ticker -> (yes_bid, qty, yes_ask)

    def markets(self, series, status=None):
        if series != "KXHIGHNY":
            return []
        return [Bracket(t, EV, "between", 1, 2, None, None, None, None) for t in self.books]

    def orderbook(self, ticker):
        bid, qty, ask = self.books[ticker]
        return {"yes_bid": bid, "yes_bid_qty": qty, "yes_ask": ask, "yes_ask_qty": 100}


class FakeAuth:
    def __init__(self, cash=150.0, positions=None, orders=None, fail=()):
        self.cash, self.pos, self.orders, self.fail, self.sent = cash, positions or [], orders or [], fail, []

    def balance(self):
        return self.cash

    def positions(self):
        return self.pos

    def resting_orders(self):
        return self.orders

    def buy_no(self, ticker, cents, count):
        if ticker in self.fail:
            raise live.LiveError("HTTP 400")
        self.sent.append((ticker, cents, count))
        return {"status": "executed", "order_id": "x"}


@pytest.fixture(autouse=True)
def no_telegram(monkeypatch):
    monkeypatch.setattr(live, "telegram", lambda text: None)


def nyc():
    return [CITIES["nyc"]]


def books(**kw):
    base = {f"{EV}-B{i}.5": (0.02, 50, 0.04) for i in range(5)}
    base.update(kw)
    return base


def test_signature_verifies_with_public_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    c = live.AuthClient("https://demo-api.kalshi.co/trade-api/v2", "kid", pem)
    h = c.headers("POST", "/portfolio/orders?x=1")
    msg = (h["KALSHI-ACCESS-TIMESTAMP"] + "POST" + "/trade-api/v2/portfolio/orders").encode()
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), msg,
                            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                        salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    assert h["KALSHI-ACCESS-KEY"] == "kid"
    # secrets pasted with literal \n still load
    live.AuthClient("https://x/trade-api/v2", "k", pem.replace("\n", "\\n"))


def test_dry_run_places_nothing():
    r = live.run("off", FakeMarket(books()), None, cities=nyc(), now=NOW, log=lambda *_: None)
    assert r["planned"] == 3 and r["placed"] == []      # 5 candidates, capped at 3 per city


def test_places_capped_orders_at_10_and_right_price():
    a = FakeAuth()
    r = live.run("demo", FakeMarket(books()), a, cities=nyc(), now=NOW, start_equity=150, log=lambda *_: None)
    assert len(a.sent) == 3 and all(c == 98 and n == 10 for _, c, n in a.sent)
    assert len(r["placed"]) == 3


def test_price_guard_and_thin_books():
    b = {f"{EV}-B0.5": (0.05, 50, 0.06),     # YES bid 5c: outside the rule
         f"{EV}-B1.5": (0.01, 3, 0.02),      # only 3 available -> 3 contracts at 99c
         f"{EV}-B2.5": (None, 0, 0.02)}      # no bid
    a = FakeAuth()
    live.run("live", FakeMarket(b), a, cities=nyc(), now=NOW, start_equity=150, log=lambda *_: None)
    assert a.sent == [(f"{EV}-B1.5", 99, 3)]


def test_never_doubles_a_held_ticker():
    held = [{"ticker": f"{EV}-B0.5", "position": -10, "market_exposure": 980}]
    resting = [{"ticker": f"{EV}-B1.5"}]
    a = FakeAuth(positions=held, orders=resting)
    r = live.run("live", FakeMarket(books()), a, cities=nyc(), now=NOW, start_equity=150, log=lambda *_: None)
    assert {t for t, _, _ in a.sent} == {f"{EV}-B2.5"}
    assert {t for t, why in r["skipped"]} == {f"{EV}-B0.5", f"{EV}-B1.5"}


def test_open_cost_cap():
    held = [{"ticker": "OTHER", "position": -128, "market_exposure_dollars": "125.00"}]
    a = FakeAuth(cash=40, positions=held)
    r = live.run("live", FakeMarket(books()), a, cities=nyc(), now=NOW, start_equity=150, log=lambda *_: None)
    assert a.sent == [] and all(why == "open-cost cap" for _, why in r["skipped"])


def test_loss_limit_stops_all_orders():
    a = FakeAuth(cash=124.0)       # equity 124 < 150 - 25
    r = live.run("live", FakeMarket(books()), a, cities=nyc(), now=NOW, start_equity=150, log=lambda *_: None)
    assert a.sent == [] and r["stopped"]
    a = FakeAuth(cash=126.0)
    assert live.run("live", FakeMarket(books()), a, cities=nyc(), now=NOW, start_equity=150,
                    log=lambda *_: None)["stopped"] is None


def test_requires_start_equity_and_keys(monkeypatch):
    with pytest.raises(live.LiveError):
        live.run("live", FakeMarket(books()), FakeAuth(), cities=nyc(), now=NOW, log=lambda *_: None)
    for k in ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LIVE_MODE", "live")
    with pytest.raises(live.LiveError):
        live.from_env()
    monkeypatch.setenv("LIVE_MODE", "off")
    assert live.from_env()[1] is None


def test_order_error_is_skipped_not_fatal():
    a = FakeAuth(fail=(f"{EV}-B0.5",))
    r = live.run("live", FakeMarket(books()), a, cities=nyc(), now=NOW, start_equity=150, log=lambda *_: None)
    assert len(a.sent) == 2 and (f"{EV}-B0.5", "order error") in r["skipped"]
