"""The decision rules, one at a time, on hand-built views (no network)."""
from xshark.config import Settings
from xshark.market import FeeSchedule, Market
from xshark.strategy import Resting, View, decide, on_tick

M = Market(slug="btc-updown-5m-1790783100", condition_id="0xc", question="q", start=1790783100,
           end=1790783400, up_token="UP", down_token="DN", fees=FeeSchedule())


def view(**kw) -> View:
    base = dict(
        now_ms=(M.start + 60) * 1000.0, market=M, secs_in=60.0, secs_left=240.0, fair_up=0.60, trigger_z=0.0,
        books={"Up": {"bid": 0.59, "ask": 0.61}, "Down": {"bid": 0.39, "ask": 0.41}},
        book_age_ms=100.0, spot_age_ms=100.0, shares={"Up": 0.0, "Down": 0.0}, window_cost=0.0, reserved=0.0,
        resting={"Up": [], "Down": []}, in_flight_taker={"Up": 0.0, "Down": 0.0}, taker_count=0,
        last_taker_ms={}, last_quote_ms={}, halted=None)
    base.update(kw)
    return View(**base)


def s(**kw) -> Settings:
    st = Settings()
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def test_on_tick_is_clean_float():
    assert on_tick(0.5799999, 0.01) == 0.57
    assert on_tick(0.58, 0.01) == 0.58          # an exact tick stays put
    assert on_tick(0.571, 0.01, up=True) == 0.58


def test_bids_sit_under_fair_by_the_edge():
    d = decide(view(), s(maker_edge=0.02, maker_improve_ticks=1, maker_pair_max=1.0))
    assert d.targets == {"Up": 0.58, "Down": 0.38}
    assert sorted(p[:2] for p in d.places) == [("Down", 0.38), ("Up", 0.58)]


def test_bid_never_crosses_the_ask_and_improves_at_most_n_ticks():
    v = view(fair_up=0.70, books={"Up": {"bid": 0.55, "ask": 0.60}, "Down": {"bid": 0.40, "ask": 0.45}})
    d = decide(v, s(maker_edge=0.02, maker_improve_ticks=1, maker_pair_max=1.0))
    assert d.targets["Up"] == 0.56           # best bid + 1 tick, although fair - edge is 0.68
    v = view(fair_up=0.70, books={"Up": {"bid": 0.66, "ask": 0.67}, "Down": {"bid": 0.33, "ask": 0.34}})
    d = decide(v, s(maker_edge=0.02, maker_improve_ticks=3, maker_pair_max=1.0))
    assert d.targets["Up"] == 0.66           # the ask minus a tick caps it (post-only)


def test_pair_cap_takes_ticks_off_the_heavier_side_first():
    v = view(shares={"Up": 20.0, "Down": 0.0})
    d = decide(v, s(maker_edge=0.0, maker_improve_ticks=5, maker_pair_max=0.97, skew_per_10sh=0.0,
                    max_imbalance_shares=100, pair_enabled=False))
    assert round(d.targets["Up"] + d.targets["Down"], 4) <= 0.97
    assert d.targets["Up"] < 0.60 - 0.01     # Up is heavier: it gave up more


def test_skew_leans_away_from_the_heavy_side():
    flat = decide(view(), s(maker_pair_max=1.0, skew_per_10sh=0.0)).targets
    v = view(shares={"Up": 30.0, "Down": 0.0}, books={"Up": {"bid": 0.50, "ask": 0.70}, "Down": {"bid": 0.30, "ask": 0.50}})
    leaned = decide(v, s(maker_pair_max=1.0, skew_per_10sh=0.01, maker_improve_ticks=10, max_imbalance_shares=100)).targets
    assert leaned["Up"] < flat["Up"]


def test_inventory_cap_stops_the_heavy_side_only():
    d = decide(view(shares={"Up": 60.0, "Down": 0.0}), s(max_imbalance_shares=60, maker_pair_max=1.0))
    assert d.targets["Up"] is None and "more Up" in d.why["maker_Up"]
    assert d.targets["Down"] is not None


def test_resting_bid_inside_the_band_is_kept():
    r = Resting(7, 0.57, 5.0, 0.0)
    d = decide(view(resting={"Up": [r], "Down": []}), s(maker_pair_max=1.0, maker_requote_ticks=2))
    assert 7 not in d.cancels and not any(p[0] == "Up" for p in d.places)


def test_bid_too_close_to_a_falling_fair_goes_at_once():
    r = Resting(7, 0.59, 5.0, 0.0)           # fair fell to 0.598: 0.59 is now closer than edge/2
    v = view(fair_up=0.598, resting={"Up": [r], "Down": []}, last_quote_ms={"Up": (M.start + 60) * 1000.0})
    d = decide(v, s(maker_pair_max=1.0, maker_requote_ticks=5, maker_min_requote_ms=10_000))
    assert 7 in d.cancels and ("Up", 0.57, 5.0) in d.places
    # exactly half the edge under fair still counts as far enough: kept
    d = decide(view(resting={"Up": [r], "Down": []}), s(maker_pair_max=1.0, maker_requote_ticks=5))
    assert 7 not in d.cancels


def test_raising_a_bid_waits_for_the_requote_gap():
    r = Resting(7, 0.54, 5.0, 0.0)
    v = view(resting={"Up": [r], "Down": []}, last_quote_ms={"Up": (M.start + 60) * 1000.0 - 100})
    d = decide(v, s(maker_pair_max=1.0, maker_requote_ticks=2, maker_min_requote_ms=500))
    assert 7 not in d.cancels and not any(p[0] == "Up" for p in d.places)
    assert "raising" in d.why["maker_Up"]


def test_window_budget_blocks_new_bids():
    d = decide(view(window_cost=59.0), s(max_window_usdc=60.0, maker_pair_max=1.0))
    assert not d.places and "budget" in d.why["maker_Up"]


def test_timing_gates():
    assert not decide(view(secs_in=2.0), s()).places
    d = decide(view(secs_left=20.0), s(maker_stop_left_s=30.0))
    assert not d.places and d.why["maker_Up"] == "too close to the close"


def test_no_fair_value_means_no_orders_and_existing_bids_come_off():
    r = Resting(9, 0.55, 5.0, 0.0)
    d = decide(view(fair_up=None, resting={"Up": [r], "Down": []}), s())
    assert d.cancels == [9] and not d.places and not d.takes


def test_halt_cancels_everything():
    r = Resting(9, 0.55, 5.0, 0.0)
    d = decide(view(halted="halted from the console", resting={"Up": [r], "Down": []}), s())
    assert d.cancels == [9] and not d.places and not d.takes and d.why["taker"] == "halted from the console"


def test_taker_needs_a_move_then_an_edge_after_fee():
    st = s(trigger_sigma=1.5, taker_edge=0.01, maker_enabled=False, clip_shares=10, clip_usdc_max=7.0)
    assert not decide(view(trigger_z=0.5), st).takes
    # fair 0.66 vs ask 0.61: fee at 0.61 = 0.07*.61*.39 = 0.0167 -> edge 0.033
    d = decide(view(trigger_z=2.0, fair_up=0.66), st)
    (o, limit, size, edge), = d.takes
    assert o == "Up" and 0.61 <= limit <= 0.64 and size == 10 and edge > 0.03
    # the limit itself still clears the edge after its own fee
    fee = M.fees.taker_fee(limit, 1.0)
    assert 0.66 - limit - fee >= 0.01 - 1e-9


def test_taker_down_move_buys_down_and_respects_cooldown():
    st = s(trigger_sigma=1.5, taker_edge=0.0, maker_enabled=False, taker_cooldown_ms=1000)
    v = view(trigger_z=-2.0, fair_up=0.50)
    d = decide(v, st)
    assert d.takes and d.takes[0][0] == "Down"
    v = view(trigger_z=-2.0, fair_up=0.50, last_taker_ms={"Down": (M.start + 60) * 1000.0 - 200})
    assert not decide(v, st).takes


def test_clip_shrinks_to_the_dollar_cap_and_never_below_five_shares():
    st = s(trigger_sigma=0.0, taker_edge=0.0, maker_enabled=False, clip_shares=50, clip_usdc_max=7.0)
    d = decide(view(fair_up=0.70), st)
    (_, limit, size, _), = d.takes
    assert limit == 0.68 and size == 10         # sized at the limit, the worst price it may pay: floor(7 / 0.68)
    st.clip_usdc_max = 2.0
    assert not decide(view(fair_up=0.70), st).takes


def test_taker_edge_short_by_the_fee_does_nothing():
    # fair 0.62 vs ask 0.61: 1 cent of edge before a 1.67 cent fee
    d = decide(view(trigger_z=3.0, fair_up=0.62), s(trigger_sigma=1.5, taker_edge=0.0, maker_enabled=False))
    assert not d.takes and "edge" in d.why["taker"]


def test_pull_on_adverse_move_cancels_only_the_endangered_side():
    r = Resting(7, 0.57, 5.0, 0.0)
    v = view(trigger_z=-2.5, resting={"Up": [r], "Down": []})
    d = decide(v, s(maker_pair_max=1.0, maker_pull_sigma=2.0, taker_enabled=False))
    assert 7 in d.cancels and d.targets["Up"] is None and "pulled" in d.why["maker_Up"]
    assert d.targets["Down"] is not None                   # a fall in BTC does not endanger the Down bid
    d = decide(view(trigger_z=-1.0, resting={"Up": [r], "Down": []}), s(maker_pair_max=1.0, maker_pull_sigma=2.0))
    assert 7 not in d.cancels


# ---- pair completion: after one side fills, buy the other so a pair costs <= target

def _held_up(avg, n=5.0, **kw):
    return view(shares={"Up": n, "Down": 0.0}, costs={"Up": avg * n, "Down": 0.0}, window_cost=avg * n, **kw)


def test_pair_take_buys_the_other_side_when_it_locks_the_pair():
    # Up bought at 0.54 avg; Down ask 0.41 + fee 1.69c = 0.4269 <= 0.97 - 0.54 = 0.43
    d = decide(_held_up(0.54), s(maker_enabled=False, pair_target=0.97))
    (o, limit, size, edge), = d.takes
    assert o == "Down" and size == 5.0 and "Down" in d.pair_sides and limit == 0.41
    assert limit + M.fees.taker_fee(limit, 1.0) <= 0.43 + 1e-9
    # at 0.55 avg the room is 0.42 and the 0.41 ask plus its fee does not fit: no take
    assert not decide(_held_up(0.55), s(maker_enabled=False, pair_target=0.97, trigger_sigma=9)).takes


def test_pair_bid_rests_when_the_ask_is_too_dear_and_needs_no_move():
    # Up bought at 0.62: room 0.35, the Down ask 0.41 is too dear -> rest a bid at 0.35
    d = decide(_held_up(0.62, trigger_z=0.0), s(maker_enabled=False, pair_target=0.97))
    assert not d.takes
    assert ("Down", 0.35, 5.0) in d.places and "pair bid 0.35" in d.why["maker_Down"]


def test_a_resting_pair_bid_is_kept_not_requoted_off_fair():
    r = Resting(11, 0.35, 5.0, 0.0)
    v = _held_up(0.62, resting={"Up": [], "Down": [r]})
    d = decide(v, s(maker_enabled=False, pair_target=0.97))
    assert 11 not in d.cancels and not d.places


def test_no_pairing_when_off_or_near_the_close():
    assert not decide(_held_up(0.55), s(maker_enabled=False, pair_enabled=False, trigger_sigma=9)).takes
    d = decide(_held_up(0.55, secs_left=5.0), s(maker_enabled=False, pair_stop_left_s=10.0))
    assert not d.takes and not d.places


def test_alternate_sides_and_share_cap():
    st = s(maker_enabled=False, pair_enabled=False, trigger_sigma=1.5, taker_edge=0.0,
           alternate_sides=True, max_window_shares=20)
    up_move = dict(trigger_z=2.0, fair_up=0.70)
    # the last fill was Up: an up-move may not buy Up again
    d = decide(view(last_fill_side="Up", **up_move), st)
    assert not d.takes and "alternating" in d.why["taker"]
    # last fill Down: Up is allowed
    assert decide(view(last_fill_side="Down", **up_move), st).takes
    # 20 shares held already: nothing more
    d = decide(view(last_fill_side="Down", shares={"Up": 10.0, "Down": 10.0}, **up_move), st)
    assert not d.takes and "cap" in d.why["taker"]
    # 15 held: the clip shrinks to the 5 that fit
    (_, _, size, _), = decide(view(last_fill_side="Down", shares={"Up": 5.0, "Down": 10.0}, **up_move),
                               replace_settings(st, clip_shares=10.0, clip_usdc_max=10.0)).takes
    assert size == 5.0


def replace_settings(st, **kw):
    from dataclasses import replace
    return replace(st, **kw)


def test_maker_bid_after_taker_on_prices_strictly_under_target():
    # Up taker-filled 5 @ 0.55 (fee 0.0866 included in cost); Down bid must make 0.55 + bid < 0.95
    v = view(shares={"Up": 5.0, "Down": 0.0}, costs={"Up": 5 * 0.55 + 0.0866, "Down": 0.0},
             fees={"Up": 0.0866, "Down": 0.0}, window_cost=2.84, last_fill_side="Up")
    st = s(maker_enabled=False, pair_enabled=True, pair_take=False, pair_bid=True, pair_target=0.95,
           pair_on_price=True, alternate_sides=True, max_window_shares=20, trigger_sigma=9)
    d = decide(v, st)
    assert ("Down", 0.39, 5.0) in d.places and not d.takes


# ---------------------------------------------------------------- favourite bid

FAV_BOOKS = {"Up": {"bid": 0.92, "ask": 0.93}, "Down": {"bid": 0.07, "ask": 0.08}}


def fav_settings(**kw):
    base = dict(maker_enabled=False, taker_enabled=False, pair_enabled=False, fav_enabled=True)
    base.update(kw)
    return s(**base)


def test_fav_bids_090_on_the_favourite_far_from_the_strike():
    d = decide(view(fair_up=0.93, books=FAV_BOOKS, fav_side="Up", dist_bps=5.4), fav_settings())
    assert d.places == [("Up", 0.90, 5.0)] and d.fav_sides == {"Up"}


def test_fav_pulled_when_btc_is_close_to_the_strike():
    d = decide(view(fair_up=0.93, books=FAV_BOOKS, fav_side="Up", dist_bps=1.4), fav_settings())
    assert d.places == [] and "from the strike" in d.why["maker_Up"]
    rest = {"Up": [Resting(7, 0.90, 5.0, 0.0)], "Down": []}
    d = decide(view(fair_up=0.93, books=FAV_BOOKS, fav_side="Up", dist_bps=1.4, resting=rest), fav_settings())
    assert d.cancels == [7]
    # Down favourite: distance counts on Down's side (BTC under the strike)
    dn = {"Up": {"bid": 0.07, "ask": 0.08}, "Down": {"bid": 0.92, "ask": 0.93}}
    d = decide(view(fair_up=0.07, books=dn, fav_side="Down", dist_bps=-3.6), fav_settings())
    assert d.places == [("Down", 0.90, 5.0)]


def test_fav_keeps_its_resting_bid_and_stops_after_a_fill_or_late():
    rest = {"Up": [Resting(7, 0.90, 5.0, 0.0)], "Down": []}
    d = decide(view(fair_up=0.93, books=FAV_BOOKS, fav_side="Up", dist_bps=5.4, resting=rest), fav_settings())
    assert d.places == [] and d.cancels == []
    d = decide(view(fair_up=0.93, books=FAV_BOOKS, fav_side="Up", dist_bps=5.4, fav_shares=5.0,
                    shares={"Up": 5.0, "Down": 0.0}), fav_settings())
    assert d.places == []
    d = decide(view(fair_up=0.93, books=FAV_BOOKS, fav_side="Up", dist_bps=5.4, secs_left=20.0), fav_settings())
    assert d.places == [] and "close" in d.why["maker_Up"]
    d = decide(view(fair_up=0.93, books=FAV_BOOKS, fav_side=None, dist_bps=5.4), fav_settings())
    assert d.places == []


def test_underdog_first_bids_005_then_090_on_the_favourite():
    st = fav_settings(fav_dog_first=True, pair_enabled=True, pair_bid=True, pair_on_price=True, pair_target=0.95)
    # step 1: Up is the favourite (above 0.90): a 0.05 bid on Down, nothing on Up, no distance rule
    d = decide(view(fair_up=0.93, books=FAV_BOOKS, fav_side="Up", dist_bps=0.5), st)
    assert d.places == [("Down", 0.05, 5.0)] and d.dog_sides == {"Down"} and not d.fav_sides
    # step 2: the Down bid filled -> 0.90 on Up (not the generic pair price 0.89)
    v = view(fair_up=0.93, books=FAV_BOOKS, fav_side="Up", dist_bps=0.5, fav_dog_shares=5.0,
             shares={"Up": 0.0, "Down": 5.0}, costs={"Down": 0.25}, fees={"Down": 0.0})
    d = decide(v, st)
    assert d.places == [("Up", 0.90, 5.0)] and d.fav_sides == {"Up"} and "Up" not in d.pair_sides
    # step 3: both filled -> nothing more
    v = view(fair_up=0.93, books=FAV_BOOKS, fav_side="Up", fav_dog_shares=5.0, fav_shares=5.0,
             shares={"Up": 5.0, "Down": 5.0}, costs={"Up": 4.5, "Down": 0.25}, fees={"Up": 0.0, "Down": 0.0})
    assert decide(v, st).places == []


def test_bids_rest_only_from_maker_start_to_the_stop():
    st = fav_settings(fav_dog_first=True, maker_start_s=30.0, fav_stop_left_s=150.0, pair_stop_left_s=150.0)
    early = view(fair_up=0.93, books=FAV_BOOKS, fav_side="Up", secs_in=20.0, secs_left=280.0)
    d = decide(early, st)
    assert d.places == [] and "30 s into" in d.why["maker_Down"]
    mid = view(fair_up=0.93, books=FAV_BOOKS, fav_side="Up", secs_in=60.0, secs_left=240.0)
    assert decide(mid, st).places == [("Down", 0.05, 5.0)]
    late = view(fair_up=0.93, books=FAV_BOOKS, fav_side="Up", secs_in=160.0, secs_left=140.0,
                resting={"Up": [], "Down": [Resting(9, 0.05, 5.0, 0.0)]})
    d = decide(late, st)
    assert d.places == [] and d.cancels == [9]


def test_last_minute_bid_090_on_the_high_token_only_in_the_last_minute():
    st = s(maker_enabled=False, taker_enabled=False, pair_enabled=False, late_enabled=True,
           maker_start_s=30.0, maker_stop_left_s=150.0)
    early = view(fair_up=0.93, books=FAV_BOOKS, secs_in=200.0, secs_left=100.0)
    assert decide(early, st).places == []
    late = view(fair_up=0.93, books=FAV_BOOKS, secs_in=250.0, secs_left=50.0)
    d = decide(late, st)
    assert d.places == [("Up", 0.90, 5.0)] and d.late_sides == {"Up"}
    done = view(fair_up=0.93, books=FAV_BOOKS, secs_in=255.0, secs_left=45.0, late_shares=5.0,
                shares={"Up": 5.0, "Down": 0.0})
    assert decide(done, st).places == []
    flat = view(fair_up=0.6, secs_in=250.0, secs_left=50.0)          # no token above 0.90
    assert decide(flat, st).places == []


def test_fav_min_z_pulls_the_bid_when_the_move_could_still_reverse():
    st = fav_settings(fav_min_z=1.0, fav_min_dist_bps=2.4)
    # 12 bps from the strike but with 3 minutes left that is only 0.9 sigma: no bid
    d = decide(view(fair_up=0.08, books={"Up": {"bid": 0.07, "ask": 0.08}, "Down": {"bid": 0.92, "ask": 0.93}},
                    fav_side="Down", dist_bps=-12.0, dist_z=-0.9), st)
    assert d.places == [] and "needs 1.0σ" in d.why["maker_Down"]
    d = decide(view(fair_up=0.08, books={"Up": {"bid": 0.07, "ask": 0.08}, "Down": {"bid": 0.92, "ask": 0.93}},
                    fav_side="Down", dist_bps=-12.0, dist_z=-1.3), st)
    assert d.places == [("Down", 0.90, 5.0)]
    # 0 = off: the sigma distance is not checked
    d = decide(view(fair_up=0.93, books=FAV_BOOKS, fav_side="Up", dist_bps=5.4, dist_z=0.2), fav_settings())
    assert d.places == [("Up", 0.90, 5.0)]


def test_pause_hours_gmt9_and_across_midnight():
    from xshark.bot import pause_reason
    st = s(pause_a_from=2.0, pause_a_to=3.0, pause_b_from=23.5, pause_b_to=0.5)
    at = lambda hh, mm: (hh * 3600 + mm * 60 - 9 * 3600) % 86400 + 1790899200   # GMT+9 clock -> epoch s
    assert pause_reason(st, at(2, 30)).startswith("paused 02:00-03:00")
    assert pause_reason(st, at(3, 0)) is None and pause_reason(st, at(1, 59)) is None
    assert pause_reason(st, at(23, 45)).startswith("paused 23:30-00:30")
    assert pause_reason(st, at(0, 15)).startswith("paused 23:30-00:30")
    assert pause_reason(s(), at(2, 30)) is None                                    # all off by default


def test_a_leg_with_a_rounded_fill_is_done_and_no_second_bid_follows():
    """2026-10-02 04:23 GMT+9: 5 shares filled as 4.5757 + 0.42 = 4.9957 and a second 5-share
    last-minute bid was sent. Any fill on a leg ends it for the window."""
    late = s(maker_enabled=False, taker_enabled=False, pair_enabled=False, late_enabled=True, late_left_s=110.0)
    v = view(fair_up=0.06, books={"Up": {"bid": 0.06, "ask": 0.07}, "Down": {"bid": 0.93, "ask": 0.94}},
             secs_in=222.0, secs_left=78.0, late_shares=4.9957, shares={"Up": 0.0, "Down": 4.9957})
    d = decide(v, late)
    assert d.places == [] and "holding to settlement" in d.why["maker_Down"]
    d = decide(view(fair_up=0.93, books=FAV_BOOKS, fav_side="Up", dist_bps=5.4, fav_shares=4.9957,
                    shares={"Up": 4.9957, "Down": 0.0}), fav_settings())
    assert d.places == []
    # underdog-first: a rounded underdog fill moves on to the favourite bid, not another underdog bid
    st = fav_settings(fav_dog_first=True, pair_enabled=True, pair_bid=True, pair_on_price=True, pair_target=0.95)
    d = decide(view(fair_up=0.93, books=FAV_BOOKS, fav_side="Up", fav_dog_shares=4.9957,
                    shares={"Up": 0.0, "Down": 4.9957}, costs={"Down": 0.25}, fees={"Down": 0.0}), st)
    assert d.places == [("Up", 0.90, 5.0)]


def test_no_new_bid_while_the_old_one_on_that_side_is_being_cancelled():
    late = s(maker_enabled=False, taker_enabled=False, pair_enabled=False, late_enabled=True, late_left_s=110.0)
    books = {"Up": {"bid": 0.06, "ask": 0.07}, "Down": {"bid": 0.93, "ask": 0.94}}
    rest = {"Up": [], "Down": [Resting(19, 0.92, 5.0, 0.0, cancelling=True)]}
    d = decide(view(fair_up=0.06, books=books, secs_in=222.0, secs_left=78.0, resting=rest), late)
    assert d.places == [] and "cancel" in d.why["maker_Down"]


# ---------------------------------------------------------------- scalp cycles

def scalp_settings(**kw):
    base = dict(maker_enabled=False, taker_enabled=False, pair_enabled=False, fav_enabled=False, late_enabled=False,
                scalp_enabled=True, max_window_usdc=15.0, max_window_shares=0.0)
    base.update(kw)
    return s(**base)


def test_scalp_first_buy_takes_the_token_priced_051_to_060():
    books = {"Up": {"bid": 0.53, "ask": 0.55}, "Down": {"bid": 0.45, "ask": 0.47}}
    d = decide(view(books=books, secs_in=6.0, secs_left=294.0), scalp_settings())
    assert d.takes == [("Up", 0.55, 5.0, 0.0)] and d.tags["taker:Up"] == "scalp"
    # nothing in range: no buy; cooling down: no buy
    flat = {"Up": {"bid": 0.49, "ask": 0.50}, "Down": {"bid": 0.50, "ask": 0.51}}
    d = decide(view(books={"Up": {"bid": 0.62, "ask": 0.64}, "Down": {"bid": 0.36, "ask": 0.38}}), scalp_settings())
    assert d.takes == []
    d = decide(view(books=books, now_ms=(M.start + 60) * 1000.0, scalp_closed_ms=(M.start + 59.5) * 1000.0),
               scalp_settings())
    assert d.takes == [] and "cooling" in d.why["taker"]
    d = decide(view(books=flat), scalp_settings())
    assert d.takes == [("Down", 0.51, 5.0, 0.0)]


def test_scalp_open_cycle_rests_a_pair_bid_and_hedges_when_the_other_side_rises():
    cyc = {"side": "Up", "price": 0.55, "shares": 5.0, "pair": 0.0}
    books = {"Up": {"bid": 0.50, "ask": 0.52}, "Down": {"bid": 0.48, "ask": 0.50}}
    d = decide(view(books=books, scalp=cyc, shares={"Up": 5.0, "Down": 0.0}, window_cost=2.84), scalp_settings())
    assert d.places == [("Down", 0.34, 5.0)] and d.tags["maker:Down"] == "scalp_pair" and d.takes == []
    # Down rose to 0.65: hedge 10 Down at once, and the pair bid stays out
    up = {"Up": {"bid": 0.33, "ask": 0.35}, "Down": {"bid": 0.64, "ask": 0.65}}
    d = decide(view(books=up, scalp=cyc, shares={"Up": 5.0, "Down": 0.0}, window_cost=2.84), scalp_settings())
    assert ("Down", 0.65, 10.0, 0.0) in d.takes and d.tags["taker:Down"] == "scalp_hedge"
    # already above the hedge cap: no chase
    jump = {"Up": {"bid": 0.24, "ask": 0.26}, "Down": {"bid": 0.74, "ask": 0.76}}
    d = decide(view(books=jump, scalp=cyc, shares={"Up": 5.0, "Down": 0.0}, window_cost=2.84), scalp_settings())
    assert d.takes == []


def test_scalp_new_cycle_needs_room_for_its_hedge_in_the_window_budget():
    books = {"Up": {"bid": 0.53, "ask": 0.55}, "Down": {"bid": 0.45, "ask": 0.47}}
    d = decide(view(books=books, window_cost=6.0), scalp_settings())     # 6 + 2.75 + 10 x 0.70 > 15
    assert d.takes == [] and "budget" in d.why["taker"]


def test_scalp_hedge_sized_for_a_profit_if_the_hedged_side_wins():
    from xshark.strategy import hedge_shares
    st = scalp_settings(scalp_hedge_profit=0.5)
    fees = M.fees
    for ask, want in ((0.55, 8), (0.65, 10), (0.70, 12)):
        n = hedge_shares(st, fees, 0.55, 5.0, ask)
        assert n == want
        cost = 5 * 0.55 + fees.taker_fee(0.55, 5) + n * ask + fees.taker_fee(ask, n)
        assert n - cost >= 0.5 - 1e-9                     # the hedged side wins: at least +$0.50
    cyc = {"side": "Up", "price": 0.55, "shares": 5.0, "pair": 0.0}
    up = {"Up": {"bid": 0.39, "ask": 0.41}, "Down": {"bid": 0.58, "ask": 0.60}}
    d = decide(view(books=up, scalp=cyc, shares={"Up": 5.0, "Down": 0.0}, window_cost=2.84),
               scalp_settings(scalp_hedge_profit=0.5, scalp_hedge_price=0.55, scalp_hedge_max=0.70))
    assert d.takes == [("Down", 0.60, float(hedge_shares(st, fees, 0.55, 5.0, 0.60)), 0.0)]


def test_scalp_partial_pair_keeps_its_bid_and_rebids_only_the_rest():
    cyc = {"side": "Up", "price": 0.53, "shares": 5.0, "pair": 3.86}
    books = {"Up": {"bid": 0.62, "ask": 0.64}, "Down": {"bid": 0.34, "ask": 0.36}}
    # the partly filled 0.36 bid keeps resting although the ask came down to it
    rest = {"Up": [], "Down": [Resting(7, 0.36, 1.14, 0.0)]}
    d = decide(view(books=books, scalp=cyc, resting=rest, shares={"Up": 5.0, "Down": 3.86}), scalp_settings())
    assert d.cancels == [] and d.places == []
    # it was cancelled: a new bid for the rest, at the venue's 5-share minimum, still under the 0.90 rule
    d = decide(view(books=books, scalp=cyc, shares={"Up": 5.0, "Down": 3.86}), scalp_settings())
    assert d.places == [("Down", 0.35, 5.0)]
    cyc8 = {"side": "Up", "price": 0.53, "shares": 15.0, "pair": 3.0}
    d = decide(view(books=books, scalp=cyc8, shares={"Up": 15.0, "Down": 3.0}), scalp_settings())
    assert d.places == [("Down", 0.35, 12.0)]


def test_scalp_hedge_is_not_blocked_by_its_own_pair_bid():
    """00:30 GMT+9 on 2026-10-05: window cost 7.62 + own pair bid 1.45 + hedge 6.66 > 15 refused the hedge."""
    cyc = {"side": "Up", "price": 0.60, "shares": 5.0, "pair": 0.0}
    books = {"Up": {"bid": 0.34, "ask": 0.35}, "Down": {"bid": 0.64, "ask": 0.65}}
    rest = {"Up": [], "Down": [Resting(9, 0.29, 5.0, 0.0)]}
    d = decide(view(books=books, scalp=cyc, resting=rest, shares={"Up": 10.0, "Down": 5.0}, window_cost=7.62,
                    reserved=1.45), scalp_settings(scalp_hedge_price=0.65, scalp_hedge_max=0.70))
    assert ("Down", 0.65, 10.0, 0.0) in d.takes


def test_scalp_takes_go_out_with_the_limit_above_the_ask():
    books = {"Up": {"bid": 0.53, "ask": 0.55}, "Down": {"bid": 0.45, "ask": 0.47}}
    d = decide(view(books=books), scalp_settings(scalp_take_slip=0.03))
    assert d.takes == [("Up", 0.58, 5.0, 0.0)]
    cyc = {"side": "Up", "price": 0.58, "shares": 5.0, "pair": 0.0}
    up = {"Up": {"bid": 0.34, "ask": 0.35}, "Down": {"bid": 0.64, "ask": 0.65}}
    d = decide(view(books=up, scalp=cyc, shares={"Up": 5.0, "Down": 0.0}, window_cost=2.98),
               scalp_settings(scalp_take_slip=0.03, scalp_hedge_price=0.65, scalp_hedge_max=0.70))
    assert d.takes == [("Down", 0.68, 10.0, 0.0)]


def test_scalp_no_new_cycle_after_a_completed_pair_when_set():
    books = {"Up": {"bid": 0.53, "ask": 0.55}, "Down": {"bid": 0.45, "ask": 0.47}}
    d = decide(view(books=books, scalp_pairs=1), scalp_settings(scalp_stop_after_pair=True))
    assert d.takes == [] and "pair completed" in d.why["taker"]
    assert decide(view(books=books, scalp_pairs=1), scalp_settings()).takes == [("Up", 0.55, 5.0, 0.0)]


def test_scalp_first_buy_as_a_maker_bid():
    st = scalp_settings(scalp_first_maker=True)
    books = {"Up": {"bid": 0.53, "ask": 0.56}, "Down": {"bid": 0.44, "ask": 0.47}}
    d = decide(view(books=books), st)
    assert d.takes == [] and d.places == [("Up", 0.54, 5.0)] and d.tags["maker:Up"] == "scalp"
    # one tick wide: joins the best bid, never the ask
    tight = {"Up": {"bid": 0.55, "ask": 0.56}, "Down": {"bid": 0.44, "ask": 0.45}}
    assert decide(view(books=tight), st).places == [("Up", 0.55, 5.0)]
    # the resting bid follows the book within a tick, and moves when the bid runs away
    rest = {"Up": [Resting(3, 0.54, 5.0, 0.0)], "Down": []}
    d = decide(view(books=tight, resting=rest), st)
    assert d.places == [] and d.cancels == []
    up = {"Up": {"bid": 0.58, "ask": 0.59}, "Down": {"bid": 0.41, "ask": 0.42}}
    d = decide(view(books=up, resting=rest), st)
    assert d.cancels == [3] and d.places == [("Up", 0.58, 5.0)]
    # out of range: no first bid
    assert decide(view(books={"Up": {"bid": 0.66, "ask": 0.67}, "Down": {"bid": 0.33, "ask": 0.34}}), st).places == []


def test_scalp_first_buy_only_in_the_first_30_s():
    st = scalp_settings(scalp_first_maker=True, scalp_first_within_s=30.0)
    books = {"Up": {"bid": 0.53, "ask": 0.56}, "Down": {"bid": 0.44, "ask": 0.47}}
    assert decide(view(books=books, secs_in=20.0, secs_left=280.0), st).places == [("Up", 0.54, 5.0)]
    rest = {"Up": [Resting(3, 0.54, 5.0, 0.0)], "Down": []}
    d = decide(view(books=books, secs_in=31.0, secs_left=269.0, resting=rest), st)
    assert d.places == [] and d.cancels == [3] and "first 30 s" in d.why["taker"]


def test_favourite_pair_bid_at_0015_waits_for_the_0001_tick():
    st = fav_settings(fav_pair_enabled=True, fav_pair_price=0.015)
    books = {"Up": {"bid": 0.95, "ask": 0.96}, "Down": {"bid": 0.04, "ask": 0.05}}
    held = dict(fair_up=0.95, books=books, fav_side="Up", dist_bps=6.0, fav_shares=5.0, shares={"Up": 5.0, "Down": 0.0})
    d = decide(view(**held), st)                                   # Down still on the cent tick
    assert d.places == [] and "0.001 tick" in d.why["maker_Down"]
    low = {"Up": {"bid": 0.97, "ask": 0.98}, "Down": {"bid": 0.02, "ask": 0.03}}
    d = decide(view(**{**held, "books": low}, ticks={"Down": 0.001}), st)
    assert d.places == [("Down", 0.015, 5.0)] and d.tags["maker:Down"] == "fav_pair"
    # paired: nothing more; and no pair after a last-minute fill alone (no favourite fill)
    assert decide(view(**{**held, "books": low}, ticks={"Down": 0.001}, fav_pair_shares=5.0), st).places == []
    late_only = dict(held, fav_shares=0.0, late_shares=5.0)
    d = decide(view(**{**late_only, "books": low}, ticks={"Down": 0.001}),
               fav_settings(fav_pair_enabled=True, fav_pair_price=0.015, fav_min_dist_bps=100.0))
    assert ("Down", 0.015, 5.0) not in d.places


def test_scalp_pair_bid_only_until_3_minutes_in():
    st = scalp_settings(scalp_pair_until_s=180.0)
    cyc = {"side": "Up", "price": 0.55, "shares": 5.0, "pair": 0.0}
    books = {"Up": {"bid": 0.50, "ask": 0.52}, "Down": {"bid": 0.48, "ask": 0.50}}
    d = decide(view(books=books, scalp=cyc, secs_in=170.0, secs_left=130.0, shares={"Up": 5.0, "Down": 0.0}), st)
    assert d.places == [("Down", 0.34, 5.0)]
    rest = {"Up": [], "Down": [Resting(4, 0.34, 5.0, 0.0)]}
    d = decide(view(books=books, scalp=cyc, secs_in=181.0, secs_left=119.0, resting=rest,
                    shares={"Up": 5.0, "Down": 0.0}), st)
    assert d.places == [] and d.cancels == [4] and "holding" in d.why["maker_Down"]


def test_scalp_without_pair_bids_still_hedges():
    st = scalp_settings(scalp_pair_enabled=False)
    cyc = {"side": "Up", "price": 0.55, "shares": 5.0, "pair": 0.0}
    books = {"Up": {"bid": 0.50, "ask": 0.52}, "Down": {"bid": 0.48, "ask": 0.50}}
    assert decide(view(books=books, scalp=cyc, shares={"Up": 5.0, "Down": 0.0}), st).places == []
    up = {"Up": {"bid": 0.33, "ask": 0.35}, "Down": {"bid": 0.64, "ask": 0.65}}
    d = decide(view(books=up, scalp=cyc, shares={"Up": 5.0, "Down": 0.0}, window_cost=2.84), st)
    assert ("Down", 0.65, 10.0, 0.0) in d.takes


def test_scalp_hedge_only_after_2_50():
    st = scalp_settings(scalp_pair_enabled=False, scalp_hedge_after_s=170.0)
    cyc = {"side": "Up", "price": 0.55, "shares": 5.0, "pair": 0.0}
    up = {"Up": {"bid": 0.24, "ask": 0.25}, "Down": {"bid": 0.75, "ask": 0.76}}
    d = decide(view(books=up, scalp=cyc, secs_in=120.0, secs_left=180.0, shares={"Up": 5.0, "Down": 0.0},
                    window_cost=2.84), st)
    assert d.takes == [] and "after 170 s" in d.why["taker"]
    d = decide(view(books=up, scalp=cyc, secs_in=171.0, secs_left=129.0, shares={"Up": 5.0, "Down": 0.0},
                    window_cost=2.84), scalp_settings(scalp_pair_enabled=False, scalp_hedge_after_s=170.0,
                                                      scalp_hedge_price=0.75, scalp_hedge_max=0.80))
    assert d.takes == [("Down", 0.76, 10.0, 0.0)]


def test_favourite_bid_follows_the_book_in_084_to_090():
    st = fav_settings(fav_trigger=0.84, fav_bid=0.90, fav_bid_follow=True, fav_bid_min=0.84)
    def run(b, a, rest=None):
        books = {"Up": {"bid": b, "ask": a}, "Down": {"bid": round(1 - a, 2), "ask": round(1 - b, 2)}}
        return decide(view(fair_up=a, books=books, fav_side="Up", dist_bps=6.0,
                           resting=rest or {"Up": [], "Down": []}), st)
    assert run(0.86, 0.88).places == [("Up", 0.87, 5.0)]
    assert run(0.89, 0.90).places == [("Up", 0.89, 5.0)]
    assert run(0.93, 0.94).places == [("Up", 0.90, 5.0)]          # capped at the top of the range
    d = run(0.80, 0.82)
    assert d.places == [] and "under the 0.84-0.90 range" in d.why["maker_Up"]
    rest = {"Up": [Resting(5, 0.87, 5.0, 0.0)], "Down": []}
    assert run(0.86, 0.88, rest).places == [] and run(0.86, 0.88, rest).cancels == []
    d = run(0.88, 0.90, rest)                                       # the book moved up 2 ticks
    assert d.cancels == [5] and d.places == [("Up", 0.89, 5.0)]


def test_last_minute_bid_follows_the_book_in_090_to_095():
    st = s(maker_enabled=False, taker_enabled=False, pair_enabled=False, late_enabled=True, late_left_s=110.0,
           late_trigger=0.90, late_bid=0.95, late_bid_follow=True, late_bid_min=0.90)
    def run(b, a):
        books = {"Up": {"bid": b, "ask": a}, "Down": {"bid": round(1 - a, 2), "ask": round(1 - b, 2)}}
        return decide(view(fair_up=a, books=books, secs_in=220.0, secs_left=80.0), st)
    assert run(0.91, 0.93).places == [("Up", 0.92, 5.0)]
    assert run(0.97, 0.98).places == [("Up", 0.95, 5.0)]          # capped at the top of the range
    assert run(0.90, 0.91).places == [("Up", 0.90, 5.0)]          # one tick wide: joins the best bid


def test_favourite_bid_only_from_its_start_time():
    st = fav_settings(fav_start_s=120.0)
    early = view(fair_up=0.93, books=FAV_BOOKS, fav_side="Up", dist_bps=5.4, secs_in=100.0, secs_left=200.0)
    d = decide(early, st)
    assert d.places == [] and "120 s into" in d.why["maker_Up"]
    later = view(fair_up=0.93, books=FAV_BOOKS, fav_side="Up", dist_bps=5.4, secs_in=125.0, secs_left=175.0)
    assert decide(later, st).places == [("Up", 0.90, 5.0)]


def test_scalp_first_buy_maker_until_15_s_then_takes_the_ask():
    st = scalp_settings(scalp_first_maker=True, scalp_first_within_s=30.0, scalp_first_take_after_s=15.0,
                        scalp_take_slip=0.03)
    books = {"Up": {"bid": 0.53, "ask": 0.56}, "Down": {"bid": 0.44, "ask": 0.47}}
    d = decide(view(books=books, secs_in=10.0, secs_left=290.0), st)
    assert d.places == [("Up", 0.54, 5.0)] and d.takes == []
    rest = {"Up": [Resting(3, 0.54, 5.0, 0.0)], "Down": []}
    d = decide(view(books=books, secs_in=16.0, secs_left=284.0, resting=rest), st)
    assert d.cancels == [3] and d.takes == [] and "cancel" in d.why["taker"]
    d = decide(view(books=books, secs_in=16.5, secs_left=283.5), st)
    assert d.takes == [("Up", 0.59, 5.0, 0.0)] and d.places == []
    assert decide(view(books=books, secs_in=31.0, secs_left=269.0), st).takes == []     # past 30 s


def test_scalp_leader_take_from_1_00_to_2_30_and_hold():
    st = scalp_settings(scalp_min=0.65, scalp_max=0.75, scalp_first_after_s=60.0, scalp_first_within_s=150.0,
                        scalp_take_slip=0.03, scalp_hedge_enabled=False, scalp_pair_enabled=False)
    books = {"Up": {"bid": 0.69, "ask": 0.70}, "Down": {"bid": 0.30, "ask": 0.31}}
    assert decide(view(books=books, secs_in=40.0, secs_left=260.0), st).takes == []          # before 1:00
    assert decide(view(books=books, secs_in=70.0, secs_left=230.0), st).takes == [("Up", 0.73, 5.0, 0.0)]
    assert decide(view(books=books, secs_in=160.0, secs_left=140.0), st).takes == []         # after 2:30
    cyc = {"side": "Up", "price": 0.70, "shares": 5.0, "pair": 0.0}
    flip = {"Up": {"bid": 0.20, "ask": 0.21}, "Down": {"bid": 0.79, "ask": 0.80}}
    d = decide(view(books=flip, scalp=cyc, secs_in=200.0, secs_left=100.0, shares={"Up": 5.0, "Down": 0.0}), st)
    assert d.takes == [] and d.places == [] and "settlement" in d.why["taker"]


def test_scalp_without_hedge_reserves_no_hedge_budget():
    st = scalp_settings(scalp_min=0.65, scalp_max=0.75, scalp_hedge_enabled=False, max_window_usdc=10.0)
    books = {"Up": {"bid": 0.69, "ask": 0.70}, "Down": {"bid": 0.30, "ask": 0.31}}
    d = decide(view(books=books, window_cost=5.0), st)                 # 5 + 3.5 fits 10 without a hedge reserve
    assert d.takes == [("Up", 0.70, 5.0, 0.0)]


def test_scalp_stop_loss_sells_the_first_buy_under_030():
    st = scalp_settings(scalp_stop_price=0.30, scalp_hedge_enabled=True, scalp_pair_enabled=False)
    cyc = {"side": "Up", "price": 0.55, "shares": 5.0, "pair": 0.0}
    ok = {"Up": {"bid": 0.33, "ask": 0.34}, "Down": {"bid": 0.66, "ask": 0.67}}
    assert decide(view(books=ok, scalp=cyc, shares={"Up": 5.0, "Down": 0.0}, window_cost=2.84), st).sells == []
    low = {"Up": {"bid": 0.28, "ask": 0.29}, "Down": {"bid": 0.71, "ask": 0.72}}
    d = decide(view(books=low, scalp=cyc, shares={"Up": 5.0, "Down": 0.0}, window_cost=2.84), st)
    assert d.sells == [("Up", 0.26, 5.0)] and d.takes == []          # sells first, no hedge on top
    # a sell already on its way: nothing new
    d = decide(view(books=low, scalp=cyc, shares={"Up": 5.0, "Down": 0.0}, window_cost=2.84,
                    in_flight_taker={"Up": 5.0, "Down": 0.0}), st)
    assert d.sells == [] and d.takes == []


def test_scalp_first_buy_needs_the_coin_past_the_strike_by_min_z():
    st = scalp_settings(scalp_min=0.80, scalp_max=0.90, scalp_min_z=1.0, scalp_first_after_s=60,
                        scalp_first_within_s=180, scalp_hedge_enabled=False, scalp_take_slip=0.03)
    books = {"Up": {"bid": 0.84, "ask": 0.85}, "Down": {"bid": 0.15, "ask": 0.16}}
    assert decide(view(books=books, secs_in=90.0, secs_left=210.0, dist_z=0.6), st).takes == []
    assert decide(view(books=books, secs_in=90.0, secs_left=210.0, dist_z=None), st).takes == []
    d = decide(view(books=books, secs_in=90.0, secs_left=210.0, dist_z=1.2), st)
    assert d.takes == [("Up", 0.88, 5.0, 0.0)]
    down = {"Up": {"bid": 0.15, "ask": 0.16}, "Down": {"bid": 0.84, "ask": 0.85}}
    assert decide(view(books=down, secs_in=90.0, secs_left=210.0, dist_z=1.2), st).takes == []    # wrong side
    d = decide(view(books=down, secs_in=90.0, secs_left=210.0, dist_z=-1.1), st)
    assert d.takes == [("Down", 0.88, 5.0, 0.0)]
    assert decide(view(books=books, secs_in=200.0, secs_left=100.0, dist_z=2.0), st).takes == []   # after 3:00
