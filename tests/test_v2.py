"""v2 paper tests: base rule, maker, early window, forecast guard, and the R2/R3 YES guards."""
from datetime import date, datetime, timedelta, timezone

from kalshi_weather import v2
from kalshi_weather.kalshi import Bracket
from kalshi_weather.ledger import Ledger, yes_guard
from kalshi_weather.strategy import Signal

NOW = datetime(2026, 11, 6, 22, 5, tzinfo=timezone.utc)      # 5:05pm New York
EV = "KXHIGHNY-26NOV07"


def br(t, floor, cap, st="between", open_time=None, ev=EV):
    return Bracket(f"{ev}-{t}", ev, st, floor, cap, None, None, None, None, "", open_time)


class FakeK:
    def __init__(self, books, brackets):
        self.books, self.brackets = books, brackets

    def markets(self, series, status=None):
        return self.brackets if series == "KXHIGHNY" else []

    def orderbook(self, ticker):
        bid, qty, ask = self.books[ticker]
        return {"yes_bid": bid, "yes_bid_qty": qty, "yes_ask": ask, "yes_ask_qty": 50}


class FakeW:
    def __init__(self, high):
        self.high = high

    def forecast_highs(self, city, days=3):
        return {date(2026, 11, 7): self.high, date(2026, 11, 8): self.high}


def world():
    brs = [br("B80.5", 80, 81), br("B78.5", 78, 79), br("B76.5", 76, 77), br("B74.5", 74, 75),
           br("B60.5", 60, 61), br("B66.5", 66, 67)]
    books = {f"{EV}-B80.5": (0.01, 50, 0.03), f"{EV}-B78.5": (0.02, 50, 0.04),
             f"{EV}-B76.5": (0.03, 50, 0.05),      # 3c: outside v2
             f"{EV}-B74.5": (0.02, 50, 0.65),      # ask 65c: no real market
             f"{EV}-B60.5": (0.01, 50, 0.02),      # below the forecast, 1c spread (no maker room)
             f"{EV}-B66.5": (0.20, 50, 0.25)}
    return FakeK(books, brs)


def strat(led, name):
    return sorted(r["ticker"].split("-")[-1] for r in
                  led.db.execute("SELECT ticker FROM trades WHERE strategy=?", (name,)))


def test_markets_exclude_miami_and_lows():
    assert "high_mia" not in v2.V2_MARKETS and all(k.startswith("high_") for k in v2.V2_MARKETS)
    assert {"high_nyc", "high_sfo", "high_atl"} <= set(v2.V2_MARKETS)
    assert all(v2.forecast_city(k) is not None for k in v2.V2_MARKETS)


def test_window_base_rule_and_cap(tmp_path):
    led = Ledger(str(tmp_path / "p.db"))
    placed = v2.run_v2(world(), led, FakeW(70), now=NOW, markets=("high_nyc",), log=lambda *_: None)
    # 1-2c with a real ask: B80.5 (1c), B60.5 (1c), B78.5 (2c); safest first, cap 3
    assert strat(led, "v2_taker") == ["B60.5", "B78.5", "B80.5"]
    assert placed["v2_taker"] == 3
    # runs again an hour later: nothing new (already held, cap reached)
    v2.run_v2(world(), led, FakeW(70), now=NOW + timedelta(hours=1), markets=("high_nyc",), log=lambda *_: None)
    assert len(strat(led, "v2_taker")) == 3


def test_outside_window_no_trades(tmp_path):
    led = Ledger(str(tmp_path / "p.db"))
    p = v2.run_v2(world(), led, FakeW(70), now=NOW - timedelta(hours=3), markets=("high_nyc",), log=lambda *_: None)
    assert p["v2_taker"] == 0 and p["v2_maker"] == 0      # 2:05pm New York


def test_maker_rests_one_cent_under_ask(tmp_path):
    led = Ledger(str(tmp_path / "p.db"))
    v2.run_v2(world(), led, FakeW(70), now=NOW, markets=("high_nyc",), log=lambda *_: None)
    rows = {r["ticker"].split("-")[-1]: (r["price"], r["status"]) for r in
            led.db.execute("SELECT * FROM trades WHERE strategy='v2_maker'")}
    assert rows == {"B80.5": (0.98, "resting"), "B78.5": (0.97, "resting")}   # B60.5: no room in spread


def test_forecast_guard_skips_brackets_the_forecast_moved_toward(tmp_path):
    led = Ledger(str(tmp_path / "p.db"))
    # morning snapshot: 70F for tomorrow
    v2.run_v2(world(), led, FakeW(70), now=NOW - timedelta(hours=8), markets=("high_nyc",), log=lambda *_: None)
    # 5pm: forecast up 2F, toward the 78-81 brackets, away from 60-61
    v2.run_v2(world(), led, FakeW(72), now=NOW, markets=("high_nyc",), log=lambda *_: None)
    assert strat(led, "v2_guard") == ["B60.5"]
    assert strat(led, "v2_taker") == ["B60.5", "B78.5", "B80.5"]
    assert led.db.execute("SELECT forecast FROM trades WHERE strategy='v2_guard'").fetchone()[0] == 72


def test_moved_toward():
    up = br("B80.5", 80, 81)
    assert v2.moved_toward(up, 72, 70) and not v2.moved_toward(up, 70.5, 70)
    assert not v2.moved_toward(up, 68, 70)
    down = br("B60.5", 60, 61)
    assert v2.moved_toward(down, 68, 70) and not v2.moved_toward(down, 72, 70)
    assert not v2.moved_toward(up, 72, None)


def test_early_window_uses_open_time(tmp_path):
    ev2 = "KXHIGHNY-26NOV08"
    opened = (NOW - timedelta(hours=1)).isoformat()
    old = (NOW - timedelta(hours=5)).isoformat()
    brs = [br("B90.5", 90, 91, open_time=opened, ev=ev2), br("B50.5", 50, 51, open_time=old, ev=ev2)]
    k = FakeK({f"{ev2}-B90.5": (0.01, 50, 0.03), f"{ev2}-B50.5": (0.01, 50, 0.03)}, brs)
    led = Ledger(str(tmp_path / "p.db"))
    v2.run_v2(k, led, FakeW(70), now=NOW - timedelta(hours=10), markets=("high_nyc",), log=lambda *_: None)
    assert strat(led, "v2_early") == []                   # 7:05am: market not open yet
    v2.run_v2(k, led, FakeW(70), now=NOW, markets=("high_nyc",), log=lambda *_: None)
    assert strat(led, "v2_early") == ["B90.5"]            # opened 1h ago; the other 5h ago


def test_brain_stopped_strategy_places_nothing(tmp_path):
    led = Ledger(str(tmp_path / "p.db"))
    p = v2.run_v2(world(), led, FakeW(70), sizes={"v2_taker": 0}, now=NOW, markets=("high_nyc",),
                  log=lambda *_: None)
    assert p["v2_taker"] == 0 and p["v2_guard"] == 3


def test_forecast_outage_does_not_stop_trading(tmp_path):
    class Down:
        def forecast_highs(self, city, days=3):
            raise RuntimeError("Open-Meteo down")
    led = Ledger(str(tmp_path / "p.db"))
    p = v2.run_v2(world(), led, Down(), now=NOW, markets=("high_nyc",), log=lambda *_: None)
    assert p["v2_taker"] == 3 and p["v2_guard"] == 3      # guard can't fire without forecasts


def sig(side, price, prob, edge):
    return Signal("T", "E", side, price, prob, edge, 10, 0.01)


def test_yes_guards_r2_r3():
    assert yes_guard(sig("no", 0.98, 0.98, 0.0), 0, 0) is None              # NO never blocked
    assert yes_guard(sig("yes", 0.05, 0.08, 0.03), 0, 0).startswith("R2")   # cheap YES
    assert yes_guard(sig("yes", 0.05, 0.15, 0.09), 0, 0) is None            # R2 exception
    assert yes_guard(sig("yes", 0.40, 0.45, 0.05), 0, 0).startswith("R3")   # edge < 8c
    assert yes_guard(sig("yes", 0.40, 0.50, 0.09), 9, 4).startswith("R3")   # 5/10 = 50% YES
    assert yes_guard(sig("yes", 0.40, 0.50, 0.09), 9, 1) is None


def test_ledger_refuses_blocked_yes(tmp_path):
    led = Ledger(str(tmp_path / "p.db"))
    assert led.record("nyc", sig("yes", 0.05, 0.08, 0.03)) is None
    assert led.record("nyc", sig("no", 0.98, 0.98, 0.0)) is not None
    assert led.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
