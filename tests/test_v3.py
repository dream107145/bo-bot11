"""Strategy V3 (user, 2026-10-13), every trigger and the settlement maths, on hand-built views."""
from dataclasses import replace

from xshark.config import Settings
from xshark.strategy import decide
from xshark.v3 import V3Params, after, position, recovery_shares

from test_strategy import M, view

P = V3Params(enabled=True)
T0 = (M.start + 60) * 1000.0
fee = lambda px, n=1.0: 0.07 * px * (1 - px) * n


def st():
    return Settings(maker_enabled=False, taker_enabled=False, pair_enabled=False, fav_enabled=False,
                    late_enabled=False, scalp_enabled=False)


def bk(ub, ua, depth=500.0, dasks=None):
    db, da = round(1 - ua, 4), round(1 - ub, 4)
    return {"Up": {"bid": ub, "ask": ua, "bid_size": depth, "ask_size": depth, "asks": [(ua, depth)], "bids": [(ub, depth)]},
            "Down": {"bid": db, "ask": da, "bid_size": depth, "ask_size": depth, "asks": dasks or [(da, depth)], "bids": [(db, depth)]}}


def fill(o, px, n, ts, tag, oid=1):
    return dict(outcome=o, price=px, size=n, fee=fee(px, n), tag=tag, ts=ts, oid=oid)


def run(w=None, books=None, ms=T0, p=P, fills=(), lows=None, **kw):
    fl = list(fills)
    cost = sum(f["price"] * f["size"] + f["fee"] for f in fl)
    sh = {o: sum(f["size"] for f in fl if f["outcome"] == o) for o in ("Up", "Down")}
    base = dict(books=books or bk(0.55, 0.56), now_ms=ms, secs_in=ms / 1000 - M.start, secs_left=M.end - ms / 1000,
                whisk=w or {}, v3_params=p, v2_fills=fl, v3_lows=lows or {}, shares=sh, window_cost=cost)
    base.update(kw)
    return decide(view(**base), st())


# ---------------------------------------------------------------- settlement maths

def test_position_settlement_outcomes_averages_pairs_and_fees():
    fs = [fill("Up", 0.40, 20.0, 1, "v3e"), fill("Down", 0.50, 10.0, 2, "v3r")]
    pos = position(fs)
    cost = 20 * 0.40 + fee(0.40, 20) + 10 * 0.50 + fee(0.50, 10)
    assert abs(pos["total"] - cost) < 1e-9 and abs(pos["fees"] - (fee(0.40, 20) + fee(0.50, 10))) < 1e-9
    assert abs(pos["avg"]["Up"] - (0.40 + fee(0.40))) < 1e-9 and abs(pos["avg"]["Down"] - (0.50 + fee(0.50))) < 1e-9
    assert pos["paired"] == 10 and pos["unmatched"] == 10
    assert abs(pos["if"]["Up"] - (20 - cost)) < 1e-9 and abs(pos["if"]["Down"] - (10 - cost)) < 1e-9
    assert abs(pos["worst"] - (10 - cost)) < 1e-9
    a = after(pos, "Down", 10.0, 10 * 0.45 + fee(0.45, 10))
    assert abs(a["Down"] - (pos["if"]["Down"] + 10 - 4.5 - fee(0.45, 10))) < 1e-9


def test_recovery_shares_cover_the_loss_with_fees_through_the_depth():
    v = view(books=bk(0.49, 0.50))                                          # Down ask 0.51
    n = recovery_shares(v, P, "Down", 4.0)
    g = 1 - 0.51 - fee(0.51)
    assert abs(n - 8.47) < 1e-9 and n * g >= 4.0 > (n - 0.01) * g          # not the fee-free 4 / (1 - 0.51) = 8.16
    v = view(books=bk(0.49, 0.50, dasks=[(0.51, 5.0), (0.52, 100.0)]))
    n2 = recovery_shares(v, P, "Down", 4.0)
    assert n2 > n                                                           # the second level pays less a share
    assert recovery_shares(view(books=bk(0.49, 0.50, dasks=[(0.51, 2.0)])), P, "Down", 4.0) is None   # too thin


# ---------------------------------------------------------------- triggers

def test_entry_buys_20_of_the_low_price_token_on_a_005_rise_and_makes_it_up():
    b = bk(0.54, 0.55)                                                      # Up 0.545, Down 0.455
    d = run(books=b, lows={"Up": 0.50, "Down": 0.40})                       # Down: low 0.40 < 0.50, +0.055
    assert d.takes == [("Down", 0.47, 20.0, 0.0)] and d.tags["taker:Down"] == "v3e"   # ask 0.46 + 0.01
    assert run(books=b, lows={"Up": 0.50, "Down": 0.42}).takes == []        # +0.035 only
    assert run(books=bk(0.44, 0.45), lows={"Up": 0.38, "Down": 0.55}).takes[0][0] == "Up"
    assert run(books=bk(0.30, 0.31), lows={"Up": 0.20, "Down": 0.69}).takes == []   # ask 0.31 > 0.20 + 0.05 + 0.03
    assert run(books=b, lows={"Up": 0.50, "Down": 0.52}).takes == []        # the low was not a low-price token


def test_momentum_adds_10_up_6_s_after_the_last_momentum_fill_005_over_it():
    e = [fill("Up", 0.45, 20.0, T0 - 7000, "v3e")]
    d = run(books=bk(0.50, 0.51), fills=e, w={"v3_dn_low": 0.49})          # Up mid 0.505 >= 0.45 + 0.05
    assert d.takes == [("Up", 0.52, 10.0, 0.0)] and d.tags["taker:Up"] == "v3m"
    assert run(books=bk(0.50, 0.51), fills=e, ms=T0 - 2000, w={"v3_dn_low": 0.49}).takes == []   # 5 s after: no
    m2 = e + [fill("Up", 0.51, 10.0, T0 - 6500, "v3m", 2)]
    assert run(books=bk(0.50, 0.51), fills=m2, w={"v3_dn_low": 0.49}).takes == []   # the reference is now 0.51
    assert run(books=bk(0.58, 0.59), fills=e, w={"v3_dn_low": 0.41}).takes == []    # ask 0.59 > 0.45 + 0.05 + 0.03


def test_recovery_buys_the_down_shares_that_bring_if_down_wins_to_zero_then_5_a_step():
    e = [fill("Up", 0.45, 20.0, T0 - 30000, "v3e")]                       # if Down wins -9.17
    b = bk(0.49, 0.50)                                                      # Down mid 0.505, ask 0.51
    d = run({"v3_dn_low": 0.40}, books=b, fills=e)
    loss = 20 * 0.45 + fee(0.45, 20)
    o, lim, n, _ = d.takes[0]
    assert (o, lim) == ("Down", 0.52) and d.tags["taker:Down"] == "v3r" and n * (1 - 0.51 - fee(0.51)) >= loss
    assert d.whisk_update["v3_rec_ref"] == 0.5
    assert not [t for t in run({"v3_dn_low": 0.42}, books=b, fills=e).takes if t[0] == "Down"]      # +0.085: no recovery
    r = e + [fill("Down", 0.51, n, T0 - 20000, "v3r", 2)]
    d = run({"v3_dn_low": 0.40, "v3_rec_ref": 0.50}, books=bk(0.44, 0.45), fills=r)   # Down 0.555 >= 0.55
    assert d.takes[0][:3] == ("Down", 0.57, 5.0) and d.tags["taker:Down"] == "v3s" and d.whisk_update["v3_rec_ref"] == 0.55
    assert not [t for t in run({"v3_dn_low": 0.40}, books=b, fills=e, p=replace(P, rec_max_px=0.50)).takes if t[0] == "Down"]  # ask over the cap


def test_level_b_buys_the_up_minus_down_gap_once():
    e = [fill("Up", 0.60, 20.0, T0 - 3000, "v3e")]
    w = {"v3_under_b_Down": True, "v3_dn_low": 0.40}
    d = run(w, books=bk(0.57, 0.58), fills=e)                              # Down 0.425 crossing 0.42
    assert d.takes == [("Down", 0.44, 20.0, 0.0)] and d.tags["taker:Down"] == "v3b"
    done = e + [fill("Down", 0.43, 20.0, T0 - 500, "v3b", 2)]
    assert not [t for t in run({**w, "v3_sent_v3b": T0 - 1500}, books=bk(0.57, 0.58), fills=done).takes if t[0] == "Down"]
    assert run({"v3_dn_low": 0.40}, books=bk(0.57, 0.58), fills=e).takes == []      # never under 0.42: no crossing


def test_balance_buys_the_side_that_lifts_the_worse_outcome_when_averages_sum_under_096():
    fs = [fill("Up", 0.40, 20.0, T0 - 2000, "v3e"), fill("Down", 0.45, 10.0, T0 - 1500, "v3r", 2)]
    d = run(books=bk(0.55, 0.56), fills=fs)                                # Down ask 0.45: lifts 'if Down' from -3.0
    assert d.takes == [("Down", 0.46, 10.0, 0.0)] and d.tags["taker:Down"] == "v3x"
    even = [fill("Up", 0.40, 20.0, T0 - 2000, "v3e"), fill("Down", 0.45, 20.0, T0 - 1500, "v3r", 2)]
    assert "v3x" not in run(books=bk(0.55, 0.56), fills=even).tags.values()   # no side lifts the worse outcome
    dear = [fill("Up", 0.55, 20.0, T0 - 2000, "v3e"), fill("Down", 0.45, 10.0, T0 - 1500, "v3r", 2)]
    assert "v3x" not in run(books=bk(0.55, 0.56), fills=dear).tags.values()   # averages 1.03: no balance


def test_level_a_buys_10_down_crossing_032_after_trading_under_030():
    e = [fill("Up", 0.60, 20.0, T0 - 2000, "v3e")]
    d = run({"v3_under_a_Down": True, "v3_dn_low": 0.28}, books=bk(0.67, 0.68), fills=e)   # Down 0.325
    assert d.takes == [("Down", 0.34, 10.0, 0.0)] and d.tags["taker:Down"] == "v3la"
    assert run({"v3_dn_low": 0.28}, books=bk(0.67, 0.68), fills=e).takes == []         # never under 0.30


def test_level_c_buys_20_down_then_10_on_each_005_over_the_last_fill():
    e = [fill("Up", 0.50, 20.0, T0 - 2000, "v3e")]
    w = {"v3_under_c_Down": True, "v3_dn_low": 0.40}
    d = run(w, books=bk(0.54, 0.55), fills=e)                              # Down 0.455
    assert d.takes == [("Down", 0.47, 20.0, 0.0)] and d.tags["taker:Down"] == "v3lc"
    c = e + [fill("Down", 0.46, 20.0, T0 - 1000, "v3lc", 2)]
    d = run({**w, "v3_done_c_Down": True, "v3_dn_low": 0.45}, books=bk(0.48, 0.49), fills=c)   # Down 0.515 >= 0.51
    assert d.takes[0][:3] == ("Down", 0.53, 10.0) and d.tags["taker:Down"] == "v3cm"


# ---------------------------------------------------------------- risk

def test_risk_limits_block_and_say_why():
    b = bk(0.54, 0.55)
    lows = {"Up": 0.50, "Down": 0.40}
    d = run(books=b, lows=lows, p=replace(P, max_spend=5.0))
    assert d.takes == [] and "spend" in d.why["taker"]
    assert run(books=b, lows=lows, in_flight_taker={"Up": 5.0, "Down": 0.0}).takes == []
    assert run(books=b, lows=lows, book_age_ms=5000.0).takes == []
    assert run(books=b, lows=lows, ms=(M.end - 20) * 1000.0).takes == []
    assert run({"v3_last_send": T0 - 500}, books=b, lows=lows, p=replace(P, resend_ms=1000.0)).takes == []
    assert run({"v3_last_send": T0 - 300, "v3_entry_tries": 1}, books=b, lows=lows).takes[0][0] == "Down"   # killed: again at once
    d = run(books=b, lows=lows, p=replace(P, max_order=4.0))
    assert d.takes == [] and "minimum" in d.why["taker"]
    d = run(books=b, lows=lows, p=replace(P, max_outcome_loss=5.0))
    assert d.takes == [] and "worse outcome" in d.why["taker"]
    wide = {"Up": {**b["Up"]}, "Down": {**b["Down"], "bid": 0.40, "ask": 0.46}}
    assert run(books=wide, lows=lows).takes == []                          # Down spread 0.06


def test_nothing_is_bought_over_085():
    e = [fill("Up", 0.45, 20.0, T0 - 30000, "v3e")]
    hi = bk(0.10, 0.11)                                                     # Down ask 0.90: recovery would buy it
    assert not [t for t in run({"v3_dn_low": 0.40}, books=hi, fills=e, p=replace(P, rec_max_px=0.95)).takes if t[0] == "Down"]
    edge = bk(0.15, 0.16)                                                   # Down ask 0.85: its limit stays 0.85, not 0.86
    d = run({"v3_dn_low": 0.40}, books=edge, fills=e, p=replace(P, rec_max_px=0.95))
    assert [t[1] for t in d.takes if t[0] == "Down"] == [0.85]
