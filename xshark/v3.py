"""Strategy V3 (user, 2026-10-13: "Analyze and Implement My Polymarket Trading Strategy"). Paper first.

Roles. UP = the low-price token the entry bought: the first fill tagged v3e locks it for the market (either
market outcome); DOWN = the other token. Before that only the entry is live.

State machine, per market (fills and the window state make it idempotent across restarts):
  FLAT     a token whose lowest mid since the open (within lookback_s) was under low_max (0.50) and whose mid
           is now entry_rise (0.05) over that low -> FAK entry_shares (20) of it          [v3e, up to 3 tries]
  ENTERED  (roles locked) one order at a time, the first rule that wants one, by priority:
    1 recovery   DOWN's mid rec_rise (0.10) over its low since the entry while 'if DOWN wins' < 0 ->
                 the DOWN shares that bring 'if DOWN wins' to 0 (priced through the ask depth, fees in),
                 capped by the risk limits; only at an ask <= rec_max_px and an improvement >= min_benefit;
                 once [v3r]. Then each rec_step (0.05) over the last trigger price -> rec_step_shares (5) DOWN,
                 projections recomputed after each fill [v3s]; rec_max orders in all.
    2 level B    DOWN crossing up to lvl_b (0.42) -> max(0, UP - DOWN) DOWN, at most max_order [v3b]; once.
    3 balance    avg UP + avg DOWN (fees in) < bal_sum (0.96) -> of bal_shares UP / bal_shares DOWN, the
                 one that leaves the worse settlement outcome best, only if it raises it by min_benefit and
                 its new pairs cost under bal_pair_max (1.00) [v3x]; bal_gap_s apart.
    4 level A    DOWN crossing up to lvl_a (0.32) after trading under lvl_a_below (0.30) -> lvl_a_shares (10) [v3la]; once.
    5 level C    DOWN crossing up to lvl_c (0.45) -> lvl_c_shares (20) DOWN [v3lc]; once. After it filled, the
                 momentum token (DOWN; c_mom_up: UP) each c_step (0.05) over its last qualifying fill ->
                 c_mom_shares (10) [v3cm].
    6 momentum   add_gap_s (6 s) after the last UP momentum fill (v3e/v3m) and UP's mid >= that fill + add_step
                 (0.05) -> add_shares (10) UP [v3m]; add_max add-ons (0 = no count limit; the risk limits hold).
  A crossed level waits while its ask is over level + max_chase (no chasing); rearm_drop > 0 re-arms a done
  level after DOWN falls that far under it (off by default).
Risk, before every order (else no order, the reason in the bot's 'why'): a fresh book, not in the last
cutoff_s, nothing on its way, resend_ms since the last order; the token's spread <= max_spread; the ask depth
fills the order within max_slip (limit = that level, at least best ask + limit_over); spend (filled + fees + on
its way at $1 a share + this order) <= max_spend; UP and DOWN shares <= max_up / max_down; the order <=
max_order; no ask or limit over max_px (0.85); |UP - DOWN| <= max_imbalance and the worse outcome >= -max_outcome_loss after it, unless the order
improves them; venue minimum (min_size shares, $1)."""
from __future__ import annotations

import math
from dataclasses import dataclass, fields

from .v2 import bid_value, buy_price

OUT = ("Up", "Down")
OTH = {"Up": "Down", "Down": "Up"}
EPS = 1e-9
TAGS = ("v3e", "v3m", "v3r", "v3s", "v3b", "v3x", "v3la", "v3lc", "v3cm")
MOM_UP = ("v3e", "v3m")


@dataclass
class V3Params:
    enabled: bool = False
    lookback_s: float = 30.0
    low_max: float = 0.50
    entry_rise: float = 0.05
    entry_shares: float = 20.0
    add_step: float = 0.05
    add_shares: float = 10.0
    add_gap_s: float = 6.0
    add_max: float = 0.0
    rec_rise: float = 0.10
    rec_max_px: float = 0.80
    rec_step: float = 0.05
    rec_step_shares: float = 5.0
    rec_max: float = 4.0
    bal_sum: float = 0.96
    bal_shares: float = 10.0
    bal_pair_max: float = 1.00
    bal_gap_s: float = 3.0
    lvl_a: float = 0.32
    lvl_a_below: float = 0.30
    lvl_a_shares: float = 10.0
    lvl_b: float = 0.42
    lvl_c: float = 0.45
    lvl_c_shares: float = 20.0
    c_mom_up: bool = False
    c_step: float = 0.05
    c_mom_shares: float = 10.0
    rearm_drop: float = 0.0
    max_chase: float = 0.03
    min_benefit: float = 0.05
    max_spend: float = 60.0
    max_up: float = 80.0
    max_down: float = 80.0
    max_order: float = 40.0
    max_outcome_loss: float = 30.0
    max_imbalance: float = 40.0
    max_slip: float = 0.02
    limit_over: float = 0.01
    max_spread: float = 0.03
    max_px: float = 0.85                  # no buy of a token over this (user, 2026-10-13), its limit included
    cutoff_s: float = 30.0
    from_s: float = 1.0
    max_book_age_ms: float = 1500.0
    resend_ms: float = 0.0                # 0: a killed order goes again at once (user, 2026-10-13); one on its way at a time

    @classmethod
    def from_settings(cls, s) -> "V3Params":
        return cls(**{f.name: getattr(s, "v3_" + f.name) for f in fields(cls)})


# ------------------------------------------------------------------ formulas

def position(fills: list, fee_rate: float = 0.07) -> dict:
    """Shares, cost (fees in), average cost per token, paired / unmatched shares and both settlement outcomes."""
    sh = {o: 0.0 for o in OUT}
    cost = {o: 0.0 for o in OUT}
    fees = 0.0
    for f in fills:
        sh[f["outcome"]] += f["size"]
        cost[f["outcome"]] += f["price"] * f["size"] + f["fee"]
        fees += f["fee"]
    total = cost["Up"] + cost["Down"]
    avg = {o: (cost[o] / sh[o] if sh[o] > EPS else None) for o in OUT}
    paired = min(sh["Up"], sh["Down"])
    return {"shares": sh, "cost": cost, "total": total, "fees": fees, "avg": avg, "paired": paired,
            "unmatched": abs(sh["Up"] - sh["Down"]), "if": {o: sh[o] - total for o in OUT},
            "worst": min(sh["Up"], sh["Down"]) - total}


def after(pos: dict, o: str, n: float, cost: float) -> dict:
    """Both settlement outcomes after buying n of o for cost (fees in)."""
    return {x: pos["if"][x] + (n if x == o else 0.0) - cost for x in OUT}


def recovery_shares(v, p, o: str, loss: float):
    """Shares of o, through its ask depth within max_slip, whose payoff (1 - price - fee each) covers `loss`:
    the simplified loss / (1 - price) only holds with no fee at one price. None when the depth cannot."""
    t = v.books.get(o) or {}
    a = t.get("ask")
    if a is None or loss <= EPS:
        return None
    levels = t.get("asks") or [(a, t.get("ask_size") or 0.0)]
    left, n = loss, 0.0
    for px, sz in sorted(levels):
        if px > a + p.max_slip + EPS:
            break
        g = 1.0 - px - v.market.fees.taker_fee(px, 1.0)
        if g <= EPS:
            break
        q = min(sz, left / g)
        n += q
        left -= q * g
        if left <= 1e-6:
            return math.ceil(n * 100 - 1e-6) / 100
    return None


def _mid(v, o):
    t = v.books.get(o) or {}
    b, a = t.get("bid"), t.get("ask")
    return (a + b) / 2.0 if a is not None and b is not None else None


def summary(fills: list, v=None, budget: float | None = None) -> dict:
    """The ledger row after a fill: position, both outcomes, the worse one, marked value at the bids."""
    pos = position(fills)
    row = {"up_shares": pos["shares"]["Up"], "down_shares": pos["shares"]["Down"],
           "avg_up": pos["avg"]["Up"], "avg_down": pos["avg"]["Down"], "total_cost": pos["total"],
           "fees": pos["fees"], "paired": pos["paired"], "unmatched": pos["unmatched"],
           "if_up_wins": pos["if"]["Up"], "if_down_wins": pos["if"]["Down"], "max_loss": min(0.0, pos["worst"])}
    if v is not None:
        row["unrealized"] = bid_value(v, "Up", pos["shares"]["Up"]) + bid_value(v, "Down", pos["shares"]["Down"]) - pos["total"]
    if budget is not None:
        row["remaining_budget"] = budget - pos["total"]
    return row


# ------------------------------------------------------------------ engine

def decide(v, p: V3Params, d) -> None:
    if not p.enabled:
        return
    st = v.whisk or {}
    up = d.whisk_update
    cur = lambda k, dflt=None: up[k] if k in up else st.get(k, dflt)
    now = v.now_ms
    F = sorted(getattr(v, "v2_fills", None) or [], key=lambda f: f["ts"])
    pos = position(F)
    mid = {o: _mid(v, o) for o in OUT}
    ms = v.market.min_size
    entry = [f for f in F if f["tag"] == "v3e"]
    U = entry[0]["outcome"] if entry else None
    D = OTH[U] if U else None
    head = (f"v3 | {'UP=' + U if U else 'no entry yet'} | Up {pos['shares']['Up']:g} Down {pos['shares']['Down']:g}"
            f" | if Up {pos['if']['Up']:+.2f} if Down {pos['if']['Down']:+.2f}")
    say = lambda s: d.why.__setitem__("taker", f"{head} | {s}")

    # bookkeeping every tick: levels each token traded under, DOWN's low since the entry
    for o in OUT:
        m = mid[o]
        if m is None:
            continue
        for lvl, key in ((p.lvl_a_below, "a"), (p.lvl_b, "b"), (p.lvl_c, "c")):
            k = f"v3_under_{key}_{o}"
            if m < lvl - EPS and not st.get(k):
                up[k] = True
            if p.rearm_drop > 0 and m < lvl - p.rearm_drop - EPS and (st.get(f"v3_done_{key}_{o}") or st.get(f"v3_tries_{key}_{o}")):
                up[f"v3_done_{key}_{o}"] = False           # re-armed after a retracement (off by default)
                up[f"v3_tries_{key}_{o}"] = 0
    if D and mid[D] is not None and (st.get("v3_dn_low") is None or mid[D] < st["v3_dn_low"] - EPS):
        up["v3_dn_low"] = mid[D]

    if v.book_age_ms is None or v.book_age_ms > p.max_book_age_ms or None in mid.values():
        return say("waiting for a fresh book")
    if v.secs_left < p.cutoff_s or v.secs_in < p.from_s:
        return say(f"no new orders in the last {p.cutoff_s:.0f} s" if v.secs_left < p.cutoff_s else "waiting for the open")
    if any(x > EPS for x in (v.in_flight_taker or {}).values()):
        return say("an order is on its way")
    if p.resend_ms > 0 and now - cur("v3_last_send", -1e18) < p.resend_ms:
        return say("just sent")

    notes = []

    def send(o, n, tag, why, max_ask=None, cap=None):
        """Risk checks, then one FAK. True when it went."""
        n = math.floor(min(n, p.max_order) * 100 + 1e-6) / 100
        if n < ms - EPS:
            notes.append(f"{why}: {n:g} under the venue minimum {ms:g}")
            return False
        t = v.books.get(o) or {}
        if (t.get("ask") or 1.0) - (t.get("bid") or 0.0) > p.max_spread + EPS:
            notes.append(f"{why}: {o} spread over {p.max_spread:.2f}")
            return False
        if (t.get("ask") or 1.0) > p.max_px + EPS:
            notes.append(f"{why}: {o} ask {(t.get('ask') or 1.0):.2f} over {p.max_px:.2f}")
            return False
        if max_ask is not None and (t.get("ask") or 1.0) > max_ask + EPS:
            notes.append(f"{why}: {o} ask {t.get('ask'):.2f} over {max_ask:.2f} (no chasing)")
            return False
        bp = buy_price(v, p, o, n, p.max_px if cap is None else min(cap, p.max_px))
        if bp is None:
            notes.append(f"{why}: {o} depth cannot fill {n:g} within {p.max_slip:.2f}")
            return False
        limit, vwap, fee = bp
        if n * limit < 1.0 - EPS:
            notes.append(f"{why}: ${n * limit:.2f} under the $1 minimum")
            return False
        pend = sum((v.in_flight_taker or {}).values())
        spend = pos["total"] + pend + n * limit + v.market.fees.taker_fee(limit, n)
        if spend > p.max_spend + EPS:
            notes.append(f"{why}: spend ${spend:.2f} over ${p.max_spend:.0f}")
            return False
        role_up = U or o                                   # before the entry the bought token is UP
        sh2 = {x: pos["shares"][x] + (n if x == o else 0.0) for x in OUT}
        if sh2[role_up] > p.max_up + EPS or sh2[OTH[role_up]] > p.max_down + EPS:
            notes.append(f"{why}: over the UP {p.max_up:g} / DOWN {p.max_down:g} share limit")
            return False
        imb0, imb1 = abs(pos["shares"]["Up"] - pos["shares"]["Down"]), abs(sh2["Up"] - sh2["Down"])
        if imb1 > p.max_imbalance + EPS and imb1 > imb0 - EPS:
            notes.append(f"{why}: imbalance {imb1:g} over {p.max_imbalance:g}")
            return False
        aft = after(pos, o, n, n * vwap + fee)
        if min(aft.values()) < -p.max_outcome_loss - EPS and min(aft.values()) < pos["worst"] - EPS:
            notes.append(f"{why}: worse outcome {min(aft.values()):+.2f} past -{p.max_outcome_loss:.0f}")
            return False
        d.takes.append((o, limit, n, 0.0))
        d.tags[f"taker:{o}"] = tag
        up["v3_last_send"] = now
        up[f"v3_sent_{tag}"] = now
        up["v3_seen_ask"] = t.get("ask")                  # the ledger's slippage: fill price - this
        say(f"{why}: FAK {n:g} {o} @ <= {limit:.2f} (VWAP {vwap:.3f}) -> if Up {aft['Up']:+.2f} / if Down {aft['Down']:+.2f}")
        return True

    # FLAT: the entry
    if not U:
        best = None
        for o in OUT:
            low = (getattr(v, "v3_lows", None) or {}).get(o)
            if low is None or low >= p.low_max - EPS or mid[o] < low + p.entry_rise - EPS:
                continue
            if best is None or mid[o] - low > best[1]:
                best = (o, mid[o] - low, low)
        if best and cur("v3_entry_tries", 0) < 3:
            o, rise, low = best
            if send(o, p.entry_shares, "v3e", f"entry: {o} {mid[o]:.3f} rose {rise:.3f} from its low {low:.3f}",
                    max_ask=low + p.entry_rise + p.max_chase):
                up["v3_entry_tries"] = cur("v3_entry_tries", 0) + 1
                return
        lows = getattr(v, "v3_lows", None) or {}
        return say("; ".join(notes) or "waiting for a low token (low under %.2f) to rise %.2f: lows %s" % (
            p.low_max, p.entry_rise, {o: round(x, 3) for o, x in lows.items() if x is not None}))

    dm, um = mid[D], mid[U]
    tagged = lambda *ts: [f for f in F if f["tag"] in ts]
    rec = tagged("v3r", "v3s")

    def level_done(key, tag):
        """A level fires once per market: done once its order filled (a killed FAK may go again, 3 tries)."""
        if cur(f"v3_done_{key}_{D}"):
            return True
        sent = cur(f"v3_sent_{tag}")
        if sent is not None and any(f["tag"] == tag and f["ts"] >= sent - 1.0 for f in F):
            up[f"v3_done_{key}_{D}"] = True
            return True
        return cur(f"v3_tries_{key}_{D}", 0) >= 3

    def level_sent(key):
        up[f"v3_tries_{key}_{D}"] = cur(f"v3_tries_{key}_{D}", 0) + 1

    # 1 recovery, then its steps
    low = cur("v3_dn_low")
    if not tagged("v3r") and low is not None and dm >= low + p.rec_rise - EPS and cur("v3_rec_tries", 0) < 3:
        loss = -pos["if"][D]
        if loss > EPS:
            n = recovery_shares(v, p, D, loss)
            a = (v.books.get(D) or {}).get("ask") or 1.0
            if n is None:
                notes.append(f"recovery: {D} depth cannot cover the ${loss:.2f} loss")
            elif a > p.rec_max_px + EPS:
                notes.append(f"recovery: {D} ask {a:.2f} over {p.rec_max_px:.2f}")
            else:
                n = max(n, ms)
                gain = n * (1.0 - a - v.market.fees.taker_fee(a, 1.0))
                if min(gain, loss) < p.min_benefit - EPS:
                    notes.append(f"recovery: improvement ${min(gain, loss):.2f} under ${p.min_benefit:.2f}")
                elif send(D, n, "v3r", f"recovery: {D} {dm:.3f} rose {dm - low:.3f} from {low:.3f}, if {D} wins -{loss:.2f}"):
                    up["v3_rec_tries"] = cur("v3_rec_tries", 0) + 1
                    up["v3_rec_ref"] = round(low + p.rec_rise * math.floor((dm - low) / p.rec_rise + 1e-6), 4)
                    return
    if tagged("v3r") and len({f["oid"] for f in rec}) < p.rec_max:
        ref = cur("v3_rec_ref") or dm
        if dm >= ref + p.rec_step - EPS:
            k = math.floor((dm - ref) / p.rec_step + 1e-6)          # a jump over several steps: one order
            if send(D, p.rec_step_shares, "v3s", f"recovery step: {D} {dm:.3f} >= {ref:.2f} + {p.rec_step:.2f}",
                    max_ask=ref + k * p.rec_step + p.max_chase):
                up["v3_rec_ref"] = round(ref + k * p.rec_step, 4)
                return

    # 2 level B: DOWN crossing up to lvl_b -> the UP - DOWN gap
    if cur(f"v3_under_b_{D}") and dm >= p.lvl_b - EPS and not level_done("b", "v3b"):
        gap = pos["shares"][U] - pos["shares"][D]
        if gap < ms - EPS:
            up[f"v3_done_b_{D}"] = True
            notes.append(f"level B: UP - DOWN {gap:g}, nothing to buy")
        elif send(D, gap, "v3b", f"level B: {D} {dm:.3f} >= {p.lvl_b:.2f}, UP - DOWN {gap:g}", max_ask=p.lvl_b + p.max_chase):
            level_sent("b")
            return

    # 3 balance on the average costs
    av = pos["avg"]
    if av["Up"] is not None and av["Down"] is not None and av["Up"] + av["Down"] < p.bal_sum - EPS \
            and now - cur("v3_sent_v3x", -1e18) >= p.bal_gap_s * 1000.0:
        best = None
        for o in OUT:
            bp = buy_price(v, p, o, p.bal_shares)
            if bp is None:
                continue
            c = p.bal_shares * bp[1] + bp[2]
            aft = after(pos, o, p.bal_shares, c)
            pair = c / p.bal_shares + av[OTH[o]]
            if pair >= p.bal_pair_max - EPS:
                continue
            gain = min(aft.values()) - pos["worst"]
            if gain >= p.min_benefit - EPS and (best is None or min(aft.values()) > best[1]):
                best = (o, min(aft.values()), pair, gain)
        if best:
            o, worst, pair, gain = best
            if send(o, p.bal_shares, "v3x", f"balance: avg sum {av['Up'] + av['Down']:.3f} < {p.bal_sum:.2f}; {o} "
                    f"(new pairs {pair:.3f}) lifts the worse outcome {gain:+.2f}"):
                return

    # 4 level A
    if cur(f"v3_under_a_{D}") and dm >= p.lvl_a - EPS and not level_done("a", "v3la"):
        if send(D, p.lvl_a_shares, "v3la", f"level A: {D} {dm:.3f} >= {p.lvl_a:.2f} after < {p.lvl_a_below:.2f}",
                max_ask=p.lvl_a + p.max_chase):
            level_sent("a")
            return

    # 5 level C, then its momentum
    if cur(f"v3_under_c_{D}") and dm >= p.lvl_c - EPS and not level_done("c", "v3lc"):
        if send(D, p.lvl_c_shares, "v3lc", f"level C: {D} {dm:.3f} >= {p.lvl_c:.2f}", max_ask=p.lvl_c + p.max_chase):
            level_sent("c")
            return
    if tagged("v3lc"):
        t = U if p.c_mom_up else D
        last = max((f for f in F if f["outcome"] == t and f["tag"] in ("v3lc", "v3cm") + (MOM_UP if t == U else ())),
                   key=lambda f: f["ts"], default=None)
        if last and mid[t] >= last["price"] + p.c_step - EPS:
            if send(t, p.c_mom_shares, "v3cm", f"level C momentum: {t} {mid[t]:.3f} >= fill {last['price']:.2f} + {p.c_step:.2f}",
                    max_ask=last["price"] + p.c_step + p.max_chase):
                return

    # 6 UP momentum add-ons on the last UP momentum fill
    last = max(tagged(*MOM_UP), key=lambda f: f["ts"], default=None)
    adds = len({f["oid"] for f in tagged("v3m")})
    if last and (p.add_max <= 0 or adds < p.add_max) and now - last["ts"] >= p.add_gap_s * 1000.0 \
            and um >= last["price"] + p.add_step - EPS:
        if send(U, p.add_shares, "v3m", f"momentum: {U} {um:.3f} >= fill {last['price']:.2f} + {p.add_step:.2f}",
                max_ask=last["price"] + p.add_step + p.max_chase):
            return

    worst = pos["worst"]
    say("; ".join(notes) if notes else f"holding: worse outcome {worst:+.2f}, paired {pos['paired']:g}, unmatched {pos['unmatched']:g}")
