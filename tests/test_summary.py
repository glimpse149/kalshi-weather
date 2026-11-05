"""Daily summary: live per-trade results and the message layout."""
from datetime import datetime, timezone

from kalshi_weather import live, summary

NOW = datetime(2026, 11, 6, 13, 0, tzinfo=timezone.utc)


def test_trade_results_wins_losses_and_24h_window():
    fills = [
        {"ticker": "A", "action": "buy", "side": "no", "count": 10, "no_price": 98, "created_time": "2026-11-04T22:01:00Z"},
        {"ticker": "B", "action": "buy", "side": "no", "count": 10, "no_price": 97, "created_time": "2026-11-05T22:01:00Z"},
        {"ticker": "C", "action": "buy", "side": "no", "count": 10, "no_price": 99, "created_time": "2026-11-05T22:02:00Z"},
    ]
    settlements = [
        {"ticker": "A", "market_result": "no", "revenue": 1000, "settled_time": "2026-11-05T12:00:00Z"},   # old win
        {"ticker": "B", "market_result": "yes", "revenue": 0, "settled_time": "2026-11-06T12:00:00Z"},    # recent loss
        {"ticker": "X", "market_result": "no", "revenue": 500, "settled_time": "2026-11-06T12:00:00Z"},   # not ours
    ]
    r = live.trade_results(fills, settlements, now=NOW)
    # A: 10 - 9.80 - fee 0.02 = +0.18 ; B: 0 - 9.70 - fee 0.03 = -9.73
    assert r["all"] == {"settled": 2, "losses": 1, "pnl": -9.55}
    assert r["day"] == {"settled": 1, "losses": 1, "pnl": -9.73, "new": 2}


def test_message_order_live_on_top_paper_below(tmp_path):
    s = {"mode": "live", "equity": 150.18, "pnl": 0.18, "open_positions": 2, "stopped": False,
         "results": {"day": {"new": 3, "settled": 1, "losses": 0, "pnl": 0.18},
                     "all": {"settled": 1, "losses": 0, "pnl": 0.18}}}
    text = "\n".join(summary.live_lines(s, 150.0) + [""] + summary.paper_lines(str(tmp_path / "none.db"), NOW))
    assert text.index("LIVE (real money)") < text.index("PAPER (fake money)")
    assert "Last 24h: 3 new trades, 1 settled, 0 losses, P&L $+0.18" in text
    assert "Progress: 1 of 100 trial trades (1%)" in text
    assert "Account: $150.18 (start $150.00, +0.18)" in text


def test_live_off_and_errors():
    assert summary.live_lines(None, None) == ["LIVE (real money)", "Off (dry run, no orders)"]
    assert summary.live_lines(None, None, "LiveError")[1] == "Unavailable: LiveError"
