"""Strategy V2, risk-reduced (user, 2026-10-13: "Build and Analyze My Risk-Reduced Polymarket Trading Strategy V2").
Paper first. Every buy is one FAK taker order sized and priced from the book's depth; one order on its way at a
time; every order passes the risk checks below first. Fill prices, not order prices, set every later threshold.

  A  entry      UP's mid rises entry_rise (0.05) over its low of the last lookback_s -> buy entry_shares (20) UP
                (once per market: done once anything filled). The low counts from the open only (user, 2026-10-13:
                a rise in the minute before the open fired the entry 1 s in, on a move already 5 s old).
  B  add-on     add_gap_s (6 s) after the latest UP fill, UP's mid add_step (0.05) over that fill's price -> buy
                add_shares (5) UP; at most add_max (2) add-ons per market (shared with D4). UP's mid stop_drop (0.05)
                under the latest UP fill stops UP add-ons for the market. No buying to recover a loss.
  C  balance    DOWN for UP shares not matched yet (oldest UP fills first): bal_shares (5), never more than the
                unmatched UP, only while the matched UP's cost a share (price + fee) + DOWN's executable price
                (depth VWAP) + its fee + slip < pair_max (0.95).
  D1 ladder     DOWN's mid at d1_px (0.32) or over for d1_hold_s (5 s) after trading under d1_below (0.30) this
                market -> buy d1_shares (5) DOWN, once per market.
  D2 ladder     DOWN's mid reaching d2_px (0.42) while UP > DOWN: at most d2_shares (5) DOWN (not the whole gap),
                only under C's pair condition; once per market, judged once per crossing.
  D3            DOWN at d3_px (0.45): no automatic buy (a note only).
  D4 ladder add after D1/D2 filled: the ladder token (DOWN; d4_up = UP) add_step over its latest fill, add_gap_s
                after it -> buy d4_shares (5), within the same add_max.
  E  low token  (user, 2026-10-13) either token, once it traded under e_low (0.30) this market: its mid at e1_px
                (0.40) -> buy e1_shares (10) of it; then at e2_px (0.50) -> e2_shares (10) more; once that filled,
                e_bal_shares (5) of the other token while the pair costs under pair_max (as C). Each once per
                token per market, one after the other (a jump past both levels buys both, in turn); none over
                an ask of e_max_px.
Risk, before any order: fresh book (max_book_age_ms); no buys in the last cutoff_s; per market budget_usd
(filled cost + fees + what is on its way, counted at $1 a share, + this order at its limit with fee);
|UP - DOWN| after the order <= max_net; marked-to-market loss (holdings at the executable bid depth) at
max_loss blocks the market and cancels our resting buys; venue minimum (min_size shares, $1 notional);
no order through more than max_slip over the best ask (thin book: skip). No selling."""
from __future__ import annotations

import math
from dataclasses import dataclass, fields

OUT = ("Up", "Down")
OTH = {"Up": "Down", "Down": "Up"}
EPS = 1e-9
TAGS = ("v2a", "v2b", "v2c", "v2d1", "v2d2", "v2d4", "v2e1", "v2e2", "v2e3")


@dataclass
class V2Params:
    enabled: bool = False
    lookback_s: float = 30.0
    entry_rise: float = 0.05
    entry_shares: float = 20.0
    add_step: float = 0.05
    add_shares: float = 5.0
    add_max: float = 2.0
    add_gap_s: float = 6.0
    stop_drop: float = 0.05
    pair_max: float = 0.95
    bal_shares: float = 5.0
    bal_gap_s: float = 2.0
    slip: float = 0.005
    max_slip: float = 0.02
    limit_over: float = 0.01              # a FAK's limit at least this over the best ask (a lost race fills a level up)
    max_spread: float = 0.03
    d1_px: float = 0.32
    d1_below: float = 0.30
    d1_hold_s: float = 5.0
    d1_shares: float = 5.0
    d2_px: float = 0.42
    d2_shares: float = 5.0
    d3_px: float = 0.45
    d4_up: bool = False
    d4_shares: float = 5.0
    e_low: float = 0.30
    e1_px: float = 0.40
    e1_shares: float = 10.0
    e2_px: float = 0.50
    e2_shares: float = 10.0
    e_bal_shares: float = 5.0
    e_max_px: float = 0.85
    budget_usd: float = 30.0
    max_net: float = 30.0
    max_loss: float = 10.0
    cutoff_s: float = 60.0
    from_s: float = 1.0
    max_book_age_ms: float = 1500.0
    resend_ms: float = 1000.0

    @classmethod
    def from_settings(cls, s) -> "V2Params":
        return cls(**{f.name: getattr(s, "v2_" + f.name) for f in fields(cls)})


def _mid(v, o):
    t = v.books.get(o) or {}
    b, a = t.get("bid"), t.get("ask")
    return (a + b) / 2.0 if a is not None and b is not None else None


def fills_of(v) -> list:
    return sorted(getattr(v, "v2_fills", None) or [], key=lambda f: f["ts"])


def buy_price(v, p: V2Params, o: str, n: float, cap: float | None = None):
    """(limit, vwap, fee) to buy n shares of o through the ask depth, at most max_slip over the best ask (and
    at most cap); None when the book cannot fill n there. The limit goes limit_over past the level that fills
    n (user, 2026-10-13: orders at exactly the ask lost the race to faster bots and got nothing)."""
    t = v.books.get(o) or {}
    a = t.get("ask")
    if a is None:
        return None
    levels = t.get("asks") or [(a, t.get("ask_size") or 0.0)]
    got = cost = fee = 0.0
    limit = a
    for px, sz in sorted(levels):
        if px > a + p.max_slip + EPS:
            break
        q = min(sz, n - got)
        if q <= EPS:
            continue
        got += q
        cost += q * px
        fee += v.market.fees.taker_fee(px, q)
        limit = px
        if got >= n - EPS:
            top = min(a + p.max_slip, 1.0 if cap is None else cap)
            if limit > top + EPS:
                return None
            return round(min(max(limit, a + p.limit_over), top), 4), cost / n, fee
    return None


def bid_value(v, o: str, n: float) -> float:
    """What n shares of o would fetch selling into the bid depth now (0 for what the depth does not hold)."""
    if n <= EPS:
        return 0.0
    t = v.books.get(o) or {}
    levels = t.get("bids") or ([(t["bid"], t.get("bid_size") or 0.0)] if t.get("bid") is not None else [])
    left, val = n, 0.0
    for px, sz in sorted(levels, reverse=True):
        q = min(sz, left)
        val += q * px - v.market.fees.taker_fee(px, q)
        left -= q
        if left <= EPS:
            break
    return val


def unmatched_up(fills: list) -> list:
    """UP lots (cost a share with fee, shares) not matched by DOWN shares yet, oldest first."""
    return unmatched(fills, "Up")


def unmatched(fills: list, side: str) -> list:
    """side's lots (cost a share with fee, shares) not matched by the other token's shares yet, oldest first."""
    lots = [[(f["price"] * f["size"] + f["fee"]) / f["size"], f["size"]] for f in fills if f["outcome"] == side and f["size"] > EPS]
    d = sum(f["size"] for f in fills if f["outcome"] == OTH[side])
    for lot in lots:
        q = min(lot[1], d)
        lot[1] -= q
        d -= q
    return [lot for lot in lots if lot[1] > EPS]


def _pair(v, p: V2Params, fills: list, q: float, buy: str = "Down"):
    """C's pair condition for q shares of `buy` against the other token's unmatched lots:
    (ok, combined cost a share, buy_price tuple, why)."""
    lots = unmatched(fills, OTH[buy])
    have = sum(l[1] for l in lots)
    if have < q - EPS:
        return False, None, None, f"only {have:g} {OTH[buy].upper()} unmatched"
    left, up_cost = q, 0.0
    for c, n in lots:
        k = min(n, left)
        up_cost += c * k
        left -= k
        if left <= EPS:
            break
    a = (v.books.get(buy) or {}).get("ask")
    if a is None:
        return False, None, None, f"no {buy.upper()} ask"
    cap = math.floor((p.pair_max - up_cost / q - p.slip - v.market.fees.taker_fee(a, 1.0) - 1e-6) * 100) / 100
    bp = buy_price(v, p, buy, q, cap)                       # its limit never makes the pair cost pair_max
    if bp is None:
        return False, None, None, f"{buy.upper()} cannot fill {q:g} at or under {cap:.2f} (pair under {p.pair_max:.2f})"
    comb = up_cost / q + bp[1] + bp[2] / q + p.slip
    if comb >= p.pair_max - EPS:
        return False, comb, bp, f"pair {comb:.3f} not under {p.pair_max:.2f}"
    return True, comb, bp, f"pair {comb:.3f} < {p.pair_max:.2f}"


def risk(v, p: V2Params, o: str, n: float, limit: float, fee: float, U: float, D: float) -> str:
    """'' when an order of n shares of o at limit passes every risk check, else why not."""
    ms = v.market.min_size
    if n < ms - EPS:
        return f"{n:g} under the venue minimum {ms:g}"
    if n * limit < 1.0 - EPS:
        return f"${n * limit:.2f} under the $1 minimum"
    pend = sum((v.in_flight_taker or {}).values())
    committed = v.window_cost + pend * 1.0 + (v.reserved or 0.0)
    if committed + n * limit + fee > p.budget_usd + EPS:
        return f"budget: ${committed:.2f} committed + ${n * limit + fee:.2f} > ${p.budget_usd:.0f}"
    u2, d2 = U + (n if o == "Up" else 0.0), D + (n if o == "Down" else 0.0)
    if abs(u2 - d2) > p.max_net + EPS and abs(u2 - d2) > abs(U - D) - EPS:
        return f"net exposure {u2 - d2:+g} over {p.max_net:g}"
    return ""


def decide(v, p: V2Params, d) -> None:
    if not p.enabled:
        return
    st = v.whisk or {}
    up = d.whisk_update
    cur = lambda k, dflt=None: up[k] if k in up else st.get(k, dflt)
    now = v.now_ms
    F = fills_of(v)
    U = sum(f["size"] for f in F if f["outcome"] == "Up")
    D = sum(f["size"] for f in F if f["outcome"] == "Down")
    cost = v.window_cost
    um, dm = _mid(v, "Up"), _mid(v, "Down")
    tags = lambda *ts: [f for f in F if f["tag"] in ts]
    adds = len({f["oid"] for f in tags("v2b", "v2d4")})
    head = f"v2 | Up {U:g} Down {D:g} | if Up {U - cost:+.2f} if Down {D - cost:+.2f}"
    say = lambda s: d.why.__setitem__("taker", f"{head} | {s}")

    # bookkeeping every tick, before any gate
    if dm is not None:
        if dm < p.d1_below - EPS and not st.get("v2_dn_below"):
            up["v2_dn_below"] = True
        if dm >= p.d1_px - EPS:
            if st.get("v2_d1_since") is None:
                up["v2_d1_since"] = now
        elif st.get("v2_d1_since") is not None:
            up["v2_d1_since"] = None
        if dm < p.d2_px - EPS and not st.get("v2_d2_armed"):
            up["v2_d2_armed"] = True
    for o, m_ in (("Up", um), ("Down", dm)):
        if m_ is not None and m_ < p.e_low - EPS and not st.get(f"v2_e_below_{o}"):
            up[f"v2_e_below_{o}"] = True
    last_up = max((f for f in F if f["outcome"] == "Up"), key=lambda f: f["ts"], default=None)
    if last_up and um is not None and um <= last_up["price"] - p.stop_drop + EPS and not st.get("v2_up_stop"):
        up["v2_up_stop"] = f"UP {um:.3f} fell {p.stop_drop:.2f} under its fill {last_up['price']:.2f}"

    # loss limit: marked to market at the bid depth
    if st.get("v2_blocked"):
        return say(f"blocked: {st['v2_blocked']}")
    mtm = bid_value(v, "Up", U) + bid_value(v, "Down", D) - cost
    if U + D > EPS and mtm <= -p.max_loss + EPS:
        up["v2_blocked"] = f"loss limit: marked {mtm:+.2f} <= -{p.max_loss:.2f}"
        for o in OUT:
            d.cancels.extend(r.id for r in v.resting.get(o, []) if r.id not in d.cancels)
        return say(f"blocked: {up['v2_blocked']}; resting buys cancelled")
    if v.book_age_ms is None or v.book_age_ms > p.max_book_age_ms or um is None or dm is None:
        return say("waiting for a fresh book")
    if v.secs_left < p.cutoff_s or v.secs_in < p.from_s:
        return say(f"no new buys in the last {p.cutoff_s:.0f} s" if v.secs_left < p.cutoff_s else "waiting for the open")
    if any(x > EPS for x in (v.in_flight_taker or {}).values()):
        return say("an order is on its way")
    if now - cur("v2_last_send", -1e18) < p.resend_ms:
        return say("just sent")

    skipped = []

    def send(o, n, tag, why, bp=None):
        bp = bp or buy_price(v, p, o, n)
        if bp is None:
            skipped.append(f"{why}: {o} depth too thin for {n:g}")
            return False
        r = risk(v, p, o, n, bp[0], bp[2], U, D)
        if r:
            skipped.append(f"{why}: {r}")
            return False
        d.takes.append((o, bp[0], n, 0.0))
        d.tags[f"taker:{o}"] = tag
        up["v2_last_send"] = now
        up[f"v2_last_{tag}"] = now
        say(f"{why}: FAK {n:g} {o} @ <= {bp[0]:.2f} (depth VWAP {bp[1]:.3f})")
        return True

    # C: balance unmatched UP with DOWN under the pair cost
    lots = unmatched_up(F)
    un = sum(l[1] for l in lots) - (v.in_flight_taker or {}).get("Down", 0.0)
    if un >= v.market.min_size - EPS and now - cur("v2_last_v2c", -1e18) >= p.bal_gap_s * 1000.0:
        q = min(p.bal_shares, math.floor(un * 100 + 1e-6) / 100)
        ok, comb, bp, why = _pair(v, p, F, q)
        if ok and send("Down", q, "v2c", f"C balance: {why}", bp):
            return
        if not ok:
            skipped.append(f"C: {why}")

    # D2: DOWN reaching d2_px while UP > DOWN, judged once per crossing, done once per market
    if dm >= p.d2_px - EPS and st.get("v2_d2_armed") and not tags("v2d2"):
        up["v2_d2_armed"] = False
        if U - D >= v.market.min_size - EPS:
            q = min(p.d2_shares, U - D)
            ok, comb, bp, why = _pair(v, p, F, q)
            if ok and send("Down", q, "v2d2", f"D2 DOWN {dm:.3f} >= {p.d2_px:.2f}: {why}", bp):
                return
            skipped.append(f"D2 skipped: {why}")
        else:
            skipped.append("D2 skipped: UP not over DOWN")

    # D1: DOWN held at d1_px for d1_hold_s after trading under d1_below
    since = cur("v2_d1_since")
    if (st.get("v2_dn_below") and since is not None and now - since >= p.d1_hold_s * 1000.0 and not tags("v2d1")
            and cur("v2_d1_tries", 0) < 3):
        if send("Down", p.d1_shares, "v2d1", f"D1 DOWN {dm:.3f} held >= {p.d1_px:.2f} for {p.d1_hold_s:g} s"):
            up["v2_d1_tries"] = cur("v2_d1_tries", 0) + 1     # a FAK that got nothing may go again (3 at most)
            return

    # E: a low token's comeback: 10 at e1_px, 10 at e2_px, then 5 of the other token under the pair cost
    for o, m_ in (("Up", um), ("Down", dm)):
        if not cur(f"v2_e_below_{o}"):
            continue
        mine = lambda tag: [f for f in F if f["tag"] == tag and f["outcome"] == o]
        a_ = (v.books.get(o) or {}).get("ask") or 1.0
        if not mine("v2e1"):
            if m_ >= p.e1_px - EPS and p.e1_shares > 0 and cur(f"v2_e1_tries_{o}", 0) < 3:
                if a_ > p.e_max_px + EPS:
                    skipped.append(f"E1 {o}: ask {a_:.2f} over {p.e_max_px:.2f}")
                elif send(o, p.e1_shares, "v2e1", f"E1 {o} {m_:.3f} >= {p.e1_px:.2f} after trading under {p.e_low:.2f}"):
                    up[f"v2_e1_tries_{o}"] = cur(f"v2_e1_tries_{o}", 0) + 1
                    return
            continue
        if not mine("v2e2"):
            if m_ >= p.e2_px - EPS and p.e2_shares > 0 and cur(f"v2_e2_tries_{o}", 0) < 3:
                if a_ > p.e_max_px + EPS:
                    skipped.append(f"E2 {o}: ask {a_:.2f} over {p.e_max_px:.2f}")
                elif send(o, p.e2_shares, "v2e2", f"E2 {o} {m_:.3f} >= {p.e2_px:.2f}"):
                    up[f"v2_e2_tries_{o}"] = cur(f"v2_e2_tries_{o}", 0) + 1
                    return
            continue
        oth = OTH[o]
        if not [f for f in F if f["tag"] == "v2e3" and f["outcome"] == oth] and cur(f"v2_e3_tries_{o}", 0) < 3:
            have = sum(l[1] for l in unmatched(F, o))
            q = min(p.e_bal_shares, math.floor(have * 100 + 1e-6) / 100)
            if q >= v.market.min_size - EPS:
                ok, comb, bp, why = _pair(v, p, F, q, oth)
                if ok and send(oth, q, "v2e3", f"E3 balance {o}'s rise with {oth}: {why}", bp):
                    up[f"v2_e3_tries_{o}"] = cur(f"v2_e3_tries_{o}", 0) + 1
                    return
                if not ok:
                    skipped.append(f"E3: {why}")

    # A: the first UP buy on a rise from the local low
    low = getattr(v, "v2_up_low", None)
    sp = (v.books.get("Up") or {})
    spread = (sp.get("ask") or 1.0) - (sp.get("bid") or 0.0)
    entered = bool(tags("v2a"))
    if not entered:
        if low is not None and um >= low + p.entry_rise - EPS:
            if spread > p.max_spread + EPS:
                skipped.append(f"A: UP spread {spread:.2f} too wide to trust")
            elif send("Up", p.entry_shares, "v2a", f"A entry: UP {um:.3f} rose {um - low:.3f} from its {p.lookback_s:g} s low {low:.3f}"):
                return
        else:
            skipped.append(f"A: UP {um:.3f}, {p.lookback_s:g} s low {'-' if low is None else f'{low:.3f}'} (entry at +{p.entry_rise:.2f})")

    # B: UP add-ons on fill prices
    if entered and adds < p.add_max and not cur("v2_up_stop") and last_up and now - last_up["ts"] >= p.add_gap_s * 1000.0 \
            and um >= last_up["price"] + p.add_step - EPS:
        if send("Up", p.add_shares, "v2b", f"B add-on {adds + 1}: UP {um:.3f} >= fill {last_up['price']:.2f} + {p.add_step:.2f}"):
            return

    # D4: after the ladder filled, its token's add-on
    if tags("v2d1", "v2d2") and adds < p.add_max:
        t = "Up" if p.d4_up else "Down"
        lt = max((f for f in F if f["outcome"] == t), key=lambda f: f["ts"], default=None)
        mt = um if t == "Up" else dm
        if lt and now - lt["ts"] >= p.add_gap_s * 1000.0 and mt >= lt["price"] + p.add_step - EPS:
            if send(t, p.d4_shares, "v2d4", f"D4 ladder add-on: {t} {mt:.3f} >= fill {lt['price']:.2f} + {p.add_step:.2f}"):
                return

    notes = list(skipped)
    if dm >= p.d3_px - EPS:
        notes.append(f"D3: DOWN {dm:.3f} >= {p.d3_px:.2f}, no automatic buy")
    if cur("v2_up_stop"):
        notes.append(f"UP add-ons stopped ({cur('v2_up_stop')})")
    if adds >= p.add_max:
        notes.append(f"add-ons used {adds}/{p.add_max:g}")
    say("; ".join(notes) if notes else f"holding; marked {mtm:+.2f}")
