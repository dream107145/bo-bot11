"""Triangle: troll-poly-bot's pair-lock recovery strategy ("triangle", entry mode "open"), ported.

One cycle per window, all taker orders:
  1. open     2-20 s in: buy 10 shares of the token with the higher ask when that ask is 0.70-0.85
              (limit ask + 0.02, at most 3 orders).
  2. recovery when the other token has become the favourite (higher mid), the side we hold sits at
              least 0.05 under what we paid and the favourite's ask is above 0.70: buy the favourite
              until its win branch is +target (fees and a slippage allowance included), stepping with
              the depth within ask + 0.03. At most max_recoveries episodes; then the window is held.
  3. lock     instead of a recovery when < lock_left_s left, the ask is over recovery_max_price, the
              actions cap is hit or a branch would fall under -max_market_loss: buy the side with fewer
              shares until both sides hold the same shares (the result is then fixed).
  4. exit     after any recovery or lock fill: when everything sold at the bids (exit fees in) beats
              the cost by profit_exit, sell it all (limit bid - 0.03): the leading token first, then
              the other (user, 2026-10-07).
  5. late     10-20 s left: buy 10 shares of the side our fair value gives >= 0.95, ask <= 0.97, with
              edge >= 0, at most 2 orders / $20 a window.
  Options (user, 2026-10-07):
  pair bid    right after the open fill, a resting bid for the other side at pair_sum - the open's
              average price, for the open's shares; while it rests no recovery runs, once it fills the
              window is locked; pulled when the other side's mid passes the held side's (-> recovery).
  last minute with last_window_s left: every last_every_ms buy the token with the higher mid (ask at
              least last_min_price) for all the depth within ask + last_slippage that the free cash
              (and last_max_usd, 0 = no cap) pays for; nothing else trades in that minute.

Settings: the tri_* fields of config.Settings (console group "Triangle"), applied while the bot runs.
Source: /root/troll-poly-bot/src/troll_poly_bot/strategy/pairlock_recovery.py (+ _engine.py)."""
from __future__ import annotations

import math
from dataclasses import dataclass, fields

OUT = ("Up", "Down")
OTH = {"Up": "Down", "Down": "Up"}
EPS = 1e-9
EXIT_GAP_MS = 500.0                       # between the profit exit's sells


@dataclass
class TriParams:
    enabled: bool = False
    # 1. open entry
    open_delay_s: float = 2.0
    open_window_s: float = 20.0
    open_min_price: float = 0.70
    open_max_price: float = 0.85
    open_shares: float = 10.0
    open_limit_offset: float = 0.02
    open_max_tries: int = 3
    # 2. recovery
    recovery_enabled: bool = True
    target_profit: float = 0.50
    min_price_move: float = 0.05
    recovery_min_price: float = 0.70
    recovery_max_price: float = 0.90
    max_recoveries: int = 2
    cooldown_ms: float = 2000.0
    min_time_left_s: float = 15.0
    max_slippage: float = 0.03
    slippage_allowance: float = 0.005
    max_order_size: float = 60.0
    max_total_position: float = 300.0
    max_capital: float = 90.0
    min_size: float = 5.0
    # 3. lock
    lock_left_s: float = 60.0
    max_actions: int = 30
    max_market_loss: float = 35.0
    # 4. profit exit
    profit_exit: float = 1.0
    # 5. late buy
    late_enabled: bool = True
    late_window_s: float = 20.0
    late_min_left_s: float = 10.0
    late_min_p: float = 0.95
    late_max_price: float = 0.97
    late_shares: float = 10.0
    late_max_capital: float = 20.0
    late_max_orders: int = 2
    late_min_edge: float = 0.0
    # options
    lock_enabled: bool = True
    pair_bid: bool = False
    pair_sum: float = 0.95                # the pair bid's sum (the start, when it rises)
    pair_sum_max: float = 0.0             # > pair_sum: the sum rises +0.01 every pair_sum_step_s up to this
    pair_sum_step_s: float = 30.0
    pair_shares: float = 0.0              # the pair bid's shares (user, 2026-10-09); 0 = as many as the open filled
    pair_pull: bool = True
    last_enabled: bool = False
    last_window_s: float = 60.0
    last_every_ms: float = 500.0
    last_min_price: float = 0.0
    last_max_usd: float = 0.0
    last_slippage: float = 0.03
    last_shares: float = 0.0              # shares per buy (0 = all the depth the cash pays for)
    last_max_price: float = 1.0           # buy only while the leader's ask is under this (1 = no cap)
    last_min_dist_bps: float = 0.0        # last-minute buys only with the coin this far past the strike (0 = off)
    last_comeback_low: float = 0.0        # a leader whose mid was at/under this earlier in the window...
    pair_timeout_s: float = 0.0           # > 0: the pair bid not complete this long after the open filled ->
    pair_timeout_profit: float = 0.10     #   pull it and buy the pair token as a taker, sized for this if it wins
    pair_timeout_shares: float = 0.0      #   ... or exactly this many shares (> 0: used instead of the sizing)
    add_shares: float = 0.0               # > 0: after the pair token's taker buy (time limit / over pair_hedge_price),
    add_first_price: float = 0.75         #   buy add_shares more of it as its ask reaches add_first_price, then every
    add_step: float = 0.03                #   add_step higher, add_levels times (10 @ 0.75 / 0.78 / 0.81); a level not
    add_levels: float = 3.0               #   filled (price jumped past it, order killed) is bought while the ask is
    add_max_price: float = 0.88           #   still <= add_max_price (user, 2026-10-09)
    btc_shares: float = 0.0               # > 0: BTC moved btc_bps within btc_ms -> buy this many of the token it pushes
    btc_bps: float = 2.0                  #   up, at once (taker, limit ask + btc_slippage) (user, 2026-10-09: "use BTC
    btc_ms: float = 500.0                 #   moves"; recorded books: the Up price follows BTC 250-500 ms later)
    btc_min_price: float = 0.10
    btc_max_price: float = 0.90
    btc_slippage: float = 0.02
    btc_from_s: float = 10.0
    btc_until_s: float = 240.0
    btc_cooldown_ms: float = 1000.0       # per token
    btc_after_open: bool = True           # only once the first buy has filled
    btc_max_buys: float = 10.0            # per window
    cb_shares: float = 0.0                # > 0: comeback of the FIRST token: it dipped to cb_low (mid) and comes back ->
    cb_low: float = 0.35                  #   buy cb_shares of it each time its ask reaches a trigger (0.57 / 0.67 / 0.77),
    cb_first_trigger: float = 0.57        #   limit trigger + cb_limit_over (0.60 / 0.70 / 0.80); shares a level did not
    cb_step: float = 0.10                 #   fill are retried at <= cb_max_price (user, 2026-10-09)
    cb_levels: float = 3.0
    cb_limit_over: float = 0.03
    cb_max_price: float = 0.94
    skip_after_loss: float = 0.0          # > 0: a window that lost more than this -> the next window trades nothing
                                          #   (user, 2026-10-09; judged from the closing book, the result comes later)
    pair_hedge_price: float = 0.0         # > 0: the pair token's ask over this before the pair bid fills -> the time-limit
                                          #   buy goes at once, no wait (user, 2026-10-09); 0 = off
    pair_raise_s: float = 0.0             # > 0: the pair bid not complete this long after the open filled -> it moves
    pair_raise_price: float = 0.50        #   up to this (still a maker bid: at most 1 tick under the ask) (user, 2026-10-09)
    last_comeback_min_price: float = 0.0  # ...may be bought in the last minute from this price (0 = off)
    repeat: bool = False                  # after a completed pair, start a new open + pair cycle
    repeat_delay_ms: float = 500.0
    repeat_until_s: float = 240.0         # later cycles may open until this far into the window
    repeat_open_min_price: float = 0.0    # later cycles' open range (both 0 = the first open's range)
    repeat_open_max_price: float = 0.0
    open_maker: bool = False              # the first cycle's open as a resting bid (False: taker)
    open_hold_top: bool = False           # ... the leader run over open_max_price: keep the bid at that price on it
                                          #   (until open_window_s) instead of giving up (user, 2026-10-09)
    sell_all_price: float = 0.0           # a held token bid over this: sell everything held (0 = off)
    cheap_sell_price: float = 0.0         # after a sell-everything / profit exit: the other side is sold
                                          # only once its bid is over this (0 = at once, from 1 c)
    hedge_by_time: bool = False           # the hedge range by time left (replaces first_rec / recovery ranges)
    hedge_t1_min: float = 0.80            # 4:00-3:00 left
    hedge_t1_max: float = 0.90
    hedge_t2_min: float = 0.77            # 3:00-2:00 left
    hedge_t2_max: float = 0.85
    hedge_t3_min: float = 0.80            # 2:00-1:00 left (no hedge in the last minute)
    hedge_t3_max: float = 0.90
    first_rec_min_price: float = 0.0      # the window's first hedge: its range (both 0 = recovery_min/max)
    first_rec_max_price: float = 0.0
    repeat_open_maker: bool = False       # later cycles' open as a resting bid
    max_quote_age_ms: float = 2000.0
    window_budget: float = 0.0            # Settings.max_window_usdc (the pair bid must fit in it; 0 = none)

    @classmethod
    def from_settings(cls, s) -> "TriParams":
        """The tri_<name> fields of a config.Settings (+ the window budget)."""
        p = cls(**{f.name: getattr(s, "tri_" + f.name) for f in fields(cls) if f.name != "window_budget"})
        p.window_budget = float(s.max_window_usdc)
        return p

    @classmethod
    def from_dict(cls, raw: dict | None) -> tuple["TriParams", list[str]]:
        """Known keys only, cast to the field's type; unknown keys are reported, not used."""
        p, notes = cls(), []
        kinds = {f.name: f.type for f in fields(cls)}
        for k, v in (raw or {}).items():
            if k not in kinds:
                notes.append(f"{k}: unknown, ignored")
                continue
            try:
                t = kinds[k]
                setattr(p, k, bool(v) if t in ("bool", bool) else int(v) if t in ("int", int) else float(v))
            except (TypeError, ValueError):
                notes.append(f"{k}: bad value {v!r}, ignored")
        return p, notes


def _ask(v, o):
    return (v.books.get(o) or {}).get("ask")


def _bid(v, o):
    return (v.books.get(o) or {}).get("bid")


def _mid(v, o):
    b, a = _bid(v, o), _ask(v, o)
    return (a + b) / 2.0 if a is not None and b is not None else None


def _levels(v, o, limit):
    """Ask levels at or under ``limit``, cheapest first (best ask alone when the book has no depth)."""
    t = v.books.get(o) or {}
    lv = [(p, s) for p, s in (t.get("asks") or []) if p <= limit + EPS]
    if not lv and t.get("ask") is not None and t["ask"] <= limit + EPS:
        lv = [(t["ask"], t.get("ask_size") or 0.0)]
    return lv


def _vwap(levels, q):
    got = cost = 0.0
    for p, s in levels:
        take = min(s, q - got)
        if take <= 0:
            break
        got += take
        cost += take * p
    return (cost / got) if got > EPS else None, got


def _floor2(x):
    return math.floor(x * 100 + 1e-6) / 100


def decide(v, p: TriParams, d) -> None:
    """Add this tick's triangle order (at most one) to the Decision ``d``."""
    st = v.tri or {}
    if v.halted or v.book_age_ms is None or v.book_age_ms > p.max_quote_age_ms:
        return
    for o in OUT:                                         # each token's lowest mid so far this window
        m = _mid(v, o)
        if m is not None and m < st.get(f"low_{o}", 1.0) - EPS:
            d.tri_update[f"low_{o}"] = m
    if any(x > EPS for x in v.in_flight_taker.values()):
        d.why["taker"] = "triangle: an order is on its way"
        return
    fee = v.market.fees.taker_fee
    sh = {o: max(0.0, v.shares.get(o, 0.0)) for o in OUT}
    held = sh["Up"] + sh["Down"] > EPS
    left, t_in, now = v.secs_left, v.secs_in, v.now_ms
    free = v.free_cash if v.free_cash is not None else float("inf")

    # sell everything once a token we hold is bid over sell_all_price (that token first, then the rest);
    # started, it finishes. Cash comes back now instead of after the settlement.
    if p.sell_all_price > 0 and held:
        hit = [o for o in OUT if sh[o] >= 0.01 and (_bid(v, o) or 0.0) > p.sell_all_price + EPS]
        if hit or st.get("selling_all"):
            if now - st.get("sell_all_ms", -1e18) < EXIT_GAP_MS:
                return
            # the token over the line first; then the other side, once its bid passes cheap_sell_price
            o = max(hit, key=lambda x: _bid(v, x)) if hit else _cheap_to_sell(v, p, sh)
            if o is None:
                d.tri_update["selling_all"] = True
                d.why["taker"] = f"triangle: sold out; the cheap side is held until its bid is over {p.cheap_sell_price:.2f}"
                return                                    # nothing else trades in this window
            b = _bid(v, o)
            d.sells.append((o, max(0.001, round(b - 0.01, 3)), _floor2(sh[o])))
            d.tri_update.update(selling_all=True, sell_all_ms=now)
            d.why["taker"] = (f"triangle: sell everything ({hit[0] if hit else o} bid over {p.sell_all_price:.3f}); "
                              f"sell {_floor2(sh[o]):.2f} {o} at bid {b:.3f}")
            return

    # a new cycle repeat_delay_ms after this one's pair completed (it may open until repeat_until_s)
    if p.repeat and p.pair_bid and st.get("pair") == "filled" \
            and now - st.get("pair_done_ms", -1e18) >= p.repeat_delay_ms:
        reset = dict(opened=False, open_done=False, open_side=None, open_sh=0.0, open_px=0.0, open_ms=None,
                     pair=None, pair_sh=0.0, open_sent=0, cycles=st.get("cycles", 0) + 1)
        d.tri_update.update(reset)
        st = {**st, **reset}

    # 1. open entry (the first cycle needs an empty window and the open window; later ones hold the
    #    locked pairs and may open until repeat_until_s)
    first = st.get("cycles", 0) == 0
    until = p.open_window_s if first else p.repeat_until_s
    if not st.get("opened") and (not held or not first) and p.open_delay_s <= t_in <= until:
        if p.open_maker if first else p.repeat_open_maker:
            d.why["taker"] = "triangle: the open is a resting bid (maker)"
            return                                        # placed by open_bid_target
        if st.get("open_sent", 0) >= p.open_max_tries:
            d.why["taker"] = f"triangle: {p.open_max_tries} open orders tried"
            return
        # the token whose ask is in the range; both in it: the dearer (equal asks: the higher bid).
        # A range over 0.50 can only hold the dearer token (troll-poly-bot's rule); one under 0.50 the cheaper.
        lo, hi = _open_range(p, first)
        o, why = _open_pick(v, lo, hi, {x: _ask(v, x) for x in OUT})
        if o is None:
            d.why["taker"] = f"triangle: no ask in the {'open' if first else 'repeat open'} range {lo:.2f}-{hi:.2f} {why}"
            return
        a = _ask(v, o)
        size = max(p.open_shares, v.market.min_size)
        limit = min(0.99, round(a + p.open_limit_offset, 2))
        if p.pair_bid and p.window_budget > 0:
            # a cycle opens only if its pair bid will fit in the window budget too (a bid over it is refused)
            pair_n = max(p.pair_shares, v.market.min_size) if p.pair_shares > 0 else size
            need = limit * size + v.market.fees.taker_fee(limit, size) + _pair_px_max(p, a) * pair_n
            if v.window_cost + v.reserved + need > p.window_budget + EPS:
                d.why["taker"] = (f"triangle: open + pair bid ${need:.2f} would pass the ${p.window_budget:.0f} window "
                                  f"budget (${v.window_cost + v.reserved:.2f} used)")
                return
        d.takes.append((o, limit, size, 0.0))
        d.tags[f"taker:{o}"] = "scalp"
        d.tri_kinds[o] = "open"
        d.why["taker"] = f"triangle: open {o} {size:.0f} sh @ <= {limit:.2f} (ask {a:.2f})"
        return

    # 1b. BTC move (user, 2026-10-09): BTC moved btc_bps within btc_ms -> buy btc_shares of the token it pushes up,
    #     at once, before the book reprices (it follows BTC 250-500 ms later)
    mv = getattr(v, "btc_move_bps", None)
    if (p.btc_shares > 0 and p.btc_bps > 0 and mv is not None and abs(mv) >= p.btc_bps - EPS
            and (st.get("opened") or not p.btc_after_open) and p.btc_from_s <= t_in <= p.btc_until_s
            and (v.spot_age_ms is None or v.spot_age_ms <= 1000.0)):
        o = "Up" if mv > 0 else "Down"
        a = _ask(v, o)
        if (a is not None and p.btc_min_price - EPS <= a <= p.btc_max_price + EPS
                and now - st.get(f"btc_ms_{o}", -1e18) >= p.btc_cooldown_ms and st.get("btc_n", 0) < p.btc_max_buys):
            limit = min(p.btc_max_price, round(a + p.btc_slippage, 2))
            n = _budget_cut(v, p, max(p.btc_shares, v.market.min_size), limit)
            if n is None:
                d.why["taker"] = f"triangle: BTC {mv:+.2f} bps, but the ${p.window_budget:.0f} window budget is used"
            else:
                d.takes.append((o, limit, n, 0.0))
                d.tags[f"taker:{o}"] = "btc"
                d.tri_kinds[o] = "btc"
                d.why["taker"] = (f"triangle: BTC {mv:+.2f} bps in {p.btc_ms:.0f} ms: buy {n:g} {o} @ <= {limit:.2f} "
                                  f"(ask {a:.2f})")
                return

    branch = {o: sh[o] - v.window_cost for o in OUT}       # P&L if o wins (costs include fees)

    # last minute: buy the leader, as much as the book and the cash allow, every last_every_ms
    if p.last_enabled and left <= p.last_window_s + EPS:
        _last_minute(v, p, d, st, free)
        return

    # 5. late buy (checked before recovery)
    if p.late_enabled and p.late_min_left_s - EPS <= left <= p.late_window_s + EPS:
        pu = v.fair_up if v.fair_up is not None else _mid(v, "Up")
        if pu is not None and st.get("late_n", 0) < p.late_max_orders and now - st.get("last_ms", -1e18) >= p.cooldown_ms:
            w = "Up" if pu >= 0.5 else "Down"
            pw = pu if w == "Up" else 1.0 - pu
            a = _ask(v, w)
            if pw >= p.late_min_p - EPS and a is not None and a <= p.late_max_price + EPS:
                limit = min(0.99, a + p.max_slippage)
                lv = _levels(v, w, limit)
                vw, _ = _vwap(lv, p.late_shares)
                if vw is not None and vw - a <= p.max_slippage + EPS \
                        and pw - vw - fee(vw, 1.0) - p.slippage_allowance >= p.late_min_edge - EPS:
                    unit = limit + fee(limit, 1.0) + p.slippage_allowance
                    size = p.late_shares
                    size = min(size, (branch[OTH[w]] + p.max_market_loss) / unit)   # the other branch stays >= -L
                    size = min(size, (p.late_max_capital - st.get("late_usd", 0.0)) / unit,
                               p.max_total_position - sh["Up"] - sh["Down"], free / unit)
                    size = _floor2(size)
                    if size >= max(p.min_size, v.market.min_size) - EPS:
                        d.takes.append((w, round(limit, 2), size, pw - vw))
                        d.tags[f"taker:{w}"] = "late"
                        d.tri_kinds[w] = "late"
                        d.why["taker"] = f"triangle: late {w} {size:.2f} sh, fair {pw:.3f} vs ask {a:.2f}"
                        return

    if not held:
        return

    # the pair bid's time is up: buy the pair token now (taker), sized so the window ends
    # +pair_timeout_profit (fees included) if it wins (user, 2026-10-08)
    o_pair = OTH.get(st.get("open_side"))
    a_pair = _ask(v, o_pair) if o_pair else None
    early = p.pair_hedge_price > 0 and a_pair is not None and a_pair > p.pair_hedge_price + EPS
    if (p.pair_bid and st.get("opened") and st.get("open_done")
            and (st.get("pair") or "working") == "working" and st.get("open_ms") is not None
            and (early or (p.pair_timeout_s > 0 and now - st["open_ms"] >= p.pair_timeout_s * 1000.0))):
        o = o_pair
        a = a_pair
        when = (f"{o} ask {a:.2f} over {p.pair_hedge_price:.2f}" if early
                else f"pair bid not filled in {p.pair_timeout_s:.0f} s")
        if a is not None:
            per = 1.0 - a - fee(a, 1.0) - p.slippage_allowance
            need = p.pair_timeout_profit - branch[o]
            d.tri_update["pair"] = "timeout"                 # the pair bid is pulled
            if p.pair_timeout_shares > 0:                    # a fixed size (user, 2026-10-08: 5 shares)
                n = max(p.pair_timeout_shares, v.market.min_size)
                limit = min(0.99, round(a + p.max_slippage, 2))
                d.takes.append((o, limit, n, 0.0))
                d.tags[f"taker:{o}"] = "scalp_hedge"
                d.tri_kinds[o] = "rec"
                d.why["taker"] = f"triangle: {when}: buy {n:g} {o} @ <= {limit:.2f}"
                return
            if need <= EPS:
                d.why["taker"] = f"triangle: pair time up, the {o} branch is already {branch[o]:+.2f}"
                return
            if per <= EPS:
                d.why["taker"] = f"triangle: pair time up, {o} at {a:.2f} leaves no profit per share"
                return
            n = max(math.ceil(need / per * 100 - 1e-6) / 100, v.market.min_size)
            limit = min(0.99, round(a + p.max_slippage, 2))
            d.takes.append((o, limit, n, 0.0))
            d.tags[f"taker:{o}"] = "scalp_hedge"
            d.tri_kinds[o] = "rec"
            d.why["taker"] = (f"triangle: {when}: buy {n:.2f} {o} @ <= {limit:.2f} "
                              f"so the window ends {p.pair_timeout_profit:+.2f} if {o} wins")
            return

    # 3b. add-on (user, 2026-10-09): the pair token bought as a taker keeps rising -> add_shares more at each
    #     level it reaches (0.75 / 0.78 / 0.81); what a level did not fill is bought up to add_max_price
    if p.add_shares > 0 and p.add_levels > 0 and st.get("pair") == "timeout" and st.get("open_side") in OUT:
        o = OTH[st["open_side"]]
        a = _ask(v, o)
        if (v.shares.get(o, 0.0) > EPS and a is not None and v.secs_left > 2.0
                and now - st.get("last_ms", -1e18) >= p.cooldown_ms):
            levels = [round(p.add_first_price + i * p.add_step, 4) for i in range(int(p.add_levels))]
            reached = sum(1 for L in levels if a >= L - EPS)
            need = reached * p.add_shares - st.get("add_sh", 0.0)
            if need > 0.01 and a <= p.add_max_price + EPS:
                limit = min(p.add_max_price, round(a + p.max_slippage, 2))
                n = _budget_cut(v, p, max(_floor2(need), v.market.min_size), limit)
                if n is None:
                    d.why["taker"] = f"triangle: add-on {o} would pass the ${p.window_budget:.0f} window budget"
                    return
                d.takes.append((o, limit, n, 0.0))
                d.tags[f"taker:{o}"] = "scalp_hedge"
                d.tri_kinds[o] = "add"
                d.why["taker"] = (f"triangle: {o} ask {a:.2f} reached {levels[reached - 1]:.2f}: buy {n:g} more @ <= {limit:.2f} "
                                  f"(add-on {st.get('add_sh', 0.0):g}/{p.add_shares * len(levels):g})")
                return

    # 3c. comeback (user, 2026-10-09): a token (either side) that fell under cb_low and comes back -> cb_shares of it
    #     at each trigger (0.57 / 0.67 / 0.77) with a limit cb_limit_over above it (0.60 / 0.70 / 0.80); what a level
    #     did not fill is retried at <= cb_max_price. Each token has its own count.
    if p.cb_shares > 0 and p.cb_levels > 0 and st.get("opened") and st.get("open_side") in OUT \
            and v.secs_left > 2.0 and now - st.get("last_ms", -1e18) >= p.cooldown_ms:
        trig = [round(p.cb_first_trigger + i * p.cb_step, 4) for i in range(int(p.cb_levels))]
        first = st["open_side"]
        for o in (first, OTH[first]):
            a = _ask(v, o)
            low = min(st.get(f"low_{o}", 1.0), d.tri_update.get(f"low_{o}", 1.0))
            if a is None or not low < p.cb_low - EPS:
                continue
            reached = sum(1 for T in trig if a >= T - EPS)
            need = reached * p.cb_shares - st.get(f"cb_sh_{o}", 0.0)
            if need <= 0.01:
                continue
            if reached > st.get(f"cb_lvl_{o}", 0):         # a new level: its own limit
                limit = round(trig[reached - 1] + p.cb_limit_over, 2)
                d.tri_update[f"cb_lvl_{o}"] = reached
                how = f"reached {trig[reached - 1]:.2f}"
            else:                                          # what a level did not fill: again, up to cb_max_price
                limit = round(a + p.max_slippage, 2)
                how = "not all filled, again"
            limit = min(limit, p.cb_max_price)
            if a > limit + EPS:
                continue
            n = _budget_cut(v, p, max(_floor2(need), v.market.min_size), limit)
            if n is None:
                d.why["taker"] = f"triangle: comeback {o} would pass the ${p.window_budget:.0f} window budget"
                continue
            d.takes.append((o, limit, n, 0.0))
            d.tags[f"taker:{o}"] = "scalp_hedge"
            d.tri_kinds[o] = "cb"
            d.why["taker"] = (f"triangle: comeback, {o} (low {low:.2f}) ask {a:.2f} {how}: buy {n:g} @ <= {limit:.2f} "
                              f"({st.get(f'cb_sh_{o}', 0.0):g}/{p.cb_shares * len(trig):g})")
            return

    # 4. profit exit (after a recovery or lock fill): once everything sold at the bids beats the cost by
    #    profit_exit, sell the leading token first, then the other (the hedge side); started, it finishes
    if p.profit_exit > 0 and st.get("had_rec"):
        bids = {o: _bid(v, o) for o in OUT}
        exiting = st.get("exiting", False)
        if not exiting and all(bids[o] is not None for o in OUT if sh[o] > EPS):
            mtm = sum(sh[o] * bids[o] - fee(bids[o], sh[o]) for o in OUT if sh[o] > EPS) - v.window_cost
            if mtm >= p.profit_exit - EPS and now - st.get("exit_ms", -1e18) >= p.cooldown_ms:
                exiting = True
                d.why["taker"] = f"triangle: profit exit, worth {mtm:+.2f} over cost"
        if exiting:
            if now - st.get("exit_ms", -1e18) < EXIT_GAP_MS:
                return
            lead = st.get("exit_leader")
            if lead is None or sh.get(lead, 0.0) >= 0.01:
                # the leader first (all of it, whatever its price)
                left_sides = [o for o in OUT if sh[o] >= 0.01 and bids[o] is not None and bids[o] > 0.01 + EPS]
                o = lead if lead in left_sides else (max(left_sides, key=lambda x: (_mid(v, x) or bids[x]))
                                                      if left_sides else None)
                what = "the leader"
            else:
                o = _cheap_to_sell(v, p, sh)                  # then the rest, once its bid passes cheap_sell_price
                what = "the rest"
            if o is None:
                if not any(sh[x] >= 0.01 for x in OUT):
                    d.tri_update["exiting"] = False
                    return
                d.why["taker"] = f"triangle: profit taken; the cheap side is held until its bid is over {p.cheap_sell_price:.2f}"
                return                                        # nothing else trades in this window
            d.sells.append((o, max(0.01, round(bids[o] - 0.03, 2)), _floor2(sh[o])))
            d.tri_update.update(exiting=True, exit_ms=now, exit_leader=lead or o)
            d.why["taker"] = (d.why.get("taker", "triangle: profit exit") + f"; sell {_floor2(sh[o]):.2f} {o} ({what})")
            return

    # 2./3. recovery or lock
    if not p.recovery_enabled or st.get("locked"):
        return
    if p.pair_bid:
        if not st.get("opened"):
            return                                        # no cycle open: the locked pairs need nothing
        ps = st.get("pair") or "working"
        if ps == "filled":
            d.why["taker"] = "triangle: pair complete, the window is locked"
            return
        if ps == "working":
            d.why["taker"] = "triangle: the pair bid is working; recovery once it is pulled"
            return
    worst = min(branch.values())
    if worst >= p.target_profit - EPS:
        d.why["taker"] = f"triangle: both branches >= {p.target_profit:+.2f}"
        return
    if left < p.min_time_left_s:
        return
    if now - st.get("last_ms", -1e18) < p.cooldown_ms:
        return
    if st.get("locking"):
        _lock(v, p, d, sh, free, "finishing the lock")
        return
    mu, md = _mid(v, "Up"), _mid(v, "Down")
    if mu is None or md is None or abs(mu - md) <= EPS:
        return
    S = "Up" if mu > md else "Down"
    O = OTH[S]
    rec_open = st.get("rec_open", False) and st.get("rec_side") == S
    if st.get("rec_open") and st.get("rec_side") != S:
        d.tri_update["rec_open"] = False                    # the other side turned higher: episode over
    if sh[O] <= EPS:
        d.why["taker"] = f"triangle: the market favours {S}, no {O} to recover from"
        return
    bo = _bid(v, O)
    if bo is None:
        return
    avg_o = (v.costs.get(O, 0.0) - v.fees.get(O, 0.0)) / sh[O]
    if avg_o - bo < p.min_price_move - EPS:
        d.why["taker"] = f"triangle: {O} bid {bo:.2f} only {avg_o - bo:+.3f} under its cost {avg_o:.3f}"
        return
    bs = branch[S]
    if bs >= p.target_profit - EPS:
        if rec_open:
            d.tri_update["rec_open"] = False                # that episode reached its target
        return
    a = _ask(v, S)
    rng = _rec_range(p, st, left)
    if rng is None:
        d.why["taker"] = f"triangle: no hedge with {left:.0f} s left (hedge by time left)"
        return
    rmin, rmax = rng
    if a is None or a <= rmin + EPS:
        d.why["taker"] = f"triangle: waiting, {S} ask {'none' if a is None else f'{a:.2f}'} not above {rmin:.2f}"
        return
    if p.max_recoveries > 0 and not rec_open and st.get("recs", 0) >= p.max_recoveries:
        d.why["taker"] = f"triangle: recovered {st.get('recs', 0)} times, the window is held ({S} branch {bs:+.2f})"
        return
    # the size the target needs at the executable price
    limit = min(0.99, a + p.max_slippage)
    lv = _levels(v, S, limit)
    q, vw = None, a
    for _ in range(6):
        per = 1.0 - vw - fee(vw, 1.0) - p.slippage_allowance
        if per <= EPS:
            q = None
            break
        q = max(math.ceil((p.target_profit - bs) / per * 100 - 1e-6) / 100, p.min_size)
        nv, _ = _vwap(lv, q)
        if nv is None or abs(nv - vw) < 1e-6:
            break
        vw = nv
    why_lock = ""
    if st.get("actions", 0) >= p.max_actions:
        why_lock = f"{p.max_actions} actions"
    elif p.lock_left_s > 0 and left < p.lock_left_s:
        why_lock = f"{left:.0f} s left < {p.lock_left_s:g}"
    elif rmax < 1.0 and a > rmax + EPS:
        why_lock = f"{S} ask {a:.2f} > {rmax:.2f}"
    elif p.max_market_loss > 0:
        if worst < -p.max_market_loss - EPS:
            why_lock = f"a branch is already {worst:+.2f}"
        elif q is None:
            why_lock = f"{S} at {vw:.2f} leaves no profit per share"
        elif branch[O] - q * (vw + fee(vw, 1.0) + p.slippage_allowance) < -p.max_market_loss - EPS:
            why_lock = f"recovering {q:g} {S} would leave the {O} branch under -{p.max_market_loss:g}"
    if why_lock:
        if not p.lock_enabled:
            d.why["taker"] = f"triangle: no recovery ({why_lock}; locks off)"
            return
        d.tri_update["lock_why"] = why_lock
        _lock(v, p, d, sh, free, why_lock)
        return
    if q is None:
        return
    unit = limit + fee(limit, 1.0) + p.slippage_allowance
    depth = sum(s for _, s in lv)
    step = min(q, p.max_order_size, depth, p.max_total_position - sh["Up"] - sh["Down"],
               (p.max_capital - st.get("rec_cap", 0.0)) / unit, free / unit)
    step = _floor2(step)
    if step < max(p.min_size, v.market.min_size) - EPS:
        d.why["taker"] = f"triangle: recovery step {step:.2f} sh under the minimum"
        return
    d.takes.append((S, round(limit, 2), step, 0.0))
    d.tags[f"taker:{S}"] = "scalp_hedge"
    d.tri_kinds[S] = "rec"
    d.why["taker"] = (f"triangle: recover via {S} {step:.2f} sh @ <= {limit:.2f} ({S} branch {bs:+.2f} -> "
                      f"target {p.target_profit:+.2f}; {O} {avg_o - bo:+.2f} under cost)")


def _cheap_to_sell(v, p: TriParams, sh: dict):
    """The side left after the leader was sold: sold once its bid is over cheap_sell_price (0: from 1 c)."""
    ok = (lambda b: b > p.cheap_sell_price + EPS) if p.cheap_sell_price > 0 else (lambda b: b >= 0.01 - EPS)
    sides = [o for o in OUT if sh[o] >= 0.01 and _bid(v, o) is not None and ok(_bid(v, o))]
    return max(sides, key=lambda x: _bid(v, x)) if sides else None


def _pair_sum_now(p: TriParams, st: dict, now_ms: float) -> float:
    """The pair sum now: pair_sum, rising +0.01 every pair_sum_step_s since the open filled, up to pair_sum_max."""
    if p.pair_sum_max > p.pair_sum + EPS and p.pair_sum_step_s > 0 and st.get("open_ms") is not None:
        steps = int(max(0.0, now_ms - st["open_ms"]) / 1000.0 // p.pair_sum_step_s)
        return round(min(p.pair_sum_max, p.pair_sum + 0.01 * steps), 2)
    return p.pair_sum


def _budget_cut(v, p: TriParams, n: float, limit: float) -> float | None:
    """n shares at limit, cut to what the window budget (max_window_usdc) has left; None under the venue's 5."""
    if p.window_budget <= 0:
        return n
    per = limit + v.market.fees.taker_fee(limit, 1.0)
    room = p.window_budget - v.window_cost - v.reserved
    n = min(n, math.floor(room / per * 100 + 1e-6) / 100)
    return n if n >= v.market.min_size - EPS else None


def pair_goal(p: TriParams, st: dict) -> float:
    """How many shares the pair bid buys: pair_shares (at least the venue's 5), or what the open filled."""
    return max(p.pair_shares, p.min_size) if p.pair_shares > 0 else st.get("open_sh", 0.0)


def _pair_raised(p: TriParams, st: dict, now_ms: float) -> bool:
    """pair_raise_s since the open filled and no pair yet: the pair bid goes up to pair_raise_price."""
    return p.pair_raise_s > 0 and st.get("open_ms") is not None and now_ms - st["open_ms"] >= p.pair_raise_s * 1000.0


def _pair_px_max(p: TriParams, open_px: float) -> float:
    """The highest the pair bid may rest after an open at open_px (the budget checks)."""
    hi = max(0.0, max(p.pair_sum, p.pair_sum_max) - open_px)
    return max(hi, p.pair_raise_price) if p.pair_raise_s > 0 else hi


def _rec_range(p: TriParams, st: dict, left: float | None = None):
    """The hedge's price range. hedge_by_time: by the time left (4:00-3:00 / 3:00-2:00 / 2:00-1:00;
    None = no hedge, e.g. the last minute). Otherwise the window's first hedge may have its own
    (first_rec_*), later ones recovery_min/max_price; an episode still filling keeps its range."""
    if p.hedge_by_time and left is not None:
        if 180.0 <= left <= 240.0 + EPS:
            return p.hedge_t1_min, p.hedge_t1_max
        if 120.0 <= left < 180.0:
            return p.hedge_t2_min, p.hedge_t2_max
        if 60.0 <= left < 120.0:
            return p.hedge_t3_min, p.hedge_t3_max
        return None
    done = st.get("recs", 0) - (1 if st.get("rec_open") else 0)
    if done <= 0 and p.first_rec_max_price > 0:
        return p.first_rec_min_price, p.first_rec_max_price
    return p.recovery_min_price, p.recovery_max_price


def _open_range(p: TriParams, first: bool) -> tuple[float, float]:
    lo, hi = p.open_min_price, p.open_max_price
    if not first and p.repeat_open_max_price > 0:
        lo, hi = p.repeat_open_min_price, p.repeat_open_max_price          # e.g. the leader over 0.55
    if p.pair_bid:
        hi = min(hi, round(p.pair_sum - 0.01, 2))         # an open must leave a pair bid of at least 1 c
    return lo, hi


def _open_pick(v, lo: float, hi: float, price: dict):
    """The token whose price is in [lo, hi]; both: the dearer (equal: the higher bid). (side, why)."""
    cands = [o for o in OUT if price.get(o) is not None and lo - EPS <= price[o] <= hi + EPS]
    if not cands:
        return None, "(" + ", ".join(f"{o} {'-' if price.get(o) is None else f'{price[o]:.2f}'}" for o in OUT) + ")"
    if len(cands) == 2 and abs(price["Up"] - price["Down"]) <= EPS:
        ub, db = _bid(v, "Up"), _bid(v, "Down")
        if ub is None or db is None or abs(ub - db) <= EPS:
            return None, "(no side ahead)"
        return ("Up" if ub > db else "Down"), ""
    return max(cands, key=lambda x: price[x]), ""


def open_bid_target(v, p: TriParams, d, sizes: dict) -> None:
    """The open as a resting bid (open_maker / repeat_open_maker), one tick over the best bid and never at
    the ask; it follows the book within its range. Called from strategy._makers before the reconcile.
    Once it holds the venue's minimum (5 sh; the bot sets open_done on that fill) the rest is cancelled
    and the pair bid pairs what filled; under 5 it keeps resting, until it leaves the range or the time
    runs out (then what filled is the open)."""
    st = v.tri or {}
    if not p.enabled or v.halted or st.get("open_done"):
        return
    first = st.get("cycles", 0) == 0
    if not (p.open_maker if first else p.repeat_open_maker):
        return
    until = p.open_window_s if first else p.repeat_until_s
    size = max(p.open_shares, v.market.min_size)
    tick = v.market.tick
    lo, hi = _open_range(p, first)
    price = {}
    for o in OUT:
        b, a = _bid(v, o), _ask(v, o)
        price[o] = None if b is None or a is None else round(min(b + tick, a - tick), 4)
    in_time = p.open_delay_s <= v.secs_in <= until
    kept = False                                          # a leader over the range, bid kept at the top
    if st.get("opened"):
        o = st.get("open_side")
        rest = size - st.get("open_sh", 0.0)
        still = bool(v.resting.get(o))
        px = price.get(o)
        if first and p.open_hold_top and px is not None and px > hi + EPS:
            px, kept = hi, True                           # it ran over the range: keep the bid at the top
        if rest < 0.01 or not still or not in_time or px is None or not (lo - EPS <= px <= hi + EPS):
            d.tri_update["open_done"] = True                # what filled is this cycle's open
            return
        d.targets[o] = px
        sizes[o] = _floor2(rest) if rest >= v.market.min_size - EPS else v.market.min_size
    else:
        held = v.shares.get("Up", 0.0) + v.shares.get("Down", 0.0) > EPS
        if (first and held) or not in_time:
            return
        o, why = _open_pick(v, lo, hi, price)
        if o is None and first and p.open_hold_top:
            # nothing in the range because the leader ran over it: a bid at the top of the range on the leader
            # (never at the ask: its bid + 1 tick is over the top, so its ask is too)
            mu, md = _mid(v, "Up"), _mid(v, "Down")
            if mu is not None and md is not None and abs(mu - md) > EPS:
                lead = "Up" if mu > md else "Down"
                if price.get(lead) is not None and price[lead] > hi + EPS:
                    o, kept = lead, True
                    price = {**price, lead: hi}
        if o is None:
            d.why["maker_Up"] = d.why["maker_Down"] = f"triangle: no bid price in the open range {lo:.2f}-{hi:.2f} {why}"
            return
        px = price[o]
        if p.pair_bid and p.window_budget > 0:
            need = px * size + _pair_px_max(p, px) * (max(p.pair_shares, v.market.min_size) if p.pair_shares > 0 else size)
            if v.window_cost + v.reserved + need > p.window_budget + EPS:
                d.why[f"maker_{o}"] = f"triangle: open bid + pair bid ${need:.2f} would pass the ${p.window_budget:.0f} budget"
                return
        d.targets[o] = px
        sizes[o] = size
    d.tags[f"maker:{o}"] = "scalp"
    d.tri_kinds[f"maker:{o}"] = "open"
    d.why[f"maker_{o}"] = (f"triangle: open bid {px:.2f} for {sizes[o]:.2f} {o} (range {lo:.2f}-{hi:.2f}"
                           + (f"; {o} ran over it, the bid stays at the top)" if kept else ")"))


def pair_target(v, p: TriParams, d, sizes: dict) -> None:
    """The open's pair bid (called from strategy._makers, before the bids are reconciled)."""
    st = v.tri or {}
    if not (p.enabled and p.pair_bid and st.get("opened")) or (st.get("pair") or "working") != "working":
        return
    held = st.get("open_side")
    if held not in OUT or not st.get("open_sh"):
        return
    o = OTH[held]
    if p.pair_pull:
        # pulled when the hedge is due: the other token's ask over the recovery's lowest price
        # (troll-poly-bot pulls as soon as it leads; that would pull a cheap open's bid at once)
        ao = _ask(v, o)
        rng = _rec_range(p, st, v.secs_left)
        rmin = rng[0] if rng else 1.0                     # no hedge now: the pair bid stays
        if ao is not None and ao > rmin + EPS:
            d.tri_update["pair"] = "pulled"
            d.why[f"maker_{o}"] = f"triangle: pair bid pulled, {o} ask {ao:.2f} over {rmin:.2f}: hedge due"
            return
    if (p.pair_timeout_s > 0 and st.get("open_done") and st.get("open_ms") is not None
            and v.now_ms - st["open_ms"] >= p.pair_timeout_s * 1000.0):
        return                                            # time up: the taker buy (decide) takes over, no bid
    a_o = _ask(v, OTH[held])
    if p.pair_hedge_price > 0 and a_o is not None and a_o > p.pair_hedge_price + EPS and st.get("open_done"):
        return                                            # over the hedge price: bought at once (decide), no bid
    need = pair_goal(p, st) - st.get("pair_sh", 0.0)        # this cycle's pair, less what its bid bought
    if need < 0.01:
        if st.get("open_done", True):
            d.tri_update["pair"] = "filled"               # complete only once the open bid is done too
        return
    S = _pair_sum_now(p, st, v.now_ms)
    cap = math.floor((S - st["open_px"]) * 100 + 1e-9) / 100
    raised = _pair_raised(p, st, v.now_ms) and p.pair_raise_price > cap + EPS
    if raised:
        cap = p.pair_raise_price                          # no pair yet: the bid moves up (still under the ask)
    a = _ask(v, o)
    capq = cap if a is None else min(cap, round(a - v.market.tick, 4))      # never at or over the ask
    working = [r for r in v.resting.get(o, []) if not r.cancelling and r.price <= cap + EPS]
    if working and need >= v.market.min_size - EPS and max(r.price for r in working) < capq - EPS:
        working = []                                      # the sum rose: requote the bid higher (exact rest)
    if working:
        # a bid already rests (maybe partly filled): it stays as it is until the pair is exact; it is
        # never replaced because the ask moved (a replacement would have to be >= 5 shares and could
        # buy more than the open)
        px = max(r.price for r in working)
        size = need
    else:
        px = capq
        size = need if need >= v.market.min_size - EPS else v.market.min_size   # the venue's minimum
    if px < 0.01 - EPS:
        return
    d.targets[o] = px
    sizes[o] = _floor2(size) if size >= v.market.min_size - EPS else v.market.min_size
    if p.pair_sum_max > p.pair_sum + EPS:
        d.exact.add(o)                                    # a rising sum: the bid moves on every 1-c step
    d.tags[f"maker:{o}"] = "scalp_pair"
    d.tri_kinds[f"maker:{o}"] = "pair"
    d.why[f"maker_{o}"] = (f"triangle: pair bid {px:.2f}, {need:.2f} {o} still to pair "
                           + (f"(raised to <= {p.pair_raise_price:.2f} after {p.pair_raise_s:.0f} s)" if raised else f"(sum {S:.2f})"))


def _last_minute(v, p: TriParams, d, st: dict, free: float) -> None:
    if v.now_ms - st.get("last_buy_ms", -1e18) < p.last_every_ms:
        return
    mu, md = _mid(v, "Up"), _mid(v, "Down")
    if mu is None or md is None or abs(mu - md) <= EPS:
        d.why["taker"] = "triangle: last minute, no leader"
        return
    o = "Up" if mu > md else "Down"
    a = _ask(v, o)
    lo = p.last_min_price
    low = min(st.get(f"low_{o}", 1.0), d.tri_update.get(f"low_{o}", 1.0))
    if p.last_comeback_low > 0 and p.last_comeback_min_price > 0 and low <= p.last_comeback_low + EPS:
        lo = min(lo, p.last_comeback_min_price)           # a comeback: it was at or under the low earlier
    if a is None or a < lo - EPS:
        d.why["taker"] = f"triangle: last minute, {o} ask {'none' if a is None else f'{a:.2f}'} under {lo:.2f}"
        return
    if p.last_min_dist_bps > 0:
        dist = None if v.dist_bps is None else (v.dist_bps if o == "Up" else -v.dist_bps)
        if dist is None or dist < p.last_min_dist_bps - EPS:
            d.why["taker"] = (f"triangle: last minute, {o} only {'?' if dist is None else f'{dist:.1f}'} bps past the "
                              f"strike (needs {p.last_min_dist_bps:g})")
            return
    if p.last_max_price < 1.0 and a >= p.last_max_price - EPS:
        d.why["taker"] = f"triangle: last minute, {o} ask {a:.2f} not under {p.last_max_price:.2f}"
        return
    fee = v.market.fees.taker_fee
    limit = min(0.99, round(a + p.last_slippage, 2))
    if p.last_max_price < 1.0:
        limit = min(limit, round(p.last_max_price - v.market.tick, 2))     # no fill at the cap or over it
    unit = limit + fee(limit, 1.0)
    depth = sum(s for _, s in _levels(v, o, limit))
    budget = (p.last_max_usd - st.get("last_usd", 0.0)) if p.last_max_usd > 0 else float("inf")
    want = max(p.last_shares, v.market.min_size) if p.last_shares > 0 else depth
    size = _floor2(min(want, budget / unit, free / unit))
    if size < v.market.min_size - EPS or size * limit < 1.0:
        d.why["taker"] = f"triangle: last minute, {size:.2f} {o} possible (depth {depth:.0f}, free ${free:.2f})"
        return
    d.takes.append((o, limit, size, 0.0))
    d.tags[f"taker:{o}"] = "late"
    d.tri_kinds[o] = "last"
    d.why["taker"] = f"triangle: last minute, buy {size:.2f} {o} @ <= {limit:.2f} (ask {a:.2f})"


def _lock(v, p: TriParams, d, sh: dict, free: float, why: str) -> None:
    """Buy the side with fewer shares until both sides hold the same shares."""
    small = "Up" if sh["Up"] < sh["Down"] else "Down"
    gap = sh[OTH[small]] - sh[small]
    if gap < p.min_size - EPS:
        d.tri_update.update(locked=True, locking=False)
        d.why["taker"] = f"triangle: LOCKED ({why}); gap {gap:.2f} sh"
        return
    a = _ask(v, small)
    if a is None:
        return
    fee = v.market.fees.taker_fee
    limit = min(0.99, a + p.max_slippage)
    depth = sum(s for _, s in _levels(v, small, limit))
    size = _floor2(min(gap, p.max_order_size, depth, free / (limit + fee(limit, 1.0))))
    if size < v.market.min_size - EPS:
        d.why["taker"] = f"triangle: lock waits ({size:.2f} sh possible)"
        return
    d.takes.append((small, round(limit, 2), size, 0.0))
    d.tags[f"taker:{small}"] = "scalp_pair"
    d.tri_kinds[small] = "lock"
    d.tri_update["locking"] = True
    d.why["taker"] = f"triangle: lock, buy {size:.2f} {small} @ <= {limit:.2f} ({why}; gap {gap:.2f})"
