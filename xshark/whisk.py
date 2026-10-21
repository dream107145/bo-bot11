"""Whiskas-style bot (user, 2026-10-10): buy each rising token, and match it with the other token.

Each token runs on its own (both can buy at the same time); per token one order in flight:
  1. rise     the token's mid reaches a fixed step of `rise` (0.05) over its low (low + 0.05, + 0.10, ...),
              anywhere in the window, after the gap since its last rising buy (gap_ms; a step passed in the
              gap does not count) -> buy `shares` (60) of it. A rise while that token is still buying adds
              `shares` more.
              Optional: a BTC move of trig_bps within trig_ms (0 = off).
  2. balance  once a token's rising buys are over (none running, none filled for bal_delay_s), the falling
              token, sized on the live prices (user, 2026-10-10: "focus on the overall profit/loss: the rising
              side's profit high, the falling side's loss low, optimised with the live Up/Down prices and
              time"): a mean-variance hedge, n = D - (cost - p_f) / (bal_k * p_r * p_f), between 0 and D,
              where D = rising shares - falling shares, p_r / p_f the live mids, cost the falling ask + fee.
              It hedges more while the outcome is open (p near 0.5) and the hedge is cheap, less when the
              rising token is likely to win or the hedge is dear; 'if rising wins' kept >= bal_keep_usd.
              (Replayed on 1,909 windows: k 0.001 after 1 s = -36.30 a window, sd 57, worst 5% -145, vs the
              old 'profit ~ loss' rule -36.61, sd 79, worst 5% -182; a maker bid first was worse.)
  3. profit   a token at profit_at or over whose 'if it wins' is negative is bought up to break even
              (at most profit_max shares, once per token per window).
Every buy is a series of FAK tries (fill-and-kill: what is there at the limit fills, the rest is dropped):
limit = the best bid + limit_over (0.01), a try every retry_gap_ms until the buy has its shares (at most
retry_s); up to max_inflight tries per token may be on their way at once, each only for the shares no try
in flight covers (user, 2026-10-10: "several orders in flight"). Optional: rest_s > 0 rests what a try did
not get as a maker bid that long; only_above: tries go on only while the token is over where the buy started.
  4. ladder   resting bids on each token at the best bid and ladder_step under it (ladder_levels of
              ladder_shares), moved when the best bid is ladder_step or more from where the ladder was set,
              so a fast swing fills bids at several prices (user, 2026-10-10). Replayed on 1,909 windows
              (fills when the ask comes down to a bid): -2.8c a share at settlement, ~100 fills a window.
(Recorded books, 1,909 windows, cash fee: a 0.05 rise bought at once returned -1.2c a share.)
Only at asks min_price-max_price, from_s to until_s, within window_usd per window (0 = none; cash always).
Settings: the wk_* fields of config.Settings (console group "Whiskas bot")."""
from __future__ import annotations

import math
from dataclasses import dataclass, fields

OUT = ("Up", "Down")
OTH = {"Up": "Down", "Down": "Up"}
EPS = 1e-9
TAG = {"rise": "rise", "bal": "bal", "top": "top", "pair": "wpair"}     # a rise triggered by BTC is tagged "btc"


@dataclass
class WhiskParams:
    enabled: bool = False
    shares: float = 60.0
    rise: float = 0.05                    # rising buys at the token's low + rise, + 2 rise, ... (fixed steps)
    trig_bps: float = 0.0                 # > 0: also a BTC move of this many bps within trig_ms
    trig_ms: float = 500.0
    limit_over: float = 0.01              # every FAK try: limit = the best bid + this
    rest_s: float = 0.0                   # > 0: what a try did not get rests as a maker bid this long
    only_above: bool = False              # tries go on only while the token is over where the buy started
    retry_s: float = 30.0                 # a buy tries at most this long
    retry_gap_ms: float = 250.0
    min_price: float = 0.09
    max_price: float = 0.89
    from_s: float = 1.0
    until_s: float = 298.0
    cooldown_ms: float = 500.0            # per token, the least time between two rising buys (middle prices)
    gap_low_px: float = 0.20              # ... under this ask: gap_low_ms (user, 2026-10-10: "cheap token: a bit slower,
    gap_low_ms: float = 1000.0            #   dear token: quicker"; replayed on 1,909 windows: 1 s under 0.20, 0.5 s,
    gap_high_px: float = 0.80             #   0.25 s at 0.80+ lost least: -1.20c a share vs -1.21c at 0.5 s for all)
    gap_high_ms: float = 250.0
    bal_k: float = 0.001                  # the balance's risk weight: higher hedges more (0 = no balance)
    bal_keep_usd: float = 1.0             # the balance keeps 'if the rising token wins' at least this ($)
    bal_delay_s: float = 1.0              # the balance waits until the rising token has not filled for this long
    profit_at: float = 0.60
    profit_max: float = 120.0
    window_usd: float = 0.0               # 0 = no window cap (cash still limits)
    max_book_age_ms: float = 2000.0
    max_inflight: float = 3.0             # FAK tries of one token that may be on their way at once
    ladder_levels: float = 0.0            # resting bids per token (0 = no ladder)
    ladder_step: float = 0.03
    ladder_shares: float = 20.0
    pair_on: bool = False                 # pair mode (see _pair_trigger) instead of rising buys / balance / top-up
    pair_move: float = 0.03               # ... after an Up/Down mid move of this within pair_ms
    pair_ms: float = 1000.0
    pair_margin: float = 0.02             # ... the token with fewer shares while ask + fee + the other's average <= 1 - this
    pair_cd_ms: float = 3000.0            # ... the least time between two pair buys
    pair_free: bool = False               # ... every move, the side whose buy leaves the worse outcome best; no $1 limit
    rise_pair_sum: float = 0.0             # > 0: after a rising buy fills, maker-bid the other token so the pair is strictly under this

    @classmethod
    def from_settings(cls, s) -> "WhiskParams":
        return cls(**{f.name: getattr(s, "wk_" + f.name) for f in fields(cls)})


def _ask(v, o):
    return (v.books.get(o) or {}).get("ask")


def _bid(v, o):
    return (v.books.get(o) or {}).get("bid")


def _mid(v, o):
    b, a = _bid(v, o), _ask(v, o)
    return (a + b) / 2.0 if a is not None and b is not None else None


def _floor2(x: float) -> float:
    return math.floor(x * 100 + 1e-6) / 100


def _tick(v, o) -> float:
    return (getattr(v, "ticks", None) or {}).get(o) or v.market.tick


def _done(leg: dict, min_size: float) -> bool:
    return leg["goal"] - leg["got"] < min_size - EPS        # under the venue's minimum: nothing more to buy


def _room(v, p: WhiskParams, own_reserved: float = 0.0) -> float:
    if p.window_usd <= 0:
        return math.inf
    return p.window_usd - v.window_cost - (v.reserved - own_reserved)


def _ours(v, o: str) -> list:
    """Resting bids on o that are not ladder bids."""
    lad = set((v.whisk or {}).get("grid_ids") or [])
    return [r for r in v.resting.get(o, []) if r.id not in lad]


def _try(v, p: WhiskParams, d, st: dict, o: str, leg: dict, now: float) -> bool:
    """One FAK try for what the leg still needs and no try in flight covers: limit = the best bid + limit_over."""
    b, a = _bid(v, o), _ask(v, o)
    if b is None or a is None or not (p.min_price - EPS <= a <= p.max_price + EPS):
        return False
    limit = round(min(p.max_price, b + p.limit_over, leg.get("max_px", 1.0)), 4)
    if limit < p.min_price - EPS:
        return False
    pend = (v.in_flight_taker or {}).get(o, 0.0)
    n = _floor2(min(leg["goal"] - leg["got"] - pend, max(p.shares, v.market.min_size)))   # one try: at most `shares` (60)
    room = _room(v, p)
    if room < math.inf:
        n = min(n, _floor2(room / (limit + v.market.fees.taker_fee(limit, 1.0))))
    if n < v.market.min_size - EPS:
        d.why["taker"] = f"whiskas: {leg['kind']} {o} would pass the ${p.window_usd:.0f} window budget"
        return False
    d.takes.append((o, limit, n, 0.0))
    d.tags[f"taker:{o}"] = "btc" if str(leg.get("why", "")).startswith("BTC") else TAG[leg["kind"]]
    d.whisk_kinds[o] = f"{leg['kind']}:{leg['id']}"
    d.whisk_update[f"leg_{o}"] = {**leg, "last_try": now, "tries": leg.get("tries", 0) + 1, "rest_t0": now, "lim": limit}
    d.why["taker"] = (f"whiskas: {leg.get('why', leg['kind'])}: FAK {n:g} {o} @ <= {limit:.2f} (bid {b:.2f} / ask {a:.2f}; "
                      f"{leg['got']:g}/{leg['goal']:g}, try {leg.get('tries', 0) + 1})")
    return True


def bid_target(v, p: WhiskParams, d, sizes: dict) -> None:
    """rest_s > 0: what the last FAK try did not get rests as a maker bid (at its limit, under the ask) for
    rest_s (called from strategy._makers before the bids are reconciled)."""
    if not p.enabled or v.halted:
        return
    st = v.whisk or {}
    # Optional comeback pair: once a rising buy fills, rest the opposite token at
    # a price that keeps the two token prices strictly below rise_pair_sum.
    if p.rise_pair_sum > 0:
        for o in OUT:
            owed = st.get(f"pair_owed_{o}", 0.0)
            cost = st.get(f"pair_cost_{o}", 0.0)
            if owed < v.market.min_size - EPS or cost <= 0:
                continue
            avg_rise = cost / owed
            tick = _tick(v, o)
            cap = math.floor((p.rise_pair_sum - avg_rise - tick + EPS) / tick) * tick
            a = _ask(v, o)
            if a is not None:
                cap = min(cap, a - tick)
            px = round(cap, 4)
            if px < tick - EPS:
                continue
            d.targets[o] = px
            sizes[o] = max(v.market.min_size, _floor2(owed))
            d.tags[f"maker:{o}"] = "wpair"
            d.whisk_kinds[f"maker:{o}"] = f"pair:{o}"
            d.own_budget.add(o)
            d.why[f"maker_{o}"] = (f"whiskas: comeback pair bid {sizes[o]:g} {o} @ {px:.2f}; "
                                    f"rising-token average {avg_rise:.2f}, sum < {p.rise_pair_sum:.2f}")
    if p.rest_s <= 0:
        return
    for o in OUT:
        leg = st.get(f"leg_{o}")
        if not leg or leg.get("tries", 0) == 0 or _done(leg, v.market.min_size):
            continue
        if v.now_ms - leg.get("rest_t0", -1e18) >= p.rest_s * 1000.0 or (v.in_flight_taker or {}).get(o, 0.0) > EPS:
            continue
        a = _ask(v, o)
        if a is None:
            continue
        px = round(min(leg.get("lim", a), a - _tick(v, o), p.max_price), 4)
        if px < p.min_price - EPS:
            continue
        own = sum(r.price * r.remaining for r in _ours(v, o))
        n = _floor2(leg["goal"] - leg["got"])
        room = _room(v, p, own)
        if room < math.inf:
            n = min(n, _floor2(room / px))
        if n < v.market.min_size - EPS:
            continue
        d.targets[o] = px
        sizes[o] = n
        d.tags[f"maker:{o}"] = TAG[leg["kind"]]
        d.whisk_kinds[f"maker:{o}"] = f"{leg['kind']}:{leg['id']}"
        d.follow.add(o)
        d.own_budget.add(o)
        d.why[f"maker_{o}"] = f"whiskas: resting {n:g} {o} @ {px:.2f} ({leg['kind']} {leg['got']:g}/{leg['goal']:g})"


def _new_leg(v, d, st: dict, kind: str, o: str, goal: float, now: float, why: str) -> dict:
    n_id = d.whisk_update.get("legs", st.get("legs", 0)) + 1
    d.whisk_update["legs"] = n_id
    leg = dict(id=n_id, kind=kind, goal=round(goal, 2), got=0.0, t0=now, ref=_mid(v, o), last_try=-1e18, tries=0, why=why)
    d.whisk_update[f"leg_{o}"] = leg
    return leg


def gap_ms(p: WhiskParams, ask: float) -> float:
    """The least time between two rising buys of a token, by its ask: cheap -> longer, dear -> shorter."""
    if ask < p.gap_low_px - EPS:
        return p.gap_low_ms
    if ask >= p.gap_high_px - EPS:
        return p.gap_high_ms
    return p.cooldown_ms


def _levels(m: float, low: float, step: float) -> int:
    """How many whole steps `m` is over `low`."""
    return math.floor((m - low) / step + 1e-6)


def _rise_trigger(v, p: WhiskParams, d, st: dict, o: str, now: float):
    """Why `o` is a rising buy now ('' if it is not). Fixed steps of `rise` over the token's low (user,
    2026-10-12: "buy if the price rises 0.10, 0.15, 0.20 after the gaps, only fixed 0.05"): rising buys at
    low + 0.05, + 0.10, + 0.15 ..., each once. A step passed during the gap after a rising buy does not count
    (user, 2026-10-12), the next one up does. A fall of a whole step under a bought step makes it count again;
    a new low starts the steps from it. rlow_<o> = the low, rnext_<o> = the next step (1 = low + rise)."""
    m = _mid(v, o)
    a = _ask(v, o)
    if m is None or a is None:
        return ""
    step = p.rise
    low, k0 = st.get(f"rlow_{o}"), st.get(f"rnext_{o}", 1)
    k = k0
    if low is None or m < low - EPS:
        low, k = m, 1                                      # a new low: the steps start from it
    elif step > 0:
        k = min(k, max(1, math.ceil((m - low) / step - 1e-6) + 1))   # a whole step under a bought step: again
    gap = now - st.get(f"trig_ms_{o}", -1e18) < gap_ms(p, a)
    if gap and step > 0 and m >= low + k * step - EPS:
        k = _levels(m, low, step) + 1                      # passed during the gap: does not count
    why = ""
    if not gap and p.from_s <= v.secs_in <= p.until_s and p.min_price - EPS <= a <= p.max_price + EPS:
        if step > 0 and m >= low + k * step - EPS:
            why = f"{o} rose {m - low:.2f} ({low:.2f} -> {m:.2f})"
        else:
            mv = getattr(v, "wk_move_bps", None)
            if (p.trig_bps > 0 and mv is not None and abs(mv) >= p.trig_bps - EPS and (mv > 0) == (o == "Up")
                    and (v.spot_age_ms is None or v.spot_age_ms <= 1000.0)):
                why = f"BTC {mv:+.2f} bps"
        if why and step > 0:
            k = _levels(m, low, step) + 1                  # the next step over this one
    if low != st.get(f"rlow_{o}"):
        d.whisk_update[f"rlow_{o}"] = low
    if k != k0:
        d.whisk_update[f"rnext_{o}"] = k
    return why


def _pair_avg(v, o: str):
    """o's average price this window, fees included (None without shares)."""
    n = v.shares.get(o, 0.0)
    return (v.costs or {}).get(o, 0.0) / n if n > EPS else None


def _pair_max_px(v, p: WhiskParams, o: str):
    """The highest price at which o still pairs under 1 - pair_margin with the other token's average."""
    avg = _pair_avg(v, OTH[o])
    if avg is None:
        return None
    a = _ask(v, o) or 0.5
    return _floor2(1.0 - p.pair_margin - avg - v.market.fees.taker_fee(a, 1.0) + 1e-9)


def _pair_trigger(v, p: WhiskParams, d, st: dict, o: str, now: float, sh: dict, in_flight: dict):
    """Pair mode (user, 2026-10-12: x-MoneyForWhiskas, decided on the overall profit/loss after each order).
    Its 671 windows 10-10..10-12 (32,482 fills merged per side within 5 s): +2,586 after fees, of which the
    shares it held as Up+Down pairs bought under $1 made +4,475 and the extra shares on one side lost -1,889;
    about half its taker orders came within 2 s of an Up/Down move of 0.03+ within 1 s, on either token.
    So: after a move of pair_move within pair_ms -- with no shares yet, the token that fell; after that only
    the token with fewer shares, and only while its ask + fee + the other token's average <= 1 - pair_margin.
    One pair buy at a time, pair_cd_ms apart. (Replayed on those windows, a FAK at the ask 250 ms later, 60
    shares: -4 to -5 a window, worst 5% -36, vs -23 for always buying the worse side and -19..-27 for
    always the rising / falling token.)"""
    cur = lambda k: d.whisk_update[k] if k in d.whisk_update else st.get(k)
    mv = getattr(v, "wk_mid_move", None)
    a = _ask(v, o)
    if mv is None or a is None or abs(mv) < p.pair_move - EPS:
        return ""
    if now - (cur("pair_last_ms") or -1e18) < p.pair_cd_ms or cur("leg_Up") or cur("leg_Down"):
        return ""
    if any(in_flight.get(x, 0.0) > EPS for x in OUT) or _ours(v, o):
        return ""
    if not (p.from_s <= v.secs_in <= p.until_s) or not (p.min_price - EPS <= a <= p.max_price + EPS):
        return ""
    fell = "Down" if mv > 0 else "Up"
    if p.pair_free:                                    # (user, 2026-10-12: "like the trader": every move, no $1 limit)
        n, best = max(p.shares, v.market.min_size), None
        for x in OUT:
            ax = _ask(v, x)
            if ax is None or not (p.min_price - EPS <= ax <= p.max_price + EPS):
                continue
            c = n * (ax + v.market.fees.taker_fee(ax, 1.0))
            u2 = sh["Up"] + (n if x == "Up" else 0.0) - v.window_cost - c
            d2 = sh["Down"] + (n if x == "Down" else 0.0) - v.window_cost - c
            key = (round(min(u2, d2), 6), x == fell)
            if best is None or key > best[0]:
                best = (key, x, u2, d2)
        if best is None or best[1] != o:
            return ""
        return f"every move: {o} -> if Up {best[2]:+.2f} / if Down {best[3]:+.2f} after"
    if sh["Up"] + sh["Down"] < v.market.min_size - EPS:
        return f"start: {o} fell {abs(mv):.2f} in {p.pair_ms / 1000:g} s" if o == fell else ""
    few = "Up" if sh["Up"] < sh["Down"] - EPS else "Down" if sh["Down"] < sh["Up"] - EPS else fell
    avg = _pair_avg(v, OTH[o])
    if o != few or avg is None:
        return ""
    tot = a + v.market.fees.taker_fee(a, 1.0) + avg
    if tot > 1.0 - p.pair_margin + EPS:
        return ""
    return f"pair: {o} ask {a:.2f} + fee + {OTH[o]} avg {avg:.3f} = {tot:.3f} <= {1.0 - p.pair_margin:.2f}"


def decide(v, p: WhiskParams, d) -> None:
    """Add this tick's FAK tries (at most one per token) to the Decision ``d``."""
    st = v.whisk or {}
    if not p.enabled or v.halted or v.book_age_ms is None or v.book_age_ms > p.max_book_age_ms:
        return
    now, t_in = v.now_ms, v.secs_in
    sh = {o: max(0.0, v.shares.get(o, 0.0)) for o in OUT}
    ms = v.market.min_size
    in_flight = v.in_flight_taker or {}
    acted = []
    for o in OUT:
        leg = st.get(f"leg_{o}")
        if p.pair_on:                                      # 1'. pair mode: one pair buy at a time
            why = _pair_trigger(v, p, d, st, o, now, sh, in_flight)
            if why:
                d.whisk_update["pair_last_ms"] = now
                a = _ask(v, o)
                leg = _new_leg(v, d, st, "pair", o, max(p.shares, ms), now, why)
                free = p.pair_free or why.startswith("start")          # no $1 pair limit on this buy
                mx = a + p.limit_over if free else _pair_max_px(v, p, o)
                leg = {**leg, "max_px": round(mx, 4), "start": free}
                d.whisk_update[f"leg_{o}"] = leg
        else:
            why = _rise_trigger(v, p, d, st, o, now)
        if why and not p.pair_on:                          # 1. a rise: a new rising buy, or 60 more on the running one
            d.whisk_update[f"trig_ms_{o}"] = now
            if leg and not _done(leg, ms):
                leg = {**leg, "goal": round(leg["goal"] + p.shares, 2), "kind": "rise", "t0": now, "why": why}
                d.whisk_update[f"leg_{o}"] = leg
            else:
                leg = _new_leg(v, d, st, "rise", o, max(p.shares, ms), now, why)
        if not leg:
            continue
        if _done(leg, ms):
            d.whisk_update[f"leg_{o}"] = None
            continue
        if now - leg["t0"] > p.retry_s * 1000.0 or t_in > p.until_s + 1.5:
            d.whisk_update[f"leg_{o}"] = None
            acted.append(f"{leg['kind']} {o} stopped at {leg['got']:g}/{leg['goal']:g}")
            continue
        pend = in_flight.get(o, 0.0)
        if pend > EPS and (pend >= p.max_inflight * max(p.shares, ms) - EPS
                           or leg["goal"] - leg["got"] - pend < ms - EPS):
            acted.append(f"{leg['kind']} {o}: {pend:g} on the way")
            continue
        if p.only_above and leg.get("tries", 0) > 0 and (_mid(v, o) or 0.0) <= (leg.get("ref") or 0.0) + EPS:
            d.whisk_update[f"leg_{o}"] = None
            acted.append(f"{leg['kind']} {o} stopped: not over {leg.get('ref') or 0:.3f}")
            continue
        if p.rest_s > 0 and leg.get("tries", 0) > 0 and now - leg.get("rest_t0", -1e18) < p.rest_s * 1000.0:
            continue                                       # its bid rests (bid_target)
        if _ours(v, o):
            continue                                       # a resting bid on this token comes off first
        if leg["kind"] == "pair" and not leg.get("start") and leg.get("tries", 0) > 0:
            mx = _pair_max_px(v, p, o)                     # the pair price can move with the other token's fills
            if mx is None or mx < p.min_price - EPS:
                d.whisk_update[f"leg_{o}"] = None
                acted.append(f"pair {o} stopped at {leg['got']:g}/{leg['goal']:g}: no pair under ${1 - p.pair_margin:.2f}")
                continue
            if abs(mx - leg.get("max_px", mx)) > EPS:
                leg = {**leg, "max_px": mx}
        if now - leg["last_try"] >= p.retry_gap_ms:
            if leg["kind"] == "pair" and leg.get("tries", 0) > 0 and (_ask(v, o) or 1.0) > leg.get("max_px", 1.0) + EPS:
                d.whisk_update[f"leg_{o}"] = None              # the price ran away: no chasing
                acted.append(f"pair {o} stopped at {leg['got']:g}/{leg['goal']:g}: ask {(_ask(v, o) or 0):.2f} over {leg['max_px']:.2f}")
                continue
            _try(v, p, d, st, o, leg, now)

    # 2. balance: once r's rising buys are over, the other token f, so that the profit if r wins and the loss
    #    if f wins come out about the same (their sum ~ 0), 'if r wins' staying >= bal_keep_usd
    for f in (() if p.pair_on else OUT):
        r = OTH[f]
        if st.get(f"owe_{f}", 0.0) <= EPS:
            continue                                       # no rising fill of r since its last balance
        busy = lambda o: (d.whisk_update[f"leg_{o}"] if f"leg_{o}" in d.whisk_update else st.get(f"leg_{o}"))
        if busy(f) or busy(r) or in_flight.get(r, 0.0) > EPS:
            continue                                       # the rising token first: its buys have to be over
        if now - st.get(f"rise_fill_ms_{r}", -1e18) < p.bal_delay_s * 1000.0:
            continue
        d.whisk_update[f"owe_{f}"] = 0.0                   # this burst's balance is decided now
        b, a = _bid(v, f), _ask(v, f)
        if b is None or a is None or not (p.min_price - EPS <= a <= p.max_price + EPS) or t_in > p.until_s:
            continue
        mf, mr = _mid(v, f), _mid(v, r)
        if mf is None or mr is None or p.bal_k <= 0:
            continue
        per = a + v.market.fees.taker_fee(a, 1.0)                    # what a share costs (it fills at the ask)
        pr, pf = sh[r] - v.window_cost, sh[f] - v.window_cost        # if r wins / if f wins
        D = sh[r] - sh[f]
        var = max(mr * mf, 1e-4)
        n = min(D, D - max(0.0, per - mf) / (p.bal_k * var))         # the mean-variance hedge
        if pr > p.bal_keep_usd:
            n = min(n, (pr - p.bal_keep_usd) / per)                  # 'if r wins' stays >= bal_keep_usd
        n = _floor2(n)
        if n < ms - EPS:
            d.why["taker"] = (f"whiskas: no balance of {f} now: if {r} wins {pr:+.2f}, if {f} wins {pf:+.2f}, "
                              f"{f} at {mf:.2f} costs {per:.3f} a share")
            continue
        leg = _new_leg(v, d, st, "bal", f, n, now, f"balance ({r} rose)")
        if in_flight.get(f, 0.0) <= EPS and not _ours(v, f):
            _try(v, p, d, st, f, leg, now)

    # 3. a token at profit_at or over must not lose if it wins
    if not p.pair_on and p.profit_at > 0 and p.profit_max > 0 and t_in <= p.until_s:
        for o in OUT:
            m = _mid(v, o)
            if m is None or m < p.profit_at - EPS or st.get(f"topped_{o}"):
                continue
            d.whisk_update[f"topped_{o}"] = True
            pnl = sh[o] - v.window_cost
            a = _ask(v, o)
            if pnl >= 0 or a is None or st.get(f"leg_{o}") or d.whisk_update.get(f"leg_{o}") or _ours(v, o):
                continue
            per = 1.0 - a - v.market.fees.taker_fee(a, 1.0)
            if per <= EPS:
                continue
            n = min(p.profit_max, math.ceil(-pnl / per))
            if n >= ms - EPS:
                leg = _new_leg(v, d, st, "top", o, n, now, f"{o} at {m:.2f}, top up")
                if in_flight.get(o, 0.0) <= EPS:
                    _try(v, p, d, st, o, leg, now)

    # 4. the bid ladder on each token
    if p.ladder_levels >= 1 and p.ladder_shares >= ms - EPS:
        _ladder(v, p, d, st)

    if "taker" not in d.why or d.why["taker"].startswith("taker clips"):  # (the ladder sets no taker reason)
        if acted:
            d.why["taker"] = "whiskas: " + "; ".join(acted)
        elif not (p.from_s <= t_in <= p.until_s):
            d.why["taker"] = f"whiskas: buys run {p.from_s:.0f}-{p.until_s:.0f} s into the window; held to the end"
        elif p.pair_on:
            d.why["taker"] = (f"whiskas pair: waiting for a {p.pair_move:.2f} move within {p.pair_ms:.0f} ms" + (
                " (then the side whose buy leaves the worse outcome best)" if p.pair_free
                else f" (then the token with fewer shares, if it pairs under ${1 - p.pair_margin:.2f})"))
        else:
            d.why["taker"] = f"whiskas: waiting for a token to rise {p.rise:.2f}" + (
                f" or BTC to move {p.trig_bps:g} bps within {p.trig_ms:.0f} ms" if p.trig_bps > 0 else "")


def _ladder(v, p: WhiskParams, d, st: dict) -> None:
    """Resting bids on each token at the best bid and every ladder_step under it; the ladder is set again (its
    bids off, new ones on) once the best bid is ladder_step or more from where it was set. A filled level is
    not replaced until then."""
    ids = set(st.get("grid_ids") or [])
    open_t = v.secs_in >= p.from_s and v.secs_in <= p.until_s
    for o in OUT:
        mine = [r for r in v.resting.get(o, []) if r.id in ids]
        b, a = _bid(v, o), _ask(v, o)
        lad = st.get(f"lad_{o}") or {}
        if not open_t or b is None or a is None:
            d.grid_cancels += [r.id for r in mine if not r.cancelling]
            if lad:
                d.whisk_update[f"lad_{o}"] = None
            continue
        if lad and abs(b - lad.get("anchor", -9.0)) < p.ladder_step - EPS:
            continue                                       # still in place
        d.grid_cancels += [r.id for r in mine if not r.cancelling]
        tick = _tick(v, o)
        prices = []
        for k in range(int(p.ladder_levels)):
            px = round(b - k * p.ladder_step, 4)
            if p.min_price - EPS <= px <= p.max_price + EPS and px <= a - tick + EPS:
                prices.append(px)
        for px in prices:
            d.grid_places.append((o, px, _floor2(p.ladder_shares)))
        d.whisk_update[f"lad_{o}"] = {"anchor": b, "prices": prices}
