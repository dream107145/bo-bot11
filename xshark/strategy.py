"""The trader's BTC 5-minute playbook, as a pure decision function.

What x-MoneyForWhiskas does in every window (full activity log, BTC only):
it keeps buying BOTH outcomes from ~17 s after the open to ~3.5 minutes in,
two ways -- resting bids on Up and on Down that other traders' orders hit
(44 % of fills), and fixed-size taker clips the moment BTC ticks toward a side,
before the book reprices (56 %). It never sells, holds to settlement ~81 %
hedged, and pays ~$0.99 per Up+Down pair.

This module decides, from one ``View`` of the market, which bids to cancel
or place and which clips to send. It holds no state of its own and does no
I/O, so every rule is testable. Every "not now" carries a reason, because
"why is it not trading" is usually the real question on the console.

Fair value (computed by the bot, passed in ``View.fair_up``) is the market's
own Up mid ``anchor_s`` ago plus how far the TWAP model moved since: the
market sets the level (it beats every model on level), the spot feed supplies
the move the book has not priced yet.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import ladder, triangle, v2, v3, v4, v5, v6, whisk
from . import copytrade, ladder, triangle, v2, v3, v4, v5, v6, whisk
from .config import Settings
from .market import Market

EPS = 1e-9
OUTCOMES = ("Up", "Down")


def other(outcome: str) -> str:
    return "Down" if outcome == "Up" else "Up"


def on_tick(price: float, tick: float, up: bool = False) -> float:
    """``price`` rounded to the tick grid (down unless ``up``), as a clean float."""
    n = price / tick
    n = math.ceil(n - 1e-6) if up else math.floor(n + 1e-6)
    return round(n * tick, 4)


@dataclass(slots=True)
class Resting:
    id: int
    price: float
    remaining: float
    created_ms: float
    cancelling: bool = False


@dataclass
class View:
    now_ms: float
    market: Market
    secs_in: float
    secs_left: float
    fair_up: float | None
    trigger_z: float | None               # composite return over trigger_ms, in sigmas (+ = up)
    books: dict[str, dict]                # outcome -> {"bid","ask","bid_size","ask_size"}
    book_age_ms: float | None
    spot_age_ms: float | None
    shares: dict[str, float]              # outcome -> shares held this window
    window_cost: float                    # USDC spent this window (fees included)
    reserved: float                       # USDC committed to open orders this window
    resting: dict[str, list[Resting]]     # outcome -> our live/pending maker bids
    in_flight_taker: dict[str, float]     # outcome -> shares in clips not yet matched
    taker_count: int
    last_taker_ms: dict[str, float]
    last_quote_ms: dict[str, float]
    halted: str | None = None
    costs: dict[str, float] = field(default_factory=dict)   # outcome -> USDC paid for it this window
    last_fill_side: str | None = None                      # the outcome of this window's latest fill
    fees: dict[str, float] = field(default_factory=dict)    # outcome -> taker fees paid for it this window
    fav_side: str | None = None           # the first token this window to trade above fav_trigger
    dist_bps: float | None = None         # (price - strike) / strike in bps (+ = Up's side), fresh price
    dist_usd: float | None = None         # price - strike in $ (+ = Up's side), fresh price
    dist_z: float | None = None           # ln(price / strike) / (sigma * sqrt(seconds left)) (+ = Up's side)
    fav_shares: float = 0.0               # shares bought this window by favourite bids
    fav_dog_shares: float = 0.0           # shares bought this window by underdog-first bids
    late_shares: float = 0.0              # shares bought this window by last-minute favourite bids
    scalp: dict | None = None             # the open scalp cycle: side, price, shares (None = none open)
    scalp_closed_ms: float = -1e18        # when the last scalp cycle ended (cooldown)
    scalp_pairs: int = 0                  # scalp pairs completed this window
    fav_pair_shares: float = 0.0          # shares bought this window by the favourite's pair bid
    ticks: dict = field(default_factory=dict)   # outcome -> the venue's tick for that token (None = market's)
    tri: dict | None = None               # the triangle cycle's state this window (xshark/triangle.py)
    tri_params: object = None             # TriParams when the triangle strategy is on
    free_cash: float | None = None        # cash new orders may use (cash - what open orders hold)
    ladder: dict | None = None            # the pair ladder's state this window (xshark/ladder.py)
    ladder_params: object = None          # LadderParams when the pair ladder is on
    btc_move_bps: float | None = None     # composite move over tri btc_ms, in bps (+ = up)
    whisk: dict | None = None             # this window's Whiskas-bot state (see xshark/whisk.py)
    whisk_params: object = None           # WhiskParams when the Whiskas bot is on
    wk_move_bps: float | None = None      # composite move over wk trig_ms, in bps (+ = up)
    wk_mid_move: float | None = None      # the Up mid's move over wk pair_ms (pair mode)
    v2_params: object = None              # V2Params when Strategy V2 is on
    v2_fills: list = field(default_factory=list)   # this window's fills: outcome, price, size, fee, tag, ts, oid
    v2_up_low: float | None = None        # Up's lowest mid over v2 lookback_s
    v3_params: object = None              # V3Params when Strategy V3 is on
    v3_lows: dict = field(default_factory=dict)    # each token's lowest mid since the open within v3 lookback_s
    v4_params: object = None              # V4Params when Strategy V4 is on
    v4_up_hist: list = field(default_factory=list) # [(ts_ms, Up mid)] over the last ~10 s (V4's velocities)
    live: bool = False                    # a live (real-money) bot
    v5_params: object = None              # V5Params when Strategy V5 is on
    v6_params: object = None              # V6Params when Strategy V6 is on
    copy_fill: dict | None = None         # current target wallet trade being copied
    copy_done: bool = False
    copy_attempts: int = 0
    copy_last_attempt_ms: float = 0.0
    copy_maker_price: float | None = None
    copy_filled_shares: float = 0.0


@dataclass
class Decision:
    cancels: list[int] = field(default_factory=list)
    places: list[tuple[str, float, float]] = field(default_factory=list)        # outcome, price, size
    takes: list[tuple[str, float, float, float]] = field(default_factory=list)  # outcome, limit, size, edge
    targets: dict[str, float | None] = field(default_factory=dict)
    why: dict[str, str] = field(default_factory=dict)                         # maker_Up, maker_Down, taker
    pair_sides: set = field(default_factory=set)                              # orders that complete pairs
    fav_sides: set = field(default_factory=set)                               # favourite bids
    dog_sides: set = field(default_factory=set)                               # underdog-first bids
    late_sides: set = field(default_factory=set)                              # last-minute favourite bids
    tags: dict = field(default_factory=dict)                                  # "maker:Up" -> fill tag
    sells: list = field(default_factory=list)                                 # outcome, limit, size
    follow: set = field(default_factory=set)                                  # bids that follow the book
    exact: set = field(default_factory=set)                                   # bids requoted on any 1-tick change
    tri_kinds: dict = field(default_factory=dict)                             # outcome -> triangle order kind
    tri_update: dict = field(default_factory=dict)                            # triangle state changes
    ladder_update: dict = field(default_factory=dict)                         # pair ladder state changes
    ladder_kinds: dict = field(default_factory=dict)                          # outcome -> ladder leg (low/high)
    whisk_update: dict = field(default_factory=dict)                          # Whiskas-bot state changes (None = drop)
    whisk_kinds: dict = field(default_factory=dict)                           # outcome / "maker:<outcome>" -> "kind:leg id"
    own_budget: set = field(default_factory=set)                              # bids sized to their own budget (Whiskas bot)
    grid_places: list = field(default_factory=list)                           # Whiskas ladder bids to place: (outcome, price, size)
    grid_cancels: list = field(default_factory=list)                          # Whiskas ladder bids to cancel (order ids)
    ledger: list = field(default_factory=list)                                # V4 decision records for its ledger
    copy_done: bool = False                                                   # the copy is filled, stale, or exhausted


def decide(v: View, s: Settings) -> Decision:
    d = Decision()
    _makers(v, s, d)
    _taker(v, s, d)
    if s.scalp_enabled:
        _scalp_takes(v, s, d)
    if v.tri_params is not None and v.tri_params.enabled:
        triangle.decide(v, v.tri_params, d)
    if v.ladder_params is not None and v.ladder_params.enabled:
        ladder.decide(v, v.ladder_params, d)
    if v.whisk_params is not None and v.whisk_params.enabled:
        whisk.decide(v, v.whisk_params, d)
    if v.v2_params is not None and v.v2_params.enabled:
        v2.decide(v, v.v2_params, d)
    if v.v3_params is not None and v.v3_params.enabled:
        v3.decide(v, v.v3_params, d)
    if v.v4_params is not None and v.v4_params.enabled:
        v4.decide(v, v.v4_params, d)
    if v.v5_params is not None and v.v5_params.enabled:
        v5.decide(v, v.v5_params, d)
    if v.v6_params is not None and v.v6_params.enabled:
        v6.decide(v, v.v6_params, d)
    if s.copy_enabled:
        copytrade.decide(v, s, d)
    return d


#: a leg that has bought anything this window is done: the venue rounds fills (5 shares came
#: back as 4.5757 + 0.42 = 4.9957 on 2026-10-02 04:23 GMT+9, and "< 5" sent a second 5-share
#: bid that doubled the loss). One order per leg per window.
BOUGHT = 0.01


def _common_block(v: View, s: Settings, stop_left_s: float) -> str | None:
    if v.halted:
        return v.halted
    if v.fair_up is None:
        return "no fair value"
    if v.secs_in < s.start_after_s:
        return "waiting after the open"
    if v.secs_left <= stop_left_s:
        return "too close to the close"
    if v.book_age_ms is None:
        return "no book"
    if v.book_age_ms > s.max_book_age_ms:
        return "book stale"
    return None


def _excess(v: View, outcome: str) -> float:
    """Shares of ``outcome`` held beyond the other side (negative = short of it)."""
    return v.shares.get(outcome, 0.0) - v.shares.get(other(outcome), 0.0)


def _pair_need(v: View, s: Settings):
    """(held side, side to buy, unpaired shares, room per share) or None.

    Room is what one share of the other side may cost, fee included, so that a
    completed Up+Down pair costs at most ``pair_target`` and pays $1 either way."""
    if not s.pair_enabled:
        return None
    for held in OUTCOMES:
        ex = _excess(v, held)
        if ex >= 1.0 and v.shares.get(held, 0.0) > EPS:
            paid = v.costs.get(held, 0.0)
            if s.pair_on_price:
                # the rule on token prices alone: held price + other price < target, strictly
                avg = (paid - v.fees.get(held, 0.0)) / v.shares[held]
                return held, other(held), ex, s.pair_target - avg - 0.001
            return held, other(held), ex, s.pair_target - paid / v.shares[held]
    return None


def _too_early_for_bids(v: View, s: Settings) -> str | None:
    if v.secs_in < s.maker_start_s:
        return f"bids start {s.maker_start_s:.0f} s into the window"
    return None


def _pair_block(v: View, s: Settings) -> str | None:
    if v.halted:
        return v.halted
    if _too_early_for_bids(v, s):
        return _too_early_for_bids(v, s)
    if v.secs_left <= s.pair_stop_left_s:
        return "too close to the close"
    if v.book_age_ms is None or v.book_age_ms > s.max_book_age_ms:
        return "book stale"
    return None


def _pair_size(v: View, ex: float) -> float:
    return float(max(v.market.min_size, round(ex)))


def _makers(v: View, s: Settings, d: Decision) -> None:
    tick = v.market.tick
    block = "maker bids off" if not s.maker_enabled else \
        (_common_block(v, s, s.maker_stop_left_s) or _too_early_for_bids(v, s))
    fair = {"Up": v.fair_up, "Down": None if v.fair_up is None else 1.0 - v.fair_up}
    sizes = {o: max(s.maker_size, v.market.min_size) for o in OUTCOMES}

    for o in OUTCOMES:
        d.targets[o] = None
        if block:
            d.why[f"maker_{o}"] = block
            continue
        ex = _excess(v, o)
        if ex >= s.max_imbalance_shares:
            d.why[f"maker_{o}"] = f"holding {ex:.0f} more {o} than {other(o)}"
            continue
        if s.alternate_sides and v.last_fill_side == o:
            d.why[f"maker_{o}"] = f"alternating: next buy is {other(o)}"
            continue
        if s.max_window_shares > 0 and v.shares.get("Up", 0.0) + v.shares.get("Down", 0.0) \
                + max(s.maker_size, v.market.min_size) > s.max_window_shares + EPS:
            d.why[f"maker_{o}"] = f"{s.max_window_shares:.0f}-share cap reached"
            continue
        if s.maker_pull_sigma > 0 and v.trigger_z is not None:
            against = -v.trigger_z if o == "Up" else v.trigger_z
            if against >= s.maker_pull_sigma:
                # the book has not repriced yet: a bid left here is the one the ask falls through
                d.why[f"maker_{o}"] = f"pulled: spot moved {against:.1f} sigma against {o}"
                continue
        px = fair[o] - s.maker_edge - s.skew_per_10sh * ex / 10.0
        bk = v.books.get(o) or {}
        if bk.get("bid") is not None:
            px = min(px, bk["bid"] + s.maker_improve_ticks * tick)
        if bk.get("ask") is not None:
            px = min(px, bk["ask"] - tick)
        px = min(on_tick(px, tick), on_tick(s.max_price, tick))
        if px < s.min_price - EPS:
            d.why[f"maker_{o}"] = f"bid {px:.2f} under the {s.min_price:.2f} floor"
            continue
        d.targets[o] = px

    # both bids together stay under maker_pair_max: take the ticks off the heavier side first
    up, dn = d.targets["Up"], d.targets["Down"]
    if up is not None and dn is not None and up + dn > s.maker_pair_max + EPS:
        steps = math.ceil((up + dn - s.maker_pair_max) / tick - 1e-6)
        heavy = "Up" if _excess(v, "Up") >= 0 else "Down"
        light = other(heavy)
        cut_heavy = (steps + 1) // 2
        cut_light = steps - cut_heavy
        for o, cut in ((heavy, cut_heavy), (light, cut_light)):
            px = round(d.targets[o] - cut * tick, 4)
            if px < s.min_price - EPS:
                d.targets[o] = None
                d.why[f"maker_{o}"] = "pair cap pushed the bid under the floor"
            else:
                d.targets[o] = px

    # completing a pair: a resting bid on the side we are short of, at the price
    # that makes the pair cost <= pair_target (it then pays $1 whichever side wins)
    need = _pair_need(v, s) if s.pair_bid else None
    if need is not None and _pair_block(v, s) is None:
        held, buy, ex, room = need
        px = on_tick(room, tick)
        bk = v.books.get(buy) or {}
        if bk.get("ask") is not None:
            px = min(px, round(bk["ask"] - tick, 4))
        total = v.shares.get("Up", 0.0) + v.shares.get("Down", 0.0)
        if s.max_window_shares > 0 and total + _pair_size(v, ex) > s.max_window_shares + EPS:
            d.why[f"maker_{buy}"] = f"{s.max_window_shares:.0f}-share cap reached"
        elif px >= tick - EPS:
            if d.targets.get(buy) is None or px > d.targets[buy] + EPS:
                d.targets[buy] = px
                sizes[buy] = _pair_size(v, ex)
                d.pair_sides.add(buy)
                d.why[f"maker_{buy}"] = f"completing {ex:.0f} {held}: pair bid {px:.2f}"
        else:
            d.why[f"maker_{buy}"] = f"no pair possible: {held} cost {s.pair_target - room:.2f} a share"

    if s.fav_enabled:
        _fav(v, s, d, sizes)
        if s.fav_pair_enabled:
            _fav_pair(v, s, d, sizes)
    if s.late_enabled:
        _late(v, s, d, sizes)
    if s.scalp_enabled:
        _scalp_pair(v, s, d, sizes)
        if s.scalp_first_maker:
            _scalp_first_bid(v, s, d, sizes)
    if v.tri_params is not None and v.tri_params.enabled:
        triangle.open_bid_target(v, v.tri_params, d, sizes)
        triangle.pair_target(v, v.tri_params, d, sizes)
    if v.whisk_params is not None and v.whisk_params.enabled:
        whisk.bid_target(v, v.whisk_params, d, sizes)
    if v.v6_params is not None and v.v6_params.enabled:
        v6.bid_target(v, v.v6_params, d, sizes)

    # reconcile with the bids already out. A bid stays while it is within the
    # requote band of its target AND still at least half the edge under fair;
    # beyond that it goes at once (a bid left above a fair value that just fell
    # is the one that gets hit), while a raise waits out maker_min_requote_ms.
    committed = v.window_cost + v.reserved
    band = s.maker_requote_ticks * tick - EPS
    ladder_ids = set((v.whisk or {}).get("grid_ids") or [])         # the Whiskas ladder's bids are its own
    for o in OUTCOMES:
        tgt = d.targets[o]
        rest = [r for r in v.resting.get(o, []) if not r.cancelling and r.id not in ladder_ids]
        keep = None
        if tgt is not None and o in d.exact:
            # a bid that must sit exactly at its target (the triangle's rising pair bid): moved on every tick
            ok = [r for r in rest if abs(r.price - tgt) < tick / 2]
            keep = max(ok, key=lambda r: r.price) if ok else None
        elif tgt is not None and (d.tags.get(f"maker:{o}") == "scalp" or o in d.follow):
            # the scalp first bid follows the best bid: keep it within one tick, else move it
            ok = [r for r in rest if abs(r.price - tgt) < band]
            keep = max(ok, key=lambda r: r.price) if ok else None
        elif tgt is not None and (o in d.pair_sides or o in d.fav_sides or o in d.dog_sides or o in d.late_sides
                                  or d.tags.get(f"maker:{o}") in ("scalp_pair", "fav_pair", "wpair")):
            # a pair bid is priced off our own cost, not off fair: keep it unless it is above its target
            ok = [r for r in rest if tgt - band < r.price <= tgt + EPS]
            keep = max(ok, key=lambda r: r.price) if ok else None
        elif tgt is not None:
            ok = [r for r in rest if abs(r.price - tgt) < band and r.price <= fair[o] - s.maker_edge / 2 + EPS]
            keep = min(ok, key=lambda r: abs(r.price - tgt)) if ok else None
        stale = [r for r in rest if r is not keep]
        freed = sum(r.price * r.remaining for r in stale)
        if tgt is None or keep is not None:
            d.cancels += [r.id for r in stale]
            committed -= freed
            if keep is not None and not (o in d.pair_sides or o in d.fav_sides or o in d.dog_sides
                                         or o in d.late_sides or f"maker:{o}" in d.tags):
                d.why[f"maker_{o}"] = f"resting at {keep.price:.2f}"
            continue
        if stale and all(r.price < tgt - EPS for r in stale) \
                and v.now_ms - v.last_quote_ms.get(o, 0.0) < s.maker_min_requote_ms:
            d.why[f"maker_{o}"] = f"resting at {stale[0].price:.2f}, raising to {tgt:.2f} soon"
            continue
        d.cancels += [r.id for r in stale]
        committed -= freed
        if any(r.cancelling for r in v.resting.get(o, []) if r.id not in ladder_ids):
            # an older bid on this side is still being cancelled and can still fill: wait for it
            d.why[f"maker_{o}"] = f"waiting for the old {o} bid's cancel"
            continue
        size = sizes[o]
        if o not in d.own_budget and committed + tgt * size > s.max_window_usdc + EPS:
            d.targets[o] = None
            d.why[f"maker_{o}"] = f"window budget ${s.max_window_usdc:.0f} used"
            continue
        d.places.append((o, tgt, size))
        committed += tgt * size
        if not (o in d.pair_sides or o in d.fav_sides or o in d.dog_sides or o in d.late_sides
                or f"maker:{o}" in d.tags):
            d.why[f"maker_{o}"] = f"bidding {tgt:.2f}"


def _fav(v: View, s: Settings, d: Decision, sizes: dict) -> None:
    """A resting bid at fav_bid on the window's favourite, while BTC is far enough from the strike."""
    o = v.fav_side
    if o is None:
        return
    if _too_early_for_bids(v, s):
        for side in OUTCOMES:
            if d.targets.get(side) is None:
                d.why[f"maker_{side}"] = _too_early_for_bids(v, s)
        return
    if s.fav_dog_first:
        _fav_dog_first(v, s, d, sizes)
        return
    key = f"maker_{o}"
    if v.secs_in < s.fav_start_s:
        d.why[key] = f"favourite: bids start {s.fav_start_s:.0f} s into the window"
        return
    if d.targets.get(o) is not None:
        return                                        # a pair bid already has this side
    if v.fav_shares > BOUGHT:
        d.why[key] = f"favourite: bought {v.fav_shares:.2f} this window"
        return
    if v.halted:
        d.why[key] = v.halted
        return
    if v.secs_left <= s.fav_stop_left_s:
        d.why[key] = "favourite: too close to the close"
        return
    if v.book_age_ms is None or v.book_age_ms > s.max_book_age_ms:
        d.why[key] = "favourite: book stale"
        return
    if v.dist_bps is None:
        d.why[key] = "favourite: no fresh price or strike"
        return
    dist = v.dist_bps if o == "Up" else -v.dist_bps
    if dist < s.fav_min_dist_bps:
        d.why[key] = f"favourite: {dist:+.1f} bps from the strike, needs {s.fav_min_dist_bps:.1f}"
        return
    if s.fav_min_z > 0:
        if v.dist_z is None:
            d.why[key] = "favourite: no sigma yet"
            return
        zs = v.dist_z if o == "Up" else -v.dist_z
        if zs < s.fav_min_z:
            d.why[key] = f"favourite: {zs:+.2f}σ from the strike for the time left, needs {s.fav_min_z:.1f}σ"
            return
    total = v.shares.get("Up", 0.0) + v.shares.get("Down", 0.0)
    if s.max_window_shares > 0 and total + s.fav_size > s.max_window_shares + EPS:
        d.why[key] = f"{s.max_window_shares:.0f}-share cap reached"
        return
    ask = (v.books.get(o) or {}).get("ask")
    if s.fav_bid_follow:
        tick = v.market.tick
        bid = (v.books.get(o) or {}).get("bid")
        if bid is None or ask is None:
            d.why[key] = "favourite: no two-sided book"
            return
        px = on_tick(min(bid + tick, ask - tick, s.fav_bid), tick)
        if px < s.fav_bid_min - EPS:
            d.why[key] = f"favourite: {o} at {bid:.2f}/{ask:.2f}, under the {s.fav_bid_min:.2f}-{s.fav_bid:.2f} range"
            return
        d.follow.add(o)
    else:
        px = on_tick(s.fav_bid, v.market.tick)
        if ask is not None and ask <= px + EPS and not v.resting.get(o):
            d.why[key] = f"favourite: ask {ask:.2f} is at the bid; a bid would cross"
            return
    d.targets[o] = px
    sizes[o] = s.fav_size
    d.fav_sides.add(o)
    d.why[key] = f"favourite: bid {px:.2f} ({dist:+.1f} bps from the strike)"


def _on_grid(px: float, tick: float) -> bool:
    return abs(px / tick - round(px / tick)) < 1e-6


def _fav_pair(v: View, s: Settings, d: Decision, sizes: dict) -> None:
    """After a favourite fill: a bid on the other token at fav_pair_price for the same shares, to the close."""
    fav = v.fav_side
    if fav is None or v.fav_shares <= BOUGHT or v.fav_pair_shares > BOUGHT:
        return
    o = other(fav)
    key = f"maker_{o}"
    if d.targets.get(o) is not None:
        return
    if v.halted:
        d.why[key] = v.halted
        return
    if v.book_age_ms is None or v.book_age_ms > s.max_book_age_ms:
        d.why[key] = "favourite pair: book stale"
        return
    px = round(s.fav_pair_price, 4)
    tick = v.ticks.get(o) or v.market.tick
    if not _on_grid(px, tick):
        d.why[key] = f"favourite pair: {px:.3f} waits for {o}'s 0.001 tick (it trades under ~0.04)"
        return
    ask = (v.books.get(o) or {}).get("ask")
    if ask is not None and ask <= px + EPS and not v.resting.get(o):
        d.why[key] = f"favourite pair: {o} ask {ask:.3f} is at the bid; a bid would cross"
        return
    d.targets[o] = px
    sizes[o] = max(v.market.min_size, math.ceil(v.fav_shares * 100 - 1e-6) / 100)
    d.tags[f"maker:{o}"] = "fav_pair"
    d.why[key] = f"favourite pair: bid {px:.3f} on {o} for {sizes[o]:.2f} sh (pays $1 either way if filled)"


def _fav_dog_first(v: View, s: Settings, d: Decision, sizes: dict) -> None:
    """Underdog first: a bid at fav_dog_bid (0.05) on the token that is NOT the favourite; once it
    has filled, a bid at fav_bid (0.90) on the favourite. Both filled: a 0.95 pair paying $1."""
    fav = v.fav_side
    dog = other(fav)
    tick = v.market.tick
    stale = v.book_age_ms is None or v.book_age_ms > s.max_book_age_ms
    if v.fav_dog_shares <= BOUGHT:
        key = f"maker_{dog}"
        if d.targets.get(dog) is not None:
            return                                    # a pair bid already has this side
        if v.halted or v.secs_left <= s.fav_stop_left_s or stale:
            d.why[key] = v.halted or ("underdog: book stale" if stale else "underdog: too close to the close")
            return
        total = v.shares.get("Up", 0.0) + v.shares.get("Down", 0.0)
        if s.max_window_shares > 0 and total + s.fav_size > s.max_window_shares + EPS:
            d.why[key] = f"{s.max_window_shares:.0f}-share cap reached"
            return
        px = on_tick(s.fav_dog_bid, tick)
        ask = (v.books.get(dog) or {}).get("ask")
        if ask is not None and ask <= px + EPS and not v.resting.get(dog):
            d.why[key] = f"underdog: ask {ask:.2f} is at the bid; a bid would cross"
            return
        d.targets[dog] = px
        sizes[dog] = s.fav_size
        d.dog_sides.add(dog)
        d.why[key] = f"underdog first: bid {px:.2f}, then {s.fav_bid:.2f} on {fav}"
        return
    # the underdog is bought: complete the pair with the favourite at fav_bid
    key = f"maker_{fav}"
    if v.fav_shares > BOUGHT:
        d.why[key] = f"pair done: {dog} {s.fav_dog_bid:.2f} + {fav} {s.fav_bid:.2f}"
        return
    if v.halted or v.secs_left <= s.pair_stop_left_s or stale:
        d.why[key] = v.halted or ("favourite: book stale" if stale else "favourite: too close to the close")
        return
    px = on_tick(s.fav_bid, tick)
    ask = (v.books.get(fav) or {}).get("ask")
    if ask is not None and ask <= px + EPS and not v.resting.get(fav):
        px = round(ask - tick, 4)                     # the favourite fell through 0.90: bid just under its ask
    if px < tick - EPS:
        return
    d.targets[fav] = px                               # replaces the generic pair bid on this side
    sizes[fav] = s.fav_size
    d.pair_sides.discard(fav)
    d.fav_sides.add(fav)
    d.why[key] = f"underdog bought: bid {px:.2f} on {fav} to complete the pair"


def _late(v: View, s: Settings, d: Decision, sizes: dict) -> None:
    """Last minute: a bid at late_bid on the token trading above late_trigger, held to settlement."""
    if v.secs_left > s.late_left_s or v.secs_left <= 0:
        return
    tops = {o: v.books.get(o) or {} for o in OUTCOMES}
    mids = {o: (t["bid"] + t["ask"]) / 2.0 for o, t in tops.items()
            if t.get("bid") is not None and t.get("ask") is not None and t["ask"] - t["bid"] <= 0.05 + EPS}
    o = max(mids, key=mids.get) if mids else None
    if o is None or mids[o] <= s.late_trigger + EPS:
        if o is not None and d.targets.get(o) is None:
            d.why[f"maker_{o}"] = f"last minute: no token above {s.late_trigger:.2f}"
        return
    key = f"maker_{o}"
    if d.targets.get(o) is not None:
        return
    if v.late_shares > BOUGHT:
        d.why[key] = f"last minute: bought {v.late_shares:.2f}, holding to settlement"
        return
    if v.halted:
        d.why[key] = v.halted
        return
    if v.book_age_ms is None or v.book_age_ms > s.max_book_age_ms:
        d.why[key] = "last minute: book stale"
        return
    if s.late_min_dist_bps > 0:
        if v.dist_bps is None:
            d.why[key] = "last minute: no fresh price or strike"
            return
        dist = v.dist_bps if o == "Up" else -v.dist_bps
        if dist < s.late_min_dist_bps:
            d.why[key] = f"last minute: {dist:+.1f} bps from the strike, needs {s.late_min_dist_bps:.1f}"
            return
    total = v.shares.get("Up", 0.0) + v.shares.get("Down", 0.0)
    if s.max_window_shares > 0 and total + s.late_size > s.max_window_shares + EPS:
        d.why[key] = f"{s.max_window_shares:.0f}-share cap reached"
        return
    if s.late_bid_follow:
        tick = v.market.tick
        b, a = tops[o].get("bid"), tops[o].get("ask")
        px = on_tick(min(b + tick, a - tick, s.late_bid), tick)      # mids[o] exists: both sides do
        if px < s.late_bid_min - EPS:
            d.why[key] = f"last minute: {o} at {b:.2f}/{a:.2f}, under the {s.late_bid_min:.2f}-{s.late_bid:.2f} range"
            return
        d.follow.add(o)
    else:
        px = on_tick(s.late_bid, v.market.tick)
        if tops[o].get("ask") is not None and tops[o]["ask"] <= px + EPS and not v.resting.get(o):
            d.why[key] = f"last minute: ask {tops[o]['ask']:.2f} is at the bid; a bid would cross"
            return
    d.targets[o] = px
    sizes[o] = s.late_size
    d.late_sides.add(o)
    d.why[key] = f"last minute: bid {px:.2f} on {o} (mid {mids[o]:.3f}), hold to settlement"


def _scalp_pair(v: View, s: Settings, d: Decision, sizes: dict) -> None:
    """Scalp step 2: while a cycle is open, a pair bid on the other token at < pair_sum - first price."""
    c = v.scalp
    if not c or not s.scalp_pair_enabled:
        return
    other_side = other(c["side"])
    key = f"maker_{other_side}"
    if d.targets.get(other_side) is not None:
        return
    if s.scalp_pair_until_s > 0 and v.secs_in > s.scalp_pair_until_s:
        d.why[key] = f"scalp: pair bids end {s.scalp_pair_until_s:.0f} s into the window; holding"
        return
    if v.halted:
        d.why[key] = v.halted
        return
    if v.book_age_ms is None or v.book_age_ms > s.max_book_age_ms:
        d.why[key] = "scalp: book stale"
        return
    tick = v.market.tick
    rule = round(math.floor(round((s.scalp_pair_sum - c["price"] - 0.001) / tick, 6)) * tick, 4)
    left = c["shares"] - c.get("pair", 0.0)              # first-buy shares not paired yet
    if left <= 0.01:
        return
    # a pair bid already resting at a price that keeps the rule stays, partly filled or not: its
    # unfilled rest is the pair's rest (the venue's 5-share minimum forbids re-posting a stub)
    keep = [r for r in v.resting.get(other_side, []) if not r.cancelling and r.price <= rule + EPS]
    if keep:
        px = max(r.price for r in keep)
    else:
        px = rule
        ask = (v.books.get(other_side) or {}).get("ask")
        if ask is not None:
            px = min(px, round(ask - tick, 4))
        px = round(px, 4)
    if px < tick - EPS:
        d.why[key] = f"scalp: no pair under {s.scalp_pair_sum:.2f} after {c['side']} {c['price']:.2f}"
        return
    d.targets[other_side] = px
    sizes[other_side] = max(v.market.min_size, math.ceil(left * 100 - 1e-6) / 100)
    d.tags[f"maker:{other_side}"] = "scalp_pair"
    d.why[key] = (f"scalp: pair bid {px:.2f} for {left:.2f} sh ({c['side']} {c['price']:.2f} + {px:.2f} < "
                  f"{s.scalp_pair_sum:.2f}); hedge if {other_side} reaches {s.scalp_hedge_price:.2f}")


def hedge_shares(s: Settings, fees, first_price: float, first_shares: float, ask: float) -> float:
    """The hedge size. With scalp_hedge_profit > 0: the fewest shares N at ``ask`` with
    N * (1 - ask - fee per share) >= first cost (with its fee) + scalp_hedge_profit."""
    if s.scalp_hedge_profit <= 0:
        return float(s.scalp_hedge_size)
    need = first_price * first_shares + fees.taker_fee(first_price, first_shares) + s.scalp_hedge_profit
    per = 1.0 - ask - fees.taker_fee(ask, 1.0)
    if per <= EPS:
        return math.inf
    return float(max(5, math.ceil(need / per - 1e-9)))


def _scalp_z_ok(v: View, s: Settings, o: str) -> bool:
    """The first buy's z filter: the coin at least scalp_min_z sigmas past the strike on o's side."""
    if s.scalp_min_z <= 0:
        return True
    if v.dist_z is None:
        return False
    return (v.dist_z if o == "Up" else -v.dist_z) >= s.scalp_min_z - EPS


def _scalp_new_cycle_block(v: View, s: Settings) -> str | None:
    if s.scalp_stop_after_pair and v.scalp_pairs > 0:
        return "scalp: a pair completed this window; no new cycle"
    if v.secs_in < s.start_after_s or v.secs_left <= s.scalp_stop_left_s:
        return "scalp: outside the time for new cycles"
    if v.secs_in < s.scalp_first_after_s:
        return f"scalp: first buys from {s.scalp_first_after_s:.0f} s into the window"
    if s.scalp_first_within_s > 0 and v.secs_in > s.scalp_first_within_s:
        return f"scalp: first buys only in the first {s.scalp_first_within_s:.0f} s"
    if v.now_ms - v.scalp_closed_ms < s.scalp_cooldown_s * 1000.0:
        return "scalp: cooling down"
    return None


def _scalp_first_bid(v: View, s: Settings, d: Decision, sizes: dict) -> None:
    """Scalp step 1 as a maker bid: one tick over the best bid (under the ask) on the token priced in range."""
    if v.scalp or v.halted or v.book_age_ms is None or v.book_age_ms > s.max_book_age_ms:
        return
    if s.scalp_first_take_after_s > 0 and v.secs_in >= s.scalp_first_take_after_s:
        return                                        # past the maker phase: _scalp_takes takes the ask
    if _scalp_new_cycle_block(v, s):
        d.why["taker"] = _scalp_new_cycle_block(v, s)
        return
    tick, fees = v.market.tick, v.market.fees
    best = None
    for o in OUTCOMES:
        t = v.books.get(o) or {}
        b, a = t.get("bid"), t.get("ask")
        if b is None or a is None or b > s.scalp_max + EPS or a < s.scalp_min - EPS:
            continue
        if not _scalp_z_ok(v, s, o):
            continue                                  # that token is not priced in the range
        px = round(min(b + tick, a - tick), 4)
        px = min(px, s.scalp_max)
        if s.scalp_min - EPS <= px and d.targets.get(o) is None:
            if best is None or px > best[1]:
                best = (o, px)
    if best is None:
        d.why["taker"] = f"scalp: no token to bid at {s.scalp_min:.2f}-{s.scalp_max:.2f}"
        return
    o, px = best
    size = float(s.scalp_size)
    hmax = min(s.scalp_hedge_max + s.scalp_take_slip, 0.99)
    worst = hedge_shares(s, fees, px, size, hmax) if s.scalp_hedge_enabled else 0.0
    own = sum(r.price * r.remaining for r in v.resting.get(o, []))
    if v.window_cost + v.reserved - own + size * px + worst * hmax > s.max_window_usdc + EPS:
        d.why["taker"] = f"scalp: a new cycle (with its hedge) would pass the ${s.max_window_usdc:.0f} window budget"
        return
    d.targets[o] = px
    sizes[o] = size
    d.tags[f"maker:{o}"] = "scalp"
    d.why[f"maker_{o}"] = f"scalp: first-buy bid {px:.2f} (maker)"
    d.why["taker"] = f"scalp: waiting for the {o} bid {px:.2f} to fill"


def _scalp_takes(v: View, s: Settings, d: Decision) -> None:
    """Scalp steps 1 and 3: the first buy (when no cycle is open) and the hedge stop-buy."""
    if v.halted or v.book_age_ms is None or v.book_age_ms > s.max_book_age_ms:
        return
    if any(x > EPS for x in v.in_flight_taker.values()):
        return                                        # one buy at a time
    fees, tick = v.market.fees, v.market.tick
    committed = v.window_cost + v.reserved
    c = v.scalp
    if c and s.scalp_stop_price > 0:                  # stop-loss on the first buy
        b = (v.books.get(c["side"]) or {}).get("bid")
        held = math.floor(v.shares.get(c["side"], 0.0) * 100 + 1e-6) / 100
        if b is not None and b < s.scalp_stop_price - EPS and held > 0:
            limit = max(0.01, round(b - 0.02, 2))
            d.sells.append((c["side"], limit, held))
            d.why["taker"] = f"scalp: {c['side']} bid {b:.2f} under {s.scalp_stop_price:.2f}: sell {held:.2f} sh"
            return
    if c:                                             # step 3: hedge when the other token rises
        o = other(c["side"])
        ask = (v.books.get(o) or {}).get("ask")
        left = max(0.0, c["shares"] - c.get("pair", 0.0))   # only the unpaired part is at risk
        if not s.scalp_hedge_enabled:
            d.why["taker"] = f"scalp: holding {c['side']} {c['shares']:.0f} @ {c['price']:.2f} to settlement"
            return
        if s.scalp_hedge_after_s > 0 and v.secs_in < s.scalp_hedge_after_s:
            d.why["taker"] = (f"scalp: cycle open ({c['side']} {c['shares']:.0f} @ {c['price']:.2f}); "
                              f"hedge only after {s.scalp_hedge_after_s:.0f} s")
            return
        if ask is not None and left > 0.01 and s.scalp_hedge_price - EPS <= ask <= s.scalp_hedge_max + EPS \
                and v.now_ms - v.last_taker_ms.get(o, -1e18) >= s.taker_cooldown_ms:
            limit = round(min(ask + s.scalp_take_slip, 0.99), 4)
            size = hedge_shares(s, fees, c["price"], left, limit)       # sized for the worst fill
            # the cycle's own pair bid on that side is cancelled once the hedge fills: not money in use
            own = sum(r.price * r.remaining for r in v.resting.get(o, []))
            if not math.isfinite(size) or committed - own + size * limit + fees.taker_fee(limit, size) > s.max_window_usdc + EPS:
                d.why["taker"] = f"scalp: hedge would pass the ${s.max_window_usdc:.0f} window budget"
                return
            d.takes.append((o, limit, size, 0.0))
            d.tags[f"taker:{o}"] = "scalp_hedge"
            d.why["taker"] = f"scalp: {o} rose to {ask:.2f}: hedge {size:.0f} sh (first {c['side']} {c['price']:.2f})"
        else:
            d.why["taker"] = f"scalp: cycle open ({c['side']} {c['shares']:.0f} @ {c['price']:.2f})"
        return
    taker_phase = s.scalp_first_take_after_s > 0 and v.secs_in >= s.scalp_first_take_after_s
    if s.scalp_first_maker and not taker_phase:       # the first buy is a resting bid (_scalp_first_bid)
        bids = [(k[6:], d.targets.get(k[6:])) for k, t in d.tags.items() if t == "scalp" and k.startswith("maker:")]
        d.why["taker"] = (_scalp_new_cycle_block(v, s)
                          or (f"scalp: waiting for the {bids[0][0]} bid {bids[0][1]:.2f} to fill" if bids
                              else f"scalp: no token to bid at {s.scalp_min:.2f}-{s.scalp_max:.2f}"))
        return
    if _scalp_new_cycle_block(v, s):
        d.why["taker"] = _scalp_new_cycle_block(v, s)
        return
    best = None
    for o in OUTCOMES:                                # step 1: the token priced scalp_min..scalp_max
        ask = (v.books.get(o) or {}).get("ask")
        if ask is not None and s.scalp_min - EPS <= ask <= s.scalp_max + EPS \
                and v.now_ms - v.last_taker_ms.get(o, -1e18) >= s.taker_cooldown_ms and _scalp_z_ok(v, s, o):
            if best is None or ask > best[1]:
                best = (o, ask)
    if best is None:
        zs = f" with z >= {s.scalp_min_z:.2f}" if s.scalp_min_z > 0 else ""
        d.why["taker"] = f"scalp: no token at {s.scalp_min:.2f}-{s.scalp_max:.2f}{zs}"
        return
    o, ask = best
    if any(v.resting.get(x) for x in OUTCOMES):
        # the maker first bid is still out (or being cancelled): it could still fill -- wait for it
        d.why["taker"] = "scalp: waiting for the first-buy bid's cancel before taking the ask"
        return
    size = float(s.scalp_size)
    limit = round(min(ask + s.scalp_take_slip, 0.99), 4)
    # room for the dearest hedge the cycle may need (it can fire anywhere up to scalp_hedge_max)
    hmax = min(s.scalp_hedge_max + s.scalp_take_slip, 0.99)
    worst = hedge_shares(s, fees, limit, size, hmax) if s.scalp_hedge_enabled else 0.0
    reserve = size * limit + fees.taker_fee(limit, size) + worst * hmax
    if committed + reserve > s.max_window_usdc + EPS:
        d.why["taker"] = f"scalp: a new cycle (with its hedge) would pass the ${s.max_window_usdc:.0f} window budget"
        return
    d.takes.append((o, limit, size, 0.0))
    d.tags[f"taker:{o}"] = "scalp"
    d.why["taker"] = f"scalp: buy {o} {size:.0f} @ <= {limit:.2f} (ask {ask:.2f})"


def _pair_take(v: View, s: Settings, d: Decision) -> bool:
    """Buy the other side's ask now when it completes a pair at <= pair_target."""
    need = _pair_need(v, s) if s.pair_take else None
    if need is None or _pair_block(v, s) is not None:
        return False
    held, buy, ex, room = need
    if v.now_ms - v.last_taker_ms.get(buy, -1e18) < s.taker_cooldown_ms:
        return False
    ask = (v.books.get(buy) or {}).get("ask")
    fees, tick = v.market.fees, v.market.tick
    if ask is None or ask + fees.taker_fee(ask, 1.0) > room + EPS:
        return False
    limit = ask
    while True:
        nxt = round(limit + tick, 4)
        if nxt > 0.99 + EPS or nxt + fees.taker_fee(nxt, 1.0) > room + EPS:
            break
        limit = nxt
    size = _pair_size(v, ex)
    if size * limit < 1.0:
        size = float(math.ceil(1.0 / limit))
    cost = size * limit + fees.taker_fee(limit, size)
    if v.window_cost + v.reserved + cost > s.max_window_usdc + EPS:
        return False
    d.takes.append((buy, limit, size, room - ask - fees.taker_fee(ask, 1.0)))
    d.pair_sides.add(buy)
    d.why["taker"] = f"completing {ex:.0f} {held}: buy {buy} <= {limit:.2f} (pair <= {s.pair_target:.2f})"
    return True


def _taker(v: View, s: Settings, d: Decision) -> None:
    if _pair_take(v, s, d):
        return
    block = "taker clips off" if not s.taker_enabled else _common_block(v, s, s.taker_stop_left_s)
    if block is None and (v.spot_age_ms is None or v.spot_age_ms > s.max_spot_age_ms):
        block = "spot stale"
    if block is None and v.taker_count >= s.taker_max_per_window:
        block = f"{s.taker_max_per_window} clips this window"
    if block:
        d.why["taker"] = block
        return
    if s.trigger_sigma > 0:
        z = v.trigger_z
        if z is None:
            d.why["taker"] = "no spot move measured yet"
            return
        if z >= s.trigger_sigma:
            sides = ["Up"]
        elif z <= -s.trigger_sigma:
            sides = ["Down"]
        else:
            d.why["taker"] = f"waiting for a move ({z:+.1f} sigma)"
            return
    else:
        sides = list(OUTCOMES)

    tick, fees = v.market.tick, v.market.fees
    fair = {"Up": v.fair_up, "Down": 1.0 - v.fair_up}
    committed = v.window_cost + v.reserved
    why = "no edge"
    held = v.shares.get("Up", 0.0) + v.shares.get("Down", 0.0) + sum(v.in_flight_taker.values())
    for o in sides:
        if s.alternate_sides and v.last_fill_side == o:
            why = f"alternating: next buy is {other(o)}"
            continue
        if s.max_window_shares > 0 and held + v.market.min_size > s.max_window_shares + EPS:
            why = f"{s.max_window_shares:.0f}-share cap reached"
            continue
        if v.now_ms - v.last_taker_ms.get(o, -1e18) < s.taker_cooldown_ms:
            why = f"{o} cooling down"
            continue
        ask = (v.books.get(o) or {}).get("ask")
        if ask is None:
            why = f"no {o} ask"
            continue
        if ask < s.min_price - EPS or ask > s.max_price + EPS:
            why = f"{o} ask {ask:.2f} out of range"
            continue
        edge = fair[o] - ask - fees.taker_fee(ask, 1.0)
        if edge < s.taker_edge - EPS:
            why = f"{o} edge {edge:+.3f} under {s.taker_edge:.3f}"
            continue
        # the highest limit that still clears the edge after its own fee
        limit = ask
        while True:
            nxt = round(limit + tick, 4)
            if nxt > s.max_price + EPS or fair[o] - nxt - fees.taker_fee(nxt, 1.0) < s.taker_edge - EPS:
                break
            limit = nxt
        size = math.floor(min(s.clip_shares, s.clip_usdc_max / limit))
        if s.max_window_shares > 0:
            size = min(size, math.floor(s.max_window_shares - held + EPS))
        if size < v.market.min_size:
            why = f"clip under {v.market.min_size:.0f} shares at {limit:.2f}"
            continue
        if size * limit < 1.0:                       # the venue's $1 floor on marketable buys
            why = "clip under $1"
            continue
        ex = _excess(v, o) + v.in_flight_taker.get(o, 0.0)
        if ex + size > s.max_imbalance_shares + EPS:
            why = f"holding {ex:.0f} more {o} than {other(o)}"
            continue
        cost = size * limit + fees.taker_fee(limit, size)
        if committed + cost > s.max_window_usdc + EPS:
            why = f"window budget ${s.max_window_usdc:.0f} used"
            continue
        d.takes.append((o, limit, float(size), edge))
        d.why["taker"] = f"clip {o} {size} @ <= {limit:.2f} (edge {edge:+.3f})"
        return
    d.why["taker"] = why
