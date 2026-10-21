"""Strategy V4, P/L-driven (user, 2026-10-13: "Build My Polymarket P/L-Driven Trading Bot"). Paper first.

Every candidate order is judged by the whole position's settlement P/L after it:
    pnl_up = UP shares - total cost (fees in),  pnl_down = DOWN shares - total cost
and the size is the permitted one whose (pnl_selected, pnl_other) lands nearest the active target pair
(+win, -lose): distance = sqrt((pnl_sel - win)^2 + (pnl_other + lose)^2). An order must cut that distance by at
least min_improve ($) and pass every hard limit; otherwise no order, the reason in the bot's 'why' and the ledger.

Targets (configurable): middle mode, the token priced mid_lo..mid_hi (0.40-0.60): +mid_win / -mid_lose
(12.80 / 12.00); high mode, over mid_hi: +high_win / -high_lose (25 / 50); low-price momentum under mid_lo:
+low_win / -low_lose (12.80 / 12.00). UP and DOWN are symmetric: 'selected' is the token a signal is about.

Signals (one central engine, one order on its way at a time, the best distance cut wins; ties by this order):
  balance    the token of the losing outcome, sizes bal_size1..5 (5,10,20,30,40), nearest +bal_win / -bal_lose
             (5 / 20: the leading outcome +5, the other -20; user, 2026-10-13); judged bal_delay_s (3 s) after
             each qualifying fill and at once (bal_gap_s apart) when the losing outcome is under -(20 + bal_tol 3);
             entry / momentum / both-negative never push the loss past -23 (cap_new_loss), so the two do not fight
  both-neg   pnl_up < 0 and pnl_down < 0: the dearer token, sizes size1..5 (5,10,20,40,60); only sizes that
             make its outcome positive, nearest the target first; none -> reported, no order; entry_gap_s apart
  momentum   the cheaper token's mid up fast_th (0.04) within fast_lookback_s (2 s): it, sizes size1..5;
             mom_gap_s (5 s) apart; at most max_consec momentum / both-negative orders in a row
  entry      the market slow (Up mid moved < slow_th 0.02 over slow_lookback_s 3 s): the dearer token (mid
             0.40+), sizes size1..5; entry_gap_s (6 s) apart
Hard limits: buy price (limit and VWAP) within min_px..max_px (0.10-0.85); spread <= max_spread; the ask depth
fills the size within max_slip; spend <= max_spend; each outcome's shares <= max_shares; worst projected
outcome >= -max_loss unless the order improves it; fresh book; none in the last cutoff_s; paper only unless
live_ok. Not possible by construction: buying both tokens at their asks never improves both outcomes (the
books mirror: Up ask + Down ask = 1 + the spread >= $1, plus fees)."""
from __future__ import annotations

import math
from dataclasses import dataclass, fields

from .v2 import buy_price
from .v3 import position

OUT = ("Up", "Down")
OTH = {"Up": "Down", "Down": "Up"}
EPS = 1e-9
QUAL = ("v4e", "v4m", "v4n")                  # fills a balance follows
TAGS = QUAL + ("v4b",)


@dataclass
class V4Params:
    enabled: bool = False
    live_ok: bool = False
    min_px: float = 0.10
    max_px: float = 0.85
    mid_lo: float = 0.40
    mid_hi: float = 0.60
    mid_win: float = 12.80
    mid_lose: float = 12.00
    high_win: float = 25.0
    high_lose: float = 50.0
    low_win: float = 12.80
    low_lose: float = 12.00
    entry_gap_s: float = 6.0
    slow_lookback_s: float = 3.0
    slow_th: float = 0.02
    mom_gap_s: float = 5.0
    fast_lookback_s: float = 2.0
    fast_th: float = 0.04
    bal_delay_s: float = 3.0
    bal_win: float = 5.0                  # balance target (user, 2026-10-13): the leading outcome +bal_win ...
    bal_lose: float = 20.0                # ... the other -bal_lose ("buy the balancing token to lose small money")
    bal_tol: float = 3.0                  # the losing outcome under -(bal_lose + bal_tol) -> balance now
    bal_gap_s: float = 3.0                # the least time between two balances
    cap_new_loss: bool = True             # entry / momentum / both-negative never push the loss past -(bal_lose + bal_tol)
    bal_reserve: float = 25.0             # of max_spend kept for balances: the other orders stop at max_spend - this
    early_s: float = 120.0                # the first early_s of a market (user, 2026-10-13): balance to EVEN outcomes...
    early_tol: float = 3.0                # ... whenever |if Up - if Down| (= |Up shares - Down shares|) is over this ...
    early_skip_px: float = 0.73           # ... unless the dearer token is over this (then no balancing then)
    max_retries: float = 3.0              # an order that got nothing goes again at once, this many times in a row
    early_pair: float = 0.97              # early balance only while its token's ask + the entry token's average < this
    size1: float = 5.0
    size2: float = 10.0
    size3: float = 20.0
    size4: float = 40.0
    size5: float = 60.0
    bal_size1: float = 5.0
    bal_size2: float = 10.0
    bal_size3: float = 20.0
    bal_size4: float = 30.0
    bal_size5: float = 40.0
    max_spend: float = 100.0
    max_shares: float = 200.0
    max_loss: float = 60.0
    max_consec: float = 4.0
    max_spread: float = 0.03
    max_slip: float = 0.02
    limit_over: float = 0.01
    min_improve: float = 0.50
    reach_tol: float = 3.0
    cutoff_s: float = 30.0
    from_s: float = 1.0
    max_book_age_ms: float = 1500.0

    @classmethod
    def from_settings(cls, s) -> "V4Params":
        return cls(**{f.name: getattr(s, "v4_" + f.name) for f in fields(cls)})

    def sizes(self):
        return sorted({x for x in (self.size1, self.size2, self.size3, self.size4, self.size5) if x > 0})

    def bal_sizes(self):
        return sorted({x for x in (self.bal_size1, self.bal_size2, self.bal_size3, self.bal_size4, self.bal_size5) if x > 0})


def target(p: V4Params, px: float) -> tuple[str, float, float]:
    """(mode, win, lose) for a selected token priced px."""
    if px > p.mid_hi + EPS:
        return "high", p.high_win, p.high_lose
    if px >= p.mid_lo - EPS:
        return "middle", p.mid_win, p.mid_lose
    return "low", p.low_win, p.low_lose


def distance(pnl: dict, sel: str, win: float, lose: float) -> float:
    if win == "mirror":                           # one outcome +X, the other -X: their sum ~ 0 (user, 2026-10-13)
        return abs(pnl["Up"] + pnl["Down"])
    return math.hypot(pnl[sel] - win, pnl[OTH[sel]] + lose)


def project(pos: dict, o: str, q: float, cost: float) -> dict:
    """Both settlement outcomes after buying q of o for cost (fees in)."""
    return {x: pos["shares"][x] + (q if x == o else 0.0) - pos["total"] - cost for x in OUT}


def candidates(v, p: V4Params, pos: dict, buy: str, sel: str, win: float, lose: float, sizes, need_positive=False,
               loss_cap: float | None = None, spend_cap: float | None = None, px_cap: float | None = None):
    """Every permitted size of `buy`, judged for the target (sel +win / other -lose). Each: dict with q, ok, why,
    limit, vwap, cost, pnl, dist."""
    out = []
    t = v.books.get(buy) or {}
    a, b = t.get("ask"), t.get("bid")
    for q in sizes:
        c = {"q": q, "ok": False}
        out.append(c)
        if a is None or b is None:
            c["why"] = "no book"
            continue
        if a - b > p.max_spread + EPS:
            c["why"] = f"spread {a - b:.2f} over {p.max_spread:.2f}"
            continue
        top = p.max_px if px_cap is None else min(p.max_px, px_cap)
        bp = buy_price(v, p, buy, q, top)
        if bp is None:
            c["why"] = f"depth cannot fill {q:g} within {p.max_slip:.2f} / at or under {top:.2f}"
            continue
        limit, vwap, fee = bp
        cost = q * vwap + fee
        pnl = project(pos, buy, q, cost)
        c.update(limit=limit, vwap=vwap, cost=cost, pnl=pnl, dist=distance(pnl, sel, win, lose))
        if vwap < p.min_px - EPS or limit > p.max_px + EPS:
            c["why"] = f"price {vwap:.2f} outside {p.min_px:.2f}-{p.max_px:.2f}"
        elif q * limit < 1.0 - EPS or q < v.market.min_size - EPS:
            c["why"] = "under the venue minimum"
        elif pos["total"] + sum((v.in_flight_taker or {}).values()) + q * limit + fee > (spend_cap or p.max_spend) + EPS:
            c["why"] = f"spend over ${(spend_cap or p.max_spend):.0f}"
        elif pos["shares"][buy] + q > p.max_shares + EPS:
            c["why"] = f"{buy} shares over {p.max_shares:g}"
        elif min(pnl.values()) < -p.max_loss - EPS and min(pnl.values()) < pos["worst"] - EPS:
            c["why"] = f"worst outcome {min(pnl.values()):+.2f} past -{p.max_loss:.0f}"
        elif need_positive and pnl[sel] <= EPS:
            c["why"] = f"{sel} outcome still {pnl[sel]:+.2f}"
        elif loss_cap is not None and min(pnl.values()) < -loss_cap - EPS and min(pnl.values()) < pos["worst"] - EPS:
            c["why"] = f"loss {min(pnl.values()):+.2f} past -{loss_cap:g} (the balance would undo it)"
        else:
            c["ok"] = True
    return out


def _mid(v, o):
    t = v.books.get(o) or {}
    b, a = t.get("bid"), t.get("ask")
    return (a + b) / 2.0 if a is not None and b is not None else None


def _up_ago(v, secs: float):
    """Up's mid secs ago from v.v4_up_hist [(ts_ms, mid)], None without data that old."""
    h = getattr(v, "v4_up_hist", None) or []
    t = v.now_ms - secs * 1000.0
    best = None
    for ts, m in h:
        if ts <= t:
            best = m
        else:
            break
    return best


def decide(v, p: V4Params, d) -> None:
    if not p.enabled:
        return
    st = v.whisk or {}
    up = d.whisk_update
    cur = lambda k, dflt=None: up[k] if k in up else st.get(k, dflt)
    now = v.now_ms
    F = sorted(getattr(v, "v2_fills", None) or [], key=lambda f: f["ts"])
    pos = position(F)
    pnl = pos["if"]
    mid = {o: _mid(v, o) for o in OUT}
    head = f"v4 | if Up {pnl['Up']:+.2f} if Down {pnl['Down']:+.2f}"
    say = lambda s: d.why.__setitem__("taker", f"{head} | {s}")
    log = getattr(d, "ledger", None)

    if getattr(v, "live", False) and not p.live_ok:
        return say("live trading is not enabled for V4 (paper only)")
    if v.book_age_ms is None or v.book_age_ms > p.max_book_age_ms or None in mid.values():
        return say("waiting for a fresh book")
    if v.secs_left < p.cutoff_s or v.secs_in < p.from_s:
        return say(f"no orders in the last {p.cutoff_s:.0f} s" if v.secs_left < p.cutoff_s else "waiting for the open")
    if any(x > EPS for x in (v.in_flight_taker or {}).values()):
        return say("an order is on its way")

    # the last order got nothing: undo its timers and try it again at once (user, 2026-10-13), re-priced
    retry = None
    sent = cur("v4_sent")
    if sent:
        up["v4_sent"] = None
        if any(f["tag"] == sent["tag"] and f["ts"] >= sent["ts"] - 1.0 for f in F):
            up["v4_retries"] = 0
        else:
            for k, x in (sent.get("prev") or {}).items():
                up[k] = x
            if cur("v4_retries", 0) < p.max_retries:
                retry = sent["sig"]
                up["v4_retries"] = cur("v4_retries", 0) + 1

    hi = "Up" if mid["Up"] >= mid["Down"] else "Down"
    lo = OTH[hi]
    proposals = []                                   # (cut, priority, signal, buy, sel, mode, win, lose, best, cands)
    reasons = []

    cap = (p.bal_lose + p.bal_tol) if p.cap_new_loss else None

    def consider(sig, prio, buy, sel, px_sel, sizes, need_positive=False, tgt=None, px_cap=None):
        mode, win, lose = tgt or target(p, px_sel)
        now_d = distance(pnl, sel, win, lose)
        bal = sig == "balance"
        cands = candidates(v, p, pos, buy, sel, win, lose, sizes, need_positive, None if bal else cap,
                           p.max_spend if bal else max(0.0, p.max_spend - p.bal_reserve), px_cap)
        ok = [c for c in cands if c["ok"]]
        if not ok:
            reasons.append(f"{sig}: no permitted size ({'; '.join(sorted({c.get('why', '') for c in cands}))})")
            return
        best = min(ok, key=lambda c: (c["dist"], c["q"]))
        cut = now_d - best["dist"]
        if cut < p.min_improve - EPS:
            reasons.append(f"{sig}: best {best['q']:g} {buy} cuts the distance to "
                           f"{'mirror outcomes (+X / -X)' if win == 'mirror' else f'{mode} +{win:g}/-{lose:g}'} by {cut:.2f} only")
            return
        proposals.append((cut, prio, sig, buy, sel, mode, win, lose, best, cands, now_d))

    # balance (user, 2026-10-13: "if the P/L is not near +5 / -20, buy the balancing token to lose small money"):
    # the token of the losing outcome, sizes bal_size1..5, nearest +bal_win (leading) / -bal_lose (other); judged
    # bal_delay_s after each qualifying fill, and at once whenever the losing outcome is under -(bal_lose + bal_tol)
    lead = "Up" if pnl["Up"] >= pnl["Down"] else "Down"
    q_fill = max((f for f in F if f["tag"] in QUAL), key=lambda f: f["ts"], default=None)
    due = q_fill and cur("v4_bal_for") != q_fill["oid"] and now - q_fill["ts"] >= p.bal_delay_s * 1000.0
    gap_ok = retry == "balance" or now - (cur("v4_last_b") or -1e18) >= p.bal_gap_s * 1000.0
    if v.secs_in < p.early_s:
        # the first early_s (user, 2026-10-13): mirror outcomes -- one +X, the other -X, their sum ~ 0 -- with the
        # balancing token (the one the last entry did not buy), only while its ask + the entry token's average price
        # < early_pair (0.97); no balance just because an entry filled; none while the dear token > early_skip_px
        if due:
            up["v4_bal_for"] = q_fill["oid"]
        tot = pnl["Up"] + pnl["Down"]
        if F and q_fill is not None and abs(tot) > p.early_tol + EPS:
            ent = q_fill["outcome"]
            bt = OTH[ent]
            ef = [f for f in F if f["outcome"] == ent]
            avg_e = sum(f["price"] * f["size"] for f in ef) / max(EPS, sum(f["size"] for f in ef))
            a_b = (v.books.get(bt) or {}).get("ask")
            if mid[hi] > p.early_skip_px + EPS:
                reasons.append(f"balance: first {p.early_s:.0f} s and {hi} {mid[hi]:.3f} over {p.early_skip_px:.2f}: not balancing")
            elif a_b is None or a_b + avg_e >= p.early_pair - EPS:
                reasons.append(f"balance: {bt} ask {a_b if a_b is None else f'{a_b:.2f}'} + {ent} average {avg_e:.3f}"
                               f" not under {p.early_pair:.2f}: waiting")
            elif gap_ok:
                consider("balance", 0, bt, ent, mid[ent], p.bal_sizes(), tgt=("mirror", "mirror", None),
                         px_cap=p.early_pair - avg_e - 1e-6)
    else:
        btgt = ("balance", p.bal_win, p.bal_lose)
        deep = F and pnl[OTH[lead]] < -(p.bal_lose + p.bal_tol) - EPS and gap_ok
        if due or deep:
            if distance(pnl, lead, p.bal_win, p.bal_lose) > p.bal_tol + EPS and pnl[OTH[lead]] < -EPS:
                consider("balance", 0, OTH[lead], lead, mid[lead], p.bal_sizes(), tgt=btgt)
            if due and not proposals:
                up["v4_bal_for"] = q_fill["oid"]           # judged once: nothing good -> skipped
    # both outcomes negative: the dearer token, only sizes that make its outcome positive
    if pnl["Up"] < -EPS and pnl["Down"] < -EPS and (retry == "both-negative" or now - (cur("v4_last_n") or -1e18) >= p.entry_gap_s * 1000.0) \
            and cur("v4_consec", 0) < p.max_consec:
        n0 = len(proposals)
        consider("both-negative", 1, hi, hi, mid[hi], p.sizes(), need_positive=True)
        if len(proposals) == n0:
            up["v4_last_n"] = now                       # reported; looked at again after entry_gap_s
            up["v4_cnt_nosize"] = cur("v4_cnt_nosize", 0) + 1
    # momentum: the cheaper token rising fast
    lo_then = _up_ago(v, p.fast_lookback_s)
    if retry == "momentum" and sent.get("buy") in OUT:
        consider("momentum", 2, sent["buy"], sent["buy"], mid[sent["buy"]], p.sizes())   # again, rise or not
    elif lo_then is not None:
        lo_then = lo_then if lo == "Up" else 1.0 - lo_then
        if mid[lo] - lo_then >= p.fast_th - EPS and now - (cur("v4_last_m") or -1e18) >= p.mom_gap_s * 1000.0 \
                and cur("v4_consec", 0) < p.max_consec:
            consider("momentum", 2, lo, lo, mid[lo], p.sizes())
    # entry: a slow market, the dearer token
    up_then = _up_ago(v, p.slow_lookback_s)
    if retry == "entry" and sent.get("buy") in OUT:
        consider("entry", 3, sent["buy"], sent["buy"], mid[sent["buy"]], p.sizes())         # again, slow or not
    elif up_then is not None and abs(mid["Up"] - up_then) < p.slow_th - EPS and mid[hi] >= p.mid_lo - EPS \
            and now - (cur("v4_last_e") or -1e18) >= p.entry_gap_s * 1000.0:
        consider("entry", 3, hi, hi, mid[hi], p.sizes())

    # where the position stands against the target now (the dashboard line)
    mode, win, lose = target(p, mid[hi])
    dist_now = distance(pnl, hi, win, lose)
    if F and dist_now <= p.reach_tol + EPS and not st.get("v4_reached"):
        up["v4_reached"] = True
    if pnl["Up"] < -EPS and pnl["Down"] < -EPS and not st.get("v4_bothneg"):
        up["v4_bothneg"] = True

    if not proposals:
        if log is not None and reasons and now - cur("v4_last_log", -1e18) >= 1000.0:
            up["v4_last_log"] = now
            log.append({"event": "skip", "reasons": reasons, "if_up": pnl["Up"], "if_down": pnl["Down"],
                        "up_mid": mid["Up"], "down_mid": mid["Down"]})
        return say((f"{mode} target {hi} +{win:g}/-{lose:g}, distance {dist_now:.2f} | " +
                    ("; ".join(reasons) if reasons else "waiting for a signal")))
    early = v.secs_in < p.early_s                    # early, a mirror balance goes before anything else
    cut, prio, sig, buy, sel, mode, win, lose, best, cands, d0 = max(
        proposals, key=lambda x: (early and x[2] == "balance", x[0], -x[1]))
    d.takes.append((buy, best["limit"], best["q"], 0.0))
    tag = {"balance": "v4b", "both-negative": "v4n", "momentum": "v4m", "entry": "v4e"}[sig]
    d.tags[f"taker:{buy}"] = tag
    key = {"balance": None, "both-negative": "v4_last_n", "momentum": "v4_last_m", "entry": "v4_last_e"}[sig]
    up["v4_sent"] = {"sig": sig, "tag": tag, "buy": buy, "ts": now,     # what to undo if it gets nothing
                     "prev": {k: cur(k) for k in ("v4_last_e", "v4_last_m", "v4_last_n", "v4_last_b", "v4_bal_for", "v4_consec")}}
    if key:
        up[key] = now
    if sig == "balance":
        if q_fill:
            up["v4_bal_for"] = q_fill["oid"]
        up["v4_last_b"] = now
        up["v4_consec"] = 0
    elif sig in ("momentum", "both-negative"):
        up["v4_consec"] = cur("v4_consec", 0) + 1
    else:
        up["v4_consec"] = 0
    up["v4_expect"] = round(best["vwap"], 4)
    up["v4_target"] = [sel, mode, win, lose]
    if log is not None:
        log.append({"event": "order", "signal": sig, "buy": buy, "selected": sel, "mode": mode,
                    "target": "mirror" if win == "mirror" else [win, -lose],
                    "if_up": pnl["Up"], "if_down": pnl["Down"], "distance": d0, "up_mid": mid["Up"], "down_mid": mid["Down"],
                    "chosen": best["q"], "candidates": [{k: (round(x, 4) if isinstance(x, float) else x) for k, x in c.items()
                                                         if k in ("q", "ok", "why", "vwap", "dist")}
                                                        | ({"if_up": round(c["pnl"]["Up"], 2), "if_down": round(c["pnl"]["Down"], 2)} if "pnl" in c else {})
                                                        for c in cands]})
    tgt_s = ("mirror outcomes (+X / -X)" if win == "mirror" else f"{mode} target {sel} +{win:g}/-{lose:g}") + (" (retry)" if retry == sig else "")
    say(f"{sig}: {best['q']:g} {buy} @ <= {best['limit']:.2f} (VWAP {best['vwap']:.3f}) -> if Up {best['pnl']['Up']:+.2f} / "
        f"if Down {best['pnl']['Down']:+.2f}; {tgt_s}, distance {d0:.2f} -> {best['dist']:.2f}")
