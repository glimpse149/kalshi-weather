"""Exit-rule backtest: selling early and buying again, on made-up hourly paths."""
from kalshi_weather import exits
from kalshi_weather.strategy import taker_fee

END = 100 * 3600   # event day ends at hour 100


def trade(no_bids, result="no", entry_bid=0.02):
    """Path of hourly NO bids; YES bid = YES ask - 1c (a 1c spread)."""
    path = []
    for h, nb in enumerate(no_bids):
        ya = round(1 - nb, 2)
        path.append((END - (len(no_bids) - h) * 3600, max(ya - 0.01, 0.0), ya))
    return {"market": "high_nyc", "ticker": "T", "date": "2026-11-12", "entry_bid": entry_bid,
            "result": result, "day_end": END, "path": path}


def test_hold_win_and_loss():
    win = exits.simulate(trade([0.98, 0.97, 0.99]))
    assert win["pnl"] == round(30 - 0.98 * 30 - taker_fee(0.98, 30), 2) and win["stopped"] is None
    loss = exits.simulate(trade([0.9, 0.5, 0.01], result="yes"))
    assert loss["pnl"] == round(-0.98 * 30 - taker_fee(0.98, 30), 2)


def test_stop_sells_first_hour_below_threshold():
    r = exits.simulate(trade([0.95, 0.85, 0.78, 0.40, 0.0], result="yes"), stop=0.80)
    assert r["stopped"] == 0.78
    assert r["pnl"] == round(-0.98 * 30 - taker_fee(0.98, 30) + 0.78 * 30 - taker_fee(0.78, 30), 2)


def test_stop_on_a_winner_that_dipped_costs_money():
    r = exits.simulate(trade([0.95, 0.75, 0.97, 0.99]), stop=0.80)
    assert r["won"] and r["stopped"] == 0.75 and r["pnl"] < 0


def test_add_only_in_last_hours_and_in_band():
    # NO bids 0.90 early (outside last 3h) then 0.89 in the last 3h -> NO ask = bid + 1c = 0.90
    t = trade([0.90, 0.95, 0.97, 0.89, 0.99, 0.99])
    r = exits.simulate(t, add=(0.85, 0.93, 3))
    assert r["added"] == 0.90
    exp = (-0.98 * 30 - taker_fee(0.98, 30)) + (-0.90 * 30 - taker_fee(0.90, 30)) + 60
    assert r["pnl"] == round(exp, 2)
    assert exits.simulate(t, add=(0.85, 0.93, 1))["added"] is None


def test_stop_after_add_sells_everything():
    r = exits.simulate(trade([0.91, 0.70, 0.0], result="yes"), stop=0.80, add=(0.85, 0.93, None))
    assert r["added"] == 0.92 and r["stopped"] == 0.70
    exp = (-0.98 * 30 - taker_fee(0.98, 30)) + (-0.92 * 30 - taker_fee(0.92, 30)) + 0.70 * 60 - taker_fee(0.70, 60)
    assert r["pnl"] == round(exp, 2)


def test_report_groups_and_six_city_losses():
    ts = [trade([0.98, 0.99]), trade([0.9, 0.6, 0.0], result="yes")]
    ts.append(dict(trade([0.97, 0.99]), market="low_chi"))
    text = exits.report(ts, 120)
    assert "## Your 6 live cities" in text and "2 trades, 1 losses" in text
    assert "## Daily lows (24 markets)" in text and "| Sell if NO bid < 80c |" in text
    assert "Losses, hourly NO bid after entry" in text


def test_collect_market_keeps_only_1_2c_entries(monkeypatch):
    from datetime import date, datetime, time
    from zoneinfo import ZoneInfo
    from kalshi_weather.kalshi import Bracket
    ny = ZoneInfo("America/New_York")
    five = int(datetime.combine(date(2026, 11, 11), time(17), ny).timestamp())

    class K:
        def __init__(self, *a, **k):
            pass

        def settled_markets(self, series, **kw):
            mk = lambda t, r: Bracket(f"KXHIGHNY-26NOV12-{t}", "KXHIGHNY-26NOV12", "between", 1, 2, r,
                                      "2026-11-13T05:00:00Z", None, None)
            return [mk("B61.5", "yes"), mk("B70.5", "no")]

        def candles(self, series, ticker, s, e, period=60, close_time=None):
            bid = 0.02 if ticker.endswith("B61.5") else 0.30
            return [{"ts": five - 600, "yes_bid": bid, "yes_ask": bid + 0.01},
                    {"ts": five + 3600, "yes_bid": 0.14, "yes_ask": 0.15},
                    {"ts": five + 20 * 3600, "yes_bid": 0.98, "yes_ask": 0.99}]

    monkeypatch.setattr(exits, "KalshiClient", K)
    out = exits.collect_market("high_nyc", date(2026, 11, 1), date(2026, 11, 13), None, log=lambda *_: None)
    assert [t["ticker"] for t in out] == ["KXHIGHNY-26NOV12-B61.5"]
    assert out[0]["entry_bid"] == 0.02 and len(out[0]["path"]) == 2
    assert exits.simulate(out[0], stop=0.80)["stopped"] == 0.01


def test_late_stop_ignores_early_dips():
    # dip to 0.70 early, then recovers; late stop (last 2h) never fires
    t = trade([0.95, 0.70, 0.97, 0.98, 0.99])
    assert exits.simulate(t, stop=0.80)["stopped"] == 0.70
    r = exits.simulate(t, stop=0.80, stop_last=2)
    assert r["stopped"] is None and r["pnl"] > 0
    # a late crash is still caught
    late = trade([0.97, 0.98, 0.99, 0.75, 0.10], result="yes")
    assert exits.simulate(late, stop=0.80, stop_last=2)["stopped"] == 0.75


def test_report_six_only_skips_whole_market():
    text = exits.report([trade([0.98, 0.99])], 120, keys=exits.LIVE_SIX)
    assert "## Your 6 live cities" in text and "Whole market" not in text
    assert "last 6h only" in text


def test_late_stop_plus_add_sells_all_60():
    # dip to 0.90 early (add 30 more), then a late crash below 70 sells all 60
    t = trade([0.97, 0.89, 0.95, 0.97, 0.60, 0.05], result="yes")
    r = exits.simulate(t, stop=0.70, add=(0.85, 0.93, None), stop_last=2)
    assert r["added"] == 0.90 and r["stopped"] == 0.60
    exp = (-0.98 * 30 - taker_fee(0.98, 30)) + (-0.90 * 30 - taker_fee(0.90, 30)) + 0.60 * 60 - taker_fee(0.60, 60)
    assert r["pnl"] == round(exp, 2)
    assert "last 6h only + buy again" in exits.report([t], 120, keys=exits.LIVE_SIX)


def test_flip_sells_no_and_holds_yes():
    # NO bid falls to 0.50 (YES ask 0.50): close 30 NO at 0.50, buy 30 YES at 0.50
    lose = exits.simulate(trade([0.95, 0.50, 0.05], result="yes"), stop=0.60, flip=True)
    win = exits.simulate(trade([0.95, 0.50, 0.99]), stop=0.60, flip=True)
    base = -0.98 * 30 - taker_fee(0.98, 30) + 0.50 * 30 - taker_fee(0.50, 30) - 0.50 * 30 - taker_fee(0.50, 30)
    assert lose["pnl"] == round(base + 30, 2) and win["pnl"] == round(base, 2)
    assert "Flip: NO bid < 50c" in exits.report([trade([0.98, 0.99])], 120, keys=exits.LIVE_SIX)
