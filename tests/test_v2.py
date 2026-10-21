"""Strategy V2 (user, 2026-10-13), on hand-built views."""
from dataclasses import replace

from xshark.config import Settings
from xshark.strategy import Resting, decide
from xshark.v2 import V2Params, unmatched_up

from test_strategy import M, view

P = V2Params(enabled=True)
T0 = (M.start + 60) * 1000.0


def st():
    return Settings(maker_enabled=False, taker_enabled=False, pair_enabled=False, fav_enabled=False,
                    late_enabled=False, scalp_enabled=False)


def bk(ub, ua, depth=500.0, asks=None, dasks=None):
    """Up bid/ask and the mirrored Down book, with depth at the best levels."""
    db, da = round(1 - ua, 4), round(1 - ub, 4)
    return {"Up": {"bid": ub, "ask": ua, "bid_size": depth, "ask_size": depth, "asks": asks or [(ua, depth)], "bids": [(ub, depth)]},
            "Down": {"bid": db, "ask": da, "bid_size": depth, "ask_size": depth, "asks": dasks or [(da, depth)], "bids": [(db, depth)]}}


def fill(o, px, n, ts, tag, oid=1):
    return dict(outcome=o, price=px, size=n, fee=0.07 * px * (1 - px) * n, tag=tag, ts=ts, oid=oid)


def run(w=None, books=None, ms=T0, p=P, fills=(), low=None, **kw):
    fl = list(fills)
    cost = sum(f["price"] * f["size"] + f["fee"] for f in fl)
    sh = {o: sum(f["size"] for f in fl if f["outcome"] == o) for o in ("Up", "Down")}
    base = dict(books=books or bk(0.55, 0.56), now_ms=ms, secs_in=ms / 1000 - M.start, secs_left=M.end - ms / 1000,
                whisk=w or {}, v2_params=p, v2_fills=fl, v2_up_low=low, shares=sh, window_cost=cost)
    base.update(kw)
    return decide(view(**base), st())


def test_a_enters_20_up_on_a_005_rise_from_the_local_low_through_the_depth():
    d = run(low=0.50)                                                       # mid 0.555 = low + 0.055
    assert d.takes == [("Up", 0.57, 20.0, 0.0)] and d.tags["taker:Up"] == "v2a"         # ask 0.56 + 0.01
    assert run(low=0.52).takes == []                                        # +0.035: not yet
    assert run(low=0.50, book_age_ms=5000.0).takes == []                    # stale book
    assert run(low=0.50, ms=(M.end - 50) * 1000.0).takes == []              # the last 60 s
    assert run(low=0.50, books=bk(0.53, 0.58)).takes == []                  # 0.05 spread: not a quote to trust
    deep = bk(0.55, 0.56, asks=[(0.56, 10.0), (0.57, 50.0)])
    assert run(low=0.50, books=deep).takes == [("Up", 0.57, 20.0, 0.0)]     # 10 at 0.56, 10 at 0.57
    thin = bk(0.55, 0.56, asks=[(0.56, 5.0), (0.60, 100.0)])
    assert run(low=0.50, books=thin).takes == []                            # 0.60 is past 0.56 + 0.02
    assert run(low=0.50, in_flight_taker={"Up": 20.0, "Down": 0.0}).takes == []   # one order on its way at a time


def test_the_budget_counts_filled_cost_and_orders_on_their_way():
    old = [fill("Down", 0.50, 40.0, T0 - 60000, "clip")]                    # $21 filled already
    d = run(low=0.50, fills=old)
    assert d.takes == [] and "budget" in d.why["taker"]
    assert run(low=0.50, p=replace(P, budget_usd=40.0), fills=old).takes[0][:3] == ("Up", 0.57, 20.0)


def test_b_adds_5_up_on_fill_price_plus_005_after_6_s_at_most_twice():
    f0 = fill("Up", 0.50, 20.0, T0 - 7000, "v2a")
    assert run(fills=[f0]).takes[0][:3] == ("Up", 0.57, 5.0)                # 0.555 >= 0.50 + 0.05
    assert run(fills=[f0], ms=T0 - 2000).takes == []                        # 5 s after the fill: too soon
    two = [f0, fill("Up", 0.40, 5.0, T0 - 30000, "v2b", 2), fill("Up", 0.45, 5.0, T0 - 20000, "v2b", 3)]
    assert all(t[0] != "Up" for t in run(fills=two).takes)                  # two add-ons used
    d = run(fills=[f0], books=bk(0.44, 0.45))                               # mid 0.445: 0.055 under the fill
    assert d.whisk_update["v2_up_stop"] and not any(t[0] == "Up" for t in d.takes)
    assert run({"v2_up_stop": "x"}, fills=[f0]).takes == []


def test_c_buys_5_down_only_while_the_matched_pair_costs_under_095():
    f0 = fill("Up", 0.50, 20.0, T0 - 3000, "v2a")                           # 0.50875 a share with its fee
    cheap = bk(0.59, 0.60)                                                  # Down ask 0.41: 0.50875 + 0.41 + 0.017 + 0.005
    d = run(fills=[f0], books=cheap)
    assert d.takes == [("Down", 0.41, 5.0, 0.0)] and d.tags["taker:Down"] == "v2c"      # capped: 0.42 would pass 0.95
    dear = bk(0.55, 0.56)                                                   # Down ask 0.45: 0.981
    assert run(fills=[f0], books=dear).takes == []
    nearly = [f0, fill("Down", 0.40, 18.0, T0 - 2000, "v2c", 2)]            # 2 UP unmatched: under the minimum
    assert run(fills=nearly, books=cheap).takes == []
    assert run({"v2_last_v2c": T0 - 1000}, fills=[f0], books=cheap).takes == []   # 2 s between balances


def test_unmatched_up_goes_oldest_first():
    fs = [fill("Up", 0.40, 10.0, 1, "v2a"), fill("Up", 0.60, 10.0, 2, "v2b"), fill("Down", 0.30, 15.0, 3, "v2c")]
    lots = unmatched_up(fs)
    assert len(lots) == 1 and abs(lots[0][1] - 5.0) < 1e-9 and abs(lots[0][0] - (0.60 + 0.07 * 0.6 * 0.4)) < 1e-9


def test_d1_buys_5_down_once_it_holds_032_after_trading_under_030():
    w = {"v2_dn_below": True, "v2_d1_since": T0 - 5000}
    b = bk(0.67, 0.68)                                                      # Down mid 0.325
    d = run(w, books=b, low=0.66)
    assert d.takes == [("Down", 0.34, 5.0, 0.0)] and d.tags["taker:Down"] == "v2d1"
    assert run({**w, "v2_d1_since": T0 - 3000}, books=b, low=0.66).takes == []      # held 3 s only
    assert run({"v2_d1_since": T0 - 5000}, books=b, low=0.66).takes == []           # never under 0.30
    assert run(w, books=b, low=0.66, fills=[fill("Down", 0.33, 5.0, T0 - 9000, "v2d1")]).takes == []   # once


def test_d2_buys_at_most_5_down_at_042_under_the_pair_condition_once_per_crossing():
    f0 = fill("Up", 0.45, 20.0, T0 - 10000, "v2a")                          # 0.4673 a share
    b = bk(0.57, 0.58)                                                      # Down mid 0.425, ask 0.43: 0.4673 + 0.43 + 0.017 + 0.005
    w = {"v2_d2_armed": True, "v2_last_v2c": T0 - 500}                      # (C waits, so D2 acts)
    d = run(w, books=b, fills=[f0])
    assert d.takes == [("Down", 0.44, 5.0, 0.0)] and d.tags["taker:Down"] == "v2d2" and d.whisk_update["v2_d2_armed"] is False
    assert not any(t[0] == "Down" for t in run({**w, "v2_d2_armed": False}, books=b, fills=[f0]).takes)   # not re-armed
    f1 = fill("Up", 0.55, 20.0, T0 - 10000, "v2a")                          # 0.5673: the pair costs 1.02
    d = run(w, books=b, fills=[f1])
    assert d.takes == [] and d.whisk_update["v2_d2_armed"] is False and "D2 skipped" in d.why["taker"]


def test_net_exposure_and_the_loss_limit():
    big = [fill("Up", 0.30, 30.0, T0 - 9000, "v2a")]
    d = run(fills=big, books=bk(0.40, 0.41), p=replace(P, budget_usd=100.0))       # B would make UP 35 - DOWN 0
    assert not any(t[0] == "Up" for t in d.takes)
    lose = [fill("Up", 0.60, 20.0, T0 - 9000, "v2a")]                      # $12.34 paid
    res = {"Up": [Resting(id=7, price=0.10, remaining=5.0, created_ms=T0 - 5000)], "Down": []}
    d = run(fills=lose, books=bk(0.05, 0.06), resting=res)                 # marked about -11.4
    assert d.whisk_update["v2_blocked"].startswith("loss limit") and d.cancels == [7] and d.takes == []
    assert run({"v2_blocked": "x"}, low=0.50).takes == []


def test_e_a_low_token_back_to_040_then_050_buys_10_and_10_then_5_of_the_other():
    w = {"v2_e_below_Down": True}
    d = run(w, books=bk(0.59, 0.60))                                       # Down mid 0.405 after trading under 0.30
    assert d.takes == [("Down", 0.42, 10.0, 0.0)] and d.tags["taker:Down"] == "v2e1"   # ask 0.41 + 0.01
    assert run({}, books=bk(0.59, 0.60)).takes == []                        # never under 0.30: no E
    e1 = [fill("Down", 0.41, 10.0, T0 - 5000, "v2e1")]
    assert run(w, books=bk(0.59, 0.60), fills=e1).takes == []              # 0.405: under 0.50
    d = run(w, books=bk(0.49, 0.50), fills=e1)                             # Down mid 0.505
    assert d.takes == [("Down", 0.52, 10.0, 0.0)] and d.tags["taker:Down"] == "v2e2"
    e2 = e1 + [fill("Down", 0.51, 10.0, T0 - 3000, "v2e2", 2)]
    d = run(w, books=bk(0.40, 0.41), fills=e2)                             # Up ask 0.41: 0.417 + 0.41 + 0.017 + 0.005
    assert d.takes == [("Up", 0.42, 5.0, 0.0)] and d.tags["taker:Up"] == "v2e3"
    e3 = e2 + [fill("Up", 0.42, 5.0, T0 - 1000, "v2e3", 3)]
    assert run(w, books=bk(0.40, 0.41), fills=e3).takes == []              # each once
    assert run(w, books=bk(0.09, 0.10)).takes == []                        # Down ask 0.91: over 0.85, no E1
