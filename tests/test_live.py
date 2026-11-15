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

    def sell_no(self, ticker, cents, count):
        self.sold = getattr(self, "sold", []) + [(ticker, cents, count)]
        return {"status": "executed", "order_id": "s"}

    def fills(self):
        return getattr(self, "fill_list", [])


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


def test_ed25519_key_signs():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    h = live.AuthClient("https://api.elections.kalshi.com/trade-api/v2", "kid", pem).headers("GET", "/portfolio/balance")
    msg = (h["KALSHI-ACCESS-TIMESTAMP"] + "GET" + "/trade-api/v2/portfolio/balance").encode()
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), msg)


def test_dry_run_places_nothing():
    r = live.run("off", FakeMarket(books()), None, cities=nyc(), now=NOW, log=lambda *_: None)
    assert r["planned"] == 3 and r["placed"] == []      # 5 candidates, capped at 3 per city


def test_places_capped_orders_at_30_and_right_price():
    a = FakeAuth(cash=300.0)
    r = live.run("demo", FakeMarket(books()), a, cities=nyc(), now=NOW, start_equity=300, log=lambda *_: None)
    assert len(a.sent) == 3 and all(c == 98 and n == 30 for _, c, n in a.sent)
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
    held = [{"ticker": "OTHER", "position": -1600, "market_exposure_dollars": "1595.00"}]
    a = FakeAuth(cash=40, positions=held)
    r = live.run("live", FakeMarket(books()), a, cities=nyc(), now=NOW, start_equity=1600, log=lambda *_: None)
    assert a.sent == [] and all(why == "open-cost cap" for _, why in r["skipped"])


def test_loss_limit_stops_all_orders():
    a = FakeAuth(cash=249.0)       # equity 249 < 300 - 50
    r = live.run("live", FakeMarket(books()), a, cities=nyc(), now=NOW, start_equity=300, log=lambda *_: None)
    assert a.sent == [] and r["stopped"]
    a = FakeAuth(cash=251.0)
    assert live.run("live", FakeMarket(books()), a, cities=nyc(), now=NOW, start_equity=300,
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


def test_window_5pm_to_9pm_local():
    from datetime import timedelta
    six = [CITIES[k] for k in live.LIVE_CITIES]
    keys = lambda t: sorted(c.key for c in live.in_window(six, t))
    assert keys(NOW) == ["nyc", "phl"]                                  # 5:05pm ET, 4:05pm CT
    assert keys(NOW + timedelta(hours=3)) == ["aus", "chi", "den", "lax", "nyc", "phl"]  # 8:05pm ET
    assert keys(NOW + timedelta(hours=5)) == ["aus", "chi", "den", "lax"]  # 10:05pm ET: NY/PHL closed
    assert keys(NOW - timedelta(hours=1)) == []                         # 4:05pm ET


def test_city_day_cap_counts_earlier_runs():
    held = [{"ticker": f"{EV}-B0.5", "position": -10, "market_exposure": 980},
            {"ticker": f"{EV}-B1.5", "position": -10, "market_exposure": 980}]
    a = FakeAuth(positions=held)
    r = live.run("live", FakeMarket(books()), a, cities=nyc(), now=NOW, start_equity=150, log=lambda *_: None)
    assert [t for t, _, _ in a.sent] == [f"{EV}-B2.5"]                  # 2 held + 1 new = cap of 3


def test_v2_order_body_sells_yes_at_complement():
    class S:
        def request(self, method, url, params=None, json=None, headers=None, timeout=None):
            S.sent = (method, url, json)
            class R:
                status_code, content = 201, b"x"
                def json(self):
                    return {"order_id": "o1", "fill_count": "10.00", "remaining_count": "0.00"}
            return R()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    pem = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM,
                                                     serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption()).decode()
    c = live.AuthClient("https://api.elections.kalshi.com/trade-api/v2", "k", pem, session=S())
    o = c.buy_no("KXHIGHNY-26NOV05-B74.5", 98, 10)
    method, url, body = S.sent
    assert method == "POST" and url.endswith("/trade-api/v2/portfolio/events/orders")
    assert body["side"] == "ask" and body["price"] == "0.02" and body["count"] == "10"
    assert body["time_in_force"] == "good_till_canceled" and body["expiration_time"] > 0
    assert o["status"] == "filled 10" and o["order_id"] == "o1"


def test_errors_sent_as_one_message(monkeypatch):
    sent = []
    monkeypatch.setattr(live, "telegram", sent.append)
    a = FakeAuth(fail=(f"{EV}-B0.5", f"{EV}-B1.5", f"{EV}-B2.5"))
    live.run("live", FakeMarket(books()), a, cities=nyc(), now=NOW, start_equity=150, log=lambda *_: None)
    assert len(sent) == 1 and "3 order(s) failed" in sent[0]


def test_v2_fills_count_as_no_purchases():
    fills = [{"ticker": "A", "book_side": "ask", "outcome_side": "yes", "action": "sell", "count_fp": "10.00",
              "no_price_dollars": "0.9800", "yes_price_dollars": "0.0200", "fee_cost": "0.0200",
              "created_time": "2026-11-04T22:01:00Z"}]
    st = [{"ticker": "A", "market_result": "no", "revenue": 1000, "settled_time": "2026-11-05T12:00:00Z"}]
    r = live.trade_results(fills, st, now=NOW.replace(day=5, hour=13))
    assert r["all"] == {"settled": 1, "losses": 0, "pnl": 0.18}


def test_safest_brackets_first_per_city():
    b = {f"{EV}-B0.5": (0.04, 50, 0.05), f"{EV}-B1.5": (0.01, 50, 0.02),
         f"{EV}-B2.5": (0.03, 50, 0.04), f"{EV}-B3.5": (0.02, 50, 0.03)}
    plan = live.plan_orders(FakeMarket(b), nyc(), now=NOW, log=lambda *_: None)
    assert [p["no_cents"] for p in plan] == [99, 98]          # 3c and 4c YES (97c/96c NO) are dropped


def test_order_error_is_skipped_not_fatal():
    a = FakeAuth(fail=(f"{EV}-B0.5",))
    r = live.run("live", FakeMarket(books()), a, cities=nyc(), now=NOW, start_equity=150, log=lambda *_: None)
    assert len(a.sent) == 2 and (f"{EV}-B0.5", "order error") in r["skipped"]


# ---------- managing open positions: late sell and buy again ----------

EVENT_7PM = datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc)    # 7pm in New York on the event day
EVENT_3PM = datetime(2026, 11, 5, 20, 0, tzinfo=timezone.utc)   # 3pm on the event day
T = f"{EV}-B0.5"


def held(n=30):
    return [{"ticker": T, "position": -n, "market_exposure": n * 99}]


def manage(book, now, auth=None, start=1500):
    a = auth or FakeAuth(cash=1500, positions=held())
    r = live.manage("live", FakeMarket({T: book}), a, now=now, start_equity=start, log=lambda *_: None)
    return a, r


def test_late_sell_after_6pm_when_no_bid_below_70():
    a, r = manage((0.30, 40, 0.35), EVENT_7PM)          # NO bid 65c, YES bid 30c
    assert a.sold == [(T, 65, 30)] and r["sold"][0]["no_cents"] == 65 and not a.sent


def test_no_late_sell_before_6pm_or_on_entry_evening():
    a, r = manage((0.30, 40, 0.35), EVENT_3PM)
    assert not getattr(a, "sold", []) and not r["sold"]
    a, r = manage((0.30, 40, 0.35), NOW)                   # evening before the event day
    assert not getattr(a, "sold", [])


def test_no_late_sell_on_an_empty_book():
    a, r = manage((0.02, 5, 0.40), EVENT_7PM)            # NO bid 60c but YES bid only 2c
    assert not getattr(a, "sold", [])


def test_buy_again_once_in_85_93_band():
    a, r = manage((0.10, 50, 0.12), EVENT_3PM)           # NO ask 90c
    assert a.sent == [(T, 90, 30)] and r["added"][0]["count"] == 30
    a2 = FakeAuth(cash=1500, positions=held(60))
    a2.fill_list = [{"ticker": T, "book_side": "ask", "no_price_dollars": "0.9000", "count_fp": "30"}]
    a2, r = manage((0.10, 50, 0.12), EVENT_3PM, auth=a2)
    assert not a2.sent                                    # already bought again


def test_buy_again_outside_band_or_thin_book():
    a, _ = manage((0.03, 50, 0.05), EVENT_3PM)           # NO ask 97c: no dip
    assert not a.sent
    a, _ = manage((0.20, 50, 0.22), EVENT_3PM)           # NO ask 80c: below the band
    assert not a.sent
    a, _ = manage((0.10, 12, 0.12), EVENT_3PM)           # only 12 offered
    assert a.sent == [(T, 90, 12)]


def test_buy_again_respects_stop_cap_and_cash():
    a, _ = manage((0.10, 50, 0.12), EVENT_3PM, start=2000)          # equity far below start: stopped
    assert not a.sent
    big = FakeAuth(cash=1500, positions=held() + [{"ticker": "X", "position": -10, "market_exposure": 159000}])
    a, r = manage((0.10, 50, 0.12), EVENT_3PM, auth=big)
    assert not a.sent and r["skipped"] == [(T, "open-cost cap")]
    poor = FakeAuth(cash=10, positions=held())
    a, r = manage((0.10, 50, 0.12), EVENT_3PM, auth=poor, start=0)
    assert not a.sent and r["skipped"] == [(T, "not enough cash")]


def test_manage_skips_resting_orders_other_series_and_finished_days():
    a = FakeAuth(cash=1500, positions=held(), orders=[{"ticker": T}])
    a, _ = manage((0.10, 50, 0.12), EVENT_3PM, auth=a)
    assert not a.sent
    other = FakeAuth(cash=1500, positions=[{"ticker": "KXHIGHMIA-26NOV05-B80.5", "position": -30}])
    other, _ = manage((0.10, 50, 0.12), EVENT_3PM, auth=other)
    assert not other.sent
    a, _ = manage((0.30, 40, 0.35), datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc))  # day after
    assert not getattr(a, "sold", []) and not a.sent


def test_sell_order_buys_yes_at_complement():
    class S:
        def request(self, method, url, params=None, json=None, headers=None, timeout=None):
            S.sent = json
            class R:
                status_code, content = 201, b"x"
                def json(self):
                    return {"order_id": "o2", "fill_count": "30.00", "remaining_count": "0.00"}
            return R()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    pem = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM,
                                                     serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption()).decode()
    c = live.AuthClient("https://api.elections.kalshi.com/trade-api/v2", "k", pem, session=S())
    o = c.sell_no(T, 65, 30)
    assert S.sent["side"] == "bid" and S.sent["price"] == "0.35" and S.sent["count"] == "30"
    assert o["status"] == "filled 30"


def test_results_count_a_late_sell():
    fills = [{"ticker": T, "book_side": "ask", "count_fp": "30", "no_price_dollars": "0.99",
              "fee_cost": "0.03", "created_time": "2026-11-04T22:01:00Z"},
             {"ticker": T, "book_side": "bid", "count_fp": "30", "yes_price_dollars": "0.35",
              "fee_cost": "0.48", "created_time": "2026-11-06T00:05:00Z"}]
    r = live.trade_results(fills, [], now=datetime(2026, 11, 6, 1, 0, tzinfo=timezone.utc))
    # paid 29.70 + 0.03, got back 30 x 0.65 - 0.48 = 19.02 -> -10.71
    assert r["all"] == {"settled": 1, "losses": 1, "pnl": -10.71} and r["day"]["settled"] == 1
