"""The Whiskas bot (user, 2026-10-10), on hand-built views."""
from dataclasses import replace

from xshark.config import Settings
from xshark.strategy import Resting, decide
from xshark.whisk import WhiskParams

from test_strategy import M, view


def st():
    return Settings(maker_enabled=False, taker_enabled=False, pair_enabled=False, fav_enabled=False,
                    late_enabled=False, scalp_enabled=False)


P = WhiskParams(enabled=True)                 # rise 0.05, FAK at bid + 0.01, every 250 ms, no rest, 1-298 s, balance 1:1
T0 = (M.start + 60) * 1000.0
BOOKS = {"Up": {"bid": 0.55, "ask": 0.56}, "Down": {"bid": 0.44, "ask": 0.45}}       # Up mid 0.555, Down 0.445


def run(w=None, books=BOOKS, ms=T0, p=P, **kw):
    return decide(view(books=books, now_ms=ms, secs_in=ms / 1000 - M.start, secs_left=M.end - ms / 1000,
                       whisk=w or {}, whisk_params=p, **kw), st())


def leg(**kw):
    return {**dict(id=1, kind="rise", goal=60.0, got=0.0, t0=T0, ref=0.555, last_try=T0, tries=1, why="x"), **kw}


def test_a_token_risen_005_over_its_low_gets_a_60_share_fak_at_the_best_bid_plus_001():
    d = run({"rlow_Up": 0.50, "rlow_Down": 0.44})                          # Up 0.505 -> 0.555
    assert [t[:3] for t in d.takes] == [("Up", 0.56, 60.0)] and d.tags["taker:Up"] == "rise"
    lg = d.whisk_update["leg_Up"]
    assert lg["kind"] == "rise" and lg["goal"] == 60.0 and lg["tries"] == 1
    assert "rlow_Up" not in d.whisk_update and d.whisk_update["rnext_Up"] == 2 and d.whisk_update["trig_ms_Up"] == T0
    assert run({"rlow_Up": 0.52, "rlow_Down": 0.44}).takes == []            # +0.035: not yet
    assert run({"rlow_Up": 0.50, "rlow_Down": 0.44}, ms=(M.start + 299) * 1000.0).takes == []   # after 4:58
    assert [t[:3] for t in run({"rlow_Up": 0.50}, ms=(M.start + 290) * 1000.0).takes] == [("Up", 0.56, 60.0)]


def test_both_tokens_buy_at_once_and_a_new_rise_adds_60_more():
    w = {"leg_Up": leg(got=20.0, last_try=T0 - 300), "legs": 1, "rlow_Up": 0.555, "rlow_Down": 0.39}
    d = run(w)                                                             # Up goes on; Down rose 0.055: a buy too
    assert sorted(t[:3] for t in d.takes) == [("Down", 0.45, 60.0), ("Up", 0.56, 40.0)]
    w = {"leg_Up": leg(got=20.0, last_try=T0 - 300), "legs": 1, "rlow_Up": 0.50}
    d = run(w)                                                             # Up rose again while buying: 60 more
    assert d.whisk_update["leg_Up"]["goal"] == 120.0 and [t[:3] for t in d.takes] == [("Up", 0.56, 60.0)]   # a try: 60 at most


def test_fak_tries_go_on_every_250_ms_until_the_buy_has_its_shares():
    w = {"leg_Up": leg(got=20.0), "legs": 1, "rlow_Up": 0.555, "rlow_Down": 0.445}
    assert run(w, ms=T0 + 100).takes == []                                 # 100 ms: wait
    assert run(w, ms=T0 + 300, in_flight_taker={"Up": 40.0, "Down": 0.0}).takes == []   # the last try is still out
    up = {"Up": {"bid": 0.58, "ask": 0.59}, "Down": {"bid": 0.41, "ask": 0.42}}
    d = run(w, books=up, ms=T0 + 300)
    assert [t[:3] for t in d.takes] == [("Up", 0.59, 40.0)] and d.whisk_update["leg_Up"]["tries"] == 2
    d = run({**w, "leg_Up": leg(got=58.0)}, ms=T0 + 300)                    # 2 left (< 5): done
    assert d.takes == [] and d.whisk_update["leg_Up"] is None
    d = run(w, ms=T0 + 31000)                                              # 30 s: it stops
    assert d.takes == [] and d.whisk_update["leg_Up"] is None
    p = replace(P, only_above=True)
    flat = {"Up": {"bid": 0.54, "ask": 0.55}, "Down": {"bid": 0.45, "ask": 0.46}}
    assert run(w, books=flat, ms=T0 + 300, p=p).whisk_update["leg_Up"] is None    # under where it started


def test_the_balance_is_a_live_price_hedge_bigger_while_open_smaller_when_dear():
    w = {"owe_Down": 1.0, "rlow_Up": 0.555, "rlow_Down": 0.445, "rise_fill_ms_Up": T0 - 2000}
    pos = dict(shares={"Up": 240.0, "Down": 60.0}, window_cost=130.0)     # gap 180 shares; if Up wins +110
    d = run(w, **pos)            # 180 - (0.4673 - 0.445) / (0.001 x 0.555 x 0.445) = 180 - 90.4 = 89.6
    goal = d.whisk_update["leg_Down"]["goal"]
    assert 89.5 < goal < 89.7 and [t[:3] for t in d.takes] == [("Down", 0.45, 60.0)] and d.tags["taker:Down"] == "bal"
    assert d.whisk_update["owe_Down"] == 0.0
    assert run(w, p=replace(P, bal_k=0.002), **pos).whisk_update["leg_Down"]["goal"] > goal + 40   # stronger: more
    bk = {"Up": {"bid": 0.70, "ask": 0.71}, "Down": {"bid": 0.29, "ask": 0.30}}   # Down far behind: no hedge now
    d = run({**w, "rlow_Up": 0.705, "rlow_Down": 0.295}, books=bk, shares={"Up": 60.0, "Down": 0.0}, window_cost=42.88)
    assert d.takes == [] and d.whisk_update["owe_Down"] == 0.0 and "no balance" in d.why["taker"]
    assert run({**w, "rise_fill_ms_Up": T0 - 500}, **pos).takes == []    # Up filled 0.5 s ago: not yet
    assert run({**w, "leg_Up": leg(got=40.0, last_try=T0 - 50)}, **pos).takes == []   # Up still buying


def test_rising_buys_are_tagged_rise_and_btc_ones_btc():
    d = run({"rlow_Up": 0.50, "rlow_Down": 0.44})
    assert d.tags["taker:Up"] == "rise"
    d = run({"rlow_Up": 0.555, "rlow_Down": 0.445}, p=replace(P, trig_bps=2.0), wk_move_bps=+2.5)
    assert d.tags["taker:Up"] == "btc"


def test_a_token_at_060_that_would_lose_if_it_wins_is_topped_up_to_break_even():
    up60 = {"Up": {"bid": 0.60, "ask": 0.61}, "Down": {"bid": 0.39, "ask": 0.40}}
    w = {"rlow_Up": 0.605, "rlow_Down": 0.395}
    d = run(w, books=up60, shares={"Up": 60.0, "Down": 60.0}, window_cost=62.0)    # if Up wins: 60 - 62 = -2
    (o, limit, n, _), = d.takes
    assert o == "Up" and d.tags["taker:Up"] == "top" and n == 6.0 and limit == 0.61
    assert run({**w, "topped_Up": True}, books=up60, shares={"Up": 60.0, "Down": 60.0}, window_cost=62.0).takes == []


def test_resting_the_rest_as_a_bid_is_an_option():
    p = replace(P, rest_s=2.0)
    w = {"leg_Up": leg(got=20.0, lim=0.56, rest_t0=T0), "legs": 1, "rlow_Up": 0.555, "rlow_Down": 0.445}
    d = run(w, ms=T0 + 500, p=p)
    assert d.takes == [] and ("Up", 0.55, 40.0) in d.places and d.whisk_kinds["maker:Up"] == "rise:1"
    d = run(w, ms=T0 + 2100, p=p, resting={"Up": [Resting(7, 0.55, 40.0, T0, True)], "Down": []})
    assert d.takes == []                                                   # its bid comes off first
    assert [t[:3] for t in run(w, ms=T0 + 2100, p=p).takes] == [("Up", 0.56, 40.0)]


def test_a_window_cap_when_set():
    w = {"rlow_Up": 0.50, "rlow_Down": 0.44}
    d = run(w, p=replace(P, window_usd=200.0), window_cost=180.0)          # $20 left: 34.6 sh at 0.56 + fee
    assert d.takes[0][0] == "Up" and 34.0 < d.takes[0][2] < 35.5
    assert run(w, window_cost=5000.0).takes[0][2] == 60.0                   # no cap by default


def test_the_gap_before_the_next_rising_buy_of_a_token_depends_on_its_price():
    from xshark.whisk import gap_ms
    assert gap_ms(P, 0.15) == 1000.0 and gap_ms(P, 0.45) == 500.0 and gap_ms(P, 0.85) == 250.0
    cheap = {"Up": {"bid": 0.84, "ask": 0.85}, "Down": {"bid": 0.15, "ask": 0.16}}       # Down mid 0.155
    w = {"rlow_Down": 0.10, "rlow_Up": 0.80, "trig_ms_Down": T0 - 700}
    assert run(w, books=cheap).takes == []                                              # cheap: 0.7 s < 1 s
    assert run({**w, "trig_ms_Down": T0 - 1100}, books=cheap).takes[0][0] == "Down"     # 1.1 s: goes
    dear = {"Up": {"bid": 0.84, "ask": 0.85}, "Down": {"bid": 0.15, "ask": 0.16}}       # Up mid 0.845
    w = {"rlow_Up": 0.79, "rlow_Down": 0.20, "trig_ms_Up": T0 - 300}
    assert run(w, books=dear).takes[0][0] == "Up"                                       # dear: 0.3 s > 0.25 s: goes


def test_several_fak_tries_may_be_on_their_way_each_for_shares_no_other_covers():
    w = {"leg_Up": leg(goal=120.0, got=0.0, last_try=T0 - 300), "legs": 1, "rlow_Up": 0.555, "rlow_Down": 0.445}
    d = run(w, in_flight_taker={"Up": 60.0, "Down": 0.0})                 # 60 on the way: the other 60 goes now
    assert [t[:3] for t in d.takes] == [("Up", 0.56, 60.0)]
    assert run(w, in_flight_taker={"Up": 120.0, "Down": 0.0}).takes == []     # all 120 covered: wait
    p = replace(P, max_inflight=1.0)
    assert run(w, p=p, in_flight_taker={"Up": 60.0, "Down": 0.0}).takes == []  # one at a time when set to 1


def test_the_bid_ladder_rests_bids_at_several_prices_and_moves_with_the_price():
    p = replace(P, ladder_levels=3.0, ladder_step=0.03, ladder_shares=20.0)
    w = {"rlow_Up": 0.555, "rlow_Down": 0.445}
    d = run(w, p=p)
    assert sorted(d.grid_places) == [("Down", 0.38, 20.0), ("Down", 0.41, 20.0), ("Down", 0.44, 20.0),
                                     ("Up", 0.49, 20.0), ("Up", 0.52, 20.0), ("Up", 0.55, 20.0)]
    assert d.whisk_update["lad_Up"]["anchor"] == 0.55
    bids = {"Up": [Resting(11, 0.55, 20.0, T0), Resting(12, 0.52, 20.0, T0)], "Down": [Resting(21, 0.44, 20.0, T0)]}
    w2 = {**w, "lad_Up": {"anchor": 0.55}, "lad_Down": {"anchor": 0.44}, "grid_ids": [11, 12, 21]}
    d = run(w2, p=p, resting=bids)                                        # still in place: nothing moves
    assert d.grid_places == [] and d.grid_cancels == [] and 11 not in d.cancels   # (and the reconcile leaves them)
    up = {"Up": {"bid": 0.59, "ask": 0.60}, "Down": {"bid": 0.40, "ask": 0.41}}
    d = run({**w2, "rlow_Up": 0.595, "rlow_Down": 0.405}, books=up, p=p, resting=bids)   # both bids moved 0.04: both ladders move
    assert sorted(d.grid_cancels) == [11, 12, 21] and ("Up", 0.59, 20.0) in d.grid_places and ("Down", 0.40, 20.0) in d.grid_places
    small = {"Up": {"bid": 0.56, "ask": 0.57}, "Down": {"bid": 0.43, "ask": 0.44}}         # 0.01 moves: both stay
    assert run({**w2, "rlow_Up": 0.565, "rlow_Down": 0.435}, books=small, p=p, resting=bids).grid_cancels == []


def test_a_rise_during_the_gap_does_not_count_only_one_after_it():
    w = {"rlow_Up": 0.50, "rlow_Down": 0.44, "trig_ms_Up": T0 - 300}         # Up bought 0.3 s ago (gap 0.5 s here)
    d = run(w)                                                              # Up 0.505 -> 0.555 inside the gap: no buy
    assert d.takes == [] and d.whisk_update["rnext_Up"] == 2                # the step 0.55 passed in the gap: skipped
    assert "rlow_Up" not in d.whisk_update
    w2 = {**w, "rnext_Up": 2}
    assert run(w2, ms=T0 + 300).takes == []                                 # gap over, next step 0.60 not reached
    up = {"Up": {"bid": 0.59, "ask": 0.61}, "Down": {"bid": 0.39, "ask": 0.41}}   # Up mid 0.60 after the gap
    assert [t[:2] for t in run(w2, books=up, ms=T0 + 400).takes] == [("Up", 0.60)]


def test_rising_buys_come_at_fixed_005_steps_over_the_low():
    w = {"rlow_Up": 0.45, "rnext_Up": 2, "trig_ms_Up": T0 - 600}           # Up bought at 0.50, its gap over
    ups = lambda d: [t for t in d.takes if t[0] == "Up"]                    # Down has no low yet: it only starts one
    at = lambda mid: {"Up": {"bid": round(mid - 0.005, 3), "ask": round(mid + 0.005, 3)},
                      "Down": {"bid": round(0.995 - mid, 3), "ask": round(1.005 - mid, 3)}}
    assert ups(run(w, books=at(0.535))) == []                               # +0.085: under the 0.10 step
    d = run(w, books=at(0.555))                                             # +0.105: the 0.10 step (not 0.53 + 0.05)
    assert len(ups(d)) == 1 and d.whisk_update["rnext_Up"] == 3 and "rlow_Up" not in d.whisk_update
    d = run(w, books=at(0.625))                                             # a jump over 0.55 and 0.60: one buy
    assert len(ups(d)) == 1 and d.whisk_update["rnext_Up"] == 4             # next at 0.65
    w3 = {**w, "rnext_Up": 4}                                               # bought the 0.15 step (0.60)
    assert "rnext_Up" not in run(w3, books=at(0.565)).whisk_update          # a 0.035 dip: 0.60 does not count again
    assert run(w3, books=at(0.545)).whisk_update["rnext_Up"] == 3           # a whole step under it: 0.55 again
    d = run(w3, books=at(0.435))                                            # a new low: the steps start from it
    assert d.whisk_update["rlow_Up"] == 0.435 and d.whisk_update["rnext_Up"] == 1 and ups(d) == []


PP = replace(P, pair_on=True)                 # pair mode: a 0.03 move within 1 s, pairs under $0.98, 3 s apart


def test_pair_mode_starts_with_the_token_that_fell():
    d = run({}, p=PP, wk_mid_move=+0.04)                                    # Up rose 0.04: Down fell
    assert [t[:3] for t in d.takes] == [("Down", 0.45, 60.0)] and d.tags["taker:Down"] == "wpair"   # bid + 0.01
    lg = d.whisk_update["leg_Down"]
    assert lg["kind"] == "pair" and lg["start"] and lg["max_px"] == 0.46 and d.whisk_update["pair_last_ms"] == T0
    assert run({}, p=PP, wk_mid_move=+0.02).takes == []                     # under the 0.03 move
    assert run({}, p=PP, wk_mid_move=None).takes == []
    assert run({"pair_last_ms": T0 - 2000}, p=PP, wk_mid_move=+0.04).takes == []   # 2 s after the last: too soon


def test_pair_mode_then_buys_only_the_token_with_fewer_shares_under_098():
    held = dict(shares={"Up": 60.0, "Down": 0.0}, costs={"Up": 30.0, "Down": 0.0}, window_cost=30.0)   # Up avg 0.50
    d = run({}, p=PP, wk_mid_move=-0.04, **held)                            # Down 0.45 + fee 0.017 + 0.50 = 0.967
    assert [t[:2] for t in d.takes] == [("Down", 0.45)] and d.whisk_update["leg_Down"]["max_px"] == 0.46
    assert run({}, p=PP, wk_mid_move=+0.04, **held).takes[0][0] == "Down"   # whichever way it moved: the fewer one
    dear = {"Up": {"bid": 0.49, "ask": 0.50}, "Down": {"bid": 0.50, "ask": 0.51}}     # 0.51 + 0.017 + 0.50 > 0.98
    assert run({}, books=dear, p=PP, wk_mid_move=-0.04, **held).takes == []
    assert run({}, p=replace(PP, pair_margin=0.05), wk_mid_move=-0.04, **held).takes == []   # 0.967 > 0.95


def test_a_pair_buy_does_not_chase_and_one_runs_at_a_time():
    held = dict(shares={"Up": 60.0, "Down": 0.0}, costs={"Up": 30.0, "Down": 0.0}, window_cost=30.0)
    lg = dict(leg(kind="pair", got=20.0, last_try=T0 - 300, max_px=0.46), start=False)
    up = {"Up": {"bid": 0.47, "ask": 0.48}, "Down": {"bid": 0.52, "ask": 0.53}}       # Down ran to 0.53
    d = run({"leg_Down": lg}, books=up, p=PP, wk_mid_move=-0.04, **held)
    assert d.takes == [] and d.whisk_update["leg_Down"] is None              # stopped, not chased
    d = run({"leg_Down": lg}, p=PP, wk_mid_move=+0.04, **held)              # still under: the rest of it
    assert [t[:3] for t in d.takes] == [("Down", 0.45, 40.0)]
    d = run({"leg_Up": dict(leg(kind="pair", got=0.0, last_try=T0 - 100), start=True, max_px=0.57)}, p=PP, wk_mid_move=+0.04, **held)
    assert all(t[0] != "Down" for t in d.takes)                              # a pair buy running: no second one


def test_pair_mode_has_no_balance_or_top_up():
    w = {"owe_Down": 60.0, "rise_fill_ms_Up": T0 - 5000}
    held = dict(shares={"Up": 60.0, "Down": 0.0}, costs={"Up": 42.0, "Down": 0.0}, window_cost=42.0)
    assert run(w, p=PP, wk_mid_move=0.0, **held).takes == []
    hi = {"Up": {"bid": 0.69, "ask": 0.70}, "Down": {"bid": 0.30, "ask": 0.31}}
    assert run({}, books=hi, p=PP, wk_mid_move=0.0, **held).takes == []      # Up at 0.695 losing if it wins: no top-up


def test_pair_every_move_buys_the_side_that_leaves_the_worse_outcome_best():
    pf = replace(PP, pair_free=True)
    held = dict(shares={"Up": 60.0, "Down": 0.0}, costs={"Up": 30.0, "Down": 0.0}, window_cost=30.0)   # if Up +30, if Down -30
    dear = {"Up": {"bid": 0.49, "ask": 0.50}, "Down": {"bid": 0.50, "ask": 0.51}}     # no pair under $0.98 here
    d = run({}, books=dear, p=pf, wk_mid_move=+0.04, **held)
    assert [t[:2] for t in d.takes] == [("Down", 0.51)] and d.whisk_update["leg_Down"]["start"]   # bid 0.50 + 0.01
    even = dict(shares={"Up": 60.0, "Down": 60.0}, costs={"Up": 30.0, "Down": 30.0}, window_cost=60.0)
    assert run({}, p=pf, wk_mid_move=-0.04, **even).takes[0][0] == "Down"   # Down cheaper (0.45): its buy costs least
    assert run({}, p=pf, wk_mid_move=+0.02, **held).takes == []             # still needs the move
