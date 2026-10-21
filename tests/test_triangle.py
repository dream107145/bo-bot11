"""The triangle strategy (troll-poly-bot's pair-lock recovery), on hand-built views (no network)."""
import pytest

from xshark.config import Settings
from xshark.strategy import decide
from xshark.triangle import TriParams

from test_strategy import M, view

FEE = M.fees.taker_fee


def st():
    return Settings(maker_enabled=False, taker_enabled=False, pair_enabled=False, fav_enabled=False,
                    late_enabled=False, scalp_enabled=False)


def st50():
    s_ = st()
    s_.max_window_usdc = 50.0                             # the user's live budget
    return s_


def tri(**kw):
    return TriParams(**{"enabled": True, **kw})


def held_up(n=10.0, px=0.75):
    """A view holding n Up bought at px (the open buy)."""
    cost = n * px + FEE(px, n)
    return dict(shares={"Up": n, "Down": 0.0}, costs={"Up": cost, "Down": 0.0}, fees={"Up": FEE(px, n), "Down": 0.0},
                window_cost=cost)


def test_open_buys_the_dearer_token_priced_070_to_085_in_the_first_20_s():
    books = {"Up": {"bid": 0.76, "ask": 0.77}, "Down": {"bid": 0.23, "ask": 0.24}}
    d = decide(view(books=books, secs_in=5.0, secs_left=295.0, tri={}, tri_params=tri()), st())
    assert d.takes == [("Up", 0.79, 10.0, 0.0)] and d.tags["taker:Up"] == "scalp" and d.tri_kinds == {"Up": "open"}
    assert decide(view(books=books, secs_in=1.0, secs_left=299.0, tri={}, tri_params=tri()), st()).takes == []
    assert decide(view(books=books, secs_in=25.0, secs_left=275.0, tri={}, tri_params=tri()), st()).takes == []
    flat = {"Up": {"bid": 0.55, "ask": 0.56}, "Down": {"bid": 0.44, "ask": 0.45}}
    assert decide(view(books=flat, secs_in=5.0, secs_left=295.0, tri={}, tri_params=tri()), st()).takes == []
    assert decide(view(books=books, secs_in=5.0, secs_left=295.0, tri={"open_sent": 3}, tri_params=tri()), st()).takes == []
    assert decide(view(books=books, secs_in=5.0, secs_left=295.0, tri={}, tri_params=None), st()).takes == []


def test_recovery_waits_until_the_favourite_is_over_070_then_sizes_for_the_target():
    p = tri(late_enabled=False)
    low = {"Up": {"bid": 0.40, "ask": 0.41}, "Down": {"bid": 0.59, "ask": 0.60}}
    d = decide(view(books=low, secs_in=120.0, secs_left=180.0, tri={"opened": True}, tri_params=p, **held_up()), st())
    assert d.takes == [] and "waiting" in d.why["taker"]
    up = {"Up": {"bid": 0.24, "ask": 0.25},
          "Down": {"bid": 0.74, "ask": 0.75, "asks": [(0.75, 100.0), (0.76, 100.0)]}}
    d = decide(view(books=up, secs_in=120.0, secs_left=180.0, tri={"opened": True}, tri_params=p, **held_up()), st())
    (o, limit, size, _), = d.takes
    branch_down = 0.0 - held_up()["window_cost"]
    need = (0.5 - branch_down) / (1 - 0.75 - FEE(0.75, 1) - 0.005)
    assert o == "Down" and limit == 0.78 and d.tags["taker:Down"] == "scalp_hedge" and d.tri_kinds == {"Down": "rec"}
    assert size == pytest.approx(need, abs=0.02)
    # the side we hold has not dropped 0.05 under its cost: no recovery
    near = {"Up": {"bid": 0.72, "ask": 0.73}, "Down": {"bid": 0.74, "ask": 0.75}}
    assert decide(view(books=near, secs_in=120.0, secs_left=180.0, tri={"opened": True}, tri_params=p, **held_up()), st()).takes == []


def test_lock_instead_of_recovery_under_60_s_and_after_two_recoveries_the_window_is_held():
    p = tri(late_enabled=False)
    up = {"Up": {"bid": 0.24, "ask": 0.25}, "Down": {"bid": 0.74, "ask": 0.75, "asks": [(0.75, 100.0)]}}
    d = decide(view(books=up, secs_in=250.0, secs_left=50.0, tri={"opened": True}, tri_params=p, **held_up()), st())
    assert d.takes == [("Down", 0.78, 10.0, 0.0)]
    assert d.tri_kinds == {"Down": "lock"} and d.tags["taker:Down"] == "scalp_pair" and d.tri_update["locking"] is True
    # equal shares: locked, nothing more
    eq = dict(held_up(), shares={"Up": 10.0, "Down": 10.0}, window_cost=15.5)       # both sides bought
    d = decide(view(books=up, secs_in=250.0, secs_left=50.0, tri={"opened": True, "locking": True}, tri_params=p, **eq), st())
    assert d.takes == [] and d.tri_update["locked"] is True and d.tri_update["locking"] is False
    d = decide(view(books=up, secs_in=120.0, secs_left=180.0, tri={"opened": True, "recs": 2}, tri_params=p, **held_up()), st())
    assert d.takes == [] and "held" in d.why["taker"]


def test_profit_exit_after_a_recovery_sells_everything_at_bid_minus_003():
    p = tri(late_enabled=False)
    pos = dict(shares={"Up": 10.0, "Down": 30.0}, costs={"Up": 7.6, "Down": 22.0}, fees={"Up": 0.1, "Down": 0.3},
               window_cost=29.6)
    books = {"Up": {"bid": 0.05, "ask": 0.06}, "Down": {"bid": 0.95, "ask": 0.96}}     # worth 0.5 + 28.5 = 29.0 < cost
    assert decide(view(books=books, secs_in=200.0, secs_left=100.0, tri={"opened": True, "had_rec": True},
                       tri_params=p, **pos), st()).sells == []
    pos["window_cost"] = 27.0
    d = decide(view(books=books, secs_in=200.0, secs_left=100.0, tri={"opened": True, "had_rec": True},
                    tri_params=p, **pos), st())
    assert d.sells == [("Down", 0.92, 30.0)] and d.tri_update["exiting"] is True      # the leader first
    # the leader sold: the other side goes next, whatever the profit now
    pos2 = dict(pos, shares={"Up": 10.0, "Down": 0.0}, window_cost=29.0)
    d = decide(view(books=books, secs_in=201.0, secs_left=99.0, now_ms=(M.start + 201) * 1000.0,
                    tri={"opened": True, "had_rec": True, "exiting": True, "exit_ms": (M.start + 200) * 1000.0},
                    tri_params=p, **pos2), st())
    assert d.sells == [("Up", 0.02, 10.0)]
    # within 500 ms of the last sell: wait
    d = decide(view(books=books, secs_in=200.2, secs_left=99.8, now_ms=(M.start + 200.2) * 1000.0,
                    tri={"opened": True, "had_rec": True, "exiting": True, "exit_ms": (M.start + 200) * 1000.0},
                    tri_params=p, **pos2), st())
    assert d.sells == []


def test_late_buy_takes_the_95_percent_side_in_the_last_20_s():
    books = {"Up": {"bid": 0.95, "ask": 0.955, "asks": [(0.955, 50.0)]}, "Down": {"bid": 0.04, "ask": 0.05}}
    d = decide(view(books=books, secs_in=285.0, secs_left=15.0, fair_up=0.985, tri={}, tri_params=tri()), st())
    assert [t[:3] for t in d.takes] == [("Up", 0.98, 10.0)] and d.tri_kinds == {"Up": "late"}
    assert decide(view(books=books, secs_in=285.0, secs_left=15.0, fair_up=0.93, tri={}, tri_params=tri()), st()).takes == []
    assert decide(view(books=books, secs_in=270.0, secs_left=30.0, fair_up=0.985, tri={}, tri_params=tri()), st()).takes == []


def test_settings_file_casts_known_keys_and_reports_unknown_ones():
    p, notes = TriParams.from_dict({"enabled": 1, "open_min_price": "0.5", "max_recoveries": 3.0, "bogus": 1})
    assert p.enabled is True and p.open_min_price == 0.5 and p.max_recoveries == 3 and notes == ["bogus: unknown, ignored"]


def opened(px=0.74, n=10.0, side="Up", **kw):
    return {"opened": True, "open_side": side, "open_px": px, "open_sh": n, **kw}


def test_pair_bid_rests_after_the_open_and_blocks_recovery_until_pulled():
    p = tri(pair_bid=True, max_recoveries=1, lock_enabled=False, late_enabled=False, last_enabled=True)
    books = {"Up": {"bid": 0.73, "ask": 0.74}, "Down": {"bid": 0.25, "ask": 0.27}}
    d = decide(view(books=books, secs_in=30.0, secs_left=270.0, tri=opened(), tri_params=p, **held_up(px=0.74)), st())
    assert d.places == [("Down", 0.21, 10.0)] and d.tags["maker:Down"] == "scalp_pair" and d.tri_kinds["maker:Down"] == "pair"
    # Down took the lead: the bid is pulled (no target -> cancelled), recovery waits for the next tick
    flip = {"Up": {"bid": 0.24, "ask": 0.25}, "Down": {"bid": 0.74, "ask": 0.75, "asks": [(0.75, 100.0)]}}
    d = decide(view(books=flip, secs_in=120.0, secs_left=180.0, tri=opened(), tri_params=p, **held_up(px=0.74)), st())
    assert d.places == [] and d.tri_update.get("pair") == "pulled" and d.takes == []
    d = decide(view(books=flip, secs_in=120.0, secs_left=180.0, tri=opened(pair="pulled"), tri_params=p,
                    **held_up(px=0.74)), st())
    assert d.tri_kinds == {"Down": "rec"}
    # one recovery only, then held
    d = decide(view(books=flip, secs_in=150.0, secs_left=150.0, tri=opened(pair="pulled", recs=1), tri_params=p,
                    **held_up(px=0.74)), st())
    assert d.takes == [] and "held" in d.why["taker"]
    # pair filled: locked, nothing more until the last minute
    pos = dict(held_up(px=0.74), shares={"Up": 10.0, "Down": 10.0}, window_cost=9.8)
    d = decide(view(books=flip, secs_in=150.0, secs_left=150.0, tri=opened(pair="filled"), tri_params=p, **pos), st())
    assert d.takes == [] and d.places == []


def test_last_minute_buys_the_leader_with_all_free_cash_every_500_ms():
    p = tri(pair_bid=True, max_recoveries=1, lock_enabled=False, late_enabled=False, last_enabled=True)
    books = {"Up": {"bid": 0.62, "ask": 0.63, "asks": [(0.63, 40.0), (0.65, 100.0)]}, "Down": {"bid": 0.36, "ask": 0.38}}
    d = decide(view(books=books, secs_in=250.0, secs_left=50.0, tri=opened(pair="pulled"), tri_params=p,
                    free_cash=30.0, **held_up(px=0.74)), st())
    (o, limit, size, _), = d.takes
    assert o == "Up" and limit == 0.66 and size == pytest.approx(30.0 / (0.66 + FEE(0.66, 1)), abs=0.01)
    assert d.tri_kinds == {"Up": "last"} and d.tags["taker:Up"] == "late"
    # within 500 ms of the last buy: wait
    d = decide(view(books=books, secs_in=250.0, secs_left=50.0, now_ms=(M.start + 250) * 1000.0,
                    tri=opened(pair="pulled", last_buy_ms=(M.start + 250) * 1000.0 - 300), tri_params=p,
                    free_cash=30.0, **held_up(px=0.74)), st())
    assert d.takes == []
    # no cash left: nothing
    assert decide(view(books=books, secs_in=250.0, secs_left=50.0, tri=opened(pair="pulled"), tri_params=p,
                       free_cash=2.0, **held_up(px=0.74)), st()).takes == []


def test_settings_feed_the_triangle_and_the_on_only_console_hides_what_is_off():
    from xshark import config
    s = config.Settings(tri_enabled=True, tri_pair_bid=True, tri_max_recoveries=1, tri_last_enabled=True)
    p = TriParams.from_settings(s)
    assert p.enabled and p.pair_bid and p.max_recoveries == 1 and p.last_enabled and p.max_quote_age_ms == 2000
    vals = {**config.defaults(), **{k: False for k in config._OLD}, "tri_enabled": True,
            "tri_pair_bid": True, "tri_lock_enabled": False, "tri_late_enabled": False, "tri_last_enabled": True}
    keys = {r["key"] for r in config.visible_spec(vals)}
    groups = {r["group"] for r in config.visible_spec(vals)}
    assert "Maker bids" not in groups and "Scalp cycles" not in groups and "Favourite bid" not in groups
    assert {"tri_enabled", "tri_pair_sum", "tri_max_recoveries", "tri_last_every_ms", "daily_loss_stop_usdc",
            "max_window_usdc", "pause_a_from", "paper_race"} <= keys
    assert not keys & {"tri_lock_left_s", "tri_late_min_p", "anchor_s", "min_price", "maker_start_s", "max_window_shares",
                       "tri_lock_enabled", "tri_late_enabled"}
    assert config.visible_spec(vals)[0]["group"] == "Triangle"            # the strategy opens first


def test_last_minute_fixed_5_shares_only_under_098():
    p = tri(pair_bid=True, max_recoveries=1, lock_enabled=False, late_enabled=False, last_enabled=True,
            last_shares=5.0, last_max_price=0.98)
    books = {"Up": {"bid": 0.94, "ask": 0.95, "asks": [(0.95, 200.0)]}, "Down": {"bid": 0.04, "ask": 0.06}}
    d = decide(view(books=books, secs_in=250.0, secs_left=50.0, tri=opened(pair="pulled"), tri_params=p,
                    free_cash=80.0, **held_up(px=0.74)), st())
    assert [t[:3] for t in d.takes] == [("Up", 0.97, 5.0)]                # limit capped a tick under 0.98
    hi = {"Up": {"bid": 0.97, "ask": 0.98, "asks": [(0.98, 200.0)]}, "Down": {"bid": 0.01, "ask": 0.03}}
    d = decide(view(books=hi, secs_in=250.0, secs_left=50.0, tri=opened(pair="pulled"), tri_params=p,
                    free_cash=80.0, **held_up(px=0.74)), st())
    assert d.takes == [] and "not under 0.98" in d.why["taker"]


def test_a_cheap_open_range_buys_the_cheaper_token_and_its_pair_bid_rests_until_the_hedge_is_due():
    p = tri(open_min_price=0.35, open_max_price=0.49, pair_bid=True, max_recoveries=1, lock_enabled=False,
            late_enabled=False, last_enabled=True)
    books = {"Up": {"bid": 0.41, "ask": 0.42}, "Down": {"bid": 0.58, "ask": 0.59}}
    d = decide(view(books=books, secs_in=7.0, secs_left=293.0, tri={}, tri_params=p), st())
    assert d.takes == [("Up", 0.44, 10.0, 0.0)] and d.tri_kinds == {"Up": "open"}
    # Down leads at 0.59 but the hedge is not due (ask <= 0.70): the pair bid rests at 0.95 - 0.42
    d = decide(view(books=books, secs_in=30.0, secs_left=270.0, tri=opened(px=0.42, side="Up"), tri_params=p,
                    **held_up(px=0.42)), st())
    assert d.places == [("Down", 0.53, 10.0)] and "pair" not in d.tri_update
    up = {"Up": {"bid": 0.26, "ask": 0.27}, "Down": {"bid": 0.72, "ask": 0.73}}
    d = decide(view(books=up, secs_in=90.0, secs_left=210.0, tri=opened(px=0.42, side="Up"), tri_params=p,
                    **held_up(px=0.42)), st())
    assert d.places == [] and d.tri_update.get("pair") == "pulled"
    # a range over 0.50 still buys only the dearer token
    p2 = tri(open_min_price=0.55, open_max_price=0.85)
    d = decide(view(books=books, secs_in=7.0, secs_left=293.0, tri={}, tri_params=p2), st())
    assert [t[0] for t in d.takes] == ["Down"]


def test_after_a_completed_pair_a_new_cycle_opens_500_ms_later():
    p = tri(open_min_price=0.35, open_max_price=0.49, pair_bid=True, repeat=True, repeat_delay_ms=500,
            max_recoveries=1, lock_enabled=False, late_enabled=False, last_enabled=True)
    books = {"Up": {"bid": 0.47, "ask": 0.48}, "Down": {"bid": 0.51, "ask": 0.52}}
    locked = dict(shares={"Up": 10.0, "Down": 10.0}, costs={"Up": 4.95, "Down": 4.6}, fees={"Up": 0.17, "Down": 0.0},
                  window_cost=9.55)
    now = (M.start + 8) * 1000.0
    done = opened(px=0.49, side="Up", pair="filled", pair_sh=10.0, pair_done_ms=now - 300)
    d = decide(view(books=books, now_ms=now, secs_in=8.0, secs_left=292.0, tri=done, tri_params=p, **locked), st())
    assert d.takes == [] and d.places == []                       # 300 ms: wait
    done["pair_done_ms"] = now - 600
    d = decide(view(books=books, now_ms=now, secs_in=8.0, secs_left=292.0, tri=done, tri_params=p, **locked), st())
    assert d.takes == [("Up", 0.50, 10.0, 0.0)] and d.tri_update["cycles"] == 1 and d.tri_update["opened"] is False
    # later cycles may open after the first open window, until repeat_until_s (240)
    d = decide(view(books=books, now_ms=(M.start + 90) * 1000.0, secs_in=90.0, secs_left=210.0, tri=done,
                    tri_params=p, **locked), st())
    assert [t[:3] for t in d.takes] == [("Up", 0.50, 10.0)]
    # no range ask and no open cycle: the locked pairs never trigger a recovery
    up = {"Up": {"bid": 0.24, "ask": 0.25}, "Down": {"bid": 0.74, "ask": 0.75, "asks": [(0.75, 100.0)]}}
    d = decide(view(books=up, now_ms=(M.start + 90) * 1000.0, secs_in=90.0, secs_left=210.0, tri=done, tri_params=p,
                    **locked), st())
    assert d.takes == [] and d.places == []
    d = decide(view(books=books, now_ms=(M.start + 230) * 1000.0, secs_in=230.0, secs_left=70.0,
                    tri={**done, "cycles": 1}, tri_params=tri(**{**vars(p), "repeat_until_s": 200.0}), **locked), st())
    assert d.takes == []                                          # past "Repeat until"


def test_a_partly_filled_pair_bid_keeps_resting_until_the_pair_is_exact():
    from xshark.strategy import Resting
    p = tri(open_min_price=0.35, open_max_price=0.49, pair_bid=True, repeat=True, max_recoveries=1,
            lock_enabled=False, late_enabled=False, last_enabled=True)
    pos = dict(shares={"Up": 5.0, "Down": 2.3}, costs={"Up": 2.45, "Down": 1.08}, fees={"Up": 0.09, "Down": 0.0},
               window_cost=3.53)
    # the Down ask fell to 0.45: a new bid would go at 0.44, but the working 0.46 bid (2.7 sh left) stays
    books = {"Up": {"bid": 0.53, "ask": 0.55}, "Down": {"bid": 0.44, "ask": 0.45}}
    rest = {"Up": [], "Down": [Resting(7, 0.46, 2.7, 0.0, False)]}
    d = decide(view(books=books, secs_in=30.0, secs_left=270.0, resting=rest,
                    tri=opened(px=0.49, n=5.0, side="Up", pair_sh=2.3), tri_params=p, **pos), st())
    assert d.cancels == [] and d.places == []
    # the bid is gone (cancelled elsewhere): a new one for the venue's minimum, 5 shares
    d = decide(view(books=books, secs_in=30.0, secs_left=270.0, tri=opened(px=0.49, n=5.0, side="Up", pair_sh=2.3),
                    tri_params=p, **pos), st())
    assert d.places == [("Down", 0.44, 5.0)]
    # exact: nothing more, the cycle is filled
    pos["shares"]["Down"] = 5.0
    d = decide(view(books=books, secs_in=30.0, secs_left=270.0, resting=rest,
                    tri=opened(px=0.49, n=5.0, side="Up", pair_sh=5.0), tri_params=p, **pos), st())
    assert d.places == [] and d.cancels == [7] and d.tri_update.get("pair") == "filled"


def test_later_cycles_buy_the_leader_over_055_then_pair_the_cheap_side():
    p = tri(open_min_price=0.35, open_max_price=0.49, pair_bid=True, repeat=True, repeat_open_min_price=0.56,
            repeat_open_max_price=0.94, max_recoveries=1, lock_enabled=False, late_enabled=False, last_enabled=True)
    locked = dict(shares={"Up": 10.0, "Down": 10.0}, costs={"Up": 4.95, "Down": 4.6}, fees={"Up": 0.17, "Down": 0.0},
                  window_cost=9.55)
    now = (M.start + 60) * 1000.0
    done = opened(px=0.49, side="Up", pair="filled", pair_sh=10.0, pair_done_ms=now - 600)
    even = {"Up": {"bid": 0.47, "ask": 0.48}, "Down": {"bid": 0.51, "ask": 0.53}}
    d = decide(view(books=even, now_ms=now, secs_in=60.0, secs_left=240.0, tri=done, tri_params=p, **locked), st())
    assert d.takes == [] and "repeat open range 0.56-0.94" in d.why["taker"]
    lead = {"Up": {"bid": 0.36, "ask": 0.37}, "Down": {"bid": 0.62, "ask": 0.63}}
    d = decide(view(books=lead, now_ms=now, secs_in=60.0, secs_left=240.0, tri=done, tri_params=p, **locked), st())
    assert [t[:3] for t in d.takes] == [("Down", 0.65, 10.0)]
    # its pair bid then goes on Up at 0.95 - 0.63
    d = decide(view(books=lead, now_ms=now + 1000, secs_in=61.0, secs_left=239.0, tri_params=p,
                    tri=opened(px=0.63, side="Down", cycles=1), **locked), st())
    assert d.places == [("Up", 0.32, 10.0)]
    # the first cycle still opens the cheap side
    d = decide(view(books=lead, secs_in=5.0, secs_left=295.0, tri={}, tri_params=p), st())
    assert [t[0] for t in d.takes] == ["Up"]


def test_an_open_never_leaves_no_room_for_its_pair_bid():
    p = tri(open_min_price=0.56, open_max_price=0.94, pair_bid=True, pair_sum=0.90)
    books = {"Up": {"bid": 0.90, "ask": 0.91}, "Down": {"bid": 0.08, "ask": 0.10}}
    d = decide(view(books=books, secs_in=5.0, secs_left=295.0, tri={}, tri_params=p), st())
    assert d.takes == [] and "0.56-0.89" in d.why["taker"]


def test_a_cycle_does_not_open_when_its_pair_bid_would_pass_the_window_budget():
    p = tri(open_min_price=0.35, open_max_price=0.49, pair_bid=True, pair_sum=0.90, repeat=True,
            repeat_open_min_price=0.56, repeat_open_max_price=0.89, window_budget=25.0)
    books = {"Up": {"bid": 0.28, "ask": 0.29}, "Down": {"bid": 0.70, "ask": 0.71}}
    now = (M.start + 36) * 1000.0
    two = dict(shares={"Up": 20.0, "Down": 20.0}, costs={"Up": 7.6, "Down": 10.74}, fees={"Up": 0.0, "Down": 0.34},
               window_cost=18.34)
    done = opened(px=0.59, side="Down", pair="filled", pair_sh=10.0, pair_done_ms=now - 600, cycles=1)
    d = decide(view(books=books, now_ms=now, secs_in=36.0, secs_left=264.0, tri=done, tri_params=p, **two), st())
    assert d.takes == [] and "window budget" in d.why["taker"]
    p.window_budget = 40.0
    d = decide(view(books=books, now_ms=now, secs_in=36.0, secs_left=264.0, tri=done, tri_params=p, **two), st())
    assert [t[0] for t in d.takes] == ["Down"]
    from xshark import config
    assert TriParams.from_settings(config.Settings(max_window_usdc=33.0)).window_budget == 33.0


def test_the_leading_token_open_rests_as_a_maker_bid_and_its_pair_waits_for_it_to_finish():
    from xshark.strategy import Resting
    p = tri(open_min_price=0.35, open_max_price=0.49, pair_bid=True, pair_sum=0.90, repeat=True,
            repeat_open_min_price=0.56, repeat_open_max_price=0.89, repeat_open_maker=True, window_budget=50.0,
            max_recoveries=1, lock_enabled=False, late_enabled=False, last_enabled=True)
    lead = {"Up": {"bid": 0.36, "ask": 0.37}, "Down": {"bid": 0.62, "ask": 0.64}}
    locked = dict(shares={"Up": 10.0, "Down": 10.0}, costs={"Up": 4.5, "Down": 4.67}, fees={"Up": 0.0, "Down": 0.17},
                  window_cost=9.17)
    new = {"cycles": 1, "opened": False, "open_done": False}
    d = decide(view(books=lead, secs_in=40.0, secs_left=260.0, tri=new, tri_params=p, **locked), st50())
    assert d.takes == [] and d.places == [("Down", 0.63, 10.0)] and d.tags["maker:Down"] == "scalp"
    assert d.tri_kinds["maker:Down"] == "open" and "resting bid" in d.why["taker"]
    # 4 of 10 filled (under the venue's 5): the open bid keeps resting, the pair bid starts (minimum 5)
    part = {"cycles": 1, "opened": True, "open_done": False, "open_side": "Down", "open_sh": 4.0, "open_px": 0.62}
    pos = dict(shares={"Up": 10.0, "Down": 14.0}, costs={"Up": 4.5, "Down": 7.15}, fees={"Up": 0.0, "Down": 0.17},
               window_cost=11.65)
    rest = {"Up": [], "Down": [Resting(9, 0.63, 6.0, 0.0, False)]}
    d = decide(view(books=lead, secs_in=45.0, secs_left=255.0, resting=rest, tri=part, tri_params=p, **pos), st50())
    assert 9 not in d.cancels and ("Up", 0.28, 5.0) in d.places
    # the pair matched the 4 so far, but the open is not done: no "filled" (no repeat yet)
    d = decide(view(books=lead, secs_in=46.0, secs_left=254.0, resting=rest, tri={**part, "pair_sh": 4.0}, tri_params=p,
                    **pos), st50())
    assert d.tri_update.get("pair") != "filled"
    # the Down bid price leaves the range: the open is done with its 4 shares, the rest cancelled
    out = {"Up": {"bid": 0.45, "ask": 0.47}, "Down": {"bid": 0.52, "ask": 0.54}}
    d = decide(view(books=out, secs_in=47.0, secs_left=253.0, resting=rest, tri={**part, "pair_sh": 4.0}, tri_params=p,
                    **pos), st50())
    assert d.tri_update.get("open_done") is True and 9 in d.cancels


def test_a_held_token_bid_over_0988_sells_everything_that_token_first():
    p = tri(sell_all_price=0.988, pair_bid=True, last_enabled=True, late_enabled=False, lock_enabled=False)
    pos = dict(shares={"Up": 30.0, "Down": 20.0}, costs={"Up": 20.0, "Down": 9.0}, fees={"Up": 0.2, "Down": 0.0},
               window_cost=29.2)
    books = {"Up": {"bid": 0.99, "ask": 0.995}, "Down": {"bid": 0.01, "ask": 0.012}}
    d = decide(view(books=books, secs_in=250.0, secs_left=50.0, tri=opened(), tri_params=p, **pos), st())
    assert d.sells == [("Up", 0.98, 30.0)] and d.tri_update["selling_all"] is True and d.takes == []
    up_sold = dict(pos, shares={"Up": 0.0, "Down": 20.0})
    d = decide(view(books=books, secs_in=251.0, secs_left=49.0, now_ms=(M.start + 251) * 1000.0, tri_params=p,
                    tri=opened(selling_all=True, sell_all_ms=(M.start + 250) * 1000.0), **up_sold), st())
    assert d.sells == [("Down", 0.001, 20.0)]
    # under the line: nothing is sold
    calm = {"Up": {"bid": 0.98, "ask": 0.99}, "Down": {"bid": 0.01, "ask": 0.02}}
    assert decide(view(books=calm, secs_in=250.0, secs_left=50.0, tri=opened(), tri_params=p, **pos), st()).sells == []


def test_the_first_hedge_has_its_own_range_and_later_ones_use_the_recovery_range():
    p = tri(pair_bid=True, max_recoveries=2, lock_enabled=False, late_enabled=False, last_enabled=True,
            recovery_min_price=0.63, recovery_max_price=0.79, first_rec_min_price=0.74, first_rec_max_price=0.80)
    pos = held_up(px=0.45)
    at = lambda ask: {"Up": {"bid": 0.30, "ask": 0.31}, "Down": {"bid": ask - 0.01, "ask": ask, "asks": [(ask, 200.0)]}}
    first = opened(px=0.45, side="Up", pair="pulled")
    d = decide(view(books=at(0.70), secs_in=100.0, secs_left=200.0, tri=first, tri_params=p, **pos), st())
    assert d.takes == [] and "not above 0.74" in d.why["taker"]               # 0.70: too low for the first hedge
    d = decide(view(books=at(0.77), secs_in=100.0, secs_left=200.0, tri=first, tri_params=p, **pos), st())
    assert d.tri_kinds == {"Down": "rec"}
    d = decide(view(books=at(0.82), secs_in=100.0, secs_left=200.0, tri=first, tri_params=p, **pos), st())
    assert d.takes == []                                                     # over 0.80 (locks off)
    second = opened(px=0.45, side="Up", pair="pulled", recs=1, rec_open=False)
    d = decide(view(books=at(0.70), secs_in=100.0, secs_left=200.0, tri=second, tri_params=p, **pos), st())
    assert d.tri_kinds == {"Down": "rec"}                                    # the second uses 0.63-0.79


def test_the_cheap_side_is_held_until_its_bid_is_over_020():
    p = tri(sell_all_price=0.988, cheap_sell_price=0.20, pair_bid=True, last_enabled=True, late_enabled=False,
            lock_enabled=False)
    pos = dict(shares={"Up": 30.0, "Down": 20.0}, costs={"Up": 20.0, "Down": 9.0}, fees={"Up": 0.2, "Down": 0.0},
               window_cost=29.2)
    books = {"Up": {"bid": 0.99, "ask": 0.995}, "Down": {"bid": 0.01, "ask": 0.012}}
    d = decide(view(books=books, secs_in=250.0, secs_left=50.0, tri=opened(), tri_params=p, **pos), st())
    assert d.sells == [("Up", 0.98, 30.0)]
    rest = dict(pos, shares={"Up": 0.0, "Down": 20.0})
    late = dict(secs_in=252.0, secs_left=48.0, now_ms=(M.start + 252) * 1000.0)
    st_ = opened(selling_all=True, sell_all_ms=(M.start + 250) * 1000.0)
    d = decide(view(books=books, tri=st_, tri_params=p, **late, **rest), st())
    assert d.sells == [] and d.takes == [] and "held until its bid is over 0.20" in d.why["taker"]
    back = {"Up": {"bid": 0.74, "ask": 0.75}, "Down": {"bid": 0.25, "ask": 0.26}}
    d = decide(view(books=back, tri=st_, tri_params=p, **late, **rest), st())
    assert d.sells == [("Down", 0.24, 20.0)]
    # the profit exit: the leader first at any price, then the rest only over 0.20
    pe = tri(cheap_sell_price=0.20, late_enabled=False, last_enabled=False, lock_enabled=False)
    pos2 = dict(shares={"Up": 10.0, "Down": 30.0}, costs={"Up": 7.6, "Down": 22.0}, fees={"Up": 0.1, "Down": 0.3},
                window_cost=27.0)
    b2 = {"Up": {"bid": 0.05, "ask": 0.06}, "Down": {"bid": 0.95, "ask": 0.96}}
    d = decide(view(books=b2, secs_in=200.0, secs_left=100.0, tri={"opened": True, "had_rec": True}, tri_params=pe,
                    **pos2), st())
    assert d.sells == [("Down", 0.92, 30.0)] and d.tri_update["exit_leader"] == "Down"
    d = decide(view(books=b2, secs_in=201.0, secs_left=99.0, now_ms=(M.start + 201) * 1000.0, tri_params=pe,
                    tri={"opened": True, "had_rec": True, "exiting": True, "exit_leader": "Down",
                         "exit_ms": (M.start + 200) * 1000.0}, **dict(pos2, shares={"Up": 10.0, "Down": 0.0})), st())
    assert d.sells == [] and "held" in d.why["taker"]


def test_a_comeback_leader_from_030_may_be_bought_in_the_last_minute_from_052():
    p = tri(last_enabled=True, last_shares=5.0, last_min_price=0.90, last_max_price=0.98, last_comeback_low=0.30,
            last_comeback_min_price=0.52, late_enabled=False, lock_enabled=False)
    books = {"Up": {"bid": 0.54, "ask": 0.55, "asks": [(0.55, 100.0)]}, "Down": {"bid": 0.44, "ask": 0.46}}
    d = decide(view(books=books, secs_in=250.0, secs_left=50.0, tri={"low_Up": 0.28}, tri_params=p, free_cash=50.0), st())
    assert [t[:3] for t in d.takes] == [("Up", 0.58, 5.0)]
    d = decide(view(books=books, secs_in=250.0, secs_left=50.0, tri={"low_Up": 0.41}, tri_params=p, free_cash=50.0), st())
    assert d.takes == [] and "under 0.90" in d.why["taker"]                  # no comeback: 0.90 still applies
    # the low is tracked as the window goes
    low = {"Up": {"bid": 0.26, "ask": 0.28}, "Down": {"bid": 0.72, "ask": 0.74}}
    d = decide(view(books=low, secs_in=100.0, secs_left=200.0, tri={}, tri_params=p), st())
    assert abs(d.tri_update["low_Up"] - 0.27) < 1e-9


def test_hedge_by_time_left_uses_the_period_s_range_and_none_in_the_last_minute():
    p = tri(pair_bid=True, max_recoveries=2, lock_enabled=False, late_enabled=False, last_enabled=False,
            recovery_min_price=0.63, recovery_max_price=0.79, hedge_by_time=True)
    pos = held_up(px=0.45)
    at = lambda ask: {"Up": {"bid": 0.10, "ask": 0.11}, "Down": {"bid": ask - 0.01, "ask": ask, "asks": [(ask, 300.0)]}}
    t = opened(px=0.45, side="Up", pair="pulled")
    hedge = lambda ask, left: decide(view(books=at(ask), secs_in=300 - left, secs_left=left, tri=t, tri_params=p,
                                          **pos), st()).tri_kinds
    assert hedge(0.75, 200) == {} and hedge(0.85, 200) == {"Down": "rec"}     # 4:00-3:00: 0.80-0.90
    assert hedge(0.78, 150) == {"Down": "rec"} and hedge(0.88, 150) == {}     # 3:00-2:00: 0.77-0.85
    assert hedge(0.85, 90) == {"Down": "rec"}                                 # 2:00-1:00: 0.80-0.90
    assert hedge(0.85, 45) == {}                                              # last minute: none
    from xshark import config
    vals = {**config.defaults(), **{k: False for k in config._OLD}, "tri_enabled": True, "tri_hedge_by_time": True}
    keys = {r["key"] for r in config.visible_spec(vals)}
    assert "tri_hedge_t2_min" in keys and "tri_recovery_min_price" not in keys and "tri_first_rec_max_price" not in keys


def test_last_minute_buys_need_the_coin_far_enough_past_the_strike():
    p = tri(last_enabled=True, last_shares=5.0, last_min_price=0.90, last_max_price=0.98, last_min_dist_bps=6.0,
            late_enabled=False, lock_enabled=False)
    books = {"Up": {"bid": 0.93, "ask": 0.94, "asks": [(0.94, 100.0)]}, "Down": {"bid": 0.05, "ask": 0.07}}
    buy = lambda dist, bk=books: decide(view(books=bk, secs_in=250.0, secs_left=50.0, tri={}, tri_params=p,
                                             free_cash=50.0, dist_bps=dist), st()).takes
    assert buy(4.0) == [] and [t[:3] for t in buy(7.5)] == [("Up", 0.97, 5.0)] and buy(None) == []
    down = {"Up": {"bid": 0.05, "ask": 0.07}, "Down": {"bid": 0.93, "ask": 0.94, "asks": [(0.94, 100.0)]}}
    assert buy(7.5, down) == [] and [t[0] for t in buy(-7.5, down)] == ["Down"]    # the leader's side counts


def test_the_pair_sum_starts_at_089_and_rises_001_every_30_s_up_to_095():
    p = tri(open_min_price=0.48, open_max_price=0.55, pair_bid=True, pair_sum=0.89, pair_sum_max=0.95,
            pair_sum_step_s=30.0, late_enabled=False, lock_enabled=False)
    books = {"Up": {"bid": 0.49, "ask": 0.50}, "Down": {"bid": 0.48, "ask": 0.52}}
    t0 = (M.start + 10) * 1000.0
    cyc = opened(px=0.50, side="Up", open_ms=t0)
    at = lambda secs, rest=None: decide(view(books=books, now_ms=t0 + secs * 1000, secs_in=10 + secs, secs_left=290 - secs,
                                             resting=rest or {"Up": [], "Down": []}, tri=cyc, tri_params=p,
                                             **held_up(px=0.50)), st())
    assert at(5).places == [("Down", 0.39, 10.0)]                  # 0.89 - 0.50
    assert at(65).places == [("Down", 0.41, 10.0)]                 # +2 steps: 0.91
    assert at(400).places == [("Down", 0.45, 10.0)]                # capped at 0.95
    from xshark.strategy import Resting
    d = at(65, {"Up": [], "Down": [Resting(5, 0.39, 10.0, 0.0, False)]})
    assert 5 in d.cancels and ("Down", 0.41, 10.0) in d.places    # the waiting bid is raised


def test_a_rising_pair_bid_moves_on_every_one_cent_step():
    from xshark.strategy import Resting
    p = tri(open_min_price=0.48, open_max_price=0.55, pair_bid=True, pair_sum=0.80, pair_sum_max=0.92,
            pair_sum_step_s=30.0, late_enabled=False, lock_enabled=False)
    books = {"Up": {"bid": 0.47, "ask": 0.48}, "Down": {"bid": 0.52, "ask": 0.53}}
    t0 = (M.start + 34) * 1000.0
    cyc = opened(px=0.55, side="Down", open_ms=t0)
    pos = dict(shares={"Up": 0.0, "Down": 10.0}, costs={"Up": 0.0, "Down": 5.5}, fees={"Up": 0.0, "Down": 0.0},
               window_cost=5.5)
    d = decide(view(books=books, now_ms=t0 + 31000, secs_in=65.0, secs_left=235.0, tri=cyc, tri_params=p,
                    resting={"Up": [Resting(3, 0.25, 10.0, 0.0, False)], "Down": []}, **pos), st())
    assert 3 in d.cancels and ("Up", 0.26, 10.0) in d.places          # 0.81 - 0.55: one tick up, at once


def test_pair_bid_time_up_buys_the_pair_token_for_plus_010():
    p = tri(open_min_price=0.50, open_max_price=0.60, open_shares=7.0, open_maker=True, pair_bid=True,
            pair_sum=0.89, pair_pull=False, recovery_enabled=False, late_enabled=False, last_enabled=False,
            lock_enabled=False, profit_exit=0.0, pair_timeout_s=120.0, pair_timeout_profit=0.10)
    t0 = (M.start + 20) * 1000.0
    pos = dict(shares={"Up": 7.0, "Down": 0.0}, costs={"Up": 3.85, "Down": 0.0}, fees={"Up": 0.0, "Down": 0.0},
               window_cost=3.85)
    cyc = opened(px=0.55, n=7.0, side="Up", open_ms=t0, open_done=True)
    books = {"Up": {"bid": 0.40, "ask": 0.41}, "Down": {"bid": 0.58, "ask": 0.59}}
    # 1:30 after the open: the pair bid (0.89 - 0.55 = 0.34) still waits
    d = decide(view(books=books, now_ms=t0 + 90000, secs_in=110.0, secs_left=190.0, tri=cyc, tri_params=p, **pos), st())
    assert d.takes == [] and ("Down", 0.34, 7.0) in d.places
    # 2:00: time up -> Down bought as a taker, sized so the window ends +0.10 if Down wins
    d = decide(view(books=books, now_ms=t0 + 121000, secs_in=141.0, secs_left=159.0, tri=cyc, tri_params=p, **pos), st())
    (o, limit, n, _), = d.takes
    assert o == "Down" and d.tri_update["pair"] == "timeout"
    end_if_down = n - 3.85 - n * 0.59 - FEE(0.59, n)
    assert 0.09 <= end_if_down <= 0.10 + 0.6


def test_pair_bid_time_up_can_buy_exactly_5_shares():
    p = tri(open_min_price=0.50, open_max_price=0.60, open_shares=7.0, open_maker=True, pair_bid=True,
            pair_sum=0.89, pair_pull=False, recovery_enabled=False, late_enabled=False, last_enabled=False,
            lock_enabled=False, profit_exit=0.0, pair_timeout_s=120.0, pair_timeout_shares=5.0)
    t0 = (M.start + 20) * 1000.0
    pos = dict(shares={"Up": 7.0, "Down": 0.0}, costs={"Up": 3.85, "Down": 0.0}, fees={"Up": 0.0, "Down": 0.0},
               window_cost=3.85)
    books = {"Up": {"bid": 0.37, "ask": 0.38}, "Down": {"bid": 0.61, "ask": 0.62}}
    d = decide(view(books=books, now_ms=t0 + 121000, secs_in=141.0, secs_left=159.0, tri_params=p,
                    tri=opened(px=0.55, n=7.0, side="Up", open_ms=t0, open_done=True), **pos), st())
    assert [t[:3] for t in d.takes] == [("Down", 0.65, 5.0)] and d.tri_update["pair"] == "timeout"


def test_no_pair_60_s_after_the_open_raises_the_pair_bid_to_050_under_the_ask():
    p = tri(open_min_price=0.50, open_max_price=0.62, open_shares=7.0, open_maker=True, pair_bid=True,
            pair_sum=0.89, pair_pull=False, recovery_enabled=False, late_enabled=False, last_enabled=False,
            lock_enabled=False, profit_exit=0.0, pair_timeout_s=120.0, pair_timeout_shares=5.0,
            pair_raise_s=60.0, pair_raise_price=0.50)
    t0 = (M.start + 20) * 1000.0
    pos = dict(shares={"Up": 7.0, "Down": 0.0}, costs={"Up": 3.85, "Down": 0.0}, fees={"Up": 0.0, "Down": 0.0},
               window_cost=3.85)
    cyc = opened(px=0.55, n=7.0, side="Up", open_ms=t0, open_done=True)
    run = lambda books, ms: decide(view(books=books, now_ms=t0 + ms, secs_in=20 + ms / 1000, secs_left=280 - ms / 1000,
                                        tri=cyc, tri_params=p, **pos), st())
    hi = {"Up": {"bid": 0.40, "ask": 0.41}, "Down": {"bid": 0.58, "ask": 0.59}}
    assert ("Down", 0.34, 7.0) in run(hi, 59000).places            # 0:59: the 0.89 pair bid (0.89 - 0.55)
    d = run(hi, 60000)
    assert ("Down", 0.50, 7.0) in d.places and "raised" in d.why["maker_Down"]   # 1:00: up to 0.50
    lo = {"Up": {"bid": 0.56, "ask": 0.57}, "Down": {"bid": 0.42, "ask": 0.44}}
    assert ("Down", 0.43, 7.0) in run(lo, 61000).places            # a maker bid: 1 tick under a 0.44 ask
    d = run(hi, 121000)                                             # 2:00: the 5-share taker rule still follows
    assert [t[:3] for t in d.takes] == [("Down", 0.62, 5.0)] and d.tri_update["pair"] == "timeout"


def test_a_raise_at_the_time_limit_sends_only_the_5_share_taker_buy():
    p = tri(open_min_price=0.50, open_max_price=0.62, open_shares=7.0, open_maker=True, pair_bid=True,
            pair_sum=0.89, pair_pull=False, recovery_enabled=False, late_enabled=False, last_enabled=False,
            lock_enabled=False, profit_exit=0.0, pair_timeout_s=120.0, pair_timeout_shares=5.0,
            pair_raise_s=120.0, pair_raise_price=0.50)
    t0 = (M.start + 20) * 1000.0
    pos = dict(shares={"Up": 7.0, "Down": 0.0}, costs={"Up": 3.85, "Down": 0.0}, fees={"Up": 0.0, "Down": 0.0},
               window_cost=3.85)
    books = {"Up": {"bid": 0.56, "ask": 0.57}, "Down": {"bid": 0.42, "ask": 0.44}}
    d = decide(view(books=books, now_ms=t0 + 120000, secs_in=140.0, secs_left=160.0, tri_params=p,
                    tri=opened(px=0.55, n=7.0, side="Up", open_ms=t0, open_done=True), **pos), st())
    assert [t[:3] for t in d.takes] == [("Down", 0.47, 5.0)] and not any(x[0] == "Down" for x in d.places)


def test_the_pair_bid_buys_the_shares_set_in_the_console_or_what_the_open_filled():
    base = dict(open_min_price=0.50, open_max_price=0.62, open_shares=7.0, open_maker=True, pair_bid=True,
                pair_sum=0.89, pair_pull=False, recovery_enabled=False, late_enabled=False, last_enabled=False,
                lock_enabled=False, profit_exit=0.0)
    t0 = (M.start + 20) * 1000.0
    pos = dict(shares={"Up": 7.0, "Down": 0.0}, costs={"Up": 3.85, "Down": 0.0}, fees={"Up": 0.0, "Down": 0.0},
               window_cost=3.85)
    books = {"Up": {"bid": 0.60, "ask": 0.61}, "Down": {"bid": 0.38, "ask": 0.40}}
    run = lambda p, cyc: decide(view(books=books, now_ms=t0 + 5000, secs_in=25.0, secs_left=275.0, tri=cyc,
                                     tri_params=p, **pos), st())
    cyc = opened(px=0.55, n=7.0, side="Up", open_ms=t0, open_done=True)
    assert ("Down", 0.34, 7.0) in run(tri(**base), cyc).places                       # 0: the open's 7
    assert ("Down", 0.34, 10.0) in run(tri(**base, pair_shares=10.0), cyc).places    # set: 10
    assert ("Down", 0.34, 5.0) in run(tri(**base, pair_shares=3.0), cyc).places      # under 5: the venue's 5
    d = run(tri(**base, pair_shares=10.0), {**cyc, "pair_sh": 7.0})                  # 7 of 10 in: 5 more (minimum)
    assert ("Down", 0.34, 5.0) in d.places
    assert run(tri(**base, pair_shares=10.0), {**cyc, "pair_sh": 10.0}).tri_update.get("pair") == "filled"


def test_a_leader_over_the_open_range_keeps_a_bid_at_the_top_until_the_open_time_ends():
    base = dict(open_min_price=0.50, open_max_price=0.62, open_shares=7.0, open_maker=True, open_window_s=120,
                pair_bid=True, pair_sum=0.89, pair_pull=False, recovery_enabled=False, late_enabled=False,
                last_enabled=False, lock_enabled=False, profit_exit=0.0)
    run = lambda p, secs: decide(view(books=books, secs_in=secs, secs_left=300 - secs, tri={}, tri_params=p), st())
    books = {"Up": {"bid": 0.70, "ask": 0.71}, "Down": {"bid": 0.29, "ask": 0.30}}     # Up ran over 0.62
    assert run(tri(**base), 10.0).places == []                                       # off: no bid
    d = run(tri(**base, open_hold_top=True), 10.0)
    assert ("Up", 0.62, 7.0) in d.places and "stays at the top" in d.why["maker_Up"]
    assert run(tri(**base, open_hold_top=True), 121.0).places == []                  # the open time is over
    books = {"Up": {"bid": 0.57, "ask": 0.59}, "Down": {"bid": 0.41, "ask": 0.43}}     # back in the range
    assert ("Up", 0.58, 7.0) in run(tri(**base, open_hold_top=True), 30.0).places    # it follows the book again


def test_a_pair_token_over_070_is_bought_at_once_not_at_the_time_limit():
    p = tri(open_min_price=0.50, open_max_price=0.62, open_shares=7.0, open_maker=True, pair_bid=True,
            pair_sum=0.89, pair_pull=False, recovery_enabled=False, late_enabled=False, last_enabled=False,
            lock_enabled=False, profit_exit=0.0, pair_timeout_s=120.0, pair_timeout_shares=5.0, pair_hedge_price=0.70)
    t0 = (M.start + 20) * 1000.0
    pos = dict(shares={"Up": 7.0, "Down": 0.0}, costs={"Up": 3.85, "Down": 0.0}, fees={"Up": 0.0, "Down": 0.0},
               window_cost=3.85)
    cyc = opened(px=0.55, n=7.0, side="Up", open_ms=t0, open_done=True)
    run = lambda books: decide(view(books=books, now_ms=t0 + 30000, secs_in=50.0, secs_left=250.0, tri=cyc,
                                    tri_params=p, **pos), st())
    d = run({"Up": {"bid": 0.30, "ask": 0.31}, "Down": {"bid": 0.68, "ask": 0.70}})     # 0.70: not over it
    assert d.takes == [] and ("Down", 0.34, 7.0) in d.places
    d = run({"Up": {"bid": 0.27, "ask": 0.28}, "Down": {"bid": 0.71, "ask": 0.72}})     # 0:30 in, 0.72: buy now
    assert [t[:3] for t in d.takes] == [("Down", 0.75, 5.0)] and d.tri_update["pair"] == "timeout"
    assert not any(x[0] == "Down" for x in d.places) and "over 0.70" in d.why["taker"]


def test_after_the_pair_token_taker_buy_10_more_at_each_level_075_078_081_up_to_088():
    p = tri(open_min_price=0.50, open_max_price=0.62, open_shares=7.0, open_maker=True, pair_bid=True,
            pair_sum=0.89, pair_pull=False, recovery_enabled=False, late_enabled=False, last_enabled=False,
            lock_enabled=False, profit_exit=0.0, pair_timeout_s=120.0, pair_timeout_shares=5.0, pair_hedge_price=0.70,
            add_shares=10.0, add_first_price=0.75, add_step=0.03, add_levels=3, add_max_price=0.88)
    t0 = (M.start + 20) * 1000.0
    pos = dict(shares={"Up": 7.0, "Down": 5.0}, costs={"Up": 3.85, "Down": 3.60}, fees={"Up": 0.0, "Down": 0.0},
               window_cost=7.45)
    cyc = {**opened(px=0.55, n=7.0, side="Up", open_ms=t0, open_done=True), "pair": "timeout"}
    run = lambda books, c=cyc: decide(view(books=books, now_ms=t0 + 60000, secs_in=80.0, secs_left=220.0, tri=c,
                                           tri_params=p, **pos), st())
    bk = lambda a: {"Up": {"bid": round(1 - a - 0.01, 2), "ask": round(1 - a + 0.01, 2)}, "Down": {"bid": round(a - 0.01, 2), "ask": a}}
    assert run(bk(0.74)).takes == []                                                   # not at 0.75 yet
    assert [t[:3] for t in run(bk(0.75)).takes] == [("Down", 0.78, 10.0)]              # level 1: 10 @ <= 0.78
    assert [t[:3] for t in run(bk(0.79), {**cyc, "add_sh": 10.0}).takes] == [("Down", 0.82, 10.0)]   # level 2
    assert [t[:3] for t in run(bk(0.82)).takes] == [("Down", 0.85, 30.0)]              # jumped past all 3: 30
    assert [t[:3] for t in run(bk(0.87), {**cyc, "add_sh": 20.0}).takes] == [("Down", 0.88, 10.0)]   # capped 0.88
    assert run(bk(0.90), {**cyc, "add_sh": 20.0}).takes == []                          # over 0.88: no
    assert run(bk(0.85), {**cyc, "add_sh": 30.0}).takes == []                          # all 30 in


def test_a_token_back_from_under_035_is_bought_10_at_060_070_080_and_retried_up_to_094():
    p = tri(open_min_price=0.50, open_max_price=0.62, open_shares=7.0, open_maker=True, pair_bid=True,
            pair_sum=0.89, pair_pull=False, recovery_enabled=False, late_enabled=False, last_enabled=False,
            lock_enabled=False, profit_exit=0.0, cb_shares=10.0, cb_low=0.35, cb_first_trigger=0.57, cb_step=0.10,
            cb_levels=3, cb_limit_over=0.03, cb_max_price=0.94, window_budget=50.0)
    t0 = (M.start + 10) * 1000.0
    pos = dict(shares={"Up": 7.0, "Down": 7.0}, costs={"Up": 4.34, "Down": 5.18}, fees={"Up": 0.0, "Down": 0.0},
               window_cost=9.52)
    cyc = {**opened(px=0.62, n=7.0, side="Down", open_ms=t0, open_done=True), "pair": "timeout", "low_Down": 0.02,
           "low_Up": 0.40}
    run = lambda a, c=cyc, **kw: decide(view(books={"Down": {"bid": round(a - 0.01, 2), "ask": a},
                                                    "Up": {"bid": round(1 - a - 0.01, 2), "ask": round(1 - a + 0.01, 2)}},
                                             now_ms=t0 + 200000, secs_in=210.0, secs_left=90.0, tri=c, tri_params=p,
                                             **{**pos, **kw}), st())
    assert run(0.56).takes == []                                                       # under the 0.57 trigger
    d = run(0.58)
    assert [t[:3] for t in d.takes] == [("Down", 0.60, 10.0)] and d.tri_update["cb_lvl_Down"] == 1
    assert [t[:3] for t in run(0.68, {**cyc, "cb_lvl_Down": 1, "cb_sh_Down": 10.0}).takes] == [("Down", 0.70, 10.0)]
    assert [t[:3] for t in run(0.79).takes] == [("Down", 0.80, 30.0)]                  # jumped past all: 30 @ 0.80
    assert [t[:3] for t in run(0.65, {**cyc, "cb_lvl_Down": 1}).takes] == [("Down", 0.68, 10.0)]   # unfilled: again
    assert run(0.95, {**cyc, "cb_lvl_Down": 3, "cb_sh_Down": 20.0}).takes == []        # over 0.94: not again
    assert run(0.70, {**cyc, "low_Down": 0.35}).takes == []                            # 0.35 is not under 0.35
    # the pair token too, with its own count: Up fell to 0.29 and is back at 0.58
    d = run(0.41, {**cyc, "low_Down": 0.40, "low_Up": 0.29})
    assert [t[:3] for t in d.takes] == [("Up", 0.60, 10.0)] and d.tri_update["cb_lvl_Up"] == 1
    # the $50 window budget cuts it: $45 spent leaves room for 8.10 sh at 0.60 (+ fee); $48 leaves under 5: none
    assert [t[:3] for t in run(0.58, window_cost=45.0).takes] == [("Down", 0.60, 8.1)]
    assert run(0.58, window_cost=48.0).takes == []


def test_a_btc_move_buys_60_of_the_token_it_pushes_up_at_once():
    p = tri(open_min_price=0.50, open_max_price=0.62, open_shares=7.0, open_maker=True, pair_bid=True, pair_sum=0.89,
            pair_pull=False, recovery_enabled=False, late_enabled=False, last_enabled=False, lock_enabled=False,
            profit_exit=0.0, btc_shares=60.0, btc_bps=2.0, btc_ms=500.0, btc_min_price=0.10, btc_max_price=0.90,
            btc_slippage=0.02, btc_from_s=10.0, btc_until_s=240.0, btc_cooldown_ms=1000.0, btc_after_open=True,
            btc_max_buys=10, window_budget=50.0)
    t0 = (M.start + 5) * 1000.0
    pos = dict(shares={"Up": 7.0, "Down": 0.0}, costs={"Up": 3.85, "Down": 0.0}, fees={"Up": 0.0, "Down": 0.0},
               window_cost=3.85)
    cyc = opened(px=0.55, n=7.0, side="Up", open_ms=t0, open_done=True)
    books = {"Up": {"bid": 0.55, "ask": 0.56}, "Down": {"bid": 0.44, "ask": 0.45}}
    run = lambda mv, c=cyc, secs=60.0, **kw: decide(view(books=books, now_ms=(M.start + secs) * 1000.0, secs_in=secs,
                                                         secs_left=300 - secs, tri=c, tri_params=p, btc_move_bps=mv,
                                                         **{**pos, **kw}), st())
    d = run(+2.4)
    assert [t[:3] for t in d.takes] == [("Up", 0.58, 60.0)] and d.tags["taker:Up"] == "btc" and d.tri_kinds["Up"] == "btc"
    assert [t[:3] for t in run(-2.1).takes] == [("Down", 0.47, 60.0)]                 # BTC down: the Down token
    assert not any(t[2] == 60.0 for t in run(+1.9).takes)                             # under 2 bps: no BTC buy
    assert not any(t[2] == 60.0 for t in run(+3.0, {**cyc, "btc_ms_Up": (M.start + 59.5) * 1000.0}).takes)   # 0.5 s ago
    assert not any(t[2] == 60.0 for t in run(+3.0, secs=5.0).takes)                   # before 10 s
    assert not any(t[2] == 60.0 for t in run(+3.0, {}).takes)                         # no first buy yet
    # the $50 window budget: $30 already in -> 60 sh at 0.58 does not fit, 33.6 sh do (0.58 + fee)
    d = run(+3.0, window_cost=30.0)
    assert d.takes[0][0] == "Up" and 33.0 <= d.takes[0][2] < 34.0
