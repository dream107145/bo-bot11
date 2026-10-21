"""Strategy V6, run and hedge (user, 2026-10-14), on hand-built views with a fixed clock."""
from dataclasses import replace

from xshark.config import Settings
from xshark.strategy import Resting, decide
from xshark.v6 import V6Params

from test_strategy import M, view

P = V6Params(enabled=True)                    # maker first for 1.5 s, then the ask
PT = replace(P, maker_s=0.0)                  # taker only
T0 = (M.start + 60) * 1000.0
fee = lambda px, n=1.0: 0.07 * px * (1 - px) * n
LOWS = {"v6_low_Up": 0.50, "v6_low_Down": 0.44}
MID = (M.start + 150) * 1000.0                # 2:30 in: a start up to 0.60


def st():
    return Settings(maker_enabled=False, taker_enabled=False, pair_enabled=False, fav_enabled=False,
                    late_enabled=False, scalp_enabled=False)


def bk(ub, ua, depth=500.0):
    db, da = round(1 - ua, 4), round(1 - ub, 4)
    return {"Up": {"bid": ub, "ask": ua, "bid_size": depth, "ask_size": depth, "asks": [(ua, depth)], "bids": [(ub, depth)]},
            "Down": {"bid": db, "ask": da, "bid_size": depth, "ask_size": depth, "asks": [(da, depth)], "bids": [(db, depth)]}}


def fill(o, px, n, ts, tag, oid=1):
    return dict(outcome=o, price=px, size=n, fee=fee(px, n), tag=tag, ts=ts, oid=oid)


def run(w=None, books=None, ms=T0, p=PT, fills=(), **kw):
    fl = list(fills)
    cost = sum(f["price"] * f["size"] + f["fee"] for f in fl)
    sh = {o: sum(f["size"] for f in fl if f["outcome"] == o) for o in ("Up", "Down")}
    base = dict(books=books or bk(0.55, 0.56), now_ms=ms, secs_in=ms / 1000 - M.start, secs_left=M.end - ms / 1000,
                whisk=w or {}, v6_params=p, v2_fills=fl, shares=sh, window_cost=cost)
    base.update(kw)
    return decide(view(**base), st())


def buy(tag, tok, q, t0, mid, stage="take"):
    return {"tag": tag, "tok": tok, "q": q, "t0": t0, "mid": mid, "stage": stage}


def test_a_005_rise_starts_a_run_with_5_shares():
    d = run(LOWS, ms=MID)                                                  # Up 0.555: +0.055
    assert d.takes == [("Up", 0.57, 10.0, 0.0)] and d.tags["taker:Up"] == "v6s"   # the start: 10
    assert run({"v6_low_Up": 0.52, "v6_low_Down": 0.44}, ms=MID).takes == []   # +0.035: no


def test_the_run_adds_5_every_6_s_while_not_fallen_and_hedges_the_runs_shares_when_it_falls():
    w = {**LOWS, "v6_buy": buy("v6s", "Up", 10.0, T0 - 300, 0.555)}
    d = run(w, fills=[fill("Up", 0.56, 10.0, T0 - 200, "v6s")])           # filled: the run, next check in 6 s
    assert d.takes == [] and d.whisk_update["v6_phase"] == "run" and d.whisk_update["v6_run_sh"] == 10.0
    assert d.whisk_update["v6_adds"] == 0
    assert d.whisk_update["v6_next_check"] == T0 - 200 + 6000
    r = {**LOWS, "v6_phase": "run", "v6_tok": "Up", "v6_run_sh": 15.0, "v6_last_mid": 0.555, "v6_next_check": T0 - 10}
    assert run({**r, "v6_adds": 0}).takes == [("Up", 0.57, 8.0, 0.0)]     # not fallen: the first add 8,
    assert run({**r, "v6_adds": 1}).takes == [("Up", 0.57, 6.0, 0.0)]     # the second 6,
    d = run({**r, "v6_adds": 2})                                           # then 5 each
    assert d.takes == [("Up", 0.57, 5.0, 0.0)] and d.tags["taker:Up"] == "v6r"
    assert run({**r, "v6_next_check": T0 + 2000}).takes == []              # before the 6 s are up
    d = run(r, books=bk(0.53, 0.54))                                       # Up 0.535: fell 0.02 -> 15 Down
    assert d.takes == [("Down", 0.48, 15.0, 0.0)] and d.tags["taker:Down"] == "v6h" and d.whisk_update["v6_target"] == 15.0


def test_after_the_hedge_only_the_hedge_token_starts_the_next_run_6_s_later():
    h = {**LOWS, "v6_phase": "hedge", "v6_tok": "Up", "v6_hedge_tok": "Down", "v6_target": 15.0, "v6_hedged": 0.0,
         "v6_buy": buy("v6h", "Down", 15.0, T0 - 300, 0.465)}
    d = run(h, books=bk(0.53, 0.54), fills=[fill("Down", 0.47, 15.0, T0 - 200, "v6h")])
    assert d.whisk_update["v6_phase"] == "idle" and d.whisk_update["v6_next"] == "Down"
    assert d.whisk_update["v6_idle_from"] == T0 - 200 + 6000 and d.takes == []
    assert not d.whisk_update.get("v6_pair_reentry")
    idle = {"v6_phase": "idle", "v6_next": "Down", "v6_idle_from": MID - 100, "v6_low_Up": 0.40, "v6_low_Down": 0.40}
    d = run(idle, books=bk(0.54, 0.55), ms=MID)                            # Down 0.455 +0.055 (Up too): Down only
    assert [t[0] for t in d.takes] == ["Down"] and d.tags["taker:Down"] == "v6s"
    assert run({**idle, "v6_idle_from": MID + 3000}, books=bk(0.54, 0.55), ms=MID).takes == []   # within 6 s
    assert run(idle, books=bk(0.54, 0.55), p=replace(PT, next_any=True), ms=MID).takes[0][0] in ("Up", "Down")


def test_a_killed_order_goes_again_at_once_and_limits_hold():
    r = {**LOWS, "v6_phase": "run", "v6_tok": "Up", "v6_run_sh": 10.0, "v6_last_mid": 0.555, "v6_next_check": T0 - 10,
         "v6_adds": 3, "v6_buy": buy("v6r", "Up", 5.0, T0 - 300, 0.555)}
    d = run(r)                                                             # nothing filled: again now
    assert d.takes == [("Up", 0.57, 5.0, 0.0)] and any(x["event"] == "killed" for x in d.ledger)
    assert run({"v6_low_Up": 0.90, "v6_low_Down": 0.04}, books=bk(0.95, 0.96)).takes == []   # over 0.95
    assert run(LOWS, ms=(M.end - 5) * 1000.0).takes == []                  # last 10 s
    assert run(LOWS, live=True).takes == []                                # paper only
    assert run(LOWS, p=replace(PT, max_spend=2.0)).takes == []


def test_maker_first_a_bid_at_the_best_bid_then_the_ask_for_the_rest():
    d = run(LOWS, p=P, ms=MID)                                             # the start: a buy in its bid stage
    assert d.takes == [] and d.whisk_update["v6_buy"]["stage"] == "bid"
    w = {**LOWS, "v6_buy": buy("v6s", "Up", 10.0, T0 - 500, 0.555, "bid")}     # (10: the start)
    d = run(w, p=P, books=bk(0.54, 0.57))                                  # spread 0.03: bid 0.55 (one tick in)
    assert d.places == [("Up", 0.55, 10.0)] and d.tags["maker:Up"] == "v6s" and d.takes == []
    d = run(w, p=P, books=bk(0.55, 0.56))                                  # spread 0.01: at the best bid
    assert d.places == [("Up", 0.55, 10.0)]
    late = {**w, "v6_buy": buy("v6s", "Up", 10.0, MID - 2000, 0.555, "bid")}
    part = [fill("Up", 0.55, 3.0, MID - 1200, "v6s")]                      # the bid got 3 in 1.5 s
    d = run(late, p=P, fills=part, ms=MID)                                       # the rest, 7, at the ask
    assert d.takes == [("Up", 0.57, 7.0, 0.0)] and d.whisk_update["v6_buy"]["stage"] == "take"
    res = {"Up": [Resting(id=5, price=0.55, remaining=7.0, created_ms=MID - 2000)], "Down": []}
    d = run(late, p=P, fills=part, resting=res, ms=MID)                            # its bid still out: cancelled first
    assert d.takes == [] and 5 in d.cancels
    full = [fill("Up", 0.55, 10.0, MID - 1000, "v6s")]
    d = run(late, p=P, fills=full, ms=MID)                                         # the bid got it all: no taker
    assert d.takes == [] and d.whisk_update["v6_phase"] == "run" and d.whisk_update["v6_run_sh"] == 10.0


def test_the_hedge_is_120_pct_when_dearer_than_the_run_and_80_pct_when_cheaper():
    r = {**LOWS, "v6_phase": "run", "v6_tok": "Up", "v6_run_sh": 24.0, "v6_run_cost": 24 * 0.40, "v6_last_mid": 0.555,
         "v6_next_check": T0 - 10}
    d = run(r, books=bk(0.53, 0.54))                                       # Down ask 0.47 > run avg 0.40: 24 x 1.2
    assert d.whisk_update["v6_target"] == 28.8 and d.takes[0][:3] == ("Down", 0.48, 28.8)
    r2 = {**r, "v6_run_cost": 24 * 0.60}
    d = run(r2, books=bk(0.53, 0.54))                                      # 0.47 < run avg 0.60: 24 x 0.8
    assert d.whisk_update["v6_target"] == 19.2 and d.takes[0][:3] == ("Down", 0.48, 19.2)


def test_the_run_keeps_its_average_fill_price():
    w = {**LOWS, "v6_phase": "run", "v6_tok": "Up", "v6_run_sh": 10.0, "v6_run_cost": 5.6, "v6_adds": 0,
         "v6_buy": buy("v6r", "Up", 8.0, T0 - 300, 0.555)}
    d = run(w, fills=[fill("Up", 0.60, 8.0, T0 - 200, "v6r")])
    assert d.whisk_update["v6_run_sh"] == 18.0 and abs(d.whisk_update["v6_run_cost"] - (5.6 + 4.8)) < 1e-9


def test_a_cheap_pair_hedges_at_once_as_a_taker():
    r = {**LOWS, "v6_phase": "run", "v6_tok": "Up", "v6_run_sh": 24.0, "v6_run_cost": 24 * 0.60, "v6_last_mid": 0.665,
         "v6_next_check": T0 + 4000}                                       # the 6 s check is not due
    d = run(r, books=bk(0.66, 0.67), p=P)                                  # Down ask 0.34: 0.34 + 0.60 = 0.94 < 0.95
    assert d.takes == [("Down", 0.34, 19.2, 0.0)] and d.tags["taker:Down"] == "v6h"   # 24 x 0.8, limit capped at 0.34
    assert d.whisk_update["v6_phase"] == "hedge" and d.whisk_update["v6_quick"] == 0.34
    assert run(r, books=bk(0.64, 0.65), p=P).takes == []                   # Down 0.36: 0.96, not under 0.95
    q = {**r, "v6_phase": "hedge", "v6_hedge_tok": "Down", "v6_target": 19.2, "v6_hedged": 0.0, "v6_quick": 0.34}
    d = run(q, books=bk(0.60, 0.61), p=P)                                  # Down rose to 0.40 before any fill
    assert d.takes == [] and d.whisk_update["v6_phase"] == "run"


def test_completed_quick_pair_buys_both_tokens_together():
    hedge = {**LOWS, "v6_phase": "hedge", "v6_tok": "Up", "v6_hedge_tok": "Down",
             "v6_target": 19.2, "v6_hedged": 0.0, "v6_quick": 0.34,
             "v6_buy": buy("v6h", "Down", 19.2, T0 - 300, 0.335, "take")}
    fills = [fill("Up", 0.60, 24.0, T0 - 60000, "v6s"), fill("Down", 0.34, 19.2, T0 - 100, "v6h")]
    completed = run(hedge, books=bk(0.54, 0.55), fills=fills)
    assert completed.takes == [] and completed.whisk_update["v6_pair_reentry"]["q"] == 10.0

    d = run({**LOWS, **completed.whisk_update}, books=bk(0.54, 0.55), ms=T0 + 500, fills=fills)
    assert [(o, q) for o, _, q, _ in d.takes] == [("Up", 10.0), ("Down", 10.0)]
    assert d.tags["taker:Up"] == d.tags["taker:Down"] == "v6s"


def test_quick_pair_reentry_waits_if_either_token_exceeds_its_time_band_cap():
    pair = {**LOWS, "v6_phase": "idle", "v6_pair_reentry": {"t0": T0, "q": 10.0, "next": "Down"}}
    d = run(pair, books=bk(0.42, 0.61), ms=T0 + 500, p=PT)
    assert d.takes == []
    assert not d.whisk_update and "quick-pair reentry waiting" in d.why["taker"]


def test_rising_buys_only_between_030_and_065():
    assert run({"v6_low_Up": 0.15, "v6_low_Down": 0.70}, books=bk(0.21, 0.22)).takes == []      # Up 0.22: no start
    assert run({"v6_low_Up": 0.54, "v6_low_Down": 0.50}, books=bk(0.60, 0.61)).takes == []      # 0.61 at 1:00: over 0.60
    assert run({"v6_low_Up": 0.30, "v6_low_Down": 0.60}, books=bk(0.35, 0.36)).takes[0][0] == "Up"   # 0.36: yes
    r = {**LOWS, "v6_phase": "run", "v6_tok": "Up", "v6_run_sh": 10.0, "v6_run_cost": 2.0, "v6_last_mid": 0.205,
         "v6_next_check": T0 - 10, "v6_adds": 0}
    d = run(r, books=bk(0.21, 0.22), p=replace(PT, quick_pair=0.0))        # not fallen, but 0.22: no add, look later
    assert d.takes == [] and d.whisk_update["v6_next_check"] == T0 + 6000
    h = {**r, "v6_phase": "hedge", "v6_hedge_tok": "Up", "v6_target": 10.0, "v6_hedged": 0.0}
    assert run(h, books=bk(0.21, 0.22)).takes[0][0] == "Up"               # a hedge still buys a cheap token


def test_a_favourite_over_080_is_bought_until_its_outcome_is_positive():
    held = [fill("Down", 0.30, 20.0, T0 - 60000, "v6s"), fill("Up", 0.65, 10.0, T0 - 50000, "v6h", 2)]   # if Up -2.8
    w = {"v6_phase": "idle", "v6_next": "Up", "v6_idle_from": T0 + 5000, "v6_low_Up": 0.60, "v6_low_Down": 0.10}
    d = run(w, books=bk(0.85, 0.86), fills=held, dist_usd=80.0)          # Up 0.855 > 0.80, BTC $80 over the strike
    assert d.takes == [("Up", 0.87, 15.0, 0.0)] and d.tags["taker:Up"] == "v6f"   # needs ~21.4: 15 now (5-15 an order)
    assert run(w, books=bk(0.85, 0.86), fills=held, dist_usd=30.0).takes == []     # only $30: not far enough
    assert run(w, books=bk(0.85, 0.86), fills=held, dist_usd=-80.0).takes == []    # on Down's side
    more = held + [fill("Up", 0.86, 15.0, T0 - 500, "v6f", 3)]             # still -0.82: again, 7.x (needs ~6.3 -> at least 5)
    o, lim, q, _ = run(w, books=bk(0.85, 0.86), fills=more, dist_usd=80.0).takes[0]
    assert o == "Up" and 5 <= q <= 15
    assert run(w, books=bk(0.78, 0.79), fills=held, dist_usd=80.0).takes == []     # 0.785: not over 0.80
    ok = held + [fill("Up", 0.86, 25.0, T0 - 1000, "v6f", 3)]
    assert "v6f" not in run(w, books=bk(0.85, 0.86), fills=ok, dist_usd=80.0).tags.values()   # already positive


def test_run_buys_go_up_to_085_starts_only_to_065():
    r = {**LOWS, "v6_phase": "run", "v6_tok": "Up", "v6_run_sh": 10.0, "v6_run_cost": 6.0, "v6_last_mid": 0.745,
         "v6_next_check": T0 - 10, "v6_adds": 0}
    assert run(r, books=bk(0.74, 0.75), p=replace(PT, quick_pair=0.0)).takes[0][:3] == ("Up", 0.76, 8.0)   # 0.75: add
    r2 = {**r, "v6_last_mid": 0.865}
    assert run(r2, books=bk(0.86, 0.87), p=replace(PT, quick_pair=0.0)).takes == []          # 0.87: over 0.85


def test_a_start_at_031_is_allowed():
    assert run({"v6_low_Up": 0.25, "v6_low_Down": 0.65}, books=bk(0.30, 0.31)).takes[0][0] == "Up"   # 0.31: from 0.30
    assert run({"v6_low_Up": 0.22, "v6_low_Down": 0.70}, books=bk(0.28, 0.29)).takes == []            # 0.29: under


def test_both_negative_buys_the_cheaper_token_until_its_outcome_is_positive():
    neg = [fill("Up", 0.55, 10.0, T0 - 60000, "v6s"), fill("Down", 0.50, 10.0, T0 - 50000, "v6h", 2)]   # -0.85 / -0.85
    w = {"v6_phase": "idle", "v6_next": "Down", "v6_idle_from": T0 + 5000, "v6_low_Up": 0.40, "v6_low_Down": 0.50}
    d = run(w, books=bk(0.45, 0.46), fills=neg)                            # Up 0.455 the cheaper
    assert d.takes == [("Up", 0.47, 5.0, 0.0)] and d.tags["taker:Up"] == "v6n"    # needs ~1.7: at least 5
    cost = sum(f["price"] * f["size"] + f["fee"] for f in neg) + 5 * (0.46 + fee(0.46))
    assert 15 - cost > 0                                                   # 'if Up wins' positive after it
    assert "v6n" not in run(w, books=bk(0.45, 0.46), fills=neg, p=replace(PT, neg_fix=False)).tags.values()


def test_start_caps_follow_each_minute_band():
    caps = (0.45, 0.60, 0.70, 0.75, 0.85)
    p = replace(PT, fav_px=0.0)
    for minute, cap in enumerate(caps):
        for ask, should_start in ((cap, True), (cap + 0.01, False)):
            low = ask - 0.06
            w = {"v6_low_Up": low, "v6_low_Down": 0.10}
            d = run(w, books=bk(ask - 0.01, ask), ms=(M.start + minute * 60 + 30) * 1000.0, p=p)
            assert bool(d.takes) is should_start, (minute, ask, d.takes)
            if should_start:
                assert d.tags["taker:Up"] == "v6s"


def test_a_run_buy_is_not_finished_as_a_taker_into_a_fall_and_fallen_counts_from_the_fill():
    late = {**LOWS, "v6_phase": "run", "v6_tok": "Down", "v6_run_sh": 10.0, "v6_run_cost": 5.1, "v6_last_mid": 0.51,
            "v6_adds": 0, "v6_next_check": T0 + 6000, "v6_buy": buy("v6r", "Down", 8.0, T0 - 2000, 0.435, "bid")}
    d = run(late, p=P, books=bk(0.63, 0.64))                               # Down 0.365 now, 0.435 when decided
    assert d.takes == [] and d.whisk_update["v6_buy"] is None and d.whisk_update["v6_next_check"] == T0
    start = {**LOWS, "v6_buy": buy("v6s", "Down", 10.0, T0 - 2000, 0.385, "bid")}
    d = run(start, p=P, books=bk(0.63, 0.64))                              # a start that fell back: dropped
    assert d.takes == [] and d.whisk_update["v6_buy"] is None
    w = {**LOWS, "v6_buy": buy("v6s", "Down", 10.0, T0 - 2500, 0.385, "take")}
    d = run(w, fills=[fill("Down", 0.51, 10.0, T0 - 200, "v6s")])          # the start filled at 0.51
    assert d.whisk_update["v6_last_mid"] == 0.51                           # 'fallen' counts from 0.51, not 0.385


def test_a_favourite_is_bought_with_no_shares_too():
    w = {"v6_phase": "idle", "v6_idle_from": T0 + 9000, "v6_low_Up": 0.80, "v6_low_Down": 0.13}
    d = run(w, books=bk(0.85, 0.86), dist_usd=80.0, p=replace(PT, fav_margin=1.0, fav_max=10.0))
    o, lim, q, _ = d.takes[0]
    assert (o, lim) == ("Up", 0.87) and d.tags["taker:Up"] == "v6f" and 5 <= q <= 10   # ~7: 'if Up wins' over $1
    assert run(w, books=bk(0.85, 0.86), dist_usd=50.0, p=replace(PT, fav_min_dist=70.0)).takes == []   # BTC too near
