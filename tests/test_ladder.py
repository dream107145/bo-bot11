"""The pair ladder (user, 2026-10-08), on hand-built views."""
from xshark.config import Settings
from xshark.ladder import LadderParams, round_times
from xshark.strategy import decide

from test_strategy import M, view

FEE = M.fees.taker_fee


def st():
    return Settings(maker_enabled=False, taker_enabled=False, pair_enabled=False, fav_enabled=False,
                    late_enabled=False, scalp_enabled=False)


P = LadderParams(enabled=True)


def test_rounds_every_30_s_from_10_to_250():
    assert round_times(P) == [10, 40, 70, 100, 130, 160, 190, 220, 250]


def test_same_leader_buys_5_low_and_6_high_at_their_asks():
    books = {"Up": {"bid": 0.59, "ask": 0.60}, "Down": {"bid": 0.40, "ask": 0.41}}       # 0.60 + 0.41 = 1.01
    d = decide(view(books=books, secs_in=10.5, secs_left=289.5, ladder={}, ladder_params=P), st())
    assert sorted(t[:3] for t in d.takes) == [("Down", 0.41, 5.0), ("Up", 0.60, 6.0)]
    assert d.tags["taker:Up"] == "ladder" and d.ladder_kinds == {"Down": "low", "Up": "high"}
    assert d.ladder_update["round"] == 0 and d.ladder_update["high"] == "Up"
    # a high token at 0.88 or over: the round is skipped
    hi = {"Up": {"bid": 0.88, "ask": 0.89}, "Down": {"bid": 0.10, "ask": 0.12}}
    d = decide(view(books=hi, secs_in=71.0, secs_left=229.0, ladder={"round": 1, "high": "Up"}, ladder_params=P), st())
    assert d.takes == [] and d.ladder_update == {"round": 2}


def test_a_pair_over_101_is_not_bought_and_the_round_keeps_trying():
    wide = {"Up": {"bid": 0.58, "ask": 0.61}, "Down": {"bid": 0.39, "ask": 0.42}}        # 1.03
    d = decide(view(books=wide, secs_in=10.5, secs_left=289.5, ladder={}, ladder_params=P), st())
    assert d.takes == [] and "over 1.01" in d.why["taker"]
    rd = d.ladder_update["rd"]
    st_ = {"round": 0, "high": "Up", "rd": rd}
    tight = {"Up": {"bid": 0.60, "ask": 0.61}, "Down": {"bid": 0.39, "ask": 0.40}}       # 1.01: now it buys
    d = decide(view(books=tight, secs_in=14.0, secs_left=286.0, ladder=st_, ladder_params=P), st())
    assert sorted(t[:3] for t in d.takes) == [("Down", 0.40, 5.0), ("Up", 0.61, 6.0)]


def test_with_one_leg_filled_the_other_only_keeps_the_pair_at_101():
    rd = {"i": 0, "low": "Down", "high": "Up", "n_low": 5.0, "n_high": 6.0, "got_low": 5.0, "got_high": 0.0,
          "px_low": 0.41, "px_high": None, "flip": False}
    st_ = {"round": 0, "high": "Up", "rd": rd}
    up = {"Up": {"bid": 0.61, "ask": 0.62}, "Down": {"bid": 0.37, "ask": 0.39}}          # 0.41 + 0.62 = 1.03
    assert decide(view(books=up, secs_in=15.0, secs_left=285.0, ladder=st_, ladder_params=P), st()).takes == []
    ok = {"Up": {"bid": 0.59, "ask": 0.60}, "Down": {"bid": 0.39, "ask": 0.41}}          # 0.41 + 0.60 = 1.01
    d = decide(view(books=ok, secs_in=16.0, secs_left=284.0, ladder=st_, ladder_params=P), st())
    assert [t[:3] for t in d.takes] == [("Up", 0.60, 6.0)]


def test_after_a_flip_the_new_leader_is_sized_for_plus_010():
    books = {"Up": {"bid": 0.38, "ask": 0.39}, "Down": {"bid": 0.61, "ask": 0.62}}       # 1.01
    pos = dict(shares={"Up": 12.0, "Down": 10.0}, window_cost=11.6)
    d = decide(view(books=books, secs_in=71.0, secs_left=229.0, ladder={"round": 1, "high": "Up"},
                    ladder_params=LadderParams(enabled=True, net_over=0), **pos), st())     # the sizing alone
    buys = {t[0]: t for t in d.takes}
    assert buys["Up"][2] == 5.0
    cost = 11.6 + 5 * 0.39 + FEE(0.39, 5)
    n = buys["Down"][2]
    end_if_down = 10.0 + n - cost - n * 0.62 - FEE(0.62, n)
    assert 0.09 <= end_if_down <= 0.10 + 0.62
    assert d.ladder_update["high"] == "Down" and d.ladder_update["rd"]["flip"] is True


def test_a_flip_round_over_5_apart_buys_only_the_high_token_net():
    books = {"Up": {"bid": 0.30, "ask": 0.32}, "Down": {"bid": 0.68, "ask": 0.69}}
    pos = dict(shares={"Up": 30.0, "Down": 25.0}, window_cost=27.0)                    # Down now leads
    d = decide(view(books=books, secs_in=101.0, secs_left=199.0, ladder={"round": 2, "high": "Up"}, ladder_params=P,
                    **pos), st())
    rd = d.ladder_update["rd"]
    assert rd["n_low"] == 0.0 and rd["n_high"] > 5
    assert [t[:2] for t in d.takes] == [("Down", 0.69)] and d.takes[0][2] == rd["n_high"]   # 0.69 + 0.32 > 1.01: still bought
