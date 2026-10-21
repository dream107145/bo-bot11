"""Every tunable of the strategy, with the ranges the console may set.

Defaults are starting points taken from measurements, not fitted values:

* the trader (x-MoneyForWhiskas, full activity log, BTC 5m): first fill a
  median 17 s into the window, trades until ~3.5 min, ~44 % of fills are
  resting bids, taker clips 5-100 shares, ends ~81 % hedged, never sells;
* troll-poly-bot's fastclip study (2026-09-30): trigger = 500 ms composite
  return >= 1.5 sigma, clips <= 16 shares / $7, same side again after 1 s.

Sized for a $40 paper bankroll (2026-09-30, the user's request): a window's
money is tied up until ~1 minute after its close, so the current and the
previous window overlap -- $15 a window, 5-share bids (the venue minimum),
5-share clips up to $4, a $10 daily loss stop.

``data/bot/controls.json`` (written by the console) overrides them while the
bot runs; the bot re-reads it every two seconds and reports what it applied.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields


@dataclass
class Settings:
    # fair value
    anchor_s: float = 1.0
    max_anchor_spread: float = 0.05
    # cross-market lag strategy (paper only; --cross-pair)
    cross_pair_limit_offset: float = 0.02
    cross_pair_shares: float = 5.0
    cross_pair_retry_ms: int = 500
    cross_pair_retry_attempts: int = 3
    cross_pair_rule1: bool = True             # leader + pair; repeats while eligible
    cross_pair_rule2: bool = True             # hold; repeats while eligible
    cross_pair_rule3: bool = True             # gap + pair; repeats while eligible
    cross_pair_rule4: bool = True             # deep gap + pair; repeats while eligible
    cross_pair_rule5: bool = True             # majority lag; repeats while eligible
    cross_pair_rule6: bool = True             # all-high sweep; repeats while eligible
    cross_pair_rule7: bool = True             # PRIMARY: best cross-asset Up+Down every 5s
    cross_pair_cset: bool = True              # SECONDARY: same-market complete-set / balance
    cross_pair_xlead_every_ms: int = 7000
    cross_pair_xlead_max_sum: float = 1.0
    cross_pair_xlead_max_tokens: float = 10.0
    cross_pair_xlead_from_s: float = 0.0
    cross_pair_xlead_until_s: float = 290.0
    cross_pair_leader_min: float = 0.30
    cross_pair_leader_max: float = 0.60
    cross_pair_peer_avg: float = 0.88
    cross_pair_leader_every_ms: int = 5000
    cross_pair_rise: float = 0.30
    cross_pair_buy_until_s: float = 240.0
    cross_pair_hold_peer_avg: float = 0.90
    cross_pair_hold_min: float = 0.50
    cross_pair_hold_max: float = 0.60
    cross_pair_hold_from_s: float = 90.0
    cross_pair_hold_every_ms: int = 5000
    cross_pair_gap: float = 0.35
    cross_pair_gap_min: float = 0.10
    cross_pair_gap_max: float = 0.35
    cross_pair_gap_until_s: float = 240.0
    cross_pair_gap_every_ms: int = 5000
    cross_pair_deep: float = 0.35
    cross_pair_deep_min: float = 0.01
    cross_pair_deep_max: float = 0.15
    cross_pair_deep_rise: float = 0.10
    cross_pair_deep_every_ms: int = 5000
    cross_pair_maj_high: float = 0.85
    cross_pair_maj_max: float = 0.70
    cross_pair_maj_min_high: int = 5
    cross_pair_maj_from_s: float = 0.0
    cross_pair_maj_until_s: float = 300.0
    cross_pair_maj_every_ms: int = 5000
    cross_pair_sweep_peer: float = 0.90
    cross_pair_sweep_min: float = 0.70
    cross_pair_sweep_max: float = 0.90
    cross_pair_sweep_min_high: int = 5
    cross_pair_sweep_every_ms: int = 5000
    cross_pair_sweep_from_s: float = 140.0    # 2:20 into the window
    cross_pair_sweep_until_s: float = 300.0
    cross_pair_cset_min_profit: float = 0.01  # $ locked profit per matched share after fees
    cross_pair_cset_min_roi: float = 0.01
    cross_pair_cset_reserve: float = 0.005    # extra $/leg for slip
    cross_pair_cset_from_s: float = 0.0
    cross_pair_cset_until_s: float = 290.0
    cross_pair_cset_max_loss: float = 0.02    # max $/sh loss to finish an unmatched leg
    cross_pair_pair_wait_s: float = 40.0
    cross_pair_pair_fallback_rise: float = 0.10
    cross_pair_chase: float = 0.05
    cross_pair_min_peers: int = 5
    # maker leg: resting bids on both outcomes
    maker_enabled: bool = True
    maker_edge: float = 0.02
    maker_size: float = 5.0
    maker_improve_ticks: int = 1
    maker_pair_max: float = 0.98
    maker_requote_ticks: int = 2
    maker_pull_sigma: float = 0.0
    maker_min_requote_ms: int = 500
    # taker leg: clips after a spot move
    taker_enabled: bool = True
    trigger_ms: int = 500
    trigger_sigma: float = 1.5
    taker_edge: float = 0.01
    clip_shares: float = 5.0
    clip_usdc_max: float = 4.0
    taker_cooldown_ms: int = 1000
    taker_max_per_window: int = 10
    # pair completion: once one side has filled, buy the other side so Up+Down pairs
    # cost <= pair_target (a pair pays $1 whichever side wins)
    pair_enabled: bool = True
    pair_target: float = 0.97
    pair_take: bool = True
    pair_bid: bool = True
    pair_stop_left_s: float = 10.0
    pair_on_price: bool = False
    # favourite bid: once a token trades above fav_trigger, rest a bid at fav_bid on it,
    # only while the price is at least fav_min_dist_bps from the strike on that token's side
    # (replay over 1,946 windows, 2026-10-01: +2.1..+3.4 % per $ bought; fills close to
    # the strike or late in the window lose)
    fav_enabled: bool = False
    fav_trigger: float = 0.90
    fav_bid: float = 0.90
    fav_size: float = 5.0
    fav_min_dist_bps: float = 2.4      # $20 at BTC 84,000
    fav_stop_left_s: float = 30.0
    fav_min_z: float = 0.0            # distance from the strike in sigmas for the time left; 0 = off
    fav_start_s: float = 0.0          # no favourite bid (and no favourite picked) before this many s in
    fav_bid_follow: bool = False      # the favourite bid follows the book (one tick over the best bid)
    fav_bid_min: float = 0.84         # ... never under this; never over fav_bid
    # after a favourite fill, a cheap bid on the other token (pair completion): 0.92 + 0.015 pays $1 either way
    fav_pair_enabled: bool = False
    fav_pair_price: float = 0.015
    # underdog first (user, 2026-10-01): bid fav_dog_bid on the OTHER token first; once that
    # fills, bid fav_bid on the favourite -- both filled is a pair at 0.05 + 0.90 = 0.95
    fav_dog_first: bool = False
    fav_dog_bid: float = 0.05
    # last-minute favourite (user, 2026-10-01): in the last late_left_s, rest a bid at late_bid
    # on the token trading above late_trigger; held to settlement. Its own time window: the
    # bid start/stop settings above do not apply to it.
    late_enabled: bool = False
    late_left_s: float = 60.0
    late_trigger: float = 0.90
    late_bid: float = 0.90
    late_size: float = 5.0
    late_min_dist_bps: float = 0.0
    late_bid_follow: bool = False     # the last-minute bid follows the book (one tick over the best bid)
    late_bid_min: float = 0.90        # ... never under this; never over late_bid
    # scalp cycles (user, 2026-10-05): 1) buy the token at 0.51-0.60 at once (taker);
    # 2) rest a pair bid on the other token so first + pair < scalp_pair_sum;
    # 3) if instead the other token RISES to scalp_hedge_price, buy scalp_hedge_size of it
    #    at once (a stop-buy: more shares than the first buy, so its win covers the loss);
    # 4) the pair or the hedge closes the cycle; after scalp_cooldown_s start again.
    #    Whatever is held at the close is held to settlement.
    scalp_enabled: bool = False
    scalp_min: float = 0.51
    scalp_max: float = 0.60
    scalp_min_z: float = 0.0              # > 0: first buy only when the coin is this many sigmas past the strike on its side
    scalp_size: float = 5.0
    scalp_pair_sum: float = 0.90
    scalp_hedge_price: float = 0.65
    scalp_hedge_max: float = 0.70
    scalp_hedge_size: float = 10.0
    scalp_hedge_profit: float = 0.0   # > 0: hedge with the shares that make the hedged side's win earn this
    scalp_cooldown_s: float = 1.0
    scalp_take_slip: float = 0.0      # first buy and hedge: limit = ask + this (fills past faster bots)
    scalp_stop_after_pair: bool = False   # a completed pair ends scalping for the rest of the window
    scalp_first_maker: bool = False   # the first buy rests as a maker bid instead of taking the ask
    scalp_first_within_s: float = 0.0     # > 0: a first buy only this many seconds into the window
    scalp_first_after_s: float = 0.0      # a first buy only from this many seconds into the window
    scalp_hedge_enabled: bool = True      # off: no hedge; the first buy is held to settlement
    scalp_stop_price: float = 0.0         # > 0: sell the first buy once its bid falls under this
    scalp_first_take_after_s: float = 0.0  # > 0 with a maker first buy: after this many s, take the ask instead
    scalp_pair_until_s: float = 0.0       # > 0: pair bids only this many seconds into the window
    scalp_pair_enabled: bool = True       # off: no pair bid; a cycle ends only by its hedge or the close
    scalp_hedge_after_s: float = 0.0      # > 0: the hedge only from this many seconds into the window
    scalp_stop_left_s: float = 60.0
    # inventory and risk
    alternate_sides: bool = False
    max_window_shares: float = 0.0
    max_window_usdc: float = 15.0
    max_imbalance_shares: float = 15.0
    skew_per_10sh: float = 0.01
    min_price: float = 0.05
    max_price: float = 0.95
    daily_loss_stop_usdc: float = 10.0
    stop_at_equity_usdc: float = 0.0      # > 0: stop trading for good once equity reaches this
    # timing and freshness
    start_after_s: float = 5.0
    # pause hours, GMT+9 (22.5 = 22:30; a range may wrap midnight, e.g. 23 -> 1); from == to = off
    pause_a_from: float = 0.0
    pause_a_to: float = 0.0
    pause_b_from: float = 0.0
    pause_b_to: float = 0.0
    maker_start_s: float = 0.0        # bids of every kind rest only from this far into the window
    maker_stop_left_s: float = 30.0
    taker_stop_left_s: float = 10.0
    max_book_age_ms: int = 3000
    max_spot_age_ms: int = 1500
    # paper venue
    paper_latency_real: bool = True
    paper_race: bool = True
    paper_presigned: bool = False
    paper_latency_ms: int = 150
    paper_cancel_latency_ms: int = 150
    # triangle (troll-poly-bot's pair-lock recovery, xshark/triangle.py); tri_<name> = TriParams.<name>
    # pair ladder (xshark/ladder.py); pl_<name> = LadderParams.<name>
    pl_enabled: bool = False
    pl_first_s: float = 10.0
    pl_every_s: float = 30.0
    pl_last_s: float = 250.0
    pl_max_price: float = 0.88
    pl_low_shares: float = 5.0
    pl_high_shares: float = 6.0
    pl_flip_profit: float = 0.10
    pl_slippage: float = 0.0
    pl_max_pair_cost: float = 1.01
    pl_net_over: float = 5.0
    pl_allowance: float = 0.005
    pl_max_book_age_ms: int = 2000
    tri_enabled: bool = False
    tri_open_delay_s: float = 2.0
    tri_open_window_s: float = 20.0
    tri_open_min_price: float = 0.70
    tri_open_max_price: float = 0.85
    tri_open_shares: float = 10.0
    tri_open_limit_offset: float = 0.02
    tri_open_max_tries: int = 3
    tri_pair_bid: bool = False
    tri_pair_sum: float = 0.95
    tri_pair_pull: bool = True
    tri_pair_sum_max: float = 0.0
    tri_pair_sum_step_s: float = 30.0
    tri_pair_shares: float = 0.0
    tri_recovery_enabled: bool = True
    tri_target_profit: float = 0.50
    tri_min_price_move: float = 0.05
    tri_recovery_min_price: float = 0.70
    tri_recovery_max_price: float = 0.90
    tri_max_recoveries: int = 2
    tri_cooldown_ms: int = 2000
    tri_min_time_left_s: float = 15.0
    tri_max_slippage: float = 0.03
    tri_slippage_allowance: float = 0.005
    tri_max_order_size: float = 60.0
    tri_max_total_position: float = 300.0
    tri_max_capital: float = 90.0
    tri_min_size: float = 5.0
    tri_max_market_loss: float = 35.0
    tri_lock_enabled: bool = True
    tri_lock_left_s: float = 60.0
    tri_max_actions: int = 30
    tri_profit_exit: float = 1.0
    tri_late_enabled: bool = True
    tri_late_window_s: float = 20.0
    tri_late_min_left_s: float = 10.0
    tri_late_min_p: float = 0.95
    tri_late_max_price: float = 0.97
    tri_late_shares: float = 10.0
    tri_late_max_capital: float = 20.0
    tri_late_max_orders: int = 2
    tri_late_min_edge: float = 0.0
    tri_last_enabled: bool = False
    tri_last_window_s: float = 60.0
    tri_last_every_ms: int = 500
    tri_last_min_price: float = 0.0
    tri_last_max_usd: float = 0.0
    tri_last_slippage: float = 0.03
    tri_last_shares: float = 0.0
    tri_last_max_price: float = 1.0
    tri_last_min_dist_bps: float = 0.0
    tri_last_comeback_low: float = 0.0
    tri_last_comeback_min_price: float = 0.0
    tri_repeat: bool = False
    tri_repeat_delay_ms: int = 500
    tri_repeat_until_s: float = 240.0
    tri_repeat_open_min_price: float = 0.0
    tri_repeat_open_max_price: float = 0.0
    tri_open_maker: bool = False
    tri_open_hold_top: bool = False
    tri_sell_all_price: float = 0.0
    tri_cheap_sell_price: float = 0.0
    tri_pair_timeout_s: float = 0.0
    tri_pair_timeout_profit: float = 0.10
    tri_pair_timeout_shares: float = 0.0
    tri_pair_hedge_price: float = 0.0
    tri_skip_after_loss: float = 0.0
    # the Whiskas bot (xshark/whisk.py, user 2026-10-10)
    wk_enabled: bool = False
    wk_shares: float = 60.0
    wk_rise: float = 0.05
    wk_trig_bps: float = 0.0
    wk_trig_ms: float = 500.0
    wk_limit_over: float = 0.01
    wk_rest_s: float = 0.0
    wk_only_above: bool = False
    wk_retry_s: float = 30.0
    wk_retry_gap_ms: float = 250.0
    wk_min_price: float = 0.09
    wk_max_price: float = 0.89
    wk_from_s: float = 1.0
    wk_until_s: float = 298.0
    wk_cooldown_ms: float = 500.0
    wk_gap_low_px: float = 0.20
    wk_gap_low_ms: float = 1000.0
    wk_gap_high_px: float = 0.80
    wk_gap_high_ms: float = 250.0
    wk_bal_k: float = 0.001
    wk_bal_keep_usd: float = 1.0
    wk_bal_delay_s: float = 1.0
    wk_profit_at: float = 0.60
    wk_profit_max: float = 120.0
    wk_window_usd: float = 0.0
    wk_max_book_age_ms: float = 2000.0
    wk_max_inflight: float = 3.0
    wk_ladder_levels: float = 0.0
    wk_ladder_step: float = 0.03
    wk_ladder_shares: float = 20.0
    wk_pair_on: bool = False
    wk_pair_move: float = 0.03
    wk_pair_ms: float = 1000.0
    wk_pair_margin: float = 0.02
    wk_pair_cd_ms: float = 3000.0
    wk_pair_free: bool = False
    wk_rise_pair_sum: float = 0.0
    v2_enabled: bool = False
    v2_lookback_s: float = 30.0
    v2_entry_rise: float = 0.05
    v2_entry_shares: float = 20.0
    v2_add_step: float = 0.05
    v2_add_shares: float = 5.0
    v2_add_max: float = 2.0
    v2_add_gap_s: float = 6.0
    v2_stop_drop: float = 0.05
    v2_pair_max: float = 0.95
    v2_bal_shares: float = 5.0
    v2_bal_gap_s: float = 2.0
    v2_slip: float = 0.005
    v2_max_slip: float = 0.02
    v2_limit_over: float = 0.01
    v2_max_spread: float = 0.03
    v2_d1_px: float = 0.32
    v2_d1_below: float = 0.3
    v2_d1_hold_s: float = 5.0
    v2_d1_shares: float = 5.0
    v2_d2_px: float = 0.42
    v2_d2_shares: float = 5.0
    v2_d3_px: float = 0.45
    v2_d4_up: bool = False
    v2_d4_shares: float = 5.0
    v2_e_low: float = 0.30
    v2_e1_px: float = 0.40
    v2_e1_shares: float = 10.0
    v2_e2_px: float = 0.50
    v2_e2_shares: float = 10.0
    v2_e_bal_shares: float = 5.0
    v2_e_max_px: float = 0.85
    v3_enabled: bool = False
    v3_lookback_s: float = 30.0
    v3_low_max: float = 0.5
    v3_entry_rise: float = 0.05
    v3_entry_shares: float = 20.0
    v3_add_step: float = 0.05
    v3_add_shares: float = 10.0
    v3_add_gap_s: float = 6.0
    v3_add_max: float = 0.0
    v3_rec_rise: float = 0.1
    v3_rec_max_px: float = 0.8
    v3_rec_step: float = 0.05
    v3_rec_step_shares: float = 5.0
    v3_rec_max: float = 4.0
    v3_bal_sum: float = 0.96
    v3_bal_shares: float = 10.0
    v3_bal_pair_max: float = 1.0
    v3_bal_gap_s: float = 3.0
    v3_lvl_a: float = 0.32
    v3_lvl_a_below: float = 0.3
    v3_lvl_a_shares: float = 10.0
    v3_lvl_b: float = 0.42
    v3_lvl_c: float = 0.45
    v3_lvl_c_shares: float = 20.0
    v3_c_mom_up: bool = False
    v3_c_step: float = 0.05
    v3_c_mom_shares: float = 10.0
    v3_rearm_drop: float = 0.0
    v3_max_chase: float = 0.03
    v3_min_benefit: float = 0.05
    v3_max_spend: float = 60.0
    v3_max_up: float = 80.0
    v3_max_down: float = 80.0
    v3_max_order: float = 40.0
    v3_max_outcome_loss: float = 30.0
    v3_max_imbalance: float = 40.0
    v3_max_slip: float = 0.02
    v3_limit_over: float = 0.01
    v3_max_spread: float = 0.03
    v3_max_px: float = 0.85
    v3_cutoff_s: float = 30.0
    v3_from_s: float = 1.0
    v3_max_book_age_ms: float = 1500.0
    v3_resend_ms: float = 0.0
    v4_enabled: bool = False
    v4_live_ok: bool = False
    v4_min_px: float = 0.1
    v4_max_px: float = 0.85
    v4_mid_lo: float = 0.4
    v4_mid_hi: float = 0.6
    v4_mid_win: float = 12.8
    v4_mid_lose: float = 12.0
    v4_high_win: float = 25.0
    v4_high_lose: float = 50.0
    v4_low_win: float = 12.8
    v4_low_lose: float = 12.0
    v4_entry_gap_s: float = 6.0
    v4_slow_lookback_s: float = 3.0
    v4_slow_th: float = 0.02
    v4_mom_gap_s: float = 5.0
    v4_fast_lookback_s: float = 2.0
    v4_fast_th: float = 0.04
    v4_bal_delay_s: float = 3.0
    v4_bal_win: float = 5.0
    v4_bal_lose: float = 20.0
    v4_bal_tol: float = 3.0
    v4_bal_gap_s: float = 3.0
    v4_cap_new_loss: bool = True
    v4_bal_reserve: float = 25.0
    v4_early_s: float = 120.0
    v4_early_tol: float = 3.0
    v4_early_skip_px: float = 0.73
    v4_max_retries: float = 3.0
    v4_early_pair: float = 0.97
    v5_enabled: bool = False
    v5_live_ok: bool = False
    v5_entry_cutoff_s: float = 285.0
    v5_bal_min_delay_s: float = 1.0
    v5_bal_max_delay_s: float = 5.0
    v5_cancel_all_s: float = 290.0
    v5_rise: float = 0.05
    v5_cooldown_s: float = 6.0
    v5_min_px: float = 0.08
    v5_max_px: float = 0.89
    v5_min_order: float = 5.0
    v5_max_order: float = 12.0
    v5_max_pair: float = 0.97
    v5_max_spend: float = 50.0
    v5_max_worst_loss: float = 5.0
    v5_max_shares: float = 60.0
    v5_max_imbalance: float = 12.0
    v5_max_unmatched_cost: float = 10.0
    v5_max_spread: float = 0.03
    v5_edge_gate: bool = False
    v5_min_edge: float = 0.02
    v5_max_slip: float = 0.02
    v5_limit_over: float = 0.01
    v5_max_book_age_ms: float = 1500.0
    v5_avg_pair_max: float = 0.97
    v5_high_wins: bool = True
    v6_enabled: bool = False
    v6_live_ok: bool = False
    v6_rise: float = 0.05
    v6_start_shares: float = 10.0
    v6_add1_shares: float = 8.0
    v6_add2_shares: float = 6.0
    v6_add_shares: float = 5.0
    v6_step_s: float = 6.0
    v6_fall_th: float = 0.01
    v6_hedge_mult: float = 1.0
    v6_hedge_dear: float = 1.2
    v6_hedge_cheap: float = 0.8
    v6_quick_pair: float = 0.95
    v6_rise_min_px: float = 0.30
    v6_add_max_px: float = 0.85
    v6_start_0_60_max_px: float = 0.45
    v6_start_60_120_max_px: float = 0.60
    v6_start_120_180_max_px: float = 0.70
    v6_start_180_240_max_px: float = 0.75
    v6_start_240_300_max_px: float = 0.85
    v6_fav_px: float = 0.80
    v6_fav_margin: float = 0.0
    v6_fav_min: float = 5.0
    v6_fav_max: float = 15.0
    v6_fav_min_dist: float = 50.0
    v6_neg_fix: bool = True
    v6_next_any: bool = False
    v6_min_px: float = 0.05
    v6_max_px: float = 0.95
    v6_max_spend: float = 100.0
    v6_max_order: float = 100.0
    v6_max_spread: float = 0.05
    v6_max_slip: float = 0.03
    v6_limit_over: float = 0.01
    v6_cutoff_s: float = 10.0
    v6_from_s: float = 1.0
    v6_max_book_age_ms: float = 1500.0
    v6_maker_s: float = 1.5
    v6_maker_improve: float = 1.0
    copy_enabled: bool = False
    copy_poll_ms: float = 100.0
    copy_max_price: float = 0.99
    copy_max_spread: float = 0.05
    copy_max_slip: float = 0.03
    copy_limit_over: float = 0.01
    copy_max_book_age_ms: float = 1500.0
    copy_cutoff_s: float = 10.0
    copy_max_fill_age_s: float = 10.0
    copy_max_attempts: float = 10.0
    copy_retry_ms: float = 1000.0
    v4_size1: float = 5.0
    v4_size2: float = 10.0
    v4_size3: float = 20.0
    v4_size4: float = 40.0
    v4_size5: float = 60.0
    v4_bal_size1: float = 5.0
    v4_bal_size2: float = 10.0
    v4_bal_size3: float = 20.0
    v4_bal_size4: float = 30.0
    v4_bal_size5: float = 40.0
    v4_max_spend: float = 100.0
    v4_max_shares: float = 200.0
    v4_max_loss: float = 60.0
    v4_max_consec: float = 4.0
    v4_max_spread: float = 0.03
    v4_max_slip: float = 0.02
    v4_limit_over: float = 0.01
    v4_min_improve: float = 0.5
    v4_reach_tol: float = 3.0
    v4_cutoff_s: float = 30.0
    v4_from_s: float = 1.0
    v4_max_book_age_ms: float = 1500.0
    v2_budget_usd: float = 30.0
    v2_max_net: float = 30.0
    v2_max_loss: float = 10.0
    v2_cutoff_s: float = 60.0
    v2_from_s: float = 1.0
    v2_max_book_age_ms: float = 1500.0
    v2_resend_ms: float = 1000.0
    tri_btc_shares: float = 0.0
    tri_btc_bps: float = 2.0
    tri_btc_ms: float = 500.0
    tri_btc_min_price: float = 0.10
    tri_btc_max_price: float = 0.90
    tri_btc_slippage: float = 0.02
    tri_btc_from_s: float = 10.0
    tri_btc_until_s: float = 240.0
    tri_btc_cooldown_ms: float = 1000.0
    tri_btc_after_open: bool = True
    tri_btc_max_buys: float = 10.0
    tri_cb_shares: float = 0.0
    tri_cb_low: float = 0.35
    tri_cb_first_trigger: float = 0.57
    tri_cb_step: float = 0.10
    tri_cb_levels: float = 3.0
    tri_cb_limit_over: float = 0.03
    tri_cb_max_price: float = 0.94
    tri_add_shares: float = 0.0
    tri_add_first_price: float = 0.75
    tri_add_step: float = 0.03
    tri_add_levels: float = 3.0
    tri_add_max_price: float = 0.88
    tri_pair_raise_s: float = 0.0
    tri_pair_raise_price: float = 0.50
    tri_hedge_by_time: bool = False
    tri_hedge_t1_min: float = 0.80
    tri_hedge_t1_max: float = 0.90
    tri_hedge_t2_min: float = 0.77
    tri_hedge_t2_max: float = 0.85
    tri_hedge_t3_min: float = 0.80
    tri_hedge_t3_max: float = 0.90
    tri_first_rec_min_price: float = 0.0
    tri_first_rec_max_price: float = 0.0
    tri_repeat_open_maker: bool = False
    tri_max_quote_age_ms: int = 2000


def _row(key, group, label, kind, lo, hi, step, unit, help_):
    return {"key": key, "group": group, "label": label, "kind": kind, "lo": lo, "hi": hi,
            "step": step, "unit": unit, "help": help_}


CONTROLS: list[dict] = [
    # Cross-market lag (--cross-pair): groups mirror rules; each rule has an on/off switch.
    _row("cross_pair_rule1", "Rule 1 · Leader", "Rule 1 on", "bool", 0, 1, 1, "",
         "Leader + pair: buy a lagging token when other markets average high, then maker-pair after the rise. Repeat a five-share clip on its own interval while eligible."),
    _row("cross_pair_leader_min", "Rule 1 · Leader", "Ask from", "float", 0.01, 0.99, 0.01, "$",
         "Buy the lagging token only while its ask is at least this (0.30)."),
    _row("cross_pair_leader_max", "Rule 1 · Leader", "Ask up to", "float", 0.01, 0.99, 0.01, "$",
         "Buy the lagging token only while its ask is at most this (0.60)."),
    _row("cross_pair_peer_avg", "Rule 1 · Leader", "Other markets avg over", "float", 0.50, 0.99, 0.01, "$",
         "Needs the average same-side ask across the other markets above this (0.88)."),
    _row("cross_pair_leader_every_ms", "Rule 1 · Leader", "Buy every", "int", 500, 60000, 500, "ms",
         "After the first filled clip, add five shares at this interval while Rule 1 stays eligible (5000 = 5 seconds)."),
    _row("cross_pair_buy_until_s", "Rule 1 · Leader", "Buys until", "float", 0, 300, 1, "s in",
         "No new Rule 1 buys after this many seconds into the window (240 = first four minutes)."),

    _row("cross_pair_rule2", "Rule 2 · Hold", "Rule 2 on", "bool", 0, 1, 1, "",
         "Hold: buy mid-band tokens from the hold start time and keep them to settlement (no pair leg). Repeat a five-share clip on its own interval while eligible."),
    _row("cross_pair_hold_min", "Rule 2 · Hold", "Ask above", "float", 0.01, 0.99, 0.01, "$",
         "Hold entry only while this market's ask is strictly above this (0.50)."),
    _row("cross_pair_hold_max", "Rule 2 · Hold", "Ask under", "float", 0.01, 0.99, 0.01, "$",
         "Hold entry only while this market's ask is strictly under this (0.60)."),
    _row("cross_pair_hold_peer_avg", "Rule 2 · Hold", "Other markets avg over", "float", 0.50, 0.99, 0.01, "$",
         "Hold-until-close needs the peer average above this (0.90)."),
    _row("cross_pair_hold_from_s", "Rule 2 · Hold", "Buys from", "float", 0, 300, 1, "s in",
         "No hold buys before this many seconds into the window (90 = from 1:30 until close)."),
    _row("cross_pair_hold_every_ms", "Rule 2 · Hold", "Buy every", "int", 500, 60000, 500, "ms",
         "After the first filled clip, add five shares at this interval while Rule 2 stays eligible (5000 = 5 seconds)."),

    _row("cross_pair_rule3", "Rule 3 · Gap", "Rule 3 on", "bool", 0, 1, 1, "",
         "Gap + pair: buy when this ask is far under the peer average in the gap band, then maker-pair after the rise. Repeat a five-share clip on its own interval while eligible."),
    _row("cross_pair_gap_min", "Rule 3 · Gap", "Ask from", "float", 0.01, 0.99, 0.01, "$",
         "Gap entry only while this market's ask is at least this (0.10)."),
    _row("cross_pair_gap_max", "Rule 3 · Gap", "Ask up to", "float", 0.01, 0.99, 0.01, "$",
         "Gap entry only while this market's ask is at most this (0.35)."),
    _row("cross_pair_gap", "Rule 3 · Gap", "Under peers by at least", "float", 0.01, 0.90, 0.01, "$",
         "Buy when this market's ask is at least this far under the other markets' average (0.35)."),
    _row("cross_pair_gap_until_s", "Rule 3 · Gap", "Buys until", "float", 0, 300, 1, "s in",
         "No new Rule 3 buys after this many seconds into the window (240 = first four minutes)."),
    _row("cross_pair_gap_every_ms", "Rule 3 · Gap", "Buy every", "int", 500, 60000, 500, "ms",
         "After the first filled clip, add five shares at this interval while Rule 3 stays eligible (5000 = 5 seconds)."),

    _row("cross_pair_rule4", "Rule 4 · Deep gap", "Rule 4 on", "bool", 0, 1, 1, "",
         "Deep gap + pair: buy a cheap lagging token ($0.01–$0.15) when ≥$0.35 under peers, then maker-pair after +$0.10. Repeat a five-share clip on its own interval while eligible."),
    _row("cross_pair_deep_min", "Rule 4 · Deep gap", "Ask from", "float", 0.01, 0.99, 0.01, "$",
         "Deep-gap entry only while this market's ask is at least this (0.01)."),
    _row("cross_pair_deep_max", "Rule 4 · Deep gap", "Ask up to", "float", 0.01, 0.99, 0.01, "$",
         "Deep-gap entry only while this market's ask is at most this (0.15)."),
    _row("cross_pair_deep", "Rule 4 · Deep gap", "Under peers by at least", "float", 0.01, 0.90, 0.01, "$",
         "Buy when this ask is at least this far under the other five markets' average (0.35)."),
    _row("cross_pair_deep_rise", "Rule 4 · Deep gap", "Pair after ask rises by", "float", 0.01, 0.90, 0.01, "$",
         "After a Rule 4 fill, once the token's ask has risen this much, rest a maker bid for five of the opposite token (0.10)."),
    _row("cross_pair_deep_every_ms", "Rule 4 · Deep gap", "Buy every", "int", 500, 60000, 500, "ms",
         "After the first filled clip, add five shares at this interval while Rule 4 stays eligible (5000 = 5 seconds)."),

    _row("cross_pair_rule5", "Rule 5 · Majority", "Rule 5 on", "bool", 0, 1, 1, "",
         "Majority: when enough other markets are above the high level, buy the lagging market under the max ask (no pair). Repeat a five-share clip on its own interval while eligible."),
    _row("cross_pair_maj_high", "Rule 5 · Majority", "Other markets above", "float", 0.50, 0.99, 0.01, "$",
         "Count other markets whose same-side ask is strictly above this (0.85)."),
    _row("cross_pair_maj_min_high", "Rule 5 · Majority", "High markets needed", "int", 1, 5, 1, "markets",
         "How many other markets must be above the high level before buying the laggard (5)."),
    _row("cross_pair_maj_max", "Rule 5 · Majority", "Buy ask under", "float", 0.01, 0.99, 0.01, "$",
         "Buy five shares only while this market's ask is strictly under this (0.70). Held to settlement, no pair."),
    _row("cross_pair_maj_from_s", "Rule 5 · Majority", "Buys from", "float", 0, 300, 1, "s in",
         "No Rule 5 buys before this many seconds into the window (0 = from the open)."),
    _row("cross_pair_maj_until_s", "Rule 5 · Majority", "Buys until", "float", 0, 300, 1, "s in",
         "No new Rule 5 buys after this many seconds into the window (300 = whole 5 minutes)."),
    _row("cross_pair_maj_every_ms", "Rule 5 · Majority", "Buy every", "int", 500, 60000, 500, "ms",
         "After the first filled clip, add five shares at this interval while Rule 5 stays eligible (5000 = 5 seconds)."),

    _row("cross_pair_rule6", "Rule 6 · Sweep", "Rule 6 on", "bool", 0, 1, 1, "",
         "Sweep: when all other markets are above the peer level, buy the laggard in the ask band, "
         "then buy another clip every interval while the condition stays true (no pair)."),
    _row("cross_pair_sweep_peer", "Rule 6 · Sweep", "Other markets above", "float", 0.50, 0.99, 0.01, "$",
         "Every other market's same-side ask must be strictly above this (0.90)."),
    _row("cross_pair_sweep_min_high", "Rule 6 · Sweep", "High markets needed", "int", 1, 5, 1, "markets",
         "How many other markets must be above the peer level (5 = all five others)."),
    _row("cross_pair_sweep_min", "Rule 6 · Sweep", "Ask from", "float", 0.01, 0.99, 0.01, "$",
         "Buy only while this market's ask is at least this (0.70)."),
    _row("cross_pair_sweep_max", "Rule 6 · Sweep", "Ask up to", "float", 0.01, 0.99, 0.01, "$",
         "Buy only while this market's ask is at most this (0.90)."),
    _row("cross_pair_sweep_every_ms", "Rule 6 · Sweep", "Buy every", "int", 500, 60000, 500, "ms",
         "While Rule 6's signal remains eligible, repeat its five-share clip at this cadence "
         "(5000 = every 5 seconds)."),
    _row("cross_pair_sweep_from_s", "Rule 6 · Sweep", "Buys from", "float", 0, 300, 1, "s in",
         "No Rule 6 buys before this many seconds into the window (140 = from 2:20)."),
    _row("cross_pair_sweep_until_s", "Rule 6 · Sweep", "Buys until", "float", 0, 300, 1, "s in",
         "No new Rule 6 buys after this many seconds into the window (300 = whole 5 minutes)."),

    _row("cross_pair_rule7", "Rule 7 · Cross pair", "Rule 7 on", "bool", 0, 1, 1, "",
         "PRIMARY: each cycle walks all 15 pairs in a distributed order "
         "(BTC×ETH, SOL×XRP, DOGE×BNB, BTC×SOL, …) so buys do not stack on one market. "
         "One pair every interval; next cycle after the 15th. Both legs same tick. "
         "Paper only. Scanned before rules 1–6."),
    _row("cross_pair_xlead_every_ms", "Rule 7 · Cross pair", "Buy every", "int", 500, 60000, 500, "ms",
         "Wait between cascade steps (7000 = buy BTC×ETH, wait 7s, buy SOL×XRP, …). "
         "Next cycle starts after all 15 pairs."),
    _row("cross_pair_xlead_max_tokens", "Rule 7 · Cross pair", "Tokens per pair", "float", 5, 200, 1, "shares",
         "Shares per cascade pair (10 = 5 Up + 5 Down). One distributed pair each interval."),
    _row("cross_pair_xlead_max_sum", "Rule 7 · Cross pair", "Max Up+Down ask", "float", 0.50, 1.99, 0.01, "$",
         "Buy a cascade pair only while its Up ask + Down ask (cheaper orientation) is at most this (1.00)."),
    _row("cross_pair_xlead_from_s", "Rule 7 · Cross pair", "Buys from", "float", 0, 300, 1, "s in",
         "No Rule 7 buys before this many seconds into the window (0 = from the open)."),
    _row("cross_pair_xlead_until_s", "Rule 7 · Cross pair", "Buys until", "float", 0, 300, 1, "s in",
         "No new Rule 7 buys after this many seconds into the window (290 = stop 10s before close)."),

    _row("cross_pair_cset", "Complete set · Secondary", "Complete set on", "bool", 0, 1, 1, "",
         "SECONDARY same-market engine: buy Up+Down when all-in cost (asks + fees + reserve) "
         "locks a positive settlement profit. Runs after Rule 7 and rules 1–6 — never first. "
         "Only labels a same-market pair as guaranteed arb when cost < $1 after fees."),
    _row("cross_pair_cset_min_profit", "Complete set · Secondary", "Min profit per share", "float", 0.0, 0.20, 0.001, "$",
         "Require at least this much locked profit per matched share after fees and reserve (0.01)."),
    _row("cross_pair_cset_min_roi", "Complete set · Secondary", "Min net ROI", "float", 0.0, 0.50, 0.001, "",
         "Require profit / all-in cost at least this (0.01 = 1%)."),
    _row("cross_pair_cset_reserve", "Complete set · Secondary", "Slip reserve per leg", "float", 0.0, 0.10, 0.001, "$",
         "Extra cost assumed on each leg for latency/slip before the edge is accepted (0.005)."),
    _row("cross_pair_cset_max_loss", "Complete set · Secondary", "Max loss to finish leg", "float", 0.0, 0.20, 0.001, "$",
         "If one leg filled and the other did not, still buy the missing leg while estimated "
         "total cost stays under $1 + this (0.02)."),
    _row("cross_pair_cset_from_s", "Complete set · Secondary", "Buys from", "float", 0, 300, 1, "s in",
         "No complete-set buys before this many seconds into the window (0 = from the open)."),
    _row("cross_pair_cset_until_s", "Complete set · Secondary", "Buys until", "float", 0, 300, 1, "s in",
         "No new complete-set buys after this many seconds into the window (290 = stop 10s before close)."),

    _row("cross_pair_rise", "Rules 1 & 3 · Pair & chase", "Pair after ask rises by", "float", 0.01, 0.90, 0.01, "$",
         "After a Rule 1 or Rule 3 fill, once the token's ask has risen this much, rest a maker bid on the opposite token (0.30). Rule 4 uses its own pair rise."),
    _row("cross_pair_pair_wait_s", "Rules 1 & 3 · Pair & chase", "Pair wait before fallback", "float", 0, 300, 1, "s",
         "If the opposite pair is still unfilled this many seconds after the Rule 1/3 entry fill, use the fallback rise (40). 0 = never fall back."),
    _row("cross_pair_pair_fallback_rise", "Rules 1 & 3 · Pair & chase", "Fallback pair rise", "float", 0.01, 0.90, 0.01, "$",
         "After the pair wait, rest the opposite maker once the entry ask has risen this much from the fill (0.10)."),
    _row("cross_pair_chase", "Rules 1, 3 & 4 · Pair & chase", "No retry after rise of", "float", 0, 0.50, 0.01, "$",
         "After the first Rule 1, 3, or 4 signal, do not buy again once the ask has risen this much (0.05). 0 = keep retrying."),

    _row("cross_pair_min_peers", "Other markets", "Other markets needed", "int", 1, 5, 1, "markets",
         "How many of the other five markets must quote the same side before the peer average counts (default 5). "
         "Rules 1–4 all use this. Lower it if books are thin or some markets sit at $0.99 and drop out of the average."),

    _row("cross_pair_shares", "Sizing & retries", "Shares per buy", "float", 5, 500, 1, "shares",
         "Shares bought on each qualifying leader, gap, deep, maj, sweep, cross-asset, complete-set, pair, or hold entry."),
    _row("cross_pair_limit_offset", "Sizing & retries", "Limit above best ask", "float", 0.00, 0.10, 0.01, "$",
         "Each buy limit is this much above the current best ask."),
    _row("cross_pair_retry_ms", "Sizing & retries", "Retry interval", "int", 0, 5000, 50, "ms",
         "Wait this many ms after an attempt before retrying (500). Zero retries as soon as the previous order resolves."),
    _row("cross_pair_retry_attempts", "Sizing & retries", "Attempts per entry", "int", 1, 20, 1, "attempts",
         "Maximum submission rounds for unfilled shares, including the first attempt (3). "
         "Still stops early if the ask rises by the chase amount from the signal."),

    _row("anchor_s", "Fair value", "Anchor lag", "float", 0.2, 5.0, 0.1, "s",
         "Fair = the Up mid this long ago + how far the TWAP model moved since. The market sets the level, "
         "the spot feed the fresh move."),
    _row("max_anchor_spread", "Fair value", "Max spread to anchor", "float", 0.01, 0.30, 0.01, "$",
         "A book wider than this is no anchor; the bot then has no fair value and does not trade."),

    _row("maker_enabled", "Maker bids", "Maker bids on", "bool", 0, 1, 1, "",
         "Rest a BUY on Up and on Down. The trader's fills were ~44 % resting bids."),
    _row("maker_edge", "Maker bids", "Bid below fair", "float", 0.0, 0.20, 0.005, "$",
         "Each bid sits at most this far under its side's fair value (makers pay no fee)."),
    _row("maker_size", "Maker bids", "Bid size", "float", 5, 200, 1, "shares",
         "Shares per resting bid. The venue minimum is 5."),
    _row("maker_improve_ticks", "Maker bids", "Improve best bid by", "int", 0, 5, 1, "ticks",
         "How far above the current best bid a bid may go (still under fair - edge and under the ask)."),
    _row("maker_pair_max", "Maker bids", "Up bid + Down bid max", "float", 0.80, 1.00, 0.01, "$",
         "Both bids together stay under this, so a bid filled on each side locks in 1 - this."),
    _row("maker_requote_ticks", "Maker bids", "Requote band", "int", 1, 10, 1, "ticks",
         "A resting bid stays while it is within this many ticks of its target and still at least half the "
         "edge under fair; beyond that it is replaced. 1 = chase every tick (a lot of cancels)."),
    _row("maker_pull_sigma", "Maker bids", "Pull on adverse move", "float", 0.0, 6.0, 0.1, "sigma",
         "When the spot moves at least this many sigmas (over the clip trigger window) against a side, that "
         "side's bid is cancelled at once and not replaced while the move lasts. 0 = off."),
    _row("maker_min_requote_ms", "Maker bids", "Min time between requotes", "int", 100, 10000, 50, "ms",
         "A bid is replaced at most this often per side (each replace is a cancel plus a new order)."),

    _row("taker_enabled", "Taker clips", "Taker clips on", "bool", 0, 1, 1, "",
         "Buy the ask right after a spot move, before the book reprices."),
    _row("trigger_ms", "Taker clips", "Move window", "int", 100, 5000, 50, "ms",
         "The spot return that triggers a clip is measured over this window."),
    _row("trigger_sigma", "Taker clips", "Move size", "float", 0.0, 6.0, 0.1, "sigma",
         "Minimum move, in sigmas of that window. 0 = no move needed (edge alone)."),
    _row("taker_edge", "Taker clips", "Min edge after fee", "float", 0.0, 0.20, 0.005, "$",
         "fair - ask - taker fee must be at least this. The fee is 0.07 * p * (1 - p) per share."),
    _row("clip_shares", "Taker clips", "Clip size", "float", 5, 500, 1, "shares",
         "Shares per clip (fill-and-kill: what is not there at the limit is dropped)."),
    _row("clip_usdc_max", "Taker clips", "Clip cap", "float", 1, 500, 1, "USDC",
         "A clip never costs more than this; it shrinks to fit. Below 5 shares it is not sent."),
    _row("taker_cooldown_ms", "Taker clips", "Same side again after", "int", 0, 30000, 100, "ms",
         "Minimum gap between two clips on the same outcome."),
    _row("taker_max_per_window", "Taker clips", "Clips per window", "int", 0, 1000, 1, "",
         "At most this many clips per 5-minute window."),

    _row("pair_enabled", "Pair completion", "Complete pairs", "bool", 0, 1, 1, "",
         "After one side fills, buy the other side so each Up+Down pair costs at most the target below: "
         "a completed pair pays $1 whichever side wins."),
    _row("pair_target", "Pair completion", "Max cost per pair", "float", 0.80, 1.05, 0.01, "$",
         "Average cost of the side held + the other side's price (fee included) must stay at or under this. "
         "0.97 locks at least 3 cents a share once paired."),
    _row("pair_take", "Pair completion", "Take the ask to complete", "bool", 0, 1, 1, "",
         "Buy the other side's ask at once (fill-and-kill) when it already completes the pair at the target."),
    _row("pair_bid", "Pair completion", "Rest a bid to complete", "bool", 0, 1, 1, "",
         "Otherwise rest a post-only bid on the other side at the price that completes the pair."),
    _row("pair_stop_left_s", "Pair completion", "Stop completing with", "float", 0, 240, 1, "s left",
         "No pair orders once this little of the window remains."),
    _row("pair_on_price", "Pair completion", "Rule on prices only", "bool", 0, 1, 1, "",
         "On: first fill's price + the other side's price must be strictly under the target (fees left out). "
         "Off: average cost including the taker fee + the other side's price <= the target."),
    _row("fav_enabled", "Favourite bid", "Bid the favourite", "bool", 0, 1, 1, "",
         "Once a token trades above the trigger, rest a bid on it at the bid price below and hold it to "
         "settlement. One fill per window."),
    _row("fav_trigger", "Favourite bid", "Trigger price", "float", 0.50, 0.99, 0.01, "$",
         "The first token whose mid goes above this is the window's favourite."),
    _row("fav_bid", "Favourite bid", "Bid price", "float", 0.50, 0.99, 0.01, "$",
         "Break-even needs the favourite to win more often than this price."),
    _row("fav_size", "Favourite bid", "Shares", "float", 5, 100, 1, "shares", "Size of the bid."),
    _row("fav_min_dist_bps", "Favourite bid", "Min distance from strike", "float", 0, 100, 0.1, "bps",
         "The bid rests only while the coin is at least this far from the strike on the favourite's side "
         "(1 bp = 0.01 %; 2.4 bps is about $20 on BTC); closer, it is pulled (those fills lost in the replay)."),
    _row("fav_min_z", "Favourite bid", "Min distance in σ", "float", 0, 5, 0.1, "σ",
         "The coin's distance from the strike on the favourite's side, in sigmas for the time left "
         "(|ln(price/strike)| / (σ·√seconds left)); the bid is pulled below this. 0 = off. Replay (2,187 BTC "
         "windows): 1.0 kept the profit and cut big losses ~10 %; higher cut the profit more than the losses."),
    _row("fav_start_s", "Favourite bid", "Start after", "float", 0, 280, 5, "s into the window",
         "No favourite bid before this many seconds into the window (120 = 2:00 in); the favourite is the first "
         "token above the trigger from then on. 0 = from the open."),
    _row("fav_bid_follow", "Favourite bid", "Bid follows the book", "bool", 0, 1, 1, "",
         "On: the favourite bid rests one tick over the best bid (never at the ask), between the lowest "
         "price below and the bid price above, and moves with the book. Off: always at the bid price."),
    _row("fav_bid_min", "Favourite bid", "Lowest bid", "float", 0.50, 0.99, 0.01, "$",
         "With the bid following the book: no bid under this. Set the trigger to this too, so a token in the "
         "range counts as the favourite."),
    _row("fav_pair_enabled", "Favourite bid", "Pair the favourite", "bool", 0, 1, 1, "",
         "After the favourite bid fills, rest a bid on the other token at the price below (same shares) and "
         "keep it to the close: if it fills, the pair pays $1 whichever side wins. Not for the last-minute bid."),
    _row("fav_pair_price", "Favourite bid", "Pair bid price", "float", 0.001, 0.10, 0.001, "$",
         "A sub-cent price (e.g. 0.015) is only accepted once that token trades under ~0.04 (the venue's "
         "tick there is 0.001); until then the bid waits."),
    _row("fav_stop_left_s", "Favourite bid", "Pull with", "float", 0, 240, 1, "s left",
         "No favourite bid once this little of the window remains."),
    _row("fav_dog_first", "Favourite bid", "Underdog first", "bool", 0, 1, 1, "",
         "On: first rest a bid on the underdog at the price below; only once it fills, bid the favourite at "
         "the bid price above (0.05 + 0.90 = a 0.95 pair that pays $1). The distance rule is not used."),
    _row("fav_dog_bid", "Favourite bid", "Underdog bid", "float", 0.01, 0.20, 0.01, "$",
         "The first bid, on the token that is not the favourite."),
    _row("late_enabled", "Last-minute favourite", "Bid the favourite late", "bool", 0, 1, 1, "",
         "In the last minute, rest a bid on the token trading above the trigger and hold it to settlement. "
         "Its own window: the bid start/stop times do not apply. One fill per window."),
    _row("late_left_s", "Last-minute favourite", "Starts with", "float", 5, 240, 1, "s left",
         "The bid rests from this many seconds before the close until the close."),
    _row("late_trigger", "Last-minute favourite", "Trigger price", "float", 0.50, 0.99, 0.01, "$",
         "The token whose mid is above this is bid."),
    _row("late_bid", "Last-minute favourite", "Bid price", "float", 0.50, 0.99, 0.01, "$",
         "Break-even needs that token to win more often than this price."),
    _row("late_size", "Last-minute favourite", "Shares", "float", 5, 100, 1, "shares", "Size of the bid."),
    _row("late_min_dist_bps", "Last-minute favourite", "Min distance from strike", "float", 0, 100, 0.1, "bps",
         "Pull the bid while the coin is closer than this to the strike on that token's side. 0 = off."),
    _row("scalp_enabled", "Scalp cycles", "Scalp cycles", "bool", 0, 1, 1, "",
         "1) buy the token priced in the range below at once; 2) rest a pair bid on the other token so the "
         "pair costs under the pair sum; 3) if the other token instead rises to the hedge price, buy the hedge "
         "size of it at once; 4) the pair or the hedge ends the cycle; after the cooldown, start again."),
    _row("scalp_min", "Scalp cycles", "First buy from", "float", 0.40, 0.95, 0.01, "$", "Lowest ask for the first buy."),
    _row("scalp_max", "Scalp cycles", "First buy to", "float", 0.40, 0.95, 0.01, "$", "Highest ask for the first buy."),
    _row("scalp_min_z", "Scalp cycles", "First buy min z", "float", 0, 3, 0.05, "sigma",
         "Buy a token only when the coin is at least this far past the strike on that token's side, in "
         "sigmas of the move still possible (ln(price/strike) / (sigma * sqrt(seconds left))). 0 = off."),
    _row("scalp_size", "Scalp cycles", "First buy shares", "float", 5, 100, 1, "shares", "Size of the first buy."),
    _row("scalp_pair_sum", "Scalp cycles", "Pair sum under", "float", 0.50, 1.00, 0.01, "$",
         "The pair bid makes first price + pair price strictly under this (0.90 locks $0.10 a share)."),
    _row("scalp_hedge_price", "Scalp cycles", "Hedge at", "float", 0.50, 0.95, 0.01, "$",
         "When the other token's ask reaches this, buy the hedge shares of it at once (a stop-buy)."),
    _row("scalp_hedge_max", "Scalp cycles", "Hedge no higher than", "float", 0.50, 0.99, 0.01, "$",
         "No hedge if the other token has already jumped above this."),
    _row("scalp_hedge_size", "Scalp cycles", "Hedge shares", "float", 5, 200, 1, "shares",
         "More than the first buy, so the hedge's win covers the first buy's loss."),
    _row("scalp_hedge_profit", "Scalp cycles", "Hedge for a profit of", "float", 0, 5, 0.05, "$",
         "Above 0: the hedge buys exactly the shares that make a win on the hedged side pay back the first buy, "
         "the hedge and fees plus this much (more shares the dearer the hedge). 0: use the fixed hedge shares."),
    _row("scalp_take_slip", "Scalp cycles", "Taker limit above the ask", "float", 0, 0.10, 0.01, "$",
         "The first buy and the hedge are sent with limit = ask + this: if faster bots take the best ask, the "
         "order still fills from the next levels up to the limit. Pays up to this much more per share."),
    _row("scalp_first_maker", "Scalp cycles", "First buy as a maker bid", "bool", 0, 1, 1, "",
         "On: the first buy rests as a bid one tick over the best bid (never at the ask) on the token priced in "
         "the range, and the cycle starts when it fills (no fee). Off: it takes the ask at once."),
    _row("scalp_first_within_s", "Scalp cycles", "First buy only in the first", "float", 0, 300, 5, "s",
         "A first buy (bid or take) only this many seconds into the window; the first-buy bid is pulled after "
         "it. Open cycles still pair or hedge. 0 = any time."),
    _row("scalp_hedge_after_s", "Scalp cycles", "Hedge only after", "float", 0, 300, 5, "s into the window",
         "No hedge before this many seconds into the window (170 = 2:50 in, 2:10 left); then it fires when the "
         "other token is in the hedge range. 0 = any time."),
    _row("scalp_pair_enabled", "Scalp cycles", "Pair bid", "bool", 0, 1, 1, "",
         "On: after the first buy, rest a pair bid on the other token (step 2). Off: no pair; the first buy is "
         "hedged or held to settlement."),
    _row("scalp_pair_until_s", "Scalp cycles", "Pair bids until", "float", 0, 300, 5, "s into the window",
         "The pair bid rests only until this many seconds into the window (180 = 3:00 in, 2:00 left); then it "
         "is pulled and the cycle is held to settlement (the hedge still works). 0 = to the close."),
    _row("scalp_first_after_s", "Scalp cycles", "First buy from", "float", 0, 280, 5, "s into the window",
         "No first buy before this many seconds into the window (60 = 1:00 in)."),
    _row("scalp_hedge_enabled", "Scalp cycles", "Hedge", "bool", 0, 1, 1, "",
         "On: buy the other token when it reaches the hedge range (step 3). Off: never hedge; hold to settlement."),
    _row("scalp_stop_price", "Scalp cycles", "Sell the first buy under", "float", 0, 0.60, 0.01, "$",
         "Stop-loss: once the first-bought token's bid falls under this, sell those shares at once (limit 2 cents "
         "under the bid) and end the cycle. 0 = off (hold or hedge)."),
    _row("scalp_first_take_after_s", "Scalp cycles", "Take the ask after", "float", 0, 300, 1, "s into the window",
         "With the first buy as a maker bid: if it has not filled this many seconds into the window, pull it and "
         "take the ask (limit = ask + 'Taker limit above the ask'). 0 = maker only."),
    _row("scalp_stop_after_pair", "Scalp cycles", "Stop after a pair", "bool", 0, 1, 1, "",
         "On: once a pair completes, no new first buy for the rest of the window (a hedged cycle still allows "
         "a new one)."),
    _row("scalp_cooldown_s", "Scalp cycles", "Cooldown", "float", 0, 60, 0.5, "s", "Wait after a cycle ends."),
    _row("scalp_stop_left_s", "Scalp cycles", "No new cycle with", "float", 0, 280, 5, "s left",
         "No new first buy once this little of the window remains (open cycles still pair or hedge)."),
    _row("late_bid_follow", "Last-minute favourite", "Bid follows the book", "bool", 0, 1, 1, "",
         "On: the last-minute bid rests one tick over the best bid (never at the ask), between the lowest price "
         "below and the bid price above, and moves with the book. Off: always at the bid price."),
    _row("late_bid_min", "Last-minute favourite", "Lowest bid", "float", 0.50, 0.99, 0.01, "$",
         "With the bid following the book: no bid under this. Set the trigger to this too."),
    _row("alternate_sides", "Inventory and risk", "Alternate sides", "bool", 0, 1, 1, "",
         "Each new buy must be the other side of the window's last fill: Up, Down, Up, Down (or Down, Up, ...)."),
    _row("max_window_shares", "Inventory and risk", "Max shares per window", "float", 0, 1000, 5, "shares",
         "Up + Down shares bought (and in flight) per window never exceed this. 20 = four 5-share buys. 0 = off."),
    _row("max_window_usdc", "Inventory and risk", "Budget per window", "float", 5, 10000, 5, "USDC",
         "Spent plus resting plus in flight, per window. Nothing new past it."),
    _row("max_imbalance_shares", "Inventory and risk", "Max one-side excess", "float", 5, 5000, 5, "shares",
         "No new buy on a side that already holds this many more shares than the other."),
    _row("skew_per_10sh", "Inventory and risk", "Skew per 10 shares", "float", 0.0, 0.05, 0.001, "$",
         "The heavier side's bid drops (and the lighter side's rises) by this per 10 shares of excess."),
    _row("min_price", "Inventory and risk", "Lowest price bought", "float", 0.01, 0.99, 0.01, "$",
         "No bid or clip below this price. Must stay under the highest price."),
    _row("max_price", "Inventory and risk", "Highest price bought", "float", 0.01, 0.99, 0.01, "$",
         "No bid or clip above this price. Must stay over the lowest price."),
    _row("stop_at_equity_usdc", "Inventory and risk", "Stop at equity", "float", 0, 100000, 1, "USDC",
         "Once equity (cash + open shares at their current price) reaches this, no new orders: it stays "
         "stopped until this is changed or cleared (0 = off). Shares already held still settle."),
    _row("daily_loss_stop_usdc", "Inventory and risk", "Daily loss stop", "float", 0, 100000, 5, "USDC",
         "Settled loss today (GMT+9) at or beyond this halts new orders until midnight GMT+9. 0 = off."),

    _row("pause_a_from", "Pause hours (GMT+9)", "Pause 1 from", "float", 0, 24, 0.25, "h",
         "No new orders from this hour (22.5 = 22:30) to the next setting; resting bids are pulled, shares "
         "already held still settle. From = to: off."),
    _row("pause_a_to", "Pause hours (GMT+9)", "Pause 1 to", "float", 0, 24, 0.25, "h",
         "End of pause 1. A range may wrap midnight (23 -> 1)."),
    _row("pause_b_from", "Pause hours (GMT+9)", "Pause 2 from", "float", 0, 24, 0.25, "h", "A second pause. From = to: off."),
    _row("pause_b_to", "Pause hours (GMT+9)", "Pause 2 to", "float", 0, 24, 0.25, "h", "End of pause 2."),
    _row("start_after_s", "Timing", "Start after the open", "float", 0, 120, 1, "s",
         "Nothing is bought in the first seconds of a window (the strike prints ~1.4 s after the open)."),
    _row("maker_start_s", "Timing", "Bids start after", "float", 0, 240, 1, "s",
         "No bid of any kind (pair, favourite, underdog, ordinary) rests before this many seconds into the "
         "window. With the 'off with' settings at 150 s left, bids rest from 0:30 to 2:30 only."),
    _row("maker_stop_left_s", "Timing", "Bids off with", "float", 0, 240, 1, "s left",
         "Every resting bid is cancelled once this little of the window remains."),
    _row("taker_stop_left_s", "Timing", "Clips off with", "float", 0, 240, 1, "s left",
         "No clip once this little of the window remains."),
    _row("max_book_age_ms", "Timing", "Max book silence", "int", 100, 30000, 100, "ms",
         "A book not heard from for longer is stale: bids come off and no clip is sent."),
    _row("max_spot_age_ms", "Timing", "Max spot age", "int", 100, 30000, 100, "ms",
         "A composite spot price older than this is stale: no fair value, no trade."),

    _row("paper_latency_real", "Paper venue", "Real VPS latency", "bool", 0, 1, 1, "",
         "Each order and cancel waits a delay drawn from this VPS's real orders (decision to venue reply: "
         "p50 ~271 ms, p90 ~534 ms, p99 ~1.2 s). Off = the fixed latencies below."),
    _row("paper_race", "Paper venue", "Real race odds for clips", "bool", 0, 1, 1, "",
         "A clip that finds its price in the book still fills only with the odds real spot-triggered orders "
         "from this VPS had (7-54 % by ask depth; 10.7 % of all real orders filled). Off = the book alone."),
    _row("paper_presigned", "Paper venue", "Pre-signed orders", "bool", 0, 1, 1, "",
         "Model orders built and signed before the trigger (a ladder of ready orders): the real pre-send time "
         "(build + sign + queue, ~12-30 ms) is dropped and only the real venue time is drawn. A what-if for a "
         "live adapter that pre-signs; xshark itself signs nothing."),
    _row("paper_latency_ms", "Paper venue", "Order latency", "int", 0, 5000, 10, "ms",
         "Fixed delay used when Real VPS latency is off."),
    _row("paper_cancel_latency_ms", "Paper venue", "Cancel latency", "int", 0, 5000, 10, "ms",
         "Fixed cancel delay used when Real VPS latency is off; a bid can still fill until it lands."),
    _row("pl_enabled", "Pair ladder", "Pair ladder on", "bool", 0, 1, 1, "",
         "Every 30 s (10-250 s into the window) buy both tokens with taker orders while the leader is under the max price; held to settlement."),
    _row("pl_first_s", "Pair ladder", "First round at", "float", 0, 290, 1, "s into the window",
         ""),
    _row("pl_every_s", "Pair ladder", "Round every", "float", 5, 300, 1, "s",
         ""),
    _row("pl_last_s", "Pair ladder", "Last round at", "float", 0, 299, 1, "s into the window",
         "10 / 30 / 250 = rounds at 10, 40, 70, ... 250 s."),
    _row("pl_max_price", "Pair ladder", "Buy while the leader is under", "float", 0.5, 0.99, 0.01, "$",
         "A round is skipped when the leading token's ask is at or over this."),
    _row("pl_low_shares", "Pair ladder", "Low token shares", "float", 5, 500, 1, "shares",
         "Bought each round (the token with the lower price)."),
    _row("pl_high_shares", "Pair ladder", "High token shares", "float", 5, 500, 1, "shares",
         "Bought each round while the leader is the same as the last round's."),
    _row("pl_flip_profit", "Pair ladder", "After a flip, win at least", "float", 0, 10, 0.05, "$",
         "When the leader flipped: the new leader's shares are sized so the window ends this much up (fees included) if it wins."),
    _row("pl_net_over", "Pair ladder", "Buy only the high token over", "float", 0, 100, 1, "shares",
         "When a round's high-token shares exceed its low-token shares by more than this (after a flip), it buys only the high token: high - low shares. 0 = off."),
    _row("pl_max_pair_cost", "Pair ladder", "Pair cost at most", "float", 0.95, 1.10, 0.01, "$",
         "A round buys only while Up ask + Down ask is at most this, and keeps trying until the next round; each leg's limit is its ask. 1.01 = the tightest book (1 c spread)."),
    _row("pl_allowance", "Pair ladder", "Slippage allowance", "float", 0, 0.05, 0.001, "$ a share",
         "Added to the cost when sizing the flip buy."),
    _row("pl_max_book_age_ms", "Pair ladder", "Book older than", "int", 200, 10000, 100, "ms",
         "No round on a book older than this."),
    _row("tri_enabled", "Triangle", "Triangle on", "bool", 0, 1, 1, "",
         "troll-poly-bot's pair-lock recovery: an open buy, then a pair bid / one recovery, then the last minute."),
    _row("tri_open_delay_s", "Triangle", "Open buy from", "float", 0, 60, 1, "s into the window",
         "The open buy is not tried before this."),
    _row("tri_open_window_s", "Triangle", "Open buy until", "float", 1, 120, 1, "s into the window",
         "The open buy is not tried after this."),
    _row("tri_open_min_price", "Triangle", "Open buy from price", "float", 0.05, 0.99, 0.01, "$",
         "A token's ask must be at least this (if both tokens are in the range, the dearer is bought)."),
    _row("tri_open_max_price", "Triangle", "Open buy to price", "float", 0.05, 0.99, 0.01, "$",
         "...and at most this."),
    _row("tri_open_shares", "Triangle", "Open buy shares", "float", 5, 200, 1, "shares",
         "Size of the open buy (taker)."),
    _row("tri_open_limit_offset", "Triangle", "Open buy limit over ask", "float", 0, 0.1, 0.01, "$",
         "Limit = ask + this."),
    _row("tri_open_max_tries", "Triangle", "Open buy tries", "int", 1, 10, 1, "orders",
         "Open orders sent per window at most."),
    _row("tri_pair_bid", "Triangle", "Pair bid on", "bool", 0, 1, 1, "",
         "After the open fill, a resting bid for the other token at pair sum - the open price. While it rests no recovery runs; once it fills the window is locked."),
    _row("tri_pair_sum", "Triangle", "Pair sum (start)", "float", 0.5, 1.0, 0.01, "$",
         "Open price + pair bid = this (0.95 locks about $0.05 a share)."),
    _row("tri_pair_shares", "Triangle", "Pair bid shares", "float", 0, 500, 1, "shares",
         "How many shares the pair bid buys (at least 5, the venue minimum). 0 = as many as the first buy filled."),
    _row("tri_pair_timeout_s", "Triangle", "Pair bid time limit", "float", 0, 300, 5, "s",
         "If the pair bid is not complete this long after the open filled: pull it and buy the pair token as a taker, sized for 'Then win at least' if it wins (at least 5 sh). 0 = off."),
    _row("tri_pair_timeout_profit", "Triangle", "Then win at least", "float", 0, 10, 0.05, "$",
         "The window's result, fees included, if the pair token wins after that taker buy."),
    _row("tri_pair_timeout_shares", "Triangle", "Then buy shares", "float", 0, 500, 1, "shares",
         "After the pair bid's time limit: buy exactly this many of the pair token (taker). 0 = size it for 'Then win at least'."),
    _row("tri_add_shares", "Triangle", "Add-on shares per level", "float", 0, 200, 1, "shares",
         "After the pair token is bought as a taker (time limit or 'Buy the pair token at once over'): buy this many more of it each time its ask reaches a level. 0 = off."),
    _row("tri_add_first_price", "Triangle", "Add-on first level", "float", 0.5, 0.99, 0.01, "$",
         "The first level (0.75); the next ones are 'Add-on step' higher each."),
    _row("tri_add_step", "Triangle", "Add-on step", "float", 0.01, 0.2, 0.01, "$",
         "The gap between levels (0.03: 0.75 / 0.78 / 0.81)."),
    _row("tri_add_levels", "Triangle", "Add-on levels", "float", 1, 10, 1, "",
         "How many levels (3)."),
    _row("tri_add_max_price", "Triangle", "Add-on up to price", "float", 0.5, 0.99, 0.01, "$",
         "Shares a level did not fill (the price jumped past it, the order was killed) are bought while the ask is at most this; never over it."),
    _row("wk_enabled", "Whiskas bot", "Whiskas bot on", "bool", 0, 1, 1, "",
         "Buy every token that rises, with FAK orders, and match each rising fill on the other token."),
    _row("wk_shares", "Whiskas bot", "Rising buy shares", "float", 5, 1000, 1, "shares",
         "When a token's price rises 'Rising buy on a rise of': buy this many of it. A rise while it is still buying adds this many more."),
    _row("wk_rise", "Whiskas bot", "Rising buy on a rise of", "float", 0, 0.5, 0.01, "$",
         "Fixed steps over a token's low: rising buys at low + 0.05, + 0.10, + 0.15 ..., each once, either token, all window. A step passed during the gap after a rising buy is skipped; a whole-step fall makes a step count again. 0 = off."),
    _row("wk_trig_bps", "Whiskas bot", "Or a BTC move of", "float", 0, 50, 0.1, "bps",
         "Also trigger a rising buy of the token BTC pushes up when BTC moves this much within 'BTC move within'. 0 = off."),
    _row("wk_trig_ms", "Whiskas bot", "BTC move within", "float", 50, 5000, 50, "ms",
         "The BTC move is measured over this much time, up to the newest BTC price."),
    _row("wk_limit_over", "Whiskas bot", "Limit over the best bid", "float", 0, 0.1, 0.01, "$",
         "Every try is a FAK order (fills what is there at the limit, the rest is dropped) with limit = the best bid + this (0.01)."),
    _row("wk_retry_gap_ms", "Whiskas bot", "FAK again every", "float", 50, 5000, 50, "ms",
         "Until the buy has all its shares, a new FAK try goes this often (once the last one is back)."),
    _row("wk_retry_s", "Whiskas bot", "Keep trying at most", "float", 0, 300, 1, "s",
         "A buy stops trying after this long."),
    _row("wk_only_above", "Whiskas bot", "Try only while over the start", "bool", 0, 1, 1, "",
         "On: tries go on only while the token is over where the buy started."),
    _row("wk_rest_s", "Whiskas bot", "Rest the rest as a bid for", "float", 0, 60, 0.5, "s",
         "Above 0: what a FAK try did not get rests as a maker bid (at its limit, under the ask) this long before the next try. 0 = FAK only."),
    _row("wk_min_price", "Whiskas bot", "Token from price", "float", 0.01, 0.99, 0.01, "$",
         "Buy only while the token's ask is at least this."),
    _row("wk_max_price", "Whiskas bot", "Token to price", "float", 0.01, 0.99, 0.01, "$",
         "Buy only while the token's ask is at most this; a limit never goes over it."),
    _row("wk_from_s", "Whiskas bot", "From", "float", 0, 300, 1, "s",
         "Seconds after the open the bot may start buying."),
    _row("wk_until_s", "Whiskas bot", "Until", "float", 0, 300, 1, "s",
         "Seconds after the open new buys stop (held to the end)."),
    _row("wk_gap_low_ms", "Whiskas bot", "Next rising buy of a token after (cheap)", "float", 0, 60000, 50, "ms",
         "The least time between two rising buys of the same token while its ask is under 'Cheap under' (1 s): a cheap token is bought a bit slower."),
    _row("wk_gap_low_px", "Whiskas bot", "Cheap under", "float", 0.01, 0.99, 0.01, "$",
         "The ask under which a token counts as cheap (0.20)."),
    _row("wk_cooldown_ms", "Whiskas bot", "Next rising buy of a token after (middle)", "float", 0, 60000, 50, "ms",
         "The least time between two rising buys of the same token at middle prices (0.5 s)."),
    _row("wk_gap_high_ms", "Whiskas bot", "Next rising buy of a token after (dear)", "float", 0, 60000, 50, "ms",
         "The least time between two rising buys of the same token while its ask is at 'Dear from' or over (0.25 s): a dear token is bought quickly."),
    _row("wk_gap_high_px", "Whiskas bot", "Dear from", "float", 0.01, 0.99, 0.01, "$",
         "The ask from which a token counts as dear (0.80)."),
    _row("wk_bal_delay_s", "Whiskas bot", "Balance after", "float", 0, 60, 0.5, "s",
         "Once the rising token has had no rising fill for this long (its buys are over), the falling token is bought, sized on the live prices (1 s)."),
    _row("wk_bal_k", "Whiskas bot", "Balance strength", "float", 0, 1, 0.0005, "",
         "How much the falling token is bought: n = gap in shares - (its cost over its live price) / (strength x p_rising x p_falling). Higher = more of it (lower loss if it wins, lower profit if the rising one wins); it buys more while the outcome is open and less once the rising token is likely to win. 0.001 replayed best; 0 = no balance."),
    _row("wk_bal_keep_usd", "Whiskas bot", "Balance: keep 'if the rising token wins' at least", "float", -1000, 10000, 1, "$",
         "The balance never buys so much that 'if the rising token wins' drops under this."),
    _row("wk_profit_at", "Whiskas bot", "Must not lose from price", "float", 0, 0.99, 0.01, "$",
         "A token reaching this price whose 'if it wins' is negative is bought up to break even. 0 = off."),
    _row("wk_profit_max", "Whiskas bot", "That top-up at most", "float", 0, 1000, 1, "shares",
         "The most shares that top-up buys (once per token per window)."),
    _row("wk_window_usd", "Whiskas bot", "Spend per window max", "float", 0, 100000, 10, "$",
         "No buy takes the window's spend (fees included) over this. 0 = no cap (the cash still limits)."),
    _row("wk_max_book_age_ms", "Whiskas bot", "Book older than", "float", 100, 10000, 100, "ms",
         "No order on a book older than this."),
    _row("wk_max_inflight", "Whiskas bot", "FAK tries on their way at once", "float", 1, 10, 1, "",
         "Up to this many tries of one token may be out at once (each only for shares no try in flight covers), so fills come at several prices in a fast move."),
    _row("wk_ladder_levels", "Whiskas bot", "Bid ladder levels", "float", 0, 10, 1, "",
         "Resting bids on each token at the best bid and every 'Ladder step' under it, so a fast swing fills bids at several prices. Moved when the best bid is a step or more away. 0 = off. (Replayed: -2.8c a share, the bids fill as prices fall through them.)"),
    _row("wk_ladder_step", "Whiskas bot", "Ladder step", "float", 0.01, 0.2, 0.01, "$",
         "The gap between ladder bids (0.03)."),
    _row("wk_ladder_shares", "Whiskas bot", "Ladder bid shares", "float", 5, 1000, 1, "shares",
         "Shares per ladder bid (20)."),
    _row("wk_pair_on", "Whiskas bot", "Pair mode", "bool", 0, 1, 1, "",
         "On: x-MoneyForWhiskas's pair buying replaces rising buys, balancing and the profit top-up. After a price move of 'Pair: move of' within 'Pair: move within': with no shares yet, buy 'Rising buy shares' of the token that fell; after that only the token with fewer shares, and only while its ask + fee + the other token's average price <= 1 - 'Pair: keep under $1 by'. One buy at a time."),
    _row("wk_pair_move", "Whiskas bot", "Pair: move of", "float", 0.01, 0.5, 0.01, "$",
         "The Up/Down price move that triggers a pair buy (0.03)."),
    _row("wk_pair_ms", "Whiskas bot", "Pair: move within", "float", 100, 10000, 100, "ms",
         "The move is measured over this much time (1000 ms)."),
    _row("wk_pair_margin", "Whiskas bot", "Pair: keep under $1 by", "float", 0, 0.5, 0.01, "$",
         "The token with fewer shares is bought only while its ask + fee + the other token's average price <= 1 - this (0.02: each new pair at most $0.98)."),
    _row("wk_pair_cd_ms", "Whiskas bot", "Pair: least time between buys", "float", 0, 60000, 500, "ms",
         "After a pair buy starts, the next waits at least this long (3000 ms)."),
    _row("wk_pair_free", "Whiskas bot", "Pair: every move (no $1 limit)", "bool", 0, 1, 1, "",
         "On: every move buys, like the trader: the token whose buy leaves the worse of 'if Up wins' / 'if Down wins' highest after the order (ties: the token that fell), at any pair price. Off: the token with fewer shares, only under the $1 limit."),
    _row("wk_rise_pair_sum", "Whiskas bot", "Rising buy maker pair under", "float", 0, 1, 0.01, "$",
         "After a rising buy fills, rest a maker bid for the other token at a price that keeps their token prices strictly under this sum (0 = off)."),
    _row("v2_enabled", "Strategy V2", 'Strategy V2 on', "bool", 0, 1, 1, '',
         'On: Strategy V2 (risk-reduced, paper) trades this bot. Turn the other strategies off.'),
    _row("v2_lookback_s", "Strategy V2", 'A: local-low lookback', "float", 2, 300, 1, 's',
         "UP's low over this many seconds is the local low for the entry."),
    _row("v2_entry_rise", "Strategy V2", 'A: entry on a rise of', "float", 0.01, 0.5, 0.01, '$',
         "UP's mid this far over its local low triggers the entry (0.05)."),
    _row("v2_entry_shares", "Strategy V2", 'A: entry shares', "float", 5, 500, 1, 'shares',
         'UP shares the entry buys (20). Once per market.'),
    _row("v2_add_step", "Strategy V2", 'B/D4: add-on step', "float", 0.01, 0.5, 0.01, '$',
         "An add-on needs the token's mid this far over its latest actual fill price (0.05)."),
    _row("v2_add_shares", "Strategy V2", 'B: add-on shares', "float", 5, 500, 1, 'shares',
         'UP shares per add-on (5).'),
    _row("v2_add_max", "Strategy V2", 'B/D4: add-ons per market', "float", 0, 20, 1, '',
         'Momentum add-ons per market, UP and ladder together (2).'),
    _row("v2_add_gap_s", "Strategy V2", 'B/D4: wait after a fill', "float", 0, 120, 1, 's',
         "An add-on waits at least this long after that token's latest fill (6 s)."),
    _row("v2_stop_drop", "Strategy V2", 'B: stop adding after a drop of', "float", 0.01, 0.5, 0.01, '$',
         "UP's mid this far under its latest fill stops UP add-ons for the market (0.05)."),
    _row("v2_pair_max", "Strategy V2", 'C/D2: pair cost under', "float", 0.5, 1.2, 0.01, '$',
         "DOWN is bought for unmatched UP only while UP's actual cost a share + DOWN's executable price + fees + slippage is under this (0.95)."),
    _row("v2_bal_shares", "Strategy V2", 'C: balance shares', "float", 5, 500, 1, 'shares',
         'DOWN shares per balancing buy, never more than the UP not matched yet (5).'),
    _row("v2_bal_gap_s", "Strategy V2", 'C: wait between balances', "float", 0, 60, 0.5, 's',
         'The least time between two balancing buys (2 s).'),
    _row("v2_slip", "Strategy V2", 'Slippage allowance', "float", 0, 0.1, 0.001, '$',
         'Added to the pair cost a share as estimated slippage (0.005).'),
    _row("v2_max_slip", "Strategy V2", 'Most over the best ask', "float", 0, 0.2, 0.01, '$',
         'An order may go through the ask depth at most this far over the best ask; a thinner book is skipped (0.02).'),
    _row("v2_limit_over", "Strategy V2", "Limit over the best ask", "float", 0, 0.2, 0.01, "$",
         "Every order's limit is at least this over the best ask, so losing the best ask to a faster bot fills a level up instead of nothing (0.01). Balancing (C/D2) limits stay where the pair cost is under its limit."),
    _row("v2_max_spread", "Strategy V2", 'A: widest UP spread', "float", 0.01, 0.2, 0.01, '$',
         "No entry signal while UP's spread is wider than this (a quote not to trust)."),
    _row("v2_d1_px", "Strategy V2", 'D1: DOWN reaches', "float", 0.01, 0.99, 0.01, '$',
         'D1 buys DOWN once it holds this mid (0.32)...'),
    _row("v2_d1_below", "Strategy V2", 'D1: after trading under', "float", 0.01, 0.99, 0.01, '$',
         '...after it traded under this in the market (0.30)...'),
    _row("v2_d1_hold_s", "Strategy V2", 'D1: held for', "float", 0, 60, 1, 's',
         '...for this long (5 s).'),
    _row("v2_d1_shares", "Strategy V2", 'D1: shares', "float", 5, 500, 1, 'shares',
         'DOWN shares D1 buys, once per market (5).'),
    _row("v2_d2_px", "Strategy V2", 'D2: DOWN reaches', "float", 0.01, 0.99, 0.01, '$',
         "At this DOWN mid, while UP > DOWN, at most 'D2: shares' DOWN under the pair condition (0.42)."),
    _row("v2_d2_shares", "Strategy V2", 'D2: at most shares', "float", 5, 500, 1, 'shares',
         'Never the whole UP-DOWN gap (5).'),
    _row("v2_d3_px", "Strategy V2", 'D3: note at', "float", 0.01, 0.99, 0.01, '$',
         'DOWN at this mid: no automatic buy, a note only (0.45).'),
    _row("v2_d4_up", "Strategy V2", 'D4: ladder add-on buys UP', "bool", 0, 1, 1, '',
         'Off: the ladder add-on buys DOWN (default); on: UP.'),
    _row("v2_d4_shares", "Strategy V2", 'D4: shares', "float", 5, 500, 1, 'shares',
         'Shares per ladder add-on (5).'),
    _row("v2_e_low", "Strategy V2", "E: low token under", "float", 0.01, 0.99, 0.01, "$",
         "E arms for a token once its mid trades under this in the market (0.30)."),
    _row("v2_e1_px", "Strategy V2", "E1: rises to", "float", 0.01, 0.99, 0.01, "$",
         "Then at this mid, buy 'E1: shares' of it (0.40)."),
    _row("v2_e1_shares", "Strategy V2", "E1: shares", "float", 0, 500, 1, "shares", "Shares at E1, once per token per market (10). 0 = E off."),
    _row("v2_e2_px", "Strategy V2", "E2: rises to", "float", 0.01, 0.99, 0.01, "$",
         "After E1 filled, at this mid buy 'E2: shares' more (0.50)."),
    _row("v2_e2_shares", "Strategy V2", "E2: shares", "float", 0, 500, 1, "shares", "Shares at E2, once per token per market (10)."),
    _row("v2_e_bal_shares", "Strategy V2", "E3: balance shares", "float", 5, 500, 1, "shares",
         "After E2 filled, this many of the other token, while the pair costs under 'C/D2: pair cost under' (5)."),
    _row("v2_e_max_px", "Strategy V2", "E: no buy over ask", "float", 0.01, 0.99, 0.01, "$",
         "E1/E2 do not buy at an ask over this (0.85)."),
    _row("v3_enabled", "Strategy V3", 'Strategy V3 on', "bool", 0, 1, 1, '',
         'On: Strategy V3 (paper) trades this bot. Turn the other strategies off.'),
    _row("v3_lookback_s", "Strategy V3", 'Entry: low lookback', "float", 2, 300, 1, 's',
         "A token's low is its lowest mid since the open within this many seconds."),
    _row("v3_low_max", "Strategy V3", 'Entry: low-price token under', "float", 0.05, 0.95, 0.01, '$',
         'Only a token whose low was under this is the low-price (UP) token (0.50).'),
    _row("v3_entry_rise", "Strategy V3", 'Entry: rise of', "float", 0.01, 0.5, 0.01, '$',
         "The low-price token's mid this far over its low buys the entry (0.05)."),
    _row("v3_entry_shares", "Strategy V3", 'Entry: shares', "float", 5, 1000, 1, 'shares',
         'UP shares the entry buys (20). The token it buys is UP for the market.'),
    _row("v3_add_step", "Strategy V3", 'Momentum: step', "float", 0.01, 0.5, 0.01, '$',
         "UP's mid this far over the last UP momentum fill price buys an add-on (0.05)."),
    _row("v3_add_shares", "Strategy V3", 'Momentum: add-on shares', "float", 5, 1000, 1, 'shares',
         'UP shares per add-on (10).'),
    _row("v3_add_gap_s", "Strategy V3", 'Momentum: wait after a fill', "float", 0, 120, 1, 's',
         'An add-on waits this long after the last UP momentum fill (6 s).'),
    _row("v3_add_max", "Strategy V3", 'Momentum: add-ons at most', "float", 0, 100, 1, '',
         'UP add-ons per market (0 = no count limit; the risk limits still hold).'),
    _row("v3_rec_rise", "Strategy V3", 'Recovery: DOWN rise of', "float", 0.01, 0.9, 0.01, '$',
         "DOWN's mid this far over its low since the entry starts the recovery (0.10)."),
    _row("v3_rec_max_px", "Strategy V3", 'Recovery: no buy over ask', "float", 0.05, 0.99, 0.01, '$',
         'No recovery buy at a DOWN ask over this (0.80).'),
    _row("v3_rec_step", "Strategy V3", 'Recovery: step', "float", 0.01, 0.5, 0.01, '$',
         "After the recovery, each step DOWN rises over the last trigger price buys 'Recovery: step shares' (0.05)."),
    _row("v3_rec_step_shares", "Strategy V3", 'Recovery: step shares', "float", 5, 1000, 1, 'shares',
         'DOWN shares per recovery step (5).'),
    _row("v3_rec_max", "Strategy V3", 'Recovery: orders at most', "float", 1, 50, 1, '',
         'Recovery orders per market, the first and its steps together (4).'),
    _row("v3_bal_sum", "Strategy V3", 'Balance: avg UP + avg DOWN under', "float", 0.5, 1.2, 0.01, '$',
         'Balancing is considered while the two average costs (fees in) add up to under this (0.96).'),
    _row("v3_bal_shares", "Strategy V3", 'Balance: shares', "float", 5, 1000, 1, 'shares',
         'Shares per balancing buy, UP or DOWN, whichever leaves the worse outcome best (10).'),
    _row("v3_bal_pair_max", "Strategy V3", 'Balance: new pairs under', "float", 0.5, 1.2, 0.01, '$',
         "A balancing buy's new pairs (its price + fee + the other token's average) must cost under this (1.00)."),
    _row("v3_bal_gap_s", "Strategy V3", 'Balance: wait between', "float", 0, 120, 1, 's',
         'The least time between two balancing buys (3 s).'),
    _row("v3_lvl_a", "Strategy V3", 'Level A: DOWN at', "float", 0.01, 0.99, 0.01, '$',
         "DOWN crossing up to this after trading under 'Level A: after under' buys 'Level A: shares' (0.32)."),
    _row("v3_lvl_a_below", "Strategy V3", 'Level A: after under', "float", 0.01, 0.99, 0.01, '$',
         '(0.30)'),
    _row("v3_lvl_a_shares", "Strategy V3", 'Level A: shares', "float", 5, 1000, 1, 'shares',
         '(10)'),
    _row("v3_lvl_b", "Strategy V3", 'Level B: DOWN at', "float", 0.01, 0.99, 0.01, '$',
         "DOWN crossing up to this buys max(0, UP - DOWN) DOWN, at most 'Max order' (0.42)."),
    _row("v3_lvl_c", "Strategy V3", 'Level C: DOWN at', "float", 0.01, 0.99, 0.01, '$',
         "DOWN crossing up to this buys 'Level C: shares' (0.45)."),
    _row("v3_lvl_c_shares", "Strategy V3", 'Level C: shares', "float", 5, 1000, 1, 'shares',
         '(20)'),
    _row("v3_c_mom_up", "Strategy V3", 'Level C momentum buys UP', "bool", 0, 1, 1, '',
         "After level C: off = DOWN's momentum (default), on = UP's."),
    _row("v3_c_step", "Strategy V3", 'Level C momentum: step', "float", 0.01, 0.5, 0.01, '$',
         "The momentum token this far over its last qualifying fill buys 'Level C momentum: shares' (0.05)."),
    _row("v3_c_mom_shares", "Strategy V3", 'Level C momentum: shares', "float", 5, 1000, 1, 'shares',
         '(10)'),
    _row("v3_rearm_drop", "Strategy V3", 'Levels: re-arm after a drop of', "float", 0, 0.5, 0.01, '$',
         'Above 0: a done level fires again after DOWN fell this far under it (0 = once per market).'),
    _row("v3_max_chase", "Strategy V3", 'No chasing past', "float", 0, 0.5, 0.01, '$',
         "A trigger's order waits while the ask is more than this over its trigger price (0.03)."),
    _row("v3_min_benefit", "Strategy V3", 'Min net benefit', "float", 0, 100, 0.01, '$',
         'Balancing and recovery orders must improve the outcome they fix by at least this after fees ($0.05).'),
    _row("v3_max_spend", "Strategy V3", 'Max spend per market', "float", 1, 100000, 1, '$',
         'Filled + fees + on its way (at $1 a share) + the new order ($60).'),
    _row("v3_max_up", "Strategy V3", 'Max UP shares', "float", 5, 100000, 1, 'shares',
         '(80)'),
    _row("v3_max_down", "Strategy V3", 'Max DOWN shares', "float", 5, 100000, 1, 'shares',
         '(80)'),
    _row("v3_max_order", "Strategy V3", 'Max order', "float", 5, 100000, 1, 'shares',
         'Shares per order at most (40).'),
    _row("v3_max_outcome_loss", "Strategy V3", 'Max loss either outcome', "float", 1, 100000, 1, '$',
         "No order that leaves 'if Up wins' or 'if Down wins' under -this, unless it improves it ($30)."),
    _row("v3_max_imbalance", "Strategy V3", 'Max imbalance', "float", 0, 100000, 1, 'shares',
         '|UP - DOWN| after an order at most this, unless it reduces it (40).'),
    _row("v3_max_slip", "Strategy V3", 'Max slippage', "float", 0, 0.2, 0.01, '$',
         'An order goes through the ask depth at most this far over the best ask; a thinner book is skipped (0.02).'),
    _row("v3_limit_over", "Strategy V3", 'Limit over the best ask', "float", 0, 0.2, 0.01, '$',
         "Each order's limit at least this over the best ask (0.01)."),
    _row("v3_max_spread", "Strategy V3", 'Max spread', "float", 0.01, 0.2, 0.01, '$',
         'No order on a token whose spread is wider (0.03).'),
    _row("v3_max_px", "Strategy V3", "No buy over", "float", 0.05, 0.99, 0.01, "$",
         "No order buys a token whose ask is over this, and no limit goes over it (0.85)."),
    _row("v3_cutoff_s", "Strategy V3", 'No orders in the last', "float", 0, 299, 1, 's',
         '(30 s)'),
    _row("v3_from_s", "Strategy V3", 'Orders from', "float", 0, 299, 1, 's',
         'Seconds into the market (1).'),
    _row("v3_max_book_age_ms", "Strategy V3", 'Freshest book needed', "float", 100, 10000, 100, 'ms',
         '(1500 ms)'),
    _row("v3_resend_ms", "Strategy V3", 'Least time between orders', "float", 0, 60000, 100, 'ms',
         'Wait this long after any order before the next (0: an order that got nothing goes again at once; only one order is ever on its way).'),
    _row("v4_enabled", "Strategy V4", 'Strategy V4 on', "bool", 0, 1, 1, '',
         'On: Strategy V4 (P/L-driven) trades this bot. Turn the other strategies off.'),
    _row("v4_live_ok", "Strategy V4", 'Allow live orders', "bool", 0, 1, 1, '',
         'Off: V4 places no orders in a live (real-money) bot; paper only.'),
    _row("v4_min_px", "Strategy V4", 'Buy price from', "float", 0.01, 0.99, 0.01, '$',
         'No buy under this executable price (0.10).'),
    _row("v4_max_px", "Strategy V4", 'Buy price up to', "float", 0.01, 0.99, 0.01, '$',
         'No buy over this executable price, the limit included (0.85).'),
    _row("v4_mid_lo", "Strategy V4", 'Middle mode from', "float", 0.01, 0.99, 0.01, '$',
         "The selected token's price from here to 'Middle mode to' uses the middle target (0.40)."),
    _row("v4_mid_hi", "Strategy V4", 'Middle mode to', "float", 0.01, 0.99, 0.01, '$',
         'Over this the high target (0.60).'),
    _row("v4_mid_win", "Strategy V4", 'Middle target: win +', "float", 0, 100000, 0.1, '$',
         "Middle mode aims the selected token's outcome at +this..."),
    _row("v4_mid_lose", "Strategy V4", 'Middle target: other -', "float", 0, 100000, 0.1, '$',
         '...and the other outcome at -this.'),
    _row("v4_high_win", "Strategy V4", 'High target: win +', "float", 0, 100000, 0.5, '$',
         "High mode: the selected (dear) token's outcome at +this..."),
    _row("v4_high_lose", "Strategy V4", 'High target: other -', "float", 0, 100000, 0.5, '$',
         '...and the other at -this.'),
    _row("v4_low_win", "Strategy V4", 'Low momentum target: win +', "float", 0, 100000, 0.1, '$',
         "A rising token under 'Middle mode from': its outcome at +this..."),
    _row("v4_low_lose", "Strategy V4", 'Low momentum target: other -', "float", 0, 100000, 0.1, '$',
         '...the other at -this.'),
    _row("v4_entry_gap_s", "Strategy V4", 'Entry every', "float", 0, 600, 0.5, 's',
         'The dear-token entry (and the both-negative buy) at most this often (6 s).'),
    _row("v4_slow_lookback_s", "Strategy V4", 'Slow market: over', "float", 0.5, 60, 0.5, 's',
         "The entry needs Up's mid to have moved less than 'Slow market: under' over this (3 s)."),
    _row("v4_slow_th", "Strategy V4", 'Slow market: under', "float", 0.001, 0.5, 0.005, '$',
         '(0.02)'),
    _row("v4_mom_gap_s", "Strategy V4", 'Momentum every', "float", 0, 600, 0.5, 's',
         'A rising cheap token is bought at most this often (5 s).'),
    _row("v4_fast_lookback_s", "Strategy V4", 'Fast rise: over', "float", 0.25, 60, 0.25, 's',
         "The cheap token's rise is measured over this (2 s)..."),
    _row("v4_fast_th", "Strategy V4", 'Fast rise: at least', "float", 0.005, 0.5, 0.005, '$',
         '...and must be at least this (0.04).'),
    _row("v4_bal_delay_s", "Strategy V4", 'Balance after', "float", 0, 60, 0.5, 's',
         "The other token's balance is judged this long after a qualifying fill (3 s)."),
    _row("v4_bal_win", "Strategy V4", "Balance target: leading +", "float", 0, 100000, 0.5, "$",
         "A balance aims the leading outcome at +this ($5)..."),
    _row("v4_bal_lose", "Strategy V4", "Balance target: other -", "float", 0, 100000, 0.5, "$",
         "...and the losing one at -this ($20): it buys the losing outcome's token, the size landing nearest."),
    _row("v4_bal_tol", "Strategy V4", "Balance when the loss passes by", "float", 0, 1000, 0.5, "$",
         "The losing outcome under -(target + this) balances at once ($3: under -$23)."),
    _row("v4_bal_gap_s", "Strategy V4", "Balance at most every", "float", 0, 600, 0.5, "s", "(3 s)"),
    _row("v4_cap_new_loss", "Strategy V4", "Other buys keep the loss within the balance target", "bool", 0, 1, 1, "",
         "On: entry, momentum and both-negative orders never push the losing outcome past -(target + tolerance), so a balance does not have to undo them."),
    _row("v4_bal_reserve", "Strategy V4", "Budget kept for balancing", "float", 0, 100000, 1, "$",
         "Entry, momentum and both-negative orders stop at 'Max spend per market' minus this, so a balance can still buy ($25)."),
    _row("v4_early_s", "Strategy V4", "Early: first", "float", 0, 300, 5, "s",
         "In the first this many seconds the balance mirrors the outcomes: one +X, the other -X (their sum near 0), e.g. +7.22 / -7.22 (120 s); after it, the +5/-20 balance."),
    _row("v4_early_tol", "Strategy V4", "Early: balance when the outcomes' sum is off by", "float", 0, 1000, 0.5, "$",
         "Early, a balance goes while 'if Up wins' + 'if Down wins' is more than this away from 0 ($3)."),
    _row("v4_early_skip_px", "Strategy V4", "Early: no balancing over", "float", 0.01, 0.99, 0.01, "$",
         "Early, while the dearer token is over this, no balancing (0.73)."),
    _row("v4_early_pair", "Strategy V4", "Early: balance only under pair", "float", 0.5, 1.2, 0.01, "$",
         "Early, the balancing token is bought only while its price + the entry token's average price is under this (0.97); an entry is not followed by a balance at once."),
    _row("v5_enabled", "Strategy V5", 'Strategy V5 on', "bool", 0, 1, 1, '',
         'On: Strategy V5 (momentum + adaptive hedging, paper) trades this bot. Turn the other strategies off.'),
    _row("v5_live_ok", "Strategy V5", 'Allow live orders', "bool", 0, 1, 1, '',
         'Off: V5 places no orders in a live (real-money) bot.'),
    _row("v5_entry_cutoff_s", "Strategy V5", 'No new momentum after', "float", 0, 300, 1, 's',
         'Seconds into the market (285).'),
    _row("v5_bal_min_delay_s", "Strategy V5", 'Balance from', "float", 0, 60, 0.5, 's',
         "After the momentum batch's last fill (1 s)..."),
    _row("v5_bal_max_delay_s", "Strategy V5", 'Balance until', "float", 0, 60, 0.5, 's',
         '...until this long after it; then the cycle closes (5 s).'),
    _row("v5_cancel_all_s", "Strategy V5", 'Cancel all buys at', "float", 0, 300, 1, 's',
         'Seconds into the market (290); nothing new after it.'),
    _row("v5_rise", "Strategy V5", 'Momentum: ask rise of', "float", 0.01, 0.5, 0.01, '$',
         "An outcome's ask this far over its reference (its lowest ask since its last signal) (0.05)."),
    _row("v5_cooldown_s", "Strategy V5", 'Momentum: cooldown', "float", 0, 120, 0.5, 's',
         'Per outcome, after its last momentum fill (6 s).'),
    _row("v5_min_px", "Strategy V5", 'Buy price from', "float", 0.01, 0.99, 0.01, '$',
         '(0.08)'),
    _row("v5_max_px", "Strategy V5", 'Buy price up to', "float", 0.01, 0.99, 0.01, '$',
         '(0.89), the limit included.'),
    _row("v5_min_order", "Strategy V5", 'Order size from', "float", 5, 100, 1, 'shares',
         'Every order 5-12 whole shares.'),
    _row("v5_max_order", "Strategy V5", 'Order size up to', "float", 5, 100, 1, 'shares',
         '(12)'),
    _row("v5_max_pair", "Strategy V5", 'Fee-adjusted pair under', "float", 0.5, 1.2, 0.01, '$',
         'Balance only while momentum VWAP + fee + opposite VWAP + fee (a share each) is under this (0.97).'),
    _row("v5_max_spend", "Strategy V5", 'Max spend per market', "float", 1, 100000, 1, '$',
         'Filled + fees + resting + on its way + the order ($50).'),
    _row("v5_max_worst_loss", "Strategy V5", 'Max worst-case loss', "float", 0.5, 100000, 0.5, '$',
         'No order leaving the worse outcome under -this; at it, the kill switch ($5).'),
    _row("v5_max_shares", "Strategy V5", 'Max shares per outcome', "float", 5, 100000, 1, 'shares',
         '(60)'),
    _row("v5_max_imbalance", "Strategy V5", 'Max |Up - Down|', "float", 0, 100000, 1, 'shares',
         '(12)'),
    _row("v5_max_unmatched_cost", "Strategy V5", 'Max unmatched inventory', "float", 0, 100000, 1, '$',
         "FIFO-unmatched shares' cost at most this ($10)."),
    _row("v5_max_spread", "Strategy V5", 'Max spread', "float", 0.01, 0.2, 0.01, '$',
         '(0.03)'),
    _row("v5_edge_gate", "Strategy V5", 'Edge gate (model)', "bool", 0, 1, 1, '',
         "On: a momentum buy also needs the bot's fair value - all-in cost >= 'Min edge'. Off by default: the fair value is not a validated win probability."),
    _row("v5_min_edge", "Strategy V5", 'Min edge', "float", 0, 0.5, 0.005, '$',
         '(0.02)'),
    _row("v5_max_slip", "Strategy V5", 'Max slippage', "float", 0, 0.2, 0.01, '$',
         'Through the ask depth at most this over the best ask (0.02).'),
    _row("v5_limit_over", "Strategy V5", 'Limit over the fill level', "float", 0, 0.2, 0.01, '$',
         'A capped limit this over the depth level that fills the size (0.01), never over the caps.'),
    _row("v5_max_book_age_ms", "Strategy V5", 'Freshest book needed', "float", 100, 10000, 100, 'ms',
         '(1500 ms)'),
    _row("v5_avg_pair_max", "Strategy V5", "Avg Up + avg Down under", "float", 0, 1.5, 0.01, "$",
         "After every order, the average Up cost + the average Down cost (fees in) must stay under this (0.97). 0 = off."),
    _row("v5_high_wins", "Strategy V5", "Leading token's outcome positive", "bool", 0, 1, 1, "",
         "On: after every order, 'if the leading (dearer) token wins' must be positive; the other outcome may be negative."),
    _row("copy_enabled", "First-fill copy", "Copy trader's ETH fills", "bool", 0, 1, 1, "",
         "On: copy each confirmed ETH 5-minute fill from the target wallet (paper only)."),
    _row("copy_poll_ms", "First-fill copy", "Poll fills every", "float", 50, 5000, 50, "ms",
         "How often to check for new trader fills (100 ms). The public activity feed can publish fills later."),
    _row("copy_max_price", "First-fill copy", "Maximum ask", "float", 0.01, 0.99, 0.01, "$",
         "Do not copy above this current ask price (0.99)."),
    _row("copy_max_spread", "First-fill copy", "Maximum spread", "float", 0.01, 0.5, 0.01, "$",
         "Wait for the outcome's spread to be at most this (0.05)."),
    _row("copy_max_slip", "First-fill copy", "Maximum slippage", "float", 0, 0.3, 0.01, "$",
         "Buy through ask depth no farther than this above the best ask (0.03)."),
    _row("copy_limit_over", "First-fill copy", "Limit over fill level", "float", 0, 0.2, 0.01, "$",
         "Place the FAK limit this far over the depth level needed to fill (0.01)."),
    _row("copy_max_book_age_ms", "First-fill copy", "Freshest book needed", "float", 100, 10000, 100, "ms",
         "Wait for a book no older than this (1500 ms)."),
    _row("copy_cutoff_s", "First-fill copy", "No orders in the last", "float", 0, 299, 1, "s",
         "Do not copy this close to the market close (10 s)."),
    _row("copy_max_fill_age_s", "First-fill copy", "Ignore fills older than", "float", 1, 60, 1, "s",
         "Skip the copy if the public activity feed surfaces the trader's fill later than this (10 s)."),
    _row("copy_max_attempts", "First-fill copy", "Maximum attempts", "float", 1, 30, 1, "orders",
         "Total 5-share FAK attempts, including the first try (10)."),
    _row("copy_retry_ms", "First-fill copy", "Retry after", "float", 100, 5000, 100, "ms",
         "Wait this long after an attempt before retrying an unfilled order (1000 ms)."),
    _row("v6_enabled", "Strategy V6", 'Strategy V6 on', "bool", 0, 1, 1, '',
         'On: Strategy V6 (run and hedge, paper) trades this bot. Turn the other strategies off.'),
    _row("v6_live_ok", "Strategy V6", 'Allow live orders', "bool", 0, 1, 1, '',
         'Off: V6 places no orders in a live (real-money) bot.'),
    _row("v6_rise", "Strategy V6", 'Start on a rise of', "float", 0.01, 0.5, 0.01, '$',
         "A token's mid this far over its low (since the open / the last hedge) starts a run (0.05)."),
    _row("v6_start_shares", "Strategy V6", 'Start shares', "float", 5, 1000, 1, 'shares',
         'The run\'s first buy (10).'),
    _row("v6_add1_shares", "Strategy V6", "Run: first add", "float", 5, 1000, 1, "shares", "The run's first add (8)."),
    _row("v6_add2_shares", "Strategy V6", "Run: second add", "float", 5, 1000, 1, "shares", "The run's second add (6)."),
    _row("v6_add_shares", "Strategy V6", "Run: later adds", "float", 5, 1000, 1, "shares",
         "Every add after the second, each 'Run: every' while the run token has not fallen (5)."),
    _row("v6_step_s", "Strategy V6", 'Run: every', "float", 0.5, 120, 0.5, 's',
         'The run is checked this long after each run fill; a hedge waits this long before the next run (6 s).'),
    _row("v6_fall_th", "Strategy V6", 'Run: fallen by', "float", 0, 0.5, 0.005, '$',
         "The run token's mid this far under its mid at the last run buy counts as fallen -> hedge (0.01)."),
    _row("v6_hedge_mult", "Strategy V6", 'Hedge: shares x', "float", 0.1, 5, 0.1, 'x',
         "The hedge buys the run's shares times this of the other token (1.0: as many)."),
    _row("v6_hedge_dear", "Strategy V6", "Hedge x when dearer", "float", 0.1, 5, 0.05, "x",
         "When the hedge token's ask is over the run's average fill price, the hedge buys the run's shares x this (1.2)."),
    _row("v6_hedge_cheap", "Strategy V6", "Hedge x when cheaper", "float", 0.1, 5, 0.05, "x",
         "When it is under the run's average fill price, x this (0.8)."),
    _row("v6_quick_pair", "Strategy V6", "Hedge at once under pair", "float", 0, 1.2, 0.01, "$",
         "During a run, the opposite token's ask + the run's average fill price under this: hedge at once, as a taker, capped under it (0.95). 0 = off."),
    _row("v6_rise_min_px", "Strategy V6", "Rising buys from", "float", 0, 0.99, 0.01, "$",
         "Start and run buys only while the token's ask is from this (0.30)."),
    _row("v6_start_0_60_max_px", "Strategy V6", "Start buy up to (0:00-1:00)", "float", 0, 0.99, 0.01, "$",
         "Maximum ask for a run's start during the first minute (0.45)."),
    _row("v6_start_60_120_max_px", "Strategy V6", "Start buy up to (1:00-2:00)", "float", 0, 0.99, 0.01, "$",
         "Maximum ask for a run's start during the second minute (0.60)."),
    _row("v6_start_120_180_max_px", "Strategy V6", "Start buy up to (2:00-3:00)", "float", 0, 0.99, 0.01, "$",
         "Maximum ask for a run's start during the third minute (0.70)."),
    _row("v6_start_180_240_max_px", "Strategy V6", "Start buy up to (3:00-4:00)", "float", 0, 0.99, 0.01, "$",
         "Maximum ask for a run's start during the fourth minute (0.75)."),
    _row("v6_start_240_300_max_px", "Strategy V6", "Start buy up to (4:00-5:00)", "float", 0, 0.99, 0.01, "$",
         "Maximum ask for a run's start during the final minute (0.85)."),
    _row("v6_add_max_px", "Strategy V6", "Run buys up to", "float", 0, 0.99, 0.01, "$",
         "A run's later buys up to this (0.85); hedges and favourite buys are not affected."),
    _row("v6_fav_px", "Strategy V6", "Favourite over", "float", 0, 0.99, 0.01, "$",
         "When the dearer token's price is over this and 'if it wins' is not positive, buy it (taker, at once) until it is (0.80). 0 = off."),
    _row("v6_fav_margin", "Strategy V6", "Favourite: profit at least", "float", 0, 1000, 0.1, "$",
         "The favourite buy lifts 'if it wins' over this ($0: just positive)."),
    _row("v6_fav_min", "Strategy V6", "Favourite: order from", "float", 5, 1000, 1, "shares", "(5)"),
    _row("v6_fav_max", "Strategy V6", "Favourite: order up to", "float", 5, 1000, 1, "shares",
         "Each favourite order 5-15 shares; it buys again until 'if it wins' is positive (15)."),
    _row("v6_fav_min_dist", "Strategy V6", "Favourite: BTC from the strike at least", "float", 0, 100000, 5, "$",
         "The favourite buy only while BTC is this far from the strike on the favourite's side ($50; above it for Up, below for Down)."),
    _row("v6_neg_fix", "Strategy V6", "Both negative: buy the cheaper token", "bool", 0, 1, 1, "",
         "On: when 'if Up wins' and 'if Down wins' are both negative, buy the cheaper token (taker, at once) until its outcome is positive."),
    _row("v6_next_any", "Strategy V6", 'After a hedge: either token', "bool", 0, 1, 1, '',
         'Off: only the hedge token may start the next run. On: whichever rises first.'),
    _row("v6_min_px", "Strategy V6", 'Buy price from', "float", 0.01, 0.99, 0.01, '$',
         '(0.05)'),
    _row("v6_max_px", "Strategy V6", 'Buy price up to', "float", 0.01, 0.99, 0.01, '$',
         '(0.95)'),
    _row("v6_max_spend", "Strategy V6", 'Max spend per market', "float", 1, 100000, 1, '$',
         '($100)'),
    _row("v6_max_order", "Strategy V6", 'Max order', "float", 5, 100000, 1, 'shares',
         'A hedge larger than this goes in more orders (100).'),
    _row("v6_max_spread", "Strategy V6", 'Max spread', "float", 0.01, 0.5, 0.01, '$',
         '(0.05)'),
    _row("v6_max_slip", "Strategy V6", 'Max slippage', "float", 0, 0.3, 0.01, '$',
         'Through the ask depth at most this over the best ask (0.03).'),
    _row("v6_limit_over", "Strategy V6", 'Limit over the fill level', "float", 0, 0.2, 0.01, '$',
         '(0.01)'),
    _row("v6_cutoff_s", "Strategy V6", 'No orders in the last', "float", 0, 299, 1, 's',
         '(10 s)'),
    _row("v6_from_s", "Strategy V6", 'Orders from', "float", 0, 299, 1, 's',
         '(1 s)'),
    _row("v6_max_book_age_ms", "Strategy V6", 'Freshest book needed', "float", 100, 10000, 100, 'ms',
         '(1500 ms)'),
    _row("v6_maker_s", "Strategy V6", "Maker first for", "float", 0, 30, 0.5, "s",
         "Each buy first rests a bid (no fee) this long; what it has not filled is then bought at the ask (1.5 s). 0 = taker only."),
    _row("v6_maker_improve", "Strategy V6", "Maker: ticks over the best bid", "float", 0, 5, 1, "ticks",
         "The bid sits this many ticks over the best bid, always under the ask (1)."),
    _row("v4_max_retries", "Strategy V4", "Retry a killed order", "float", 0, 20, 1, "times",
         "An order that got nothing goes again at once, re-priced and re-sized, up to this many times in a row (3); its signal's wait does not hold it back."),
    _row("v4_size1", "Strategy V4", 'Entry size 1', "float", 0, 1000, 1, 'shares',
         'A permitted entry / momentum / both-negative size (0 = unused).'),
    _row("v4_size2", "Strategy V4", 'Entry size 2', "float", 0, 1000, 1, 'shares',
         'A permitted entry / momentum / both-negative size (0 = unused).'),
    _row("v4_size3", "Strategy V4", 'Entry size 3', "float", 0, 1000, 1, 'shares',
         'A permitted entry / momentum / both-negative size (0 = unused).'),
    _row("v4_size4", "Strategy V4", 'Entry size 4', "float", 0, 1000, 1, 'shares',
         'A permitted entry / momentum / both-negative size (0 = unused).'),
    _row("v4_size5", "Strategy V4", 'Entry size 5', "float", 0, 1000, 1, 'shares',
         'A permitted entry / momentum / both-negative size (0 = unused).'),
    _row("v4_bal_size1", "Strategy V4", 'Balance size 1', "float", 0, 1000, 1, 'shares',
         'A permitted balance size (0 = unused).'),
    _row("v4_bal_size2", "Strategy V4", 'Balance size 2', "float", 0, 1000, 1, 'shares',
         'A permitted balance size (0 = unused).'),
    _row("v4_bal_size3", "Strategy V4", 'Balance size 3', "float", 0, 1000, 1, 'shares',
         'A permitted balance size (0 = unused).'),
    _row("v4_bal_size4", "Strategy V4", 'Balance size 4', "float", 0, 1000, 1, 'shares',
         'A permitted balance size (0 = unused).'),
    _row("v4_bal_size5", "Strategy V4", 'Balance size 5', "float", 0, 1000, 1, 'shares',
         'A permitted balance size (0 = unused).'),
    _row("v4_max_spend", "Strategy V4", 'Max spend per market', "float", 1, 100000, 1, '$',
         'Filled + fees + on its way (at $1 a share) + the order ($100).'),
    _row("v4_max_shares", "Strategy V4", 'Max shares per outcome', "float", 5, 100000, 1, 'shares',
         '(200)'),
    _row("v4_max_loss", "Strategy V4", 'Max projected loss', "float", 1, 100000, 1, '$',
         'No order leaving either outcome under -this unless it improves it ($60).'),
    _row("v4_max_consec", "Strategy V4", 'Max momentum / both-negative in a row', "float", 1, 100, 1, '',
         '(4)'),
    _row("v4_max_spread", "Strategy V4", 'Max spread', "float", 0.01, 0.2, 0.01, '$',
         '(0.03)'),
    _row("v4_max_slip", "Strategy V4", 'Max slippage', "float", 0, 0.2, 0.01, '$',
         'An order goes through the ask depth at most this over the best ask; thinner: no order (0.02).'),
    _row("v4_limit_over", "Strategy V4", 'Limit over the best ask', "float", 0, 0.2, 0.01, '$',
         '(0.01)'),
    _row("v4_min_improve", "Strategy V4", 'Min improvement', "float", 0, 1000, 0.05, '$',
         'An order must bring the P/L pair at least this much ($) nearer its target (0.50).'),
    _row("v4_reach_tol", "Strategy V4", 'Target reached within', "float", 0, 1000, 0.5, '$',
         'For the stats: the P/L pair within this of the target counts as reached ($3).'),
    _row("v4_cutoff_s", "Strategy V4", 'No orders in the last', "float", 0, 299, 1, 's',
         '(30 s)'),
    _row("v4_from_s", "Strategy V4", 'Orders from', "float", 0, 299, 1, 's',
         '(1 s)'),
    _row("v4_max_book_age_ms", "Strategy V4", 'Freshest book needed', "float", 100, 10000, 100, 'ms',
         '(1500 ms)'),
    _row("v2_budget_usd", "Strategy V2", 'Budget per market', "float", 1, 100000, 1, '$',
         'Filled cost + fees + orders on their way (at $1 a share) + the new order must stay within this ($30).'),
    _row("v2_max_net", "Strategy V2", 'Max net exposure', "float", 5, 10000, 1, 'shares',
         '|UP - DOWN| filled shares after an order may not pass this (30).'),
    _row("v2_max_loss", "Strategy V2", 'Max loss per market', "float", 0.5, 100000, 0.5, '$',
         "Marked-to-market loss (holdings at the executable bid depth) at this blocks the market's new buys and cancels its resting buys ($10)."),
    _row("v2_cutoff_s", "Strategy V2", 'No buys in the last', "float", 0, 299, 1, 's',
         'No new buys this close to the close (60 s).'),
    _row("v2_from_s", "Strategy V2", 'Buys from', "float", 0, 299, 1, 's',
         'No buys before this many seconds into the market.'),
    _row("v2_max_book_age_ms", "Strategy V2", 'Freshest book needed', "float", 100, 10000, 100, 'ms',
         'No order on a book older than this.'),
    _row("v2_resend_ms", "Strategy V2", 'Least time between orders', "float", 100, 60000, 100, 'ms',
         'Guards against duplicate orders after a delayed answer.'),
    _row("tri_btc_shares", "Triangle", "BTC move: buy shares", "float", 0, 500, 1, "shares",
         "When BTC moves at least 'BTC move of' within 'BTC move within', buy this many of the token it pushes up, at once (taker). The token's price follows BTC 250-500 ms later. 0 = off."),
    _row("tri_btc_bps", "Triangle", "BTC move of", "float", 0.1, 50, 0.1, "bps",
         "The move that triggers the buy (2 bps = 0.02%). Smaller fires more often but needs a faster reaction."),
    _row("tri_btc_ms", "Triangle", "BTC move within", "float", 50, 5000, 50, "ms",
         "The move is measured over this much time, up to the newest BTC price."),
    _row("tri_btc_min_price", "Triangle", "BTC move: token from price", "float", 0.01, 0.99, 0.01, "$",
         "Buy only while the token's ask is at least this."),
    _row("tri_btc_max_price", "Triangle", "BTC move: token to price", "float", 0.01, 0.99, 0.01, "$",
         "Buy only while the token's ask is at most this; the limit never goes over it."),
    _row("tri_btc_slippage", "Triangle", "BTC move: limit over the ask", "float", 0, 0.1, 0.01, "$",
         "The buy's limit: the ask + this."),
    _row("tri_btc_from_s", "Triangle", "BTC move: from", "float", 0, 300, 1, "s",
         "Seconds after the open the BTC-move buys may start."),
    _row("tri_btc_until_s", "Triangle", "BTC move: until", "float", 0, 300, 1, "s",
         "Seconds after the open the BTC-move buys stop."),
    _row("tri_btc_cooldown_ms", "Triangle", "BTC move: gap per token", "float", 0, 60000, 100, "ms",
         "After a BTC-move buy of a token, wait this long before the next one of the same token."),
    _row("tri_btc_after_open", "Triangle", "BTC move: only after the first buy", "bool", 0, 1, 1, "",
         "On: BTC-move buys start once the first buy has filled."),
    _row("tri_btc_max_buys", "Triangle", "BTC move: buys per window max", "float", 0, 100, 1, "",
         "At most this many BTC-move buys in a window. The window budget also applies."),
    _row("tri_cb_shares", "Triangle", "Comeback shares per level", "float", 0, 200, 1, "shares",
         "A token (either side) that fell under 'Comeback after a low of' and comes back: buy this many of it at each trigger (taker); each token its own count. 0 = off."),
    _row("tri_cb_low", "Triangle", "Comeback after a low of", "float", 0.01, 0.5, 0.01, "$",
         "The token's price must have been under this in the window (0.35)."),
    _row("tri_cb_first_trigger", "Triangle", "Comeback first trigger", "float", 0.3, 0.99, 0.01, "$",
         "Its ask reaching this sends the first buy (0.57); the next triggers are 'Comeback step' higher."),
    _row("tri_cb_step", "Triangle", "Comeback step", "float", 0.01, 0.3, 0.01, "$",
         "The gap between triggers (0.10: 0.57 / 0.67 / 0.77)."),
    _row("tri_cb_levels", "Triangle", "Comeback levels", "float", 1, 10, 1, "",
         "How many triggers (3)."),
    _row("tri_cb_limit_over", "Triangle", "Comeback limit over the trigger", "float", 0, 0.2, 0.01, "$",
         "Each buy's limit: the trigger + this (0.03: 0.60 / 0.70 / 0.80)."),
    _row("tri_cb_max_price", "Triangle", "Comeback retry up to", "float", 0.5, 0.99, 0.01, "$",
         "Shares a level did not fill are tried again at up to this (0.94: under 0.95)."),
    _row("tri_skip_after_loss", "Triangle", "Skip the next window after a loss over", "float", 0, 100, 0.5, "$",
         "A window that lost more than this (judged from its closing book, before the official result): the next window trades nothing. 0 = off."),
    _row("tri_pair_hedge_price", "Triangle", "Buy the pair token at once over", "float", 0, 0.99, 0.01, "$",
         "After the first buy, if the pair token's ask goes over this before the pair bid fills: buy it now as a taker ('Then buy shares'), without waiting for 'Pair bid time limit'. 0 = off."),
    _row("tri_pair_raise_s", "Triangle", "Raise the pair bid after", "float", 0, 300, 5, "s",
         "If the pair bid is not complete this long after the open filled: move it up to 'Raise the pair bid to' (still a maker bid, at most 1 tick under the ask). 0 = off."),
    _row("tri_pair_raise_price", "Triangle", "Raise the pair bid to", "float", 0.01, 0.99, 0.01, "$",
         "The pair bid's price after 'Raise the pair bid after' (0.50: open 0.50-0.62 + 0.50 pairs at 1.00-1.12, a small sure loss)."),
    _row("tri_pair_sum_max", "Triangle", "Pair sum up to", "float", 0, 1.0, 0.01, "$",
         "Above 'Pair sum': the sum starts there and rises +0.01 every 'Pair sum +0.01 every' while the bid waits, up to this (0 = fixed)."),
    _row("tri_pair_sum_step_s", "Triangle", "Pair sum +0.01 every", "float", 1, 300, 1, "s",
         "How fast the pair sum rises after the open filled."),
    _row("tri_pair_pull", "Triangle", "Pull the pair bid when the hedge is due", "bool", 0, 1, 1, "",
         "Cancel the pair bid once the other token's ask is over 'Recovery from price' (the hedge is due); then the recovery may run."),
    _row("tri_repeat", "Triangle", "Repeat after a pair completes", "bool", 0, 1, 1, "",
         "Once a cycle's pair bid has filled, start a new open buy + pair bid (until 'Repeat until')."),
    _row("tri_repeat_delay_ms", "Triangle", "Repeat delay", "int", 0, 60000, 100, "ms",
         "Wait this long after a completed pair before the next open buy."),
    _row("tri_repeat_until_s", "Triangle", "Repeat until", "float", 0, 300, 5, "s into the window",
         "Cycles after the first may open until this (240 = when the last minute starts). The first open keeps 'Open buy until'."),
    _row("tri_repeat_open_min_price", "Triangle", "Repeat open from price", "float", 0, 0.99, 0.01, "$",
         "Cycles after the first buy a token whose ask is at least this (both 0 = the first open's range)."),
    _row("tri_repeat_open_max_price", "Triangle", "Repeat open to price", "float", 0, 0.99, 0.01, "$",
         "...and at most this. Never above pair sum - 0.01 while the pair bid is on (it would leave no pair bid)."),
    _row("tri_open_maker", "Triangle", "First open as maker bid", "bool", 0, 1, 1, "",
         "The first open rests as a bid one tick over the best bid (never at the ask), following the book in its range. Off = taker buy."),
    _row("tri_open_hold_top", "Triangle", "Keep bidding the top when the leader runs over", "bool", 0, 1, 1, "",
         "First open as maker bid: when the leading token's price runs over 'Open buy to price', keep a bid at that price on it (until 'Open buy until') instead of giving up."),
    _row("tri_repeat_open_maker", "Triangle", "Repeat open as maker bid", "bool", 0, 1, 1, "",
         "Cycles after the first open with a resting bid (the leading token) instead of a taker buy."),
    _row("tri_sell_all_price", "Triangle", "Sell everything at", "float", 0, 0.999, 0.001, "$ bid",
         "Once a token we hold is bid over this, sell everything held (that token first, then the other side if it has a bid of 1 c or more). 0 = off."),
    _row("tri_cheap_sell_price", "Triangle", "Sell the cheap side above", "float", 0, 0.99, 0.01, "$ bid",
         "After 'Sell everything' or the profit exit has sold the leading token, the other side is held and sold only once its bid is over this (until the close). 0 = sell it at once, from 1 c."),
    _row("tri_recovery_enabled", "Triangle", "Recovery on", "bool", 0, 1, 1, "",
         "Buy the side that took the lead until its win pays the target."),
    _row("tri_target_profit", "Triangle", "Recovery target", "float", 0, 5, 0.05, "$",
         "The recovered side's win branch is sized to pay this (fees included)."),
    _row("tri_min_price_move", "Triangle", "Recovery after a drop of", "float", 0, 0.5, 0.01, "$",
         "Our side's bid must be this far under what we paid."),
    _row("tri_recovery_min_price", "Triangle", "Recovery from price", "float", 0.05, 0.99, 0.01, "$",
         "The leading token's ask must be above this."),
    _row("tri_recovery_max_price", "Triangle", "Recovery to price", "float", 0.05, 1.0, 0.01, "$",
         "Above this no recovery (a lock if locks are on)."),
    _row("tri_hedge_by_time", "Triangle", "Hedge by time left", "bool", 0, 1, 1, "",
         "The hedge's price range depends on the time left (measured on 1,701 windows: the least costly ranges); "
         "no hedge in the last minute. Replaces the first-hedge and recovery from/to prices while on."),
    _row("tri_hedge_t1_min", "Triangle", "Hedge 4:00-3:00 left from", "float", 0.5, 0.99, 0.01, "$", "Leader's ask above this..."),
    _row("tri_hedge_t1_max", "Triangle", "Hedge 4:00-3:00 left to", "float", 0.5, 0.99, 0.01, "$", "...and at most this."),
    _row("tri_hedge_t2_min", "Triangle", "Hedge 3:00-2:00 left from", "float", 0.5, 0.99, 0.01, "$", "Leader's ask above this..."),
    _row("tri_hedge_t2_max", "Triangle", "Hedge 3:00-2:00 left to", "float", 0.5, 0.99, 0.01, "$", "...and at most this."),
    _row("tri_hedge_t3_min", "Triangle", "Hedge 2:00-1:00 left from", "float", 0.5, 0.99, 0.01, "$", "Leader's ask above this..."),
    _row("tri_hedge_t3_max", "Triangle", "Hedge 2:00-1:00 left to", "float", 0.5, 0.99, 0.01, "$", "...and at most this."),
    _row("tri_first_rec_min_price", "Triangle", "First hedge from price", "float", 0, 0.99, 0.01, "$",
         "The window's first hedge fires only when the leading token's ask is above this (both 0 = 'Recovery from/to price')."),
    _row("tri_first_rec_max_price", "Triangle", "First hedge to price", "float", 0, 1.0, 0.01, "$",
         "...and at most this. Later hedges use 'Recovery from price' / 'Recovery to price'."),
    _row("tri_max_recoveries", "Triangle", "Recoveries per window", "int", 0, 10, 1, "times",
         "After this many the window is held."),
    _row("tri_cooldown_ms", "Triangle", "Recovery cooldown", "int", 0, 30000, 100, "ms",
         "Time between recovery / lock / late orders."),
    _row("tri_min_time_left_s", "Triangle", "No recovery under", "float", 0, 120, 1, "s left",
         "No recovery this close to the end."),
    _row("tri_max_slippage", "Triangle", "Recovery limit over ask", "float", 0, 0.1, 0.01, "$",
         "Recovery limit = ask + this; depth is counted up to it."),
    _row("tri_slippage_allowance", "Triangle", "Slippage allowance", "float", 0, 0.05, 0.001, "$ a share",
         "Added to the cost when sizing a recovery."),
    _row("tri_max_order_size", "Triangle", "Recovery order max", "float", 5, 500, 5, "shares",
         "Largest single recovery order."),
    _row("tri_max_total_position", "Triangle", "Shares per window max", "float", 10, 2000, 10, "shares",
         "Both sides together."),
    _row("tri_max_capital", "Triangle", "Recovery spend max", "float", 1, 1000, 5, "$",
         "Recovery USDC per window at most."),
    _row("tri_min_size", "Triangle", "Smallest order", "float", 5, 50, 1, "shares",
         "Recovery steps under this are skipped."),
    _row("tri_max_market_loss", "Triangle", "Branch loss limit", "float", 1, 500, 1, "$",
         "No recovery that would leave the other branch under minus this."),
    _row("tri_lock_enabled", "Triangle", "Lock on", "bool", 0, 1, 1, "",
         "Instead of a recovery: buy the side with fewer shares until both are equal."),
    _row("tri_lock_left_s", "Triangle", "Lock under", "float", 0, 300, 5, "s left",
         "Recovery turns into a lock this close to the end."),
    _row("tri_max_actions", "Triangle", "Actions before a lock", "int", 1, 100, 1, "orders",
         "Recovery orders per window before it locks."),
    _row("tri_profit_exit", "Triangle", "Profit exit at", "float", 0, 20, 0.5, "$",
         "After a recovery: sell everything once it is worth this over cost (0 = off)."),
    _row("tri_late_enabled", "Triangle", "Late buy on", "bool", 0, 1, 1, "",
         "10-20 s left: buy the side our fair value gives >= the late chance."),
    _row("tri_late_window_s", "Triangle", "Late buy from", "float", 1, 120, 1, "s left",
         ""),
    _row("tri_late_min_left_s", "Triangle", "Late buy until", "float", 0, 60, 1, "s left",
         ""),
    _row("tri_late_min_p", "Triangle", "Late buy chance", "float", 0.5, 1.0, 0.01, "",
         "Our fair value for that side at least this."),
    _row("tri_late_max_price", "Triangle", "Late buy max price", "float", 0.5, 0.99, 0.01, "$",
         ""),
    _row("tri_late_shares", "Triangle", "Late buy shares", "float", 5, 200, 1, "shares",
         ""),
    _row("tri_late_max_capital", "Triangle", "Late buy spend max", "float", 1, 500, 1, "$",
         "Per window."),
    _row("tri_late_max_orders", "Triangle", "Late buy orders", "int", 1, 10, 1, "orders",
         "Per window."),
    _row("tri_late_min_edge", "Triangle", "Late buy min edge", "float", -0.1, 0.2, 0.01, "$",
         "Fair - price - fee - allowance at least this."),
    _row("tri_last_enabled", "Triangle", "Last minute on", "bool", 0, 1, 1, "",
         "In the last minute buy the leading token every few hundred ms; nothing else trades then."),
    _row("tri_last_window_s", "Triangle", "Last minute from", "float", 5, 300, 5, "s left",
         "60 = from 4:00 into the window."),
    _row("tri_last_every_ms", "Triangle", "Last minute every", "int", 100, 10000, 100, "ms",
         "Time between the buys."),
    _row("tri_last_min_price", "Triangle", "Last minute min price", "float", 0, 0.99, 0.01, "$",
         "The leader's ask at least this (0 = any price)."),
    _row("tri_last_max_usd", "Triangle", "Last minute spend max", "float", 0, 10000, 5, "$",
         "Per window (0 = all free cash)."),
    _row("tri_last_slippage", "Triangle", "Last minute limit over ask", "float", 0, 0.1, 0.01, "$",
         "Each buy takes the depth up to ask + this."),
    _row("tri_last_shares", "Triangle", "Last minute shares", "float", 0, 500, 1, "shares",
         "Shares per buy (0 = as many as the book and the free cash allow)."),
    _row("tri_last_max_price", "Triangle", "Last minute max price", "float", 0.05, 1.0, 0.01, "$",
         "Buy only while the leader's ask is under this; the limit stays a tick under it (1 = no cap)."),
    _row("tri_last_min_dist_bps", "Triangle", "Last minute min distance", "float", 0, 50, 0.5, "bps",
         "Last-minute buys only while the coin is at least this far past the strike on the leader's side (0 = off). "
         "Replay of 1,725 windows: none -$247/day, 6 bps +$6/day (both halves positive, worst window -$4.86)."),
    _row("tri_last_comeback_low", "Triangle", "Last minute comeback low", "float", 0, 0.5, 0.01, "$",
         "A leader whose price was at or under this earlier in the window counts as a comeback (0 = off)."),
    _row("tri_last_comeback_min_price", "Triangle", "Last minute comeback from price", "float", 0, 0.99, 0.01, "$",
         "A comeback leader may be bought in the last minute from this price (up to 'Last minute max price'); others keep 'Last minute min price'."),
    _row("tri_max_quote_age_ms", "Triangle", "Book older than", "int", 200, 10000, 100, "ms",
         "No triangle order on a book older than this."),
]

SPEC = {r["key"]: r for r in CONTROLS}

# what a "live-on only" console shows (web --only-on): a group whose switch is off goes, and so do the
# rows that serve only switched-off features
GROUP_SWITCH = {"Pair ladder": "pl_enabled", "Maker bids": "maker_enabled", "Taker clips": "taker_enabled", "Pair completion": "pair_enabled",
                "Favourite bid": "fav_enabled", "Last-minute favourite": "late_enabled", "Scalp cycles": "scalp_enabled",
                "Triangle": "tri_enabled", "Whiskas bot": "wk_enabled",
                "Strategy V2": "v2_enabled", "Strategy V3": "v3_enabled",
                "Strategy V4": "v4_enabled", "Strategy V5": "v5_enabled",
                "Strategy V6": "v6_enabled", "First-fill copy": "copy_enabled"}
NEEDS_ALL = {**{k: ["tri_pair_bid"] for k in ("tri_pair_sum", "tri_pair_pull", "tri_repeat", "tri_pair_sum_max",
                                               "tri_pair_sum_step_s", "tri_pair_timeout_s", "tri_pair_timeout_profit",
                                               "tri_pair_timeout_shares", "tri_pair_raise_s", "tri_pair_raise_price",
                                               "tri_pair_shares", "tri_pair_hedge_price", "tri_add_shares",
                                               "tri_add_first_price", "tri_add_step", "tri_add_levels", "tri_add_max_price")},
             "tri_repeat_delay_ms": ["tri_pair_bid", "tri_repeat"],
             "tri_repeat_until_s": ["tri_pair_bid", "tri_repeat"],
             "tri_repeat_open_min_price": ["tri_pair_bid", "tri_repeat"],
             "tri_repeat_open_maker": ["tri_pair_bid", "tri_repeat"],
             "tri_open_hold_top": ["tri_open_maker"],
             "tri_repeat_open_max_price": ["tri_pair_bid", "tri_repeat"],
             **{k: ["tri_recovery_enabled"] for k in ("tri_target_profit", "tri_min_price_move", "tri_recovery_min_price",
                                                       "tri_recovery_max_price", "tri_max_recoveries", "tri_cooldown_ms",
                                                       "tri_min_time_left_s", "tri_max_slippage", "tri_slippage_allowance",
                                                       "tri_max_order_size", "tri_max_capital", "tri_min_size",
                                                       "tri_max_market_loss", "tri_lock_enabled", "tri_profit_exit",
                                                       "tri_first_rec_min_price", "tri_first_rec_max_price",
                                                       "tri_hedge_by_time")},
             **{k: ["tri_recovery_enabled", "tri_hedge_by_time"] for k in (
                 "tri_hedge_t1_min", "tri_hedge_t1_max", "tri_hedge_t2_min", "tri_hedge_t2_max",
                 "tri_hedge_t3_min", "tri_hedge_t3_max")},
             **{k: ["tri_recovery_enabled", "tri_lock_enabled"] for k in ("tri_lock_left_s", "tri_max_actions")},
             **{k: ["tri_late_enabled"] for k in ("tri_late_window_s", "tri_late_min_left_s", "tri_late_min_p",
                                                   "tri_late_max_price", "tri_late_shares", "tri_late_max_capital",
                                                   "tri_late_max_orders", "tri_late_min_edge")},
             **{k: ["tri_last_enabled"] for k in ("tri_last_window_s", "tri_last_every_ms", "tri_last_min_price",
                                                   "tri_last_max_usd", "tri_last_slippage", "tri_last_shares",
                                                   "tri_last_max_price", "tri_last_comeback_low",
                                                   "tri_last_min_dist_bps",
                                                   "tri_last_comeback_min_price")}}
_OLD = ["maker_enabled", "taker_enabled", "pair_enabled", "fav_enabled", "late_enabled", "scalp_enabled"]
NEEDS_ANY = {"anchor_s": _OLD + ["tri_late_enabled"], "max_anchor_spread": _OLD + ["tri_late_enabled"],
             "max_window_shares": _OLD, "max_imbalance_shares": ["maker_enabled"], "skew_per_10sh": ["maker_enabled"],
             "alternate_sides": ["maker_enabled", "taker_enabled"], "min_price": ["maker_enabled", "taker_enabled"],
             "max_price": ["maker_enabled", "taker_enabled"], "max_window_usdc": _OLD + ["tri_pair_bid"],
             "start_after_s": _OLD, "maker_start_s": ["maker_enabled"], "maker_stop_left_s": ["maker_enabled"],
             "taker_stop_left_s": ["taker_enabled"], "max_book_age_ms": _OLD, "max_spot_age_ms": _OLD}


FEATURE_SWITCHES = set(GROUP_SWITCH.values()) | {"tri_pair_bid", "tri_recovery_enabled", "tri_lock_enabled",
                                                 "tri_late_enabled", "tri_last_enabled", "tri_repeat"}


# rows that go while a switch is ON (replaced by what that switch turns on)
HIDDEN_WHEN = {k: ["tri_hedge_by_time"] for k in ("tri_recovery_min_price", "tri_recovery_max_price",
                                                   "tri_first_rec_min_price", "tri_first_rec_max_price")}


def visible_spec(values: dict) -> list[dict]:
    """The rows a live-on console shows for these settings (stored over defaults)."""
    on = lambda k: bool(values.get(k))
    out = []
    for r in CONTROLS:
        if str(r["key"]).startswith("cross_pair_"):
            continue
        sw = GROUP_SWITCH.get(r["group"])
        if sw and not on(sw):
            continue
        if r["key"] in FEATURE_SWITCHES and not on(r["key"]):
            continue                                  # a switched-off feature: its switch goes too
        if not all(on(k) for k in NEEDS_ALL.get(r["key"], [])):
            continue
        if r["key"] in NEEDS_ANY and not any(on(k) for k in NEEDS_ANY[r["key"]]):
            continue
        if any(on(k) for k in HIDDEN_WHEN.get(r["key"], [])):
            continue
        out.append(r)
    # the strategies that are on first (the console opens its first groups), the general groups after
    return sorted(out, key=lambda r: 0 if r["group"] in GROUP_SWITCH else 1)


def defaults() -> dict:
    return asdict(Settings())


def clamp_values(values: dict) -> tuple[dict, list[str]]:
    """Whitelist and clamp console input. Returns (clean, notes)."""
    clean, notes = {}, []
    for k, v in (values or {}).items():
        row = SPEC.get(k)
        if row is None:
            notes.append(f"{k}: unknown, ignored")
            continue
        if row["kind"] == "bool":
            clean[k] = bool(v)
            continue
        try:
            x = float(v)
        except (TypeError, ValueError):
            notes.append(f"{k}: not a number, ignored")
            continue
        if not math.isfinite(x):
            notes.append(f"{k}: not finite, ignored")
            continue
        lo, hi = row["lo"], row["hi"]
        if x < lo or x > hi:
            notes.append(f"{k}: {x:g} clamped to {min(max(x, lo), hi):g}")
            x = min(max(x, lo), hi)
        clean[k] = int(round(x)) if row["kind"] == "int" else x
    lo_p, hi_p = clean.get("min_price"), clean.get("max_price")
    if lo_p is not None and hi_p is not None and lo_p >= hi_p:
        # a floor at or over the cap would stop every buy without saying why
        notes.append(f"min_price: {lo_p:g} is not under max_price {hi_p:g}, ignored")
        del clean["min_price"]
    return clean, notes


def apply(settings: Settings, values: dict) -> list[str]:
    """Apply clamped values to a live Settings object; returns what changed."""
    changed = []
    names = {f.name for f in fields(Settings)}
    for k, v in values.items():
        if k in names and getattr(settings, k) != v:
            changed.append(f"{k} {getattr(settings, k)!r} -> {v!r}")
            setattr(settings, k, v)
    return changed
