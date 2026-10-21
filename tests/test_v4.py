"""Strategy V4, P/L-driven (user, 2026-10-13): the P/L maths, the size choice and every signal, on hand-built views."""
import math
from dataclasses import replace

from xshark.config import Settings
from xshark.strategy import decide
from xshark.v3 import position
from xshark.v4 import V4Params, distance, project, target

from test_strategy import M, view

P = V4Params(enabled=True)
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


def run(w=None, books=None, ms=T0, p=P, fills=(), hist=None, **kw):
    fl = list(fills)
    cost = sum(f["price"] * f["size"] + f["fee"] for f in fl)
    sh = {o: sum(f["size"] for f in fl if f["outcome"] == o) for o in ("Up", "Down")}
    b = books or bk(0.50, 0.51)
    um = (b["Up"]["bid"] + b["Up"]["ask"]) / 2
    base = dict(books=b, now_ms=ms, secs_in=ms / 1000 - M.start, secs_left=M.end - ms / 1000, whisk=w or {},
                v4_params=p, v2_fills=fl, v4_up_hist=hist if hist is not None else [(ms - 4000, um)], shares=sh, window_cost=cost)
    base.update(kw)
    return decide(view(**base), st())


def test_pl_after_an_order_and_the_targets():
    fs = [fill("Up", 0.51, 20.0, 1, "v4e")]
    pos = position(fs)
    after = project(pos, "Down", 10.0, 10 * 0.49 + fee(0.49, 10))
    cost = 20 * 0.51 + fee(0.51, 20) + 10 * 0.49 + fee(0.49, 10)
    assert abs(after["Up"] - (20 - cost)) < 1e-9 and abs(after["Down"] - (10 - cost)) < 1e-9
    assert target(P, 0.50) == ("middle", 12.8, 12.0) and target(P, 0.70) == ("high", 25.0, 50.0)
    assert target(P, 0.30)[0] == "low"
    assert abs(distance({"Up": 12.8, "Down": -12.0}, "Up", 12.8, 12.0)) < 1e-9
    assert abs(distance({"Up": 9.8, "Down": -8.0}, "Up", 12.8, 12.0) - 5.0) < 1e-9


def test_entry_picks_the_size_nearest_the_target_not_the_largest():
    d = run()                                                               # slow market, Up the dearer at 0.505
    assert d.takes == [("Up", 0.52, 20.0, 0.0)] and d.tags["taker:Up"] == "v4e"   # 20: +9.45 / -10.55, nearest +12.8/-12
    assert run({"v4_last_e": T0 - 4000}).takes == []                        # 6 s between entries
    assert run(hist=[(T0 - 4000, 0.45)]).tags.get("taker:Up") != "v4e"    # Up moved 0.055 in 3 s: not slow, no entry
    d = run(books=bk(0.70, 0.71), p=replace(P, cap_new_loss=False))       # high mode +25 / -50: 60 is nearest
    assert d.takes[0][:3] == ("Up", 0.72, 60.0)
    assert run(books=bk(0.88, 0.89)).takes == []                           # over 0.85: no permitted size


def test_momentum_buys_the_cheaper_token_rising_fast():
    d = run(books=bk(0.54, 0.55), hist=[(T0 - 2000, 0.60)])                # Down 0.40 -> 0.455 in 2 s
    assert d.takes[0][0] == "Down" and d.tags["taker:Down"] == "v4m" and d.takes[0][2] == 20.0
    assert run({"v4_last_m": T0 - 3000}, books=bk(0.54, 0.55), hist=[(T0 - 2000, 0.60)]).takes == []   # 5 s apart
    assert run({"v4_consec": 4}, books=bk(0.54, 0.55), hist=[(T0 - 2000, 0.60)]).takes == []          # 4 in a row


def test_balance_buys_the_losing_side_toward_plus_5_minus_20():
    L = T0 + 90000                                                          # 150 s in: after the first 2 min
    e = [fill("Up", 0.51, 20.0, L - 3500, "v4e")]                        # +9.45 / -10.55: loss within -20
    d = run({"v4_last_e": L - 3500}, fills=e, ms=L)
    assert d.takes == [] and d.whisk_update["v4_bal_for"] == 1             # judged once, nothing to balance
    assert "v4_bal_for" not in run({"v4_last_e": L - 2500}, fills=e, ms=L - 1000).whisk_update   # under 3 s: not yet
    big = [fill("Up", 0.51, 60.0, L - 3500, "v4e")]                      # +28.35 / -31.65
    d = run({"v4_last_e": L - 3500}, fills=big, ms=L)
    assert d.takes[0][:3] == ("Down", 0.51, 40.0) and d.tags["taker:Down"] == "v4b"   # nearest: +7.65 / -12.35
    # the user's 05:10 window: 65 Up / 110 Down, if Up -33.51 / if Down +11.49 -> about 20 Up (Up ask 0.34)
    shot = [fill("Up", 0.45, 65.0, L - 60000, "v4e"), fill("Down", 0.60, 110.0, L - 50000, "v4m", 2)]
    pos = position(shot)                                                    # $98.23 spent: -33.23 / +11.77
    assert pos["if"]["Up"] < -23
    p = replace(P, max_spend=130.0)                                         # (at $100 the balance would not fit)
    d = run({"v4_bal_for": 2, "v4_last_e": L, "v4_last_m": L}, books=bk(0.33, 0.34), fills=shot, p=p, ms=L)
    o, lim, q, _ = d.takes[0]
    assert (o, q) == ("Up", 20.0) and d.tags["taker:Up"] == "v4b"           # about -20.3 / +4.7


def test_only_balances_may_use_the_reserved_budget():
    fs = [fill("Up", 0.60, 100.0, T0 - 60000, "v4e"), fill("Down", 0.30, 40.0, T0 - 50000, "v4b", 2)]   # ~$78
    assert all(c is not None for c in [position(fs)])
    d = run({"v4_bal_for": 1}, books=bk(0.50, 0.51), fills=fs)              # entry: only $75 for it -> no entry
    assert "v4e" not in d.tags.values()


def test_entry_does_not_push_the_loss_past_23():
    d = run(books=bk(0.70, 0.71))                                          # high mode wants 60 (-43): capped
    assert d.takes[0][:3] == ("Up", 0.72, 20.0)                            # 20: about -14.5 on Down


def test_both_negative_buys_the_dear_token_only_if_its_outcome_turns_positive():
    neg = [fill("Up", 0.70, 10.0, T0 - 9000, "v4e"), fill("Down", 0.60, 10.0, T0 - 8000, "v4b", 2)]   # -3.32 / -3.32
    d = run({"v4_bal_for": 1}, books=bk(0.70, 0.71), fills=neg, p=replace(P, cap_new_loss=False))
    o, lim, q, _ = d.takes[0]
    assert (o, q) == ("Up", 60.0) and d.tags["taker:Up"] == "v4n"
    small = replace(P, size3=0.0, size4=0.0, size5=0.0)                    # 5 or 10 Up cannot make 'if Up' positive
    d = run({"v4_bal_for": 1, "v4_last_e": T0}, books=bk(0.70, 0.71), fills=neg, p=small)
    assert d.takes == [] and d.whisk_update["v4_cnt_nosize"] == 1 and "both-negative: no permitted size" in d.why["taker"]


def test_hard_limits_and_paper_only():
    assert run(live=True).takes == []                                       # a live bot: V4 orders only with live_ok
    assert run(live=True, p=replace(P, live_ok=True)).takes != []
    d = run(p=replace(P, max_spend=2.0))                                   # even 5 shares (~$2.69) is over
    assert d.takes == [] and "spend" in d.why["taker"]
    assert run(in_flight_taker={"Up": 5.0, "Down": 0.0}).takes == []
    assert run(ms=(M.end - 20) * 1000.0).takes == []
    assert run(book_age_ms=5000.0).takes == []


def test_first_2_min_mirror_the_outcomes_under_a_097_pair():
    early = T0 - 30000                                                      # 30 s into the market
    e = [fill("Down", 0.60, 50.0, early - 4000, "v4e"), fill("Up", 0.42, 50.0, early - 3000, "v4b", 2)]
    pos = position(e)                                                       # 50 / 50: -2.0 / -2.0 (sum -4.0)
    assert pos["if"]["Up"] < 0 and pos["if"]["Down"] < 0
    d = run({"v4_last_e": early, "v4_bal_for": 1}, books=bk(0.30, 0.31), fills=e, ms=early)   # Up ask 0.31 + 0.60 < 0.97
    o, lim, q, _ = d.takes[0]
    assert o == "Up" and d.tags["taker:Up"] == "v4b" and "mirror" in d.why["taker"]
    assert run({"v4_last_e": early, "v4_bal_for": 1}, books=bk(0.37, 0.38), fills=e, ms=early).tags.get("taker:Up") != "v4b"   # 0.38 + 0.60
    new = [fill("Down", 0.60, 20.0, early - 2000, "v4e")]                  # just entered: no balance only for that
    d = run({"v4_last_e": early - 2000}, books=bk(0.39, 0.40), fills=new, ms=early + 2000)
    assert "v4b" not in d.tags.values()

def test_a_killed_order_goes_again_at_once():
    sent = {"sig": "momentum", "tag": "v4m", "buy": "Down", "ts": T0 - 400,
            "prev": {"v4_last_m": None, "v4_last_e": None, "v4_last_n": None, "v4_last_b": None, "v4_bal_for": None, "v4_consec": 0}}
    w = {"v4_sent": sent, "v4_last_m": T0 - 400, "v4_consec": 1}
    d = run(w, books=bk(0.54, 0.55), hist=[(T0 - 2000, 0.545)])            # no fast rise any more: retried anyway
    assert d.takes and d.takes[0][0] == "Down" and d.tags["taker:Down"] == "v4m"
    assert d.whisk_update["v4_retries"] == 1 and "(retry)" in d.why["taker"]
    filled = [fill("Down", 0.46, 20.0, T0 - 300, "v4m")]
    d = run(w, books=bk(0.54, 0.55), hist=[(T0 - 2000, 0.545)], fills=filled)
    assert "v4m" not in d.tags.values() and d.whisk_update["v4_retries"] == 0       # it filled: no retry
    assert run({**w, "v4_retries": 3}, books=bk(0.54, 0.55), hist=[(T0 - 2000, 0.545)]).takes == []   # 3 in a row: stop
