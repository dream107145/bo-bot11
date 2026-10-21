"""Strategy V5, momentum + adaptive hedging (user, 2026-10-13): the 20 cases of the spec, on hand-built views
with a fixed clock (no network, no venue, no real orders)."""
from dataclasses import replace

from xshark.config import Settings
from xshark.strategy import Resting, decide
from xshark.v3 import position
from xshark.v5 import V5Params, child_sizes, dedupe, projected, risk, unmatched_cost

from test_strategy import M, view

P = V5Params(enabled=True)
T0 = (M.start + 60) * 1000.0
fee = lambda px, n=1.0: 0.07 * px * (1 - px) * n


def st():
    return Settings(maker_enabled=False, taker_enabled=False, pair_enabled=False, fav_enabled=False,
                    late_enabled=False, scalp_enabled=False)


def bk(ub, ua, depth=500.0):
    db, da = round(1 - ua, 4), round(1 - ub, 4)
    return {"Up": {"bid": ub, "ask": ua, "bid_size": depth, "ask_size": depth, "asks": [(ua, depth)], "bids": [(ub, depth)]},
            "Down": {"bid": db, "ask": da, "bid_size": depth, "ask_size": depth, "asks": [(da, depth)], "bids": [(db, depth)]}}


def fill(o, px, n, ts, tag, oid=1):
    return dict(outcome=o, price=px, size=n, fee=fee(px, n), tag=tag, ts=ts, oid=oid)


def run(w=None, books=None, ms=T0, p=P, fills=(), **kw):
    fl = list(fills)
    cost = sum(f["price"] * f["size"] + f["fee"] for f in fl)
    sh = {o: sum(f["size"] for f in fl if f["outcome"] == o) for o in ("Up", "Down")}
    base = dict(books=books or bk(0.54, 0.55), now_ms=ms, secs_in=ms / 1000 - M.start, secs_left=M.end - ms / 1000,
                whisk=w or {}, v5_params=p, v2_fills=fl, shares=sh, window_cost=cost)
    base.update(kw)
    return decide(view(**base), st())


REF = {"v5_ref_Up": 0.55, "v5_ref_Down": 0.46}           # no move on either side at bk(0.54, 0.55)


def cycle(t_fill, q=8.0, px=0.56):
    return {"id": 1, "phase": "balance", "o": "Up", "q": 12, "qr": q, "vwap": px, "fee_ps": fee(px), "t_fill": t_fill,
            "deadline": t_fill + 5000.0, "target": int(q) + 1, "sent": t_fill - 300, "prev_ref": 0.50}


# 1-6: the momentum signal

def test_01_up_rising_exactly_005_signals():
    d = run({**REF, "v5_ref_Up": 0.50})
    assert d.takes == [("Up", 0.56, 8.0, 0.0)] and d.tags["taker:Up"] == "v5m"     # 8: the most within the -$5 worst case
    assert d.whisk_update["v5_ref_Up"] == 0.55 and d.whisk_update["v5_cycle"]["phase"] == "entry"


def test_02_down_rising_exactly_005_signals():
    d = run({"v5_ref_Up": 0.45, "v5_ref_Down": 0.51}, books=bk(0.44, 0.45))      # Down ask 0.56, the leading token
    assert d.takes[0][0] == "Down" and d.tags["taker:Down"] == "v5m"


def test_03_a_rise_under_005_does_not_trigger():
    assert run({**REF, "v5_ref_Up": 0.51}).takes == []


def test_04_the_same_move_does_not_trigger_twice():
    d = run({**REF, "v5_ref_Up": 0.50})
    w = {**REF, **d.whisk_update}
    assert run(w, in_flight_taker={"Up": 8.0, "Down": 0.0}).takes == []           # on its way
    filled = [fill("Up", 0.55, 8.0, T0 + 300, "v5m")]
    d2 = run(w, fills=filled, ms=T0 + 500)                                         # back, filled: balancing, not again
    assert d2.whisk_update["v5_cycle"]["phase"] == "balance" and not d2.takes


def test_05_cooldown_is_per_outcome():
    recent = [fill("Up", 0.50, 5.0, T0 - 3000, "v5m"), fill("Down", 0.45, 5.0, T0 - 2500, "v5b", 2)]
    w = {"v5_ref_Up": 0.50, "v5_ref_Down": 0.41}
    d = run(w, fills=recent, p=replace(P, avg_pair_max=0.0, high_wins=False))      # (only the cooldowns here)
    assert [t[0] for t in d.takes] == ["Down"]                                     # Up 3 s ago: cooling; Down free


def test_06_prices_outside_008_089_are_rejected():
    assert run({"v5_ref_Up": 0.02, "v5_ref_Down": 0.95}, books=bk(0.06, 0.07)).takes == []
    assert run({"v5_ref_Up": 0.85, "v5_ref_Down": 0.12}, books=bk(0.89, 0.90)).takes == []


# 7-10: balancing

def test_07_a_balance_inside_5_s_only_when_every_check_passes():
    t_fill = T0 - 2000
    f = [fill("Up", 0.56, 8.0, t_fill, "v5m")]
    d = run({**REF, "v5_cycle": cycle(t_fill)}, books=bk(0.65, 0.66), fills=f)    # Down ask 0.35: pair 0.943
    assert d.takes == [("Down", 0.36, 8.0, 0.0)] and d.tags["taker:Down"] == "v5b"   # 8: both outcomes positive
    assert run({**REF, "v5_cycle": cycle(t_fill)}, books=bk(0.65, 0.66), fills=f, ms=t_fill + 500).takes == []   # < 1 s


def test_08_a_balance_after_the_5_s_deadline_is_rejected_and_the_cycle_closed():
    t_fill = T0 - 5500
    f = [fill("Up", 0.56, 8.0, t_fill, "v5m")]
    d = run({**REF, "v5_ref_Up": 0.66, "v5_ref_Down": 0.35, "v5_cycle": cycle(t_fill)}, books=bk(0.65, 0.66), fills=f)
    assert not [t for t in d.takes if t[0] == "Down"] and d.whisk_update["v5_cycle"] is None
    assert d.ledger[-1]["event"] == "cycle" and d.ledger[-1]["status"] == "unhedged"


def test_09_a_pair_under_097_before_fees_but_not_after_is_rejected():
    t_fill = T0 - 2000
    f = [fill("Up", 0.56, 8.0, t_fill, "v5m")]                                     # 0.56 + 0.40 = 0.96 < 0.97
    d = run({**REF, "v5_cycle": cycle(t_fill)}, books=bk(0.60, 0.61), fills=f)    # with fees: 0.577 + 0.417 = 0.994
    assert d.takes == []


def test_10_a_cheap_pair_fails_the_portfolio_limits():
    t_fill = T0 - 2000
    f = [fill("Up", 0.56, 8.0, t_fill, "v5m")]
    assert run({**REF, "v5_cycle": cycle(t_fill)}, books=bk(0.65, 0.66), fills=f, p=replace(P, max_spend=6.0)).takes == []


# 11-15: P/L and accounting

def test_11_both_outcomes_positive_is_recognised():
    f = [fill("Up", 0.56, 8.0, 1, "v5m"), fill("Down", 0.36, 8.0, 2, "v5b", 2)]
    pos = position(f)
    assert pos["if"]["Up"] > 0 and pos["if"]["Down"] > 0 and abs(pos["if"]["Up"] - (8 - pos["total"])) < 1e-12
    pj = projected([f[0]], "Down", 8, 8 * 0.36 + fee(0.36, 8))
    assert pj["Up"] > 0 and pj["Down"] > 0


def test_12_partial_fills_set_the_batch_from_what_filled():
    w = {**REF, "v5_cycle": {"id": 3, "phase": "entry", "o": "Up", "q": 12, "sent": T0 - 400, "prev_ref": 0.50}}
    f = [fill("Up", 0.55, 4.0, T0 - 200, "v5m", 7), fill("Up", 0.56, 3.0, T0 - 150, "v5m", 7)]
    d = run(w, fills=f)
    c = d.whisk_update["v5_cycle"]
    assert c["phase"] == "balance" and c["qr"] == 7.0 and c["target"] == 8
    assert abs(c["vwap"] - (4 * 0.55 + 3 * 0.56) / 7) < 1e-12 and abs(c["fee_ps"] - (fee(0.55, 4) + fee(0.56, 3)) / 7) < 1e-12
    assert d.ledger[0]["event"] == "momentum_filled" and d.ledger[0]["remaining"] == 5.0
    pos = position(f)
    assert pos["shares"]["Up"] == 7.0 and abs(pos["total"] - (4 * 0.55 + 3 * 0.56 + fee(0.55, 4) + fee(0.56, 3))) < 1e-12


def test_13_a_duplicate_fill_event_is_counted_once_and_stops_the_market():
    f = fill("Up", 0.56, 8.0, T0 - 3000, "v5m")
    once, dup = dedupe([f, dict(f)])
    assert dup == 1 and position(once)["shares"]["Up"] == 8.0 and abs(position(once)["fees"] - fee(0.56, 8)) < 1e-12
    d = run({**REF, "v5_ref_Up": 0.50}, fills=[f, dict(f)])
    assert d.takes == [] and "duplicate" in d.whisk_update["v5_kill"]


def test_14_and_15_child_orders_are_5_to_12_and_a_13_target_splits_8_then_5():
    for rem in range(5, 30):
        assert all(5 <= q <= 12 for q in child_sizes(P, rem))
    assert child_sizes(P, 4) == [] and child_sizes(P, 13)[0] == 8 and child_sizes(P, 5) == [5] and child_sizes(P, 9)[0] == 9
    t_fill = T0 - 2000                                                              # 12 filled: target 13 -> 8 first
    f = [fill("Up", 0.40, 12.0, t_fill, "v5m")]
    p = replace(P, max_worst_loss=50.0)
    d = run({**REF, "v5_cycle": cycle(t_fill, 12.0, 0.40)}, books=bk(0.53, 0.54), fills=f, p=p)   # Down ask 0.47
    assert d.takes and d.takes[0][2] in (8.0, 12.0, 5.0) and 5 <= d.takes[0][2] <= 12


# 16: the limits

def test_16_spend_shares_imbalance_unmatched_and_loss_limits():
    v = view(books=bk(0.54, 0.55), now_ms=T0, secs_in=60, secs_left=240, shares={"Up": 0.0, "Down": 0.0}, window_cost=0.0)
    assert risk(v, P, [], "Up", 8.0, 0.56, fee(0.56, 8)) == ""
    assert "worst outcome" in risk(v, P, [], "Up", 12.0, 0.56, fee(0.56, 12))
    assert "spend" in risk(v, replace(P, max_spend=2.5), [], "Up", 5.0, 0.56, fee(0.56, 5))       # $2.89
    big = [fill("Up", 0.50, 55.0, 1, "x"), fill("Down", 0.50, 55.0, 2, "x", 2)]
    assert "shares" in risk(v, replace(P, max_spend=1e9, max_worst_loss=1e9), big, "Up", 8.0, 0.56, fee(0.56, 8))
    assert "imbalance" in risk(v, replace(P, max_worst_loss=1e9, max_unmatched_cost=1e9), [fill("Up", 0.5, 8.0, 1, "x")], "Up", 5.0, 0.56, fee(0.56, 5))
    assert "unmatched" in risk(v, replace(P, max_unmatched_cost=3.0), [], "Up", 8.0, 0.56, fee(0.56, 8))
    assert abs(unmatched_cost([fill("Up", 0.5, 8.0, 1, "x"), fill("Down", 0.4, 5.0, 2, "x")]) - 3 * (0.5 + fee(0.5))) < 1e-12
    lose = [fill("Up", 0.80, 7.0, T0 - 9000, "v5m")]                                 # worst -5.64: kill switch
    d = run(REF, fills=lose)
    assert "worst outcome" in d.whisk_update["v5_kill"] and d.takes == []


# 17-19: time, failures, paper only

def test_17_no_momentum_from_285_s_all_buys_cancelled_at_290_s():
    end = (M.start + 286) * 1000.0
    assert run({**REF, "v5_ref_Up": 0.50}, ms=end).takes == []
    t_fill = end - 2000
    f = [fill("Up", 0.56, 8.0, t_fill, "v5m")]
    assert run({**REF, "v5_cycle": cycle(t_fill)}, books=bk(0.65, 0.66), fills=f, ms=end).takes   # its own cycle still runs
    res = {"Up": [Resting(id=9, price=0.40, remaining=5.0, created_ms=end)], "Down": []}
    d = run({**REF, "v5_cycle": cycle(t_fill)}, books=bk(0.65, 0.66), fills=f, ms=(M.start + 291) * 1000.0, resting=res)
    assert d.takes == [] and d.cancels == [9]


def test_18_stale_books_errors_and_failed_cancels_fail_safe():
    w = {**REF, "v5_ref_Up": 0.50}
    assert run(w, book_age_ms=5000.0).takes == []
    assert run(w, halted="live halted: order status unknown").takes == []
    bad = bk(0.54, 0.55)
    bad["Up"]["ask"] = None
    assert run(w, books=bad).takes == []
    res = {"Up": [Resting(id=4, price=0.40, remaining=5.0, created_ms=T0)], "Down": []}
    for _ in range(2):                                                             # a cancel that did not take: asked again
        d = run({**w, "v5_kill": "x"}, resting=res)
        assert d.takes == [] and d.cancels == [4]


def test_19_paper_only_unless_live_is_allowed():
    w = {**REF, "v5_ref_Up": 0.50}
    assert run(w, live=True).takes == []
    assert run(w, live=True, p=replace(P, live_ok=True)).takes != []


# 20: settlement

def test_20_settlement_pnl_matches_an_independent_count():
    f = [fill("Up", 0.56, 8.0, 1, "v5m"), fill("Down", 0.36, 8.0, 2, "v5b", 2), fill("Down", 0.30, 5.0, 3, "v5m", 3)]
    paid = 8 * 0.56 + 8 * 0.36 + 5 * 0.30 + 0.07 * (0.56 * 0.44 * 8 + 0.36 * 0.64 * 8 + 0.30 * 0.70 * 5)
    pos = position(f)
    assert abs(pos["if"]["Up"] - (8 - paid)) < 1e-12 and abs(pos["if"]["Down"] - (13 - paid)) < 1e-12
    assert abs(pos["worst"] - min(8 - paid, 13 - paid)) < 1e-12


def test_21_after_every_order_avg_up_plus_avg_down_under_097():
    held = [fill("Up", 0.60, 7.0, T0 - 60000, "v5m")]                               # avg Up 0.617 (worst -4.32)
    p = replace(P, high_wins=False)
    d = run({"v5_ref_Up": 0.61, "v5_ref_Down": 0.40}, books=bk(0.54, 0.55), fills=held, p=p)      # Down 0.46: 0.617 + 0.477
    assert d.takes == [] and "averages" in d.why["taker"]
    d = run({"v5_ref_Up": 0.71, "v5_ref_Down": 0.25}, books=bk(0.69, 0.70), fills=held, p=p)      # Down 0.31: 0.617 + 0.325
    assert d.takes and d.takes[0][0] == "Down"


def test_22_the_leading_tokens_outcome_stays_positive():
    d = run({"v5_ref_Up": 0.55, "v5_ref_Down": 0.41})                              # Down rose but Up leads: no
    assert d.takes == [] and "not positive" in d.why["taker"]
    d = run({"v5_ref_Up": 0.50, "v5_ref_Down": 0.46})                              # Up, the leader: yes
    assert d.takes and d.takes[0][0] == "Up"
