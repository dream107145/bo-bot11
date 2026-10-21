"""Cross-market lag entry and pair rules."""
from xshark.cross_pair import (
    PairParams, Quote, best_xlead_pair, chased_away, cset_costs, cset_eligible,
    deep_eligible, gap_eligible, hold_eligible, high_peer_count, leader_eligible,
    maj_eligible, pair_ready, pair_rise_for, peer_average, rule_pnl_from_fills,
    still_cheap, sweep_eligible, xlead_all_pairs,
)

P = PairParams()


def _peers(n=5, px=0.90, outcome="Up", self_asset="btc"):
    asks = {f"{self_asset}:{outcome}": 0.40}
    for i, coin in enumerate(("eth", "sol", "xrp", "doge", "bnb", "hype")[:n]):
        asks[f"{coin}:{outcome}"] = px
    return asks


def test_peer_average_needs_min_peers():
    asks = _peers(4, 0.91)
    assert peer_average(asks, "btc", "Up", min_peers=5) is None
    asks = _peers(5, 0.91)
    assert abs(peer_average(asks, "btc", "Up", min_peers=5) - 0.91) < 1e-9


def test_leader_entry_in_band_with_peers_and_time():
    peer = peer_average(_peers(5, 0.89), "btc", "Up")
    assert leader_eligible(0.40, peer, elapsed_s=60.0, p=P)
    assert not leader_eligible(0.40, peer, elapsed_s=240.0, p=P)          # last minute
    assert not leader_eligible(0.25, peer, elapsed_s=60.0, p=P)           # under 0.30
    assert not leader_eligible(0.65, peer, elapsed_s=60.0, p=P)           # over 0.60
    assert not leader_eligible(0.40, 0.87, elapsed_s=60.0, p=P)           # peers too low


def test_hold_entry_strict_band_and_higher_peer_gate():
    assert hold_eligible(0.55, 0.91, elapsed_s=90.0, p=P)
    assert not hold_eligible(0.55, 0.91, elapsed_s=89.0, p=P)             # before 1:30
    assert not hold_eligible(0.50, 0.91, elapsed_s=120.0, p=P)            # not > 0.50
    assert not hold_eligible(0.60, 0.91, elapsed_s=120.0, p=P)            # not < 0.60
    assert not hold_eligible(0.55, 0.90, elapsed_s=120.0, p=P)            # needs > 0.90
    assert not hold_eligible(0.40, 0.91, elapsed_s=120.0, p=P)            # outside hold band


def test_hold_wins_over_leader_when_both_could_apply():
    # (0.5, 0.6) with peers > 0.90 from 1:30: hold eligible; leader also would be early on
    peer = 0.91
    assert hold_eligible(0.55, peer, 120.0, P) and leader_eligible(0.55, peer, 30.0, P)
    assert not hold_eligible(0.55, peer, 30.0, P)                         # hold waits until 1:30


def test_gap_entry_when_ask_is_035_under_peers_in_010_035_band():
    # ask 0.20, peers 0.55 -> gap 0.35; in band
    assert gap_eligible(0.20, 0.55, elapsed_s=60.0, p=P)
    assert gap_eligible(0.10, 0.50, elapsed_s=60.0, p=P)
    assert gap_eligible(0.35, 0.70, elapsed_s=60.0, p=P)
    assert not gap_eligible(0.20, 0.54, elapsed_s=60.0, p=P)             # gap only 0.34
    assert not gap_eligible(0.09, 0.50, elapsed_s=60.0, p=P)              # under 0.10
    assert not gap_eligible(0.36, 0.80, elapsed_s=60.0, p=P)              # over 0.35
    assert not gap_eligible(0.20, 0.55, elapsed_s=240.0, p=P)             # last minute
    # Rule 3 uses its own gap_until_s, independent of Rule 1's buy_until_s
    late_ok = PairParams(gap_until_s=300.0, buy_until_s=60.0)
    assert gap_eligible(0.20, 0.55, elapsed_s=200.0, p=late_ok)
    assert not leader_eligible(0.40, 0.89, elapsed_s=200.0, p=late_ok)
    early_cut = PairParams(gap_until_s=120.0)
    assert not gap_eligible(0.20, 0.55, elapsed_s=120.0, p=early_cut)


def test_deep_entry_when_ask_is_035_under_peers_in_001_015_band():
    # ask 0.10, peers 0.45 -> gap 0.35; in cheap band
    assert deep_eligible(0.10, 0.45, elapsed_s=60.0, p=P)
    assert deep_eligible(0.01, 0.36, elapsed_s=60.0, p=P)
    assert deep_eligible(0.15, 0.50, elapsed_s=60.0, p=P)
    assert not deep_eligible(0.10, 0.44, elapsed_s=60.0, p=P)            # gap only 0.34
    assert not deep_eligible(0.00, 0.40, elapsed_s=60.0, p=P)             # under 0.01
    assert not deep_eligible(0.16, 0.55, elapsed_s=60.0, p=P)             # over 0.15
    assert not deep_eligible(0.10, 0.45, elapsed_s=240.0, p=P)            # last minute


def test_deep_wins_over_gap_in_overlap_band():
    # 0.12 is in both Rule 3 [0.10, 0.35] and Rule 4 [0.01, 0.15]
    peer = 0.50
    assert deep_eligible(0.12, peer, 60.0, P) and gap_eligible(0.12, peer, 60.0, P)
    assert pair_rise_for("deep", P) == 0.10
    assert pair_rise_for("gap", P) == 0.30
    assert pair_rise_for("leader", P) == 0.30


def test_rules_1_and_3_pair_fallback_rise_after_40s():
    t0 = 1_000_000.0
    # Before wait: still need +$0.30
    assert pair_rise_for("leader", P, filled_at_ms=t0, now_ms=t0 + 39_000) == 0.30
    assert pair_rise_for("gap", P, filled_at_ms=t0, now_ms=t0 + 39_000) == 0.30
    # After 40s without pair: fall back to +$0.10
    assert pair_rise_for("leader", P, filled_at_ms=t0, now_ms=t0 + 40_000) == 0.10
    assert pair_rise_for("gap", P, filled_at_ms=t0, now_ms=t0 + 40_000) == 0.10
    assert pair_ready(0.50, entry_px=0.40, rise=0.10)
    assert not pair_ready(0.49, entry_px=0.40, rise=0.10)
    # Rule 4 never falls back
    assert pair_rise_for("deep", P, filled_at_ms=t0, now_ms=t0 + 60_000) == 0.10
    # Wait off: keep primary rise
    off = PairParams(pair_wait_s=0.0)
    assert pair_rise_for("leader", off, filled_at_ms=t0, now_ms=t0 + 60_000) == 0.30


def test_chase_stops_retry_after_ask_rises_005_from_signal():
    assert not chased_away(0.25, signal_ask=0.21, chase=0.05)            # +0.04
    assert chased_away(0.26, signal_ask=0.21, chase=0.05)                 # +0.05
    assert chased_away(0.39, signal_ask=0.21, chase=0.05)                 # XRP-style chase
    assert not chased_away(0.39, signal_ask=0.21, chase=0.0)              # chase off


def test_rebuy_while_ask_still_at_or_below_making_order_signal():
    # BNB Rule 5 style: signal 0.69, fill rejected — keep buying while ask ≤ 0.69
    assert still_cheap(0.69, signal_ask=0.69)
    assert still_cheap(0.65, signal_ask=0.69)
    assert not still_cheap(0.70, signal_ask=0.69)
    assert not still_cheap(0.69, signal_ask=0.0)


def test_pair_after_rise_of_030_from_entry():
    assert not pair_ready(0.65, entry_px=0.40, rise=0.30)
    assert pair_ready(0.70, entry_px=0.40, rise=0.30)
    assert pair_ready(0.71, entry_px=0.40, rise=0.30)


def test_deep_pair_after_rise_of_010_from_entry():
    assert not pair_ready(0.19, entry_px=0.10, rise=0.10)
    assert pair_ready(0.20, entry_px=0.10, rise=0.10)
    assert pair_ready(0.21, entry_px=0.10, rise=0.10)


def test_maj_buys_laggard_when_five_peers_above_085():
    asks = {
        "btc:Down": 0.40,
        "eth:Down": 0.90, "sol:Down": 0.91, "xrp:Down": 0.92,
        "doge:Down": 0.88, "bnb:Down": 0.99,
    }
    assert high_peer_count(asks, "btc", "Down", 0.85) == 5
    assert maj_eligible(0.40, asks, "btc", "Down", 60.0, P)
    assert maj_eligible(0.69, asks, "btc", "Down", 60.0, P)
    assert not maj_eligible(0.70, asks, "btc", "Down", 60.0, P)         # not < 0.70
    assert not maj_eligible(0.40, asks, "btc", "Down", 60.0, PairParams(rule5=False))
    # only four peers high
    thin = {**asks, "bnb:Down": 0.80}
    assert high_peer_count(thin, "btc", "Down", 0.85) == 4
    assert not maj_eligible(0.40, thin, "btc", "Down", 60.0, P)
    # Rule 5 uses its own maj_from_s / maj_until_s
    assert maj_eligible(0.40, asks, "btc", "Down", 299.0, P)
    assert not maj_eligible(0.40, asks, "btc", "Down", 300.0, P)
    assert not maj_eligible(0.40, asks, "btc", "Down", 200.0, PairParams(maj_until_s=180.0))
    assert not maj_eligible(0.40, asks, "btc", "Down", 89.0, PairParams(maj_from_s=90.0))
    assert maj_eligible(0.40, asks, "btc", "Down", 90.0, PairParams(maj_from_s=90.0))


def test_cset_locks_profit_after_fees_and_rejects_thin_edge():
    # Up 0.48 + Down 0.48 all-in, reserve 0 → cost 0.96, profit 0.04, ROI ~4.2%
    cost, profit, roi = cset_costs(0.48, 0.48, reserve=0.0)
    assert abs(cost - 0.96) < 1e-9
    assert abs(profit - 0.04) < 1e-9
    assert roi > 0.04
    assert cset_eligible(0.48, 0.48, 60.0, PairParams(cset_reserve=0.0, cset_min_profit=0.01,
                                                       cset_min_roi=0.01))
    # Fees/reserve wipe the edge
    _c, profit2, _r = cset_costs(0.495, 0.495, reserve=0.01)
    assert profit2 < 0.0
    assert not cset_eligible(0.495, 0.495, 60.0, PairParams(cset_reserve=0.01, cset_min_profit=0.01))
    # Apparent arb at best ask can fail min profit after reserve
    assert not cset_eligible(0.49, 0.49, 60.0,
                             PairParams(cset_reserve=0.01, cset_min_profit=0.02, cset_min_roi=0.0))
    # Time gate
    assert not cset_eligible(0.45, 0.45, 10.0, PairParams(cset_from_s=30.0, cset_reserve=0.0))
    assert not cset_eligible(0.45, 0.45, 295.0, PairParams(cset_until_s=290.0, cset_reserve=0.0))
    assert not cset_eligible(0.45, 0.45, 60.0, PairParams(cset=False, cset_reserve=0.0))
    # Cross-asset illusion: two legs that do NOT sum under $1 are not a complete set
    assert not cset_eligible(0.60, 0.60, 60.0, PairParams(cset_reserve=0.0, cset_min_profit=0.0,
                                                          cset_min_roi=0.0))
    # Rule 7 off must not disable secondary complete-set
    assert cset_eligible(0.48, 0.48, 60.0, PairParams(rule7=False, cset_reserve=0.0,
                                                      cset_min_profit=0.01, cset_min_roi=0.01))


def test_cset_pnl_attributes_both_legs_to_cset_not_rule7():
    fills = [
        {"tag": "crosspair:cset:btc:Up", "outcome": "Up", "price": 0.45, "size": 5.0, "fee": 0.05},
        {"tag": "crosspair:cset:btc:Down", "outcome": "Down", "price": 0.45, "size": 5.0, "fee": 0.05},
    ]
    # Up wins: Up payout 5 - 2.25 - 0.05 = 2.70; Down 0 - 2.25 - 0.05 = -2.30 → +0.40
    rp = rule_pnl_from_fills(fills, winner="Up")
    assert abs(rp["cset"] - 0.40) < 1e-9
    assert rp["rule7"] == 0.0
    assert rp["rule1"] == 0.0


def _q(ask: float) -> Quote:
    return Quote(bot=None, market=None, outcome="Up", ask=ask, bid=None,
                 limit=ask + 0.02, all_in=ask + 0.02)


def test_xlead_cascade_follows_distributed_order():
    from xshark.cross_pair import XLEAD_CASCADE, xlead_pair_key, xlead_resolve_pair
    from xshark.market import ASSETS
    assert len(XLEAD_CASCADE) == 15
    # Wave 1–2 match the user's distributed pattern
    assert XLEAD_CASCADE[:6] == (
        ("btc", "eth"), ("sol", "xrp"), ("doge", "bnb"),
        ("btc", "sol"), ("eth", "doge"), ("xrp", "bnb"),
    )
    assert XLEAD_CASCADE[6:9] == (("btc", "xrp"), ("eth", "bnb"), ("sol", "doge"))
    assert XLEAD_CASCADE[-1] == ("xrp", "doge")
    # All 15 unordered pairs of the six markets, no duplicates
    keys = [xlead_pair_key(a, b) for a, b in XLEAD_CASCADE]
    assert len(set(keys)) == 15
    expected = {xlead_pair_key(a, b) for i, a in enumerate(ASSETS) for b in ASSETS[i + 1:]}
    assert set(keys) == expected
    # Each wave of 3 uses six distinct assets (no market twice in one wave)
    for w in range(5):
        assets = [x for pair in XLEAD_CASCADE[w * 3:(w + 1) * 3] for x in pair]
        assert len(assets) == 6 and len(set(assets)) == 6
    quotes = {
        "btc:Up": _q(0.31), "btc:Down": _q(0.70),
        "eth:Up": _q(0.55), "eth:Down": _q(0.60),
        "sol:Up": _q(0.50), "sol:Down": _q(0.64),
    }
    pairs = xlead_all_pairs(quotes, max_sum=1.0)
    assert len(pairs) == 2
    assert pairs[0][0] == "btc" and pairs[0][1] == "eth"
    assert pairs[1][0] == "btc" and pairs[1][1] == "sol"
    assert abs(xlead_resolve_pair(quotes, "btc", "eth", 1.0)[4] - 0.91) < 1e-9
    assert xlead_resolve_pair(quotes, "eth", "sol", 1.0) is None
    picked = best_xlead_pair(quotes, max_sum=1.0)
    assert picked is not None and abs(picked[4] - 0.91) < 1e-9
    assert xlead_all_pairs(quotes, max_sum=0.80) == []
    assert 2 * 5 == 10


def test_xlead_pnl_attributes_both_legs_to_rule7():
    fills = [
        {"tag": "crosspair:xlead:btc>eth:Up", "outcome": "Up", "price": 0.31, "size": 5.0, "fee": 0.02},
        {"tag": "crosspair:xlead:btc>eth:Down", "outcome": "Down", "price": 0.60, "size": 5.0, "fee": 0.02},
    ]
    # Note: different markets — PnL attributed by each fill's own outcome vs winner.
    rp = rule_pnl_from_fills(fills, winner="Up")
    # Up: 5-1.55-0.02=3.43; Down: 0-3.0-0.02=-3.02 → +0.41
    assert abs(rp["rule7"] - 0.41) < 1e-9
    assert rp["cset"] == 0.0


def test_all_rules_have_independent_slots():
    from xshark.cross_pair import CrossPairStrategy, Position
    s = CrossPairStrategy.__new__(CrossPairStrategy)
    s.positions = {}
    for kind in ("maj", "sweep", "gap", "leader", "hold", "deep"):
        assert not s._has_kind(1, "bnb", "Up", kind)
        s.positions[s._pos_key(1, "bnb", "Up", kind)] = Position(
            start=1, asset="bnb", outcome="Up", kind=kind, entry_ask=0.40, signal_ask=0.40)
        assert s._has_kind(1, "bnb", "Up", kind)
    assert all(s._has_kind(1, "bnb", "Up", k)
               for k in ("maj", "sweep", "gap", "leader", "hold", "deep"))
    assert CrossPairStrategy._pair_tag("gap", "bnb", "Down") == "crosspair:pair:gap:bnb:Down"


def test_sweep_buys_when_all_five_peers_above_090_and_ask_in_070_090():
    asks = {
        "btc:Up": 0.80,
        "eth:Up": 0.91, "sol:Up": 0.92, "xrp:Up": 0.93,
        "doge:Up": 0.94, "bnb:Up": 0.95,
    }
    assert high_peer_count(asks, "btc", "Up", 0.90) == 5
    # Default Buys from = 140s (2:20)
    assert not sweep_eligible(0.80, asks, "btc", "Up", 139.0, P)
    assert sweep_eligible(0.70, asks, "btc", "Up", 140.0, P)
    assert sweep_eligible(0.80, asks, "btc", "Up", 140.0, P)
    assert sweep_eligible(0.90, asks, "btc", "Up", 140.0, P)
    assert not sweep_eligible(0.69, asks, "btc", "Up", 140.0, P)     # under band
    assert not sweep_eligible(0.91, asks, "btc", "Up", 140.0, P)     # over band
    assert not sweep_eligible(0.80, asks, "btc", "Up", 140.0, PairParams(rule6=False))
    assert not sweep_eligible(0.80, asks, "btc", "Up", 300.0, P)     # until close of window
    assert not sweep_eligible(0.80, asks, "btc", "Up", 200.0, PairParams(sweep_until_s=180.0))
    # one peer at 0.90 is not strictly above
    thin = {**asks, "bnb:Up": 0.90}
    assert high_peer_count(thin, "btc", "Up", 0.90) == 4
    assert not sweep_eligible(0.80, thin, "btc", "Up", 140.0, P)
    # Rule 2 still eligible on its own band when peers average high
    peer = peer_average(
        {"btc:Up": 0.55, "eth:Up": 0.91, "sol:Up": 0.92, "xrp:Up": 0.93,
         "doge:Up": 0.94, "bnb:Up": 0.95},
        "btc", "Up", min_peers=5,
    )
    assert hold_eligible(0.55, peer, 120.0, P)


def test_rule_switches_disable_entries():
    peer = 0.91
    assert leader_eligible(0.40, peer, 60.0, PairParams(rule1=False)) is False
    assert hold_eligible(0.55, peer, 120.0, PairParams(rule2=False)) is False
    assert gap_eligible(0.20, 0.55, 60.0, PairParams(rule3=False)) is False
    assert deep_eligible(0.10, 0.45, 60.0, PairParams(rule4=False)) is False
    asks = {"btc:Down": 0.40, "eth:Down": 0.90, "sol:Down": 0.91, "xrp:Down": 0.92,
            "doge:Down": 0.88, "bnb:Down": 0.99}
    assert maj_eligible(0.40, asks, "btc", "Down", 60.0, PairParams(rule5=False)) is False
    sweep_asks = {"btc:Up": 0.80, "eth:Up": 0.91, "sol:Up": 0.92, "xrp:Up": 0.93,
                  "doge:Up": 0.94, "bnb:Up": 0.95}
    assert sweep_eligible(0.80, sweep_asks, "btc", "Up", 140.0, PairParams(rule6=False)) is False
    # other rules still work when only one is off
    assert leader_eligible(0.40, peer, 60.0, PairParams(rule2=False, rule3=False, rule4=False))
    assert deep_eligible(0.10, 0.45, 60.0, PairParams(rule1=False, rule2=False, rule3=False))
    assert maj_eligible(0.40, asks, "btc", "Down", 60.0,
                        PairParams(rule1=False, rule2=False, rule3=False, rule4=False))
    assert hold_eligible(0.55, peer, 120.0, PairParams(rule6=False))  # rule 2 kept
    assert sweep_eligible(0.80, sweep_asks, "btc", "Up", 140.0,
                          PairParams(rule1=False, rule2=True, rule3=False, rule4=False, rule5=False))


def test_rule_pnl_attributes_pair_leg_to_entry_rule():
    # Named pair tags join their entry rule; legacy pair tags still resolve via opposite entry.
    leader = [
        {"tag": "crosspair:leader:btc:Up", "outcome": "Up", "price": 0.40, "size": 5.0, "fee": 0.0},
        {"tag": "crosspair:pair:leader:btc:Down", "outcome": "Down", "price": 0.55, "size": 5.0, "fee": 0.0},
    ]
    # Up wins: leader +5 - 2.0 = +3; pair Down 0 - 2.75 = -2.75 → rule1 = 0.25
    rp = rule_pnl_from_fills(leader, winner="Up")
    assert abs(rp["rule1"] - (3.0 - 2.75)) < 1e-9
    assert rp["rule2"] == 0.0
    legacy = [
        {"tag": "crosspair:gap:btc:Up", "outcome": "Up", "price": 0.20, "size": 5.0, "fee": 0.0},
        {"tag": "crosspair:pair:btc:Down", "outcome": "Down", "price": 0.55, "size": 5.0, "fee": 0.0},
    ]
    assert abs(rule_pnl_from_fills(legacy, winner="Up")["rule3"] - (4.0 - 2.75)) < 1e-9
    hold = [{"tag": "crosspair:hold:eth:Up", "outcome": "Up", "price": 0.55, "size": 5.0, "fee": 0.0}]
    assert abs(rule_pnl_from_fills(hold, winner="Up")["rule2"] - 2.25) < 1e-9


def test_pair_maker_price_is_post_only():
    from types import SimpleNamespace
    from xshark.cross_pair import CrossPairStrategy, Quote

    m = SimpleNamespace(tick=0.01)
    q = Quote(bot=None, market=m, outcome="Down", ask=0.35, bid=0.34, limit=0.37, all_in=0.37)
    assert CrossPairStrategy._maker_price(q) == 0.34
    q2 = Quote(bot=None, market=m, outcome="Down", ask=0.35, bid=0.35, limit=0.37, all_in=0.37)
    assert CrossPairStrategy._maker_price(q2) == 0.34   # one tick under the ask
