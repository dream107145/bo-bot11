"""Strategy V5: momentum + adaptive hedging (user, 2026-10-13: "Update My Polymarket Bot to Use Momentum + Adaptive
Hedging Strategy"). Paper first: no order in a live bot unless live_ok.

Cycle (at most one unresolved at a time; nothing is queued -- current prices are judged when a cycle ends):
  MOMENTUM  per outcome a reference ask = its lowest ask since that outcome's last signal (reset each market).
            Signal: ask - reference >= rise (0.05), ask within min_px..max_px (0.08-0.89), spread <= max_spread,
            cooldown_s (6 s) since that outcome's last momentum FILL, before entry_cutoff_s (285 s). Size: the
            largest integer min_order..max_order (5-12) passing every check (and, if edge_gate, the model's win
            probability - all-in cost >= min_edge); 5 failing -> no trade. Sending consumes the signal (reference
            = the ask now); an order that fills nothing gives it back (reference restored), so current prices
            are judged again at once.
  BALANCE   from bal_min_delay_s (1 s) to bal_max_delay_s (5 s) after the momentum batch's last actual fill:
            the opposite token, target ceil(filled) + 1 shares, child orders of 5-12 (a 13 target: 8 then 5),
            each only while   (the rising token's average cost, fees in) + (opposite depth VWAP + its fee a share)
            < max_pair (0.97), its limit capped so a fill cannot pass it; the target first (balancing shares >
            rising shares), smaller only when a limit stops it. At the deadline the cycle is closed as balanced /
            partially balanced / unhedged and logged; FAK orders leave no remainder to cancel.
Every order passes one risk check on the state after it, at its limit price and fee (conservative): price
range; spread; book age; committed spend (filled + fees + resting + on its way at $1 a share + this) <=
max_spend; worst settlement outcome >= -max_worst_loss; each outcome's shares <= max_shares; |Up - Down| <=
max_imbalance; FIFO-unmatched inventory cost <= max_unmatched_cost; avg Up cost + avg Down cost < avg_pair_max
(0.97); 'if the leading (dearer) token wins' > 0 (high_wins; the other outcome may be negative). Kill switch for the market (no orders, our
resting buys cancelled): worst outcome at or under -max_worst_loss, duplicate fills, fills not matching the
venue's position. Paused (not killed): stale / invalid book, the bot halted (console, live venue error).
After cancel_all_s (290 s): resting buys cancelled, nothing new. P/L, buy and hold, $1 a winning share:
if Up wins = Up shares - cost (fees in); if Down wins = Down shares - cost; worst = the lower."""
from __future__ import annotations

import math
from dataclasses import dataclass, fields

from .v2 import buy_price
from .v3 import position

OUT = ("Up", "Down")
OTH = {"Up": "Down", "Down": "Up"}
EPS = 1e-9
TAGS = ("v5m", "v5b")


@dataclass
class V5Params:
    enabled: bool = False
    live_ok: bool = False
    entry_cutoff_s: float = 285.0
    bal_min_delay_s: float = 1.0
    bal_max_delay_s: float = 5.0
    cancel_all_s: float = 290.0
    rise: float = 0.05
    cooldown_s: float = 6.0
    min_px: float = 0.08
    max_px: float = 0.89
    min_order: float = 5.0
    max_order: float = 12.0
    max_pair: float = 0.97
    max_spend: float = 50.0
    max_worst_loss: float = 5.0
    max_shares: float = 60.0
    max_imbalance: float = 12.0
    max_unmatched_cost: float = 10.0
    max_spread: float = 0.03
    edge_gate: bool = False
    min_edge: float = 0.02
    max_slip: float = 0.02
    limit_over: float = 0.01
    max_book_age_ms: float = 1500.0
    avg_pair_max: float = 0.97            # after every order: avg Up cost + avg Down cost (fees in) < this (user, 2026-10-14)
    high_wins: bool = True                # after every order: 'if the leading (dearer) token wins' > 0 (user, 2026-10-14)

    @classmethod
    def from_settings(cls, s) -> "V5Params":
        return cls(**{f.name: getattr(s, "v5_" + f.name) for f in fields(cls)})


# ------------------------------------------------------------------ accounting

def fill_key(f: dict) -> tuple:
    return (f.get("oid"), round(f["ts"], 3), f["outcome"], round(f["price"], 6), round(f["size"], 6))


def dedupe(fills: list) -> tuple[list, int]:
    """Each fill once (by order, time, outcome, price, size), oldest first; and how many duplicates there were."""
    seen, out = set(), []
    for f in sorted(fills, key=lambda f: f["ts"]):
        k = fill_key(f)
        if k not in seen:
            seen.add(k)
            out.append(f)
    return out, len(fills) - len(out)


def unmatched(fills: list) -> dict:
    """FIFO matching: each fill first pairs with the other outcome's oldest unmatched shares; what is left of each
    outcome, as [cost a share with fee, shares] lots, oldest first."""
    q = {o: [] for o in OUT}
    for f in sorted(fills, key=lambda f: f["ts"]):
        o, n = f["outcome"], f["size"]
        if n <= EPS:
            continue
        c = (f["price"] * n + f["fee"]) / n
        other = q[OTH[o]]
        while n > EPS and other:
            k = min(n, other[0][1])
            other[0][1] -= k
            n -= k
            if other[0][1] <= EPS:
                other.pop(0)
        if n > EPS:
            q[o].append([c, n])
    return q


def unmatched_cost(fills: list) -> float:
    return sum(c * n for lots in unmatched(fills).values() for c, n in lots)


# ------------------------------------------------------------------ the risk manager

def risk(v, p: V5Params, fills: list, o: str, q: float, limit: float, fee: float) -> str:
    """'' when buying q of o at limit (fee included) leaves every limit intact, else the reason. The state after the
    order is judged at the limit price: the most it can cost."""
    t = v.books.get(o) or {}
    a, b = t.get("ask"), t.get("bid")
    if a is None or b is None or a < b:
        return f"{o} book invalid"
    if a - b > p.max_spread + EPS:
        return f"{o} spread {a - b:.2f} over {p.max_spread:.2f}"
    if not (p.min_px - EPS <= a and limit <= p.max_px + EPS):
        return f"{o} price {a:.2f}-{limit:.2f} outside {p.min_px:.2f}-{p.max_px:.2f}"
    if q < p.min_order - EPS or q > p.max_order + EPS or abs(q - round(q)) > EPS:
        return f"{q:g} shares outside {p.min_order:g}-{p.max_order:g}"
    if q < v.market.min_size - EPS or q * limit < 1.0 - EPS:
        return "under the venue minimum"
    pos = position(fills)
    pend = sum((v.in_flight_taker or {}).values())
    cost = q * limit + fee
    spend = pos["total"] + (v.reserved or 0.0) + pend * 1.0 + cost
    if spend > p.max_spend + EPS:
        return f"spend ${spend:.2f} over ${p.max_spend:.0f}"
    sh = {x: pos["shares"][x] + (q if x == o else 0.0) + (v.in_flight_taker or {}).get(x, 0.0) for x in OUT}
    if sh[o] > p.max_shares + EPS:
        return f"{o} shares {sh[o]:g} over {p.max_shares:g}"
    if abs(sh["Up"] - sh["Down"]) > p.max_imbalance + EPS:
        return f"imbalance {abs(sh['Up'] - sh['Down']):g} over {p.max_imbalance:g}"
    worst = min(pos["shares"][x] + (q if x == o else 0.0) for x in OUT) - pos["total"] - cost
    if worst < -p.max_worst_loss - EPS:
        return f"worst outcome {worst:+.2f} under -{p.max_worst_loss:.2f}"
    after = fills + [dict(outcome=o, price=limit, size=q, fee=fee, ts=float("inf"))]
    uc = unmatched_cost(after)
    if uc > p.max_unmatched_cost + EPS:
        return f"unmatched inventory ${uc:.2f} over ${p.max_unmatched_cost:.0f}"
    pa = position(after)
    if p.avg_pair_max > 0 and pa["avg"]["Up"] is not None and pa["avg"]["Down"] is not None \
            and pa["avg"]["Up"] + pa["avg"]["Down"] >= p.avg_pair_max - EPS:
        return f"averages {pa['avg']['Up']:.3f} + {pa['avg']['Down']:.3f} not under {p.avg_pair_max:.2f}"
    if p.high_wins:
        mid = {x: ((v.books.get(x) or {}).get("bid", 0.0) + (v.books.get(x) or {}).get("ask", 0.0)) / 2.0 for x in OUT}
        hi = "Up" if mid["Up"] >= mid["Down"] else "Down"
        if pa["if"][hi] <= EPS:
            return f"if {hi} (leading) wins {pa['if'][hi]:+.2f}: not positive"
    return ""


def projected(fills: list, o: str, q: float, cost: float) -> dict:
    pos = position(fills)
    return {x: pos["shares"][x] + (q if x == o else 0.0) - pos["total"] - cost for x in OUT}


def child_sizes(p: V5Params, remaining: float) -> list:
    """Valid child sizes for what is left of a balance target, best first: an exact finish, then sizes that leave
    a valid (>= min_order) remainder, then any other valid size (the largest that passes is used)."""
    lo, hi = int(p.min_order), int(p.max_order)
    rem = math.ceil(remaining - 1e-9)
    if rem < lo:
        return []
    allq = list(range(min(hi, rem), lo - 1, -1))
    exact = [q for q in allq if q == rem]
    tidy = [q for q in allq if rem - q >= lo and q not in exact]
    return exact + tidy + [q for q in allq if q not in exact and q not in tidy]


# ------------------------------------------------------------------ engine

def decide(v, p: V5Params, d) -> None:
    if not p.enabled:
        return
    st = v.whisk or {}
    up = d.whisk_update
    cur = lambda k, dflt=None: up[k] if k in up else st.get(k, dflt)
    now, t = v.now_ms, v.secs_in
    raw = list(getattr(v, "v2_fills", None) or [])
    F, dup = dedupe(raw)
    pos = position(F)
    log = getattr(d, "ledger", None)
    head = f"v5 | if Up {pos['if']['Up']:+.2f} if Down {pos['if']['Down']:+.2f}"
    say = lambda s: d.why.__setitem__("taker", f"{head} | {s}")

    def note(event, **kw):
        if log is not None:
            log.append({"strategy": "v5", "event": event, "if_up": round(pos["if"]["Up"], 4),
                        "if_down": round(pos["if"]["Down"], 4), **kw})

    def cancel_buys():
        for o in OUT:
            d.cancels.extend(r.id for r in (v.resting or {}).get(o, []) if r.id not in d.cancels)

    if getattr(v, "live", False) and not p.live_ok:
        return say("live trading is not enabled for V5 (paper only)")

    # kill switch: worst outcome at the limit, duplicate fills, accounting that disagrees with the venue
    if not st.get("v5_kill"):
        why = ""
        if dup:
            why = f"{dup} duplicate fill event(s)"
        elif any(abs(pos["shares"][o] - (v.shares or {}).get(o, 0.0)) > 0.01 for o in OUT):
            why = "fills do not match the venue's position"
        elif F and pos["worst"] <= -p.max_worst_loss + EPS:
            why = f"worst outcome {pos['worst']:+.2f} at -{p.max_worst_loss:.2f}"
        if why:
            up["v5_kill"] = why
            note("kill", reason=why)
    if cur("v5_kill"):
        cancel_buys()
        return say(f"kill switch: {cur('v5_kill')}; no new orders this market")
    if t >= p.cancel_all_s:
        cancel_buys()
        return say(f"after {p.cancel_all_s:.0f} s: buys cancelled, no new orders")
    if v.halted:
        return say(f"paused: {v.halted}")
    books_ok = all((v.books.get(o) or {}).get("ask") is not None and (v.books.get(o) or {}).get("bid") is not None
                   for o in OUT)
    if v.book_age_ms is None or v.book_age_ms > p.max_book_age_ms or not books_ok:
        return say("paused: stale or invalid book")

    ask = {o: v.books[o]["ask"] for o in OUT}
    for o in OUT:                                     # each outcome's reference: its lowest ask since its last signal
        ref = cur(f"v5_ref_{o}")
        if ref is None or ask[o] < ref - EPS:
            up[f"v5_ref_{o}"] = ask[o]
    if any(x > EPS for x in (v.in_flight_taker or {}).values()):
        return say("an order is on its way")

    cyc = cur("v5_cycle")
    # the momentum order of the cycle has come back
    if cyc and cyc["phase"] == "entry":
        got = [f for f in F if f["tag"] == "v5m" and f["outcome"] == cyc["o"] and f["ts"] >= cyc["sent"] - 1.0]
        if not got:
            up["v5_cycle"] = None
            up[f"v5_ref_{cyc['o']}"] = cyc["prev_ref"]           # nothing filled: the move counts again
            note("cycle", id=cyc["id"], outcome=cyc["o"], status="momentum filled nothing")
            cyc = None
        else:
            qr = sum(f["size"] for f in got)
            vw = sum(f["price"] * f["size"] for f in got) / qr
            fee = sum(f["fee"] for f in got)
            tf = max(f["ts"] for f in got)
            cyc = {**cyc, "phase": "balance", "qr": qr, "vwap": vw, "fee_ps": fee / qr, "t_fill": tf,
                   "deadline": tf + p.bal_max_delay_s * 1000.0, "target": math.ceil(qr - 1e-9) + 1}
            up["v5_cycle"] = cyc
            note("momentum_filled", id=cyc["id"], outcome=cyc["o"], shares=qr, vwap=round(vw, 4), fees=round(fee, 5),
                 balance_target=cyc["target"], requested=cyc["q"], remaining=round(cyc["q"] - qr, 2))
    # balancing
    if cyc and cyc["phase"] == "balance":
        b = OTH[cyc["o"]]
        bal = sum(f["size"] for f in F if f["tag"] == "v5b" and f["outcome"] == b and f["ts"] >= cyc["t_fill"] - 1.0)
        if now >= cyc["deadline"] or bal >= cyc["target"] - EPS:
            status = "balanced" if bal >= cyc["target"] - EPS else "partially balanced" if bal > EPS else "unhedged"
            up["v5_cycle"] = None
            note("cycle", id=cyc["id"], outcome=cyc["o"], status=status, momentum=cyc["qr"], balanced=bal,
                 target=cyc["target"])
            cyc = None
        elif now < cyc["t_fill"] + p.bal_min_delay_s * 1000.0:
            return say(f"cycle {cyc['id']}: balancing from {p.bal_min_delay_s:g} s after the fill")
        else:
            pr = position(F)                              # the rising token's average cost so far, fees in
            eff_r = pr["avg"][cyc["o"]] if pr["avg"][cyc["o"]] is not None else cyc["vwap"] + cyc["fee_ps"]
            fee1 = v.market.fees.taker_fee(ask[b], 1.0)
            cap = math.floor((p.max_pair - eff_r - fee1 - 1e-6) * 100) / 100
            best, why = None, []
            for q in child_sizes(p, cyc["target"] - bal):
                bp = buy_price(v, p, b, q, min(cap, p.max_px))
                if bp is None:
                    why.append(f"{q}: {b} cannot fill at or under {min(cap, p.max_px):.2f}")
                    continue
                limit, vwap, fee = bp
                pair = eff_r + vwap + fee / q
                if pair >= p.max_pair - EPS:
                    why.append(f"{q}: pair {pair:.4f} not under {p.max_pair:.2f}")
                    continue
                r = risk(v, p, F, b, q, limit, fee)
                if r:
                    why.append(f"{q}: {r}")
                    continue
                pj = projected(F, b, q, q * limit + fee)
                best = (None, q, limit, vwap, fee, pair, pj)         # the target first (balancing shares > rising)
                break
            if best:
                _, q, limit, vwap, fee, pair, pj = best
                d.takes.append((b, limit, float(q), 0.0))
                d.tags[f"taker:{b}"] = "v5b"
                up["v5_last_send"] = now
                note("order", kind="balance", cycle=cyc["id"], outcome=b, shares=q, limit=limit, vwap=round(vwap, 4),
                     fee=round(fee, 5), pair_cost=round(pair, 4), proj_if_up=round(pj["Up"], 4), proj_if_down=round(pj["Down"], 4))
                return say(f"cycle {cyc['id']} balance: {q} {b} @ <= {limit:.2f}, pair {pair:.3f} -> if Up {pj['Up']:+.2f} / if Down {pj['Down']:+.2f}")
            if log is not None and now - cur("v5_last_rej", -1e18) >= 1000.0:
                up["v5_last_rej"] = now
                note("reject", kind="balance", cycle=cyc["id"], reasons=why[:6])
            return say(f"cycle {cyc['id']}: no balance now ({'; '.join(why[:3]) or 'nothing left under 5 shares'})")

    # momentum (no unresolved cycle)
    if t >= p.entry_cutoff_s:
        return say(f"no new momentum after {p.entry_cutoff_s:.0f} s")
    sig = []
    for o in OUT:
        ref = cur(f"v5_ref_{o}")
        last = max((f["ts"] for f in F if f["tag"] == "v5m" and f["outcome"] == o), default=-1e18)
        if ref is None or ask[o] - ref < p.rise - EPS:
            continue
        if not (p.min_px - EPS <= ask[o] <= p.max_px + EPS):
            continue
        if now - last < p.cooldown_s * 1000.0:
            continue
        sig.append((ask[o] - ref, o, ref))
    if not sig:
        return say("waiting: no outcome's ask {:.2f} over its reference (Up {} / Down {})".format(
            p.rise, *(f"{ask[o]:.2f} vs {cur(f'v5_ref_{o}'):.2f}" for o in OUT)))
    rise, o, ref = max(sig)
    why = []
    for q in range(int(p.max_order), int(p.min_order) - 1, -1):
        bp = buy_price(v, p, o, float(q), p.max_px)
        if bp is None:
            why.append(f"{q}: depth")
            continue
        limit, vwap, fee = bp
        r = risk(v, p, F, o, float(q), limit, fee)
        if r:
            why.append(f"{q}: {r}")
            continue
        if p.edge_gate:
            fair = getattr(v, "fair_up", None)
            fair = None if fair is None else (fair if o == "Up" else 1.0 - fair)
            if fair is None or fair - (vwap + fee / q) < p.min_edge - EPS:
                why.append(f"{q}: edge {'n/a' if fair is None else f'{fair - (vwap + fee / q):+.3f}'} under {p.min_edge:.2f}")
                continue
        pj = projected(F, o, q, q * limit + fee)
        n_id = cur("v5_cycles", 0) + 1
        up["v5_cycles"] = n_id
        up["v5_cycle"] = {"id": n_id, "phase": "entry", "o": o, "q": q, "sent": now, "prev_ref": ref}
        up[f"v5_ref_{o}"] = ask[o]                                # consumed: the next signal needs another 0.05
        up["v5_last_send"] = now
        d.takes.append((o, limit, float(q), 0.0))
        d.tags[f"taker:{o}"] = "v5m"
        note("order", kind="momentum", cycle=n_id, outcome=o, shares=q, rise=round(rise, 4), reference=ref, limit=limit,
             vwap=round(vwap, 4), fee=round(fee, 5), proj_if_up=round(pj["Up"], 4), proj_if_down=round(pj["Down"], 4))
        return say(f"momentum: {o} ask {ask[o]:.2f} rose {rise:.2f} over {ref:.2f}: {q} @ <= {limit:.2f}"
                   f" -> if Up {pj['Up']:+.2f} / if Down {pj['Down']:+.2f}")
    if log is not None and now - cur("v5_last_rej", -1e18) >= 1000.0:
        up["v5_last_rej"] = now
        note("reject", kind="momentum", outcome=o, reasons=why[:8])
    say(f"momentum {o}: no size passes ({'; '.join(why[-3:])})")
