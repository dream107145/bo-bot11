"""Bot-level stops that do not need the feeds."""
import types

import pytest

from xshark import bot as B, config


def _bot(cash):
    class V:
        pos = {}
        halted = None

        @staticmethod
        def reserved():
            return 0.0

        def spendable(self):
            return self.cash
    V.cash = cash
    o = types.SimpleNamespace(s=config.Settings(), venue=V(), runs={}, books=None, live=False, target_hit=None,
                              stats={"today_key": B.day_key(0), "today": 0.0, "today_windows": 0},
                              _path=lambda n: "/nonexistent/" + n)
    o._equity = types.MethodType(B.Bot._equity, o)
    return o, V


def test_stop_at_equity_latches_until_the_target_is_changed():
    o, V = _bot(24.0)
    o.s.stop_at_equity_usdc = 25.0
    assert B.Bot._halt_reason(o, 0.0) is None
    V.cash = 25.5
    assert "target" in B.Bot._halt_reason(o, 0.0)
    V.cash = 20.0
    assert "target" in B.Bot._halt_reason(o, 0.0)            # stays stopped after a dip
    o.s.stop_at_equity_usdc = 30.0
    assert B.Bot._halt_reason(o, 0.0) is None                 # a new target: trading again


def test_orders_beyond_the_free_cash_are_dropped_not_flapped():
    from types import SimpleNamespace
    from xshark.market import FeeSchedule
    from xshark.strategy import Decision
    o, V = _bot(9.23)
    V.open_orders = staticmethod(lambda *a: [])
    d = Decision()
    d.places = [("Down", 0.90, 5.0)]                     # $4.50 fits $9.23
    d.takes = [("Up", 0.73, 5.0, 0.0)]                   # then $3.65 + fee fits the $4.73 left
    B.Bot._affordable(o, d, SimpleNamespace(fees=FeeSchedule()))
    assert d.places and d.takes
    o2, V2 = _bot(6.0)
    V2.open_orders = staticmethod(lambda *a: [])
    d2 = Decision()
    d2.places = [("Down", 0.90, 5.0), ("Up", 0.35, 5.0)]
    B.Bot._affordable(o2, d2, SimpleNamespace(fees=FeeSchedule()))
    assert d2.places == [("Down", 0.90, 5.0)] and "not enough cash" in d2.why["maker_Up"]
    assert B.Bot._halt_reason(o2, 0.0) is None           # no cash halt above $1


def test_account_sync_books_the_gap_as_outside_money(tmp_path):
    from xshark.bot import Bot
    o = types.SimpleNamespace(venue=types.SimpleNamespace(cash=14.81, venue_balance=10.94, unredeemed={},
                                                          account_gap=lambda: 10.94 - 14.81),
                              outside=7.01, dir=str(tmp_path))
    o._path = lambda name: str(tmp_path / name)
    Bot._sync_account(o)
    assert o.venue.cash == pytest.approx(10.94) and o.outside == pytest.approx(7.01 - 3.87)
    assert Bot._load_outside(o) == pytest.approx(-3.87)       # the file holds this run's rows
    o.venue.account_gap = lambda: 0.01                        # cents of noise: left alone
    Bot._sync_account(o)
    assert o.venue.cash == pytest.approx(10.94)


def test_an_open_bid_is_done_once_it_holds_5_shares_and_the_rest_is_not_chased():
    from xshark.bot import Bot
    from xshark.triangle import TriParams
    run = types.SimpleNamespace(tri={"kinds": {}}, m=types.SimpleNamespace(min_size=5.0))
    o = types.SimpleNamespace(tri=TriParams(enabled=True, open_shares=10.0))
    fill = lambda size, kind="maker": types.SimpleNamespace(outcome="Up", size=size, price=0.45, kind=kind, cost=size * 0.45, ts_ms=0)
    Bot._tri_fill(o, run, fill(3.2), "open")
    assert not run.tri.get("open_done")                    # under 5: keeps resting
    Bot._tri_fill(o, run, fill(3.53), "open")
    assert run.tri["open_done"] and abs(run.tri["open_sh"] - 6.73) < 1e-9
