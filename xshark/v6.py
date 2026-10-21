"""Strategy V6: run and hedge (user, 2026-10-14: "I build new bot"). Paper first: no order in a live bot unless live_ok.

  IDLE   a token's mid rise (0.05) over its low (since the open, or since the last hedge) -> buy start_shares (10) of it:
         it is the run token. After a hedge only the hedge token may start the next run (next_any: either), and
         only step_s (6 s) after the hedge filled.
  RUN    at any moment the opposite token's ask + the run's average fill price < quick_pair (0.95) -> HEDGE at once,
         as a taker, its limit capped under that pair (user, 2026-10-14);
         step_s (6 s) after each run fill: the run token's mid not fallen (>= its mid at that buy - fall_th 0.01)
         -> more: the first add add1_shares (8), the second add2_shares (6), every later one add_shares (5)
         (user, 2026-10-14); fallen -> HEDGE.
  HEDGE  the opposite token, as many shares as the run bought x hedge_mult, x hedge_dear (1.2) when its ask is over the
         run's average fill price, x hedge_cheap (0.8) when under it (user, 2026-10-14), in orders of at most max_order,
         until done (what is left under the venue minimum is dropped) -> IDLE, the hedge token next.
  FAVOURITE (user, 2026-10-14) any time, holding shares or not: the dearer token's mid over fav_px (0.80) and 'if it wins' not over
         fav_margin (0) -> buy it, as a taker at once, the shares (through the depth, fees in) that make it positive,
         fav_min..fav_max (5-15) an order, one after another until it is; only while BTC is fav_min_dist ($50) or
         more from the strike on that token's side.
  BOTH NEGATIVE (user, 2026-10-14) any time: 'if Up wins' and 'if Down wins' both under 0 -> buy the cheaper token,
         as a taker at once, the shares (through the depth, fees in; at least 5) that make its outcome positive.
         (A favourite buy, when one applies, goes first.)
Each buy (user, 2026-10-14: "buy at the lowest price"): first a resting bid at the best bid (+ maker_improve ticks
while that stays under the ask) for maker_s (1.5 s; makers pay no fee), following the bid; what it has not filled
by then is cancelled and bought as a taker (FAK) at the ask; a FAK that gets nothing goes again at once
(re-priced); maker_s 0 = taker only. Every order: ask and limit within min_px..max_px, spread <=
max_spread, the ask depth fills it within max_slip, spend (filled + fees + this) <= max_spend, venue minimum
(5 shares, $1); none in the last cutoff_s. No price rule ties the hedge to the run: a hedge after a fall usually
pays more for the pair than $1 (see the backtest)."""
from __future__ import annotations

import math
from dataclasses import dataclass, fields

from .v2 import buy_price
from .v3 import position, recovery_shares

OUT = ("Up", "Down")
OTH = {"Up": "Down", "Down": "Up"}
EPS = 1e-9
TAGS = ("v6s", "v6r", "v6h", "v6f", "v6n")


@dataclass
class V6Params:
    enabled: bool = False
    live_ok: bool = False
    rise: float = 0.05
    start_shares: float = 10.0
    add1_shares: float = 8.0
    add2_shares: float = 6.0
    add_shares: float = 5.0
    step_s: float = 6.0
    fall_th: float = 0.01
    hedge_mult: float = 1.0
    hedge_dear: float = 1.2               # hedge shares x this when the hedge token's ask > the run's average price
    hedge_cheap: float = 0.8              # ... x this when it is under it
    quick_pair: float = 0.95              # opposite ask + run average under this: hedge at once (0 = off)
    rise_min_px: float = 0.30             # rising buys only at an ask from this (user, 2026-10-14)...
    start_0_60_max_px: float = 0.45
    start_60_120_max_px: float = 0.60
    start_120_180_max_px: float = 0.70
    start_180_240_max_px: float = 0.75
    start_240_300_max_px: float = 0.85
    add_max_px: float = 0.85              # ... a run's later buys up to this
    fav_px: float = 0.80                  # the dearer token over this: make 'if it wins' positive (0 = off)
    fav_margin: float = 0.0               # ... over this many dollars
    fav_min: float = 5.0                  # ... in orders of fav_min..fav_max shares, one after another (user, 2026-10-14)
    fav_max: float = 15.0
    fav_min_dist: float = 50.0            # ... only with BTC at least this many $ from the strike on its side (user, 2026-10-14)
    neg_fix: bool = True                  # both outcomes negative: buy the cheaper token until its outcome is positive
    next_any: bool = False
    min_px: float = 0.05
    max_px: float = 0.95
    max_spend: float = 100.0
    max_order: float = 100.0
    max_spread: float = 0.05
    max_slip: float = 0.03
    limit_over: float = 0.01
    cutoff_s: float = 10.0
    from_s: float = 1.0
    max_book_age_ms: float = 1500.0
    maker_s: float = 1.5                  # each buy rests a bid this long before taking the ask (0 = taker only)
    maker_improve: float = 1.0            # ticks over the best bid, while that stays under the ask

    @classmethod
    def from_settings(cls, s) -> "V6Params":
        return cls(**{f.name: getattr(s, "v6_" + f.name) for f in fields(cls)})


def _mid(v, o):
    t = v.books.get(o) or {}
    b, a = t.get("bid"), t.get("ask")
    return (a + b) / 2.0 if a is not None and b is not None else None


def _start_max_px(v, p):
    minute = min(max(int(v.secs_in // 60), 0), 4)
    return (p.start_0_60_max_px, p.start_60_120_max_px, p.start_120_180_max_px,
            p.start_180_240_max_px, p.start_240_300_max_px)[minute]


def decide(v, p: V6Params, d) -> None:
    if not p.enabled:
        return
    st = v.whisk or {}
    up = d.whisk_update
    cur = lambda k, dflt=None: up[k] if k in up else st.get(k, dflt)
    now = v.now_ms
    F = sorted(getattr(v, "v2_fills", None) or [], key=lambda f: f["ts"])
    pos = position(F)
    mid = {o: _mid(v, o) for o in OUT}
    log = getattr(d, "ledger", None)
    phase = cur("v6_phase", "idle")
    head = f"v6 {phase} | if Up {pos['if']['Up']:+.2f} if Down {pos['if']['Down']:+.2f}"
    say = lambda s: d.why.__setitem__("taker", f"{head} | {s}")

    def note(event, **kw):
        if log is not None:
            log.append({"strategy": "v6", "event": event, "if_up": round(pos["if"]["Up"], 4),
                        "if_down": round(pos["if"]["Down"], 4), **kw})

    if getattr(v, "live", False) and not p.live_ok:
        return say("live trading is not enabled for V6 (paper only)")
    if None in mid.values() or v.book_age_ms is None or v.book_age_ms > p.max_book_age_ms:
        return say("waiting for a fresh book")
    for o in OUT:                                   # each token's low since the open / the last hedge
        lo = cur(f"v6_low_{o}")
        if lo is None or mid[o] < lo - EPS:
            up[f"v6_low_{o}"] = mid[o]
    if v.secs_left < p.cutoff_s or v.secs_in < p.from_s:
        return say(f"no orders in the last {p.cutoff_s:.0f} s" if v.secs_left < p.cutoff_s else "waiting for the open")
    if any(x > EPS for x in (v.in_flight_taker or {}).values()):
        return say("an order is on its way")

    def complete(buy, n, tf, got=()):
        """A buy is done with n shares (maker + taker; its fills `got`): move the run / hedge on."""
        nonlocal phase
        up["v6_buy"] = None
        if buy["tag"] in ("v6f", "v6n"):
            note("filled", kind="favourite" if buy["tag"] == "v6f" else "both negative", token=buy["tok"], shares=n)
            return
        if buy["tag"] in ("v6s", "v6r"):
            phase = "run"
            run_sh = round((0.0 if buy["tag"] == "v6s" else cur("v6_run_sh", 0.0)) + n, 4)
            run_cost = (0.0 if buy["tag"] == "v6s" else cur("v6_run_cost", 0.0)) + sum(f["price"] * f["size"] for f in got)
            adds = 0 if buy["tag"] == "v6s" else cur("v6_adds", 0) + 1
            paid = sum(f["price"] * f["size"] for f in got) / n if got and n > EPS else buy["mid"]
            up.update(v6_phase="run", v6_tok=buy["tok"], v6_run_sh=run_sh, v6_last_mid=round(paid, 4),   # 'fallen' vs what it paid
                      v6_next_check=tf + p.step_s * 1000.0, v6_adds=adds, v6_run_cost=round(run_cost, 6))
            note("filled", kind="start" if buy["tag"] == "v6s" else "run", token=buy["tok"], shares=n, run_shares=run_sh)
        else:
            h = round(cur("v6_hedged", 0.0) + n, 4)
            up["v6_hedged"] = h
            note("filled", kind="hedge", token=buy["tok"], shares=n, hedged=h, target=cur("v6_target"))
            if cur("v6_target", 0.0) - h < v.market.min_size - EPS:
                quick_pair = buy["tag"] == "v6h" and cur("v6_quick") is not None
                phase = "idle"
                up.update(v6_phase="idle", v6_next=None if quick_pair or p.next_any else buy["tok"],
                          v6_run_sh=0.0, v6_quick=None,
                          v6_idle_from=tf + p.step_s * 1000.0, v6_low_Up=mid["Up"], v6_low_Down=mid["Down"],
                          v6_pair_reentry={"t0": now, "q": p.start_shares, "next": buy["tok"]} if quick_pair else None)
                note("hedged", token=buy["tok"], shares=h)

    def quote(o, q, tag, cap=None):
        """Validate one FAK and return (size, limit, vwap, fee), or a reason it cannot go."""
        t = v.books.get(o) or {}
        a, b = t.get("ask"), t.get("bid")
        q = math.floor(min(q, p.max_order) * 100 + 1e-6) / 100
        if q < v.market.min_size - EPS:
            return None, f"{q:g} under the venue minimum"
        if a - b > p.max_spread + EPS:
            return None, f"{o} spread {a - b:.2f} over {p.max_spread:.2f}"
        if not (p.min_px - EPS <= a <= p.max_px + EPS):
            return None, f"{o} ask {a:.2f} outside {p.min_px:.2f}-{p.max_px:.2f}"
        top = _start_max_px(v, p) if tag == "v6s" else p.add_max_px
        if tag in ("v6s", "v6r") and not (p.rise_min_px - EPS <= a <= top + EPS):
            return None, f"{o} ask {a:.2f} outside {p.rise_min_px:.2f}-{top:.2f}: no {'start' if tag == 'v6s' else 'run'} buy"
        bp = buy_price(v, p, o, q, p.max_px if cap is None else min(p.max_px, cap))
        if bp is None:
            return None, f"{o} depth cannot fill {q:g} within {p.max_slip:.2f}" + ("" if cap is None else f" at or under {cap:.2f}")
        limit, vwap, fee = bp
        if q * limit < 1.0 - EPS:
            return None, "under the $1 minimum"
        if pos["total"] + q * limit + fee > p.max_spend + EPS:
            return None, f"spend over ${p.max_spend:.0f}"
        return (q, limit, vwap, fee), ""

    def take(o, q, tag, cap=None):
        """One FAK for q of o at the ask (its limit at most cap): '' when it went, else why not."""
        quote_, reason = quote(o, q, tag, cap)
        if reason:
            return reason
        q, limit, vwap, fee = quote_
        d.takes.append((o, limit, q, 0.0))
        d.tags[f"taker:{o}"] = tag
        note("order", tag=tag, token=o, shares=q, limit=limit, vwap=round(vwap, 4), how="taker")
        say(f"{tag}: taker {q:g} {o} @ <= {limit:.2f}")
        return ""

    def send(o, q, tag, why):
        """Start a buy: a resting bid first (maker_s), else a FAK at once. '' when it started, else why not."""
        a = (v.books.get(o) or {}).get("ask")
        if not (p.min_px - EPS <= a <= p.max_px + EPS):
            return f"{o} ask {a:.2f} outside {p.min_px:.2f}-{p.max_px:.2f}"
        top = _start_max_px(v, p) if tag == "v6s" else p.add_max_px
        if tag in ("v6s", "v6r") and not (p.rise_min_px - EPS <= a <= top + EPS):
            return f"{o} ask {a:.2f} outside {p.rise_min_px:.2f}-{top:.2f}: no {'start' if tag == 'v6s' else 'run'} buy"
        if pos["total"] + q * a > p.max_spend + EPS:
            return f"spend over ${p.max_spend:.0f}"
        if p.maker_s <= 0:
            r = take(o, q, tag)
            if r:
                return r
        up["v6_buy"] = {"tag": tag, "tok": o, "q": q, "t0": now, "mid": mid[o], "stage": "bid" if p.maker_s > 0 else "take"}
        note("buy", tag=tag, token=o, shares=q, why=why, maker_s=p.maker_s)
        if p.maker_s > 0:
            say(f"{why}: {q:g} {o}, a bid for {p.maker_s:g} s first")
        return ""

    # a buy under way: its bid, then its taker remainder; what it got moves the state
    buy = cur("v6_buy")
    if buy:
        got = [f for f in F if f["tag"] == buy["tag"] and f["outcome"] == buy["tok"] and f["ts"] >= buy["t0"] - 1.0]
        n = sum(f["size"] for f in got)
        tf = max((f["ts"] for f in got), default=now)
        if buy["stage"] == "bid":
            if n >= buy["q"] - EPS:
                complete(buy, n, tf, got)
                return say(f"{buy['tag']}: the bid filled all {n:g}")
            if now - buy["t0"] < p.maker_s * 1000.0:
                return say(f"{buy['tag']}: bidding for {buy['q'] - n:g} {buy['tok']} ({n:g} filled)")
            if (v.resting or {}).get(buy["tok"]):
                return say(f"{buy['tag']}: cancelling the bid ({n:g} filled)")
            if buy["tag"] in ("v6s", "v6r") and mid[buy["tok"]] < buy["mid"] - p.fall_th - EPS:
                # it fell while the bid waited: no taker rest (user, 2026-10-14: a run buy into a falling token)
                if n > EPS:
                    complete(buy, n, tf, got)
                else:
                    up["v6_buy"] = None
                    if buy["tag"] == "v6r":
                        up["v6_next_check"] = now            # the run is judged now: fallen -> the hedge
                note("dropped", tag=buy["tag"], token=buy["tok"], mid=mid[buy["tok"]], was=buy["mid"], filled=n)
                return say(f"{buy['tag']}: {buy['tok']} fell to {mid[buy['tok']]:.3f} from {buy['mid']:.3f} during the bid: "
                           f"no taker rest")
            left = buy["q"] - n
            if left < v.market.min_size - EPS:
                complete(buy, n, tf, got)
                return
            r = take(buy["tok"], left, buy["tag"])
            if r and n > EPS:
                complete(buy, n, tf, got)                     # the rest cannot be taken now: keep what the bid got
                return
            if r:
                up["v6_buy"] = None                      # nothing at all: the rule judges again
                return say(f"{buy['tag']}: {r}")
            up["v6_buy"] = {**buy, "stage": "take", "q_take": left}
            return
        # stage take: the FAK is back (nothing in flight)
        if n > EPS:
            complete(buy, n, tf, got)
            return
        note("killed", tag=buy["tag"], token=buy["tok"])
        if buy["tag"] in ("v6f", "v6n"):                 # judged again from scratch
            up["v6_buy"] = None
            return
        r = take(buy["tok"], buy.get("q_take", buy["q"]), buy["tag"],
                 cur("v6_quick") if buy["tag"] == "v6h" else None)  # nothing: again at once, re-priced
        if r:
            up["v6_buy"] = None
            return say(f"{buy['tag']}: {r}")
        return

    pair = cur("v6_pair_reentry")
    if pair:
        filled = {o: sum(f["size"] for f in F if f["tag"] == "v6s" and f["outcome"] == o
                         and f["ts"] >= pair["t0"] - 1.0) for o in OUT}
        remaining = {o: max(0.0, pair["q"] - filled[o]) for o in OUT}
        waiting = {o: q for o, q in remaining.items() if q >= v.market.min_size - EPS}
        if not waiting:
            up.update(v6_pair_reentry=None, v6_phase="idle", v6_next=None, v6_run_sh=0.0,
                      v6_idle_from=now + p.step_s * 1000.0, v6_low_Up=mid["Up"], v6_low_Down=mid["Down"])
            note("quick-pair reentry filled", up=round(filled["Up"], 4), down=round(filled["Down"], 4))
            return say("quick-pair complete: both tokens bought; waiting for the next rise")
        if v.secs_left <= p.cutoff_s + EPS:
            up.update(v6_pair_reentry=None, v6_phase="idle", v6_next=pair["next"])
            return say("quick-pair reentry stopped at the order cutoff")
        quotes = {}
        for o, q in waiting.items():
            quote_, reason = quote(o, q, "v6s")
            if reason:
                return say(f"quick-pair reentry waiting: {reason}")
            quotes[o] = quote_
        combined = pos["total"] + sum(q * limit + fee for q, limit, _, fee in quotes.values())
        if combined > p.max_spend + EPS:
            return say(f"quick-pair reentry stopped: combined spend over ${p.max_spend:.0f}")
        for o, (q, limit, vwap, fee) in quotes.items():
            d.takes.append((o, limit, q, 0.0))
            d.tags[f"taker:{o}"] = "v6s"
            note("order", tag="v6s", token=o, shares=q, limit=limit, vwap=round(vwap, 4),
                 how="taker", quick_pair_reentry=True)
        return say("quick-pair reentry: buying both tokens now")

    # the favourite: the dearer token over fav_px must leave 'if it wins' positive (user, 2026-10-14)
    if p.fav_px > 0:                                     # also with no shares yet (user, 2026-10-14)
        hi = "Up" if mid["Up"] >= mid["Down"] else "Down"
        dist = getattr(v, "dist_usd", None)
        dist = None if dist is None else (dist if hi == "Up" else -dist)        # $ from the strike on hi's side
        if mid[hi] > p.fav_px + EPS and pos["if"][hi] <= p.fav_margin + EPS and (dist is None or dist < p.fav_min_dist - EPS):
            say(f"favourite {hi} {mid[hi]:.3f}: BTC {'?' if dist is None else f'${dist:+,.0f}'} from the strike, needs ${p.fav_min_dist:,.0f}")
        elif mid[hi] > p.fav_px + EPS and pos["if"][hi] <= p.fav_margin + EPS:
            need = p.fav_margin + 0.01 - pos["if"][hi]
            n = recovery_shares(v, p, hi, need)
            if n is None:
                say(f"favourite {hi} {mid[hi]:.3f}: the depth cannot make 'if {hi} wins' positive")
            else:
                n = min(max(n, p.fav_min, v.market.min_size), p.fav_max)      # 5-15 an order; again until positive
                r = take(hi, n, "v6f")
                if not r:
                    up["v6_buy"] = {"tag": "v6f", "tok": hi, "q": n, "t0": now, "mid": mid[hi], "stage": "take"}
                    note("favourite", token=hi, mid=mid[hi], if_wins=round(pos["if"][hi], 4), shares=n)
                    return say(f"favourite {hi} {mid[hi]:.3f}: if {hi} wins {pos['if'][hi]:+.2f} -> {n:g} {hi} now")
                say(f"favourite {hi}: {r}")

    # both outcomes negative: the cheaper token until its outcome is positive (user, 2026-10-14)
    if F and p.neg_fix and pos["if"]["Up"] < -EPS and pos["if"]["Down"] < -EPS:
        lo = "Up" if mid["Up"] < mid["Down"] else "Down"
        n = recovery_shares(v, p, lo, 0.01 - pos["if"][lo])
        if n is None:
            say(f"both negative: the {lo} depth cannot make 'if {lo} wins' positive")
        else:
            n = max(n, v.market.min_size)
            r = take(lo, n, "v6n")
            if not r:
                up["v6_buy"] = {"tag": "v6n", "tok": lo, "q": n, "t0": now, "mid": mid[lo], "stage": "take"}
                note("both negative", token=lo, if_up=round(pos["if"]["Up"], 4), if_down=round(pos["if"]["Down"], 4), shares=n)
                return say(f"both negative: {n:g} {lo} (the cheaper) now -> 'if {lo} wins' positive")
            say(f"both negative, {lo}: {r}")

    if phase == "idle":
        nxt = cur("v6_next")
        if now < cur("v6_idle_from", -1e18):
            return say(f"after the hedge: {nxt or 'a token'} may start a run in {(cur('v6_idle_from') - now) / 1000:.1f} s")
        for o in ([nxt] if nxt else OUT):
            lo = cur(f"v6_low_{o}")
            if lo is not None and mid[o] >= lo + p.rise - EPS:
                r = send(o, p.start_shares, "v6s", f"start: {o} {mid[o]:.3f} rose {mid[o] - lo:.3f} over its low {lo:.3f}")
                if not r:
                    return
                return say(f"start {o}: {r}")
        return say(f"waiting for {nxt or 'a token'} to rise {p.rise:.2f} over its low")
    def hedge_size(x):
        """(target shares, run average, hedge ask, multiplier): the run's shares x 1.2 / 0.8 by the hedge's price."""
        run_sh = cur("v6_run_sh", 0.0)
        rc = cur("v6_run_cost")
        run_avg = rc / run_sh if rc is not None and run_sh > EPS else None     # unknown: no adjustment
        h_ask = (v.books.get(OTH[x]) or {}).get("ask")
        k = 1.0 if run_avg is None or h_ask is None or abs(h_ask - run_avg) < EPS else \
            p.hedge_dear if h_ask > run_avg else p.hedge_cheap      # 120% when the hedge is dearer, 80% when cheaper
        return round(run_sh * p.hedge_mult * k, 2), run_avg, h_ask, k

    if phase == "run" and p.quick_pair > 0:
        x = cur("v6_tok")
        tgt, run_avg, h_ask, k = hedge_size(x)
        if run_avg is not None and h_ask is not None and h_ask + run_avg < p.quick_pair - EPS:
            phase = "hedge"
            up.update(v6_phase="hedge", v6_target=tgt, v6_hedged=0.0, v6_hedge_tok=OTH[x],
                      v6_quick=math.floor((p.quick_pair - run_avg - 1e-6) * 100) / 100)   # on the tick, under the pair
            note("cheap pair", token=x, run_avg=round(run_avg, 4), hedge_ask=h_ask, pair=round(h_ask + run_avg, 4),
                 hedge_x=k, hedge_target=tgt)
    if phase == "run":
        x = cur("v6_tok")
        if now < cur("v6_next_check", 0.0):
            return say(f"run {x} ({cur('v6_run_sh', 0.0):g} sh): next check in {(cur('v6_next_check') - now) / 1000:.1f} s")
        last = cur("v6_last_mid")
        if mid[x] >= last - p.fall_th - EPS:
            k = cur("v6_adds", 0)                          # 8, then 6, then 5 each
            q = p.add1_shares if k == 0 else p.add2_shares if k == 1 else p.add_shares
            r = send(x, q, "v6r", f"run add {k + 1}: {x} {mid[x]:.3f} not fallen from {last:.3f}")
            if r:
                up["v6_next_check"] = now + p.step_s * 1000.0        # cannot buy now: look again in step_s
                return say(f"run {x}: {r}; next check in {p.step_s:g} s")
            return
        run_sh = cur("v6_run_sh", 0.0)
        tgt, run_avg, h_ask, k = hedge_size(x)
        phase = "hedge"
        up.update(v6_phase="hedge", v6_target=tgt, v6_hedged=0.0, v6_hedge_tok=OTH[x], v6_quick=None)
        note("fell", token=x, mid=mid[x], last=last, run_shares=run_sh, run_avg=None if run_avg is None else round(run_avg, 4),
             hedge_ask=h_ask, hedge_x=k, hedge_target=tgt)
    if phase == "hedge":
        y = cur("v6_hedge_tok")
        left = cur("v6_target", 0.0) - cur("v6_hedged", 0.0)
        if left < v.market.min_size - EPS:
            up.update(v6_phase="idle", v6_next=None if p.next_any else y, v6_run_sh=0.0,
                      v6_idle_from=now + p.step_s * 1000.0, v6_low_Up=mid["Up"], v6_low_Down=mid["Down"])
            return say(f"hedge done ({cur('v6_hedged', 0.0):g}); {y} next")
        cap = cur("v6_quick")
        if cap is not None:                              # the cheap-pair hedge: a taker now, while the pair stays cheap
            a = (v.books.get(y) or {}).get("ask")
            if a is None or a > cap + EPS:
                if cur("v6_hedged", 0.0) > EPS:
                    up.update(v6_phase="idle", v6_next=None if p.next_any else y, v6_run_sh=0.0, v6_quick=None,
                              v6_idle_from=now + p.step_s * 1000.0, v6_low_Up=mid["Up"], v6_low_Down=mid["Down"])
                    note("hedged", token=y, shares=cur("v6_hedged", 0.0), quick=True)
                    return say(f"cheap-pair hedge: {y} ask over {cap:.2f}, kept {cur('v6_hedged', 0.0):g}")
                up.update(v6_phase="run", v6_quick=None, v6_target=None)
                return say(f"cheap-pair hedge: {y} ask over {cap:.2f} before any fill: back to the run")
            r = take(y, left, "v6h", cap)
            if not r:
                up["v6_buy"] = {"tag": "v6h", "tok": y, "q": left, "t0": now, "mid": mid[y], "stage": "take"}
                return say(f"cheap-pair hedge: {left:g} {y} @ <= {cap:.2f} now")
            return say(f"cheap-pair hedge {y}: {r}")
        r = send(y, left, "v6h", f"hedge: {cur('v6_tok')} fell, {y} for the run's {cur('v6_target'):g} shares")
        if r:
            say(f"hedge {y} ({cur('v6_hedged', 0.0):g}/{cur('v6_target'):g}): {r}")


def bid_target(v, p: V6Params, d, sizes: dict) -> None:
    """The resting bid of a V6 buy in its maker stage (called from strategy._makers before the bids are reconciled):
    at the best bid + maker_improve ticks, under the ask, for what the buy still needs; following the best bid."""
    if not p.enabled or v.halted:
        return
    buy = (v.whisk or {}).get("v6_buy")
    if not buy or buy.get("stage") != "bid" or v.now_ms - buy["t0"] >= p.maker_s * 1000.0:
        return
    o = buy["tok"]
    t = v.books.get(o) or {}
    b, a = t.get("bid"), t.get("ask")
    if b is None or a is None:
        return
    got = sum(f["size"] for f in (getattr(v, "v2_fills", None) or [])
              if f["tag"] == buy["tag"] and f["outcome"] == o and f["ts"] >= buy["t0"] - 1.0)
    left = math.floor((buy["q"] - got) * 100 + 1e-6) / 100
    if left < v.market.min_size - EPS:
        return
    tick = v.market.tick
    px = round(min(b + p.maker_improve * tick, a - tick), 4)
    if not (p.min_px - EPS <= px <= p.max_px + EPS) or left * px < 1.0 - EPS:
        return
    d.targets[o] = px
    sizes[o] = left
    d.tags[f"maker:{o}"] = buy["tag"]
    d.follow.add(o)
    d.own_budget.add(o)
    d.why[f"maker_{o}"] = f"v6 {buy['tag']}: bid {px:.2f} for {left:g}"
