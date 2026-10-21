"""Pair ladder (user, 2026-10-08).

Rounds every every_s from first_s to last_s into the window (10, 40, ..., 250 s). In a round, while the
leading token's ask (the "high" token, higher mid) is under max_price, buy with taker orders:
  * the same leader as the last round (or the first round): low_shares of the low token + high_shares
    of the high token (5 + 6);
  * the leader flipped since the last round: low_shares of the new low token (the old leader) and as many
    of the new high token as make the window end +flip_profit, fees included, if that token wins.
Everything is held to settlement. Settings: the pl_* fields of config.Settings (console group "Pair ladder")."""
from __future__ import annotations

import math
from dataclasses import dataclass, fields

OUT = ("Up", "Down")
OTH = {"Up": "Down", "Down": "Up"}
EPS = 1e-9


@dataclass
class LadderParams:
    enabled: bool = False
    first_s: float = 10.0
    every_s: float = 30.0
    last_s: float = 250.0
    max_price: float = 0.88
    low_shares: float = 5.0
    high_shares: float = 6.0
    flip_profit: float = 0.10
    slippage: float = 0.0                 # (unused since 2026-10-08: each leg's limit is its ask)
    max_pair_cost: float = 1.01           # a round buys only while Up ask + Down ask is at most this
    net_over: float = 5.0                 # a round's high - low shares over this: buy only the high token,
                                          # (high - low) shares (the pairs in between are skipped); 0 = off
    allowance: float = 0.005
    max_book_age_ms: float = 2000.0

    @classmethod
    def from_settings(cls, s) -> "LadderParams":
        return cls(**{f.name: getattr(s, "pl_" + f.name) for f in fields(cls)})


def _mid(v, o):
    t = v.books.get(o) or {}
    b, a = t.get("bid"), t.get("ask")
    return (a + b) / 2.0 if a is not None and b is not None else None


def _ask(v, o):
    return (v.books.get(o) or {}).get("ask")


def round_times(p: LadderParams) -> list[float]:
    out, t = [], p.first_s
    while t <= p.last_s + EPS and p.every_s > 0:
        out.append(t)
        t += p.every_s
    return out


def flip_high_shares(v, p: LadderParams, high: str, a_high: float, a_low: float) -> float:
    """After buying low_shares of the low token: the high-token shares that make the window end
    +flip_profit (fees included) if the high token wins. 0 when it already does."""
    fee = v.market.fees.taker_fee
    cost = v.window_cost + p.low_shares * a_low + fee(a_low, p.low_shares)
    branch = v.shares.get(high, 0.0) - cost                 # P&L if `high` wins, before this buy
    per = 1.0 - a_high - fee(a_high, 1.0) - p.allowance     # what each new high share adds if it wins
    need = p.flip_profit - branch
    if need <= EPS:
        return 0.0
    if per <= EPS:
        return math.inf
    n = math.ceil(need / per * 100 - 1e-6) / 100
    return max(n, v.market.min_size)


def decide(v, p: LadderParams, d) -> None:
    """One round at a time. A round starts at its time (leader, sizes fixed then) and keeps trying, until
    the next round's time, to buy both legs while Up ask + Down ask <= max_pair_cost; each leg's limit is
    its ask (no slippage). With one leg filled, the other is bought only at a price that keeps that leg's
    fill + this ask <= max_pair_cost."""
    st = v.ladder or {}
    if v.halted or v.book_age_ms is None or v.book_age_ms > p.max_book_age_ms:
        return
    if any(x > EPS for x in v.in_flight_taker.values()):
        return
    times = round_times(p)
    due = [i for i, t in enumerate(times) if v.secs_in >= t - EPS]
    if not due:
        return
    i = due[-1]
    rd = st.get("rd") or {}
    if rd.get("i") != i:
        # a new round: the leader and the sizes are decided now
        if i <= st.get("round", -1):
            return
        d.ladder_update["round"] = i
        mu, md = _mid(v, "Up"), _mid(v, "Down")
        if mu is None or md is None or abs(mu - md) <= EPS:
            d.why["taker"] = f"ladder: round {i + 1} at {times[i]:.0f} s skipped (no leader)"
            return
        high = "Up" if mu > md else "Down"
        low = OTH[high]
        ah, al = _ask(v, high), _ask(v, low)
        if ah is None or al is None:
            return
        if ah >= p.max_price - EPS:
            d.why["taker"] = f"ladder: round {i + 1} skipped, {high} ask {ah:.2f} not under {p.max_price:.2f}"
            return
        prev = st.get("high")
        d.ladder_update["high"] = high
        n_low = max(p.low_shares, v.market.min_size)
        if prev is None or prev == high:
            n_high = max(p.high_shares, v.market.min_size)
        else:
            n_high = flip_high_shares(v, p, high, ah, al)
            if n_high == math.inf:
                n_high = 0.0
        if p.net_over > 0 and n_high - n_low > p.net_over + EPS:
            n_high, n_low = n_high - n_low, 0.0           # only the high token: the net of the two
        rd = {"i": i, "low": low, "high": high, "n_low": n_low, "n_high": round(n_high, 2),
              "got_low": 0.0, "got_high": 0.0, "px_low": None, "px_high": None, "flip": prev not in (None, high)}
        d.ladder_update["rd"] = rd
    low, high = rd["low"], rd["high"]
    legs = []
    for leg, side in (("low", low), ("high", high)):
        rest = rd[f"n_{leg}"] - rd[f"got_{leg}"]
        if rest >= v.market.min_size - EPS:              # under the venue's 5: that leg is done
            legs.append((leg, side, round(rest, 2)))
    if not legs:
        return
    asks = {s_: _ask(v, s_) for s_ in OUT}
    if any(asks[s_] is None for _, s_, _ in legs):
        return
    other = lambda leg: "high" if leg == "low" else "low"
    sent = []
    single = rd["n_low"] <= EPS or rd["n_high"] <= EPS    # one token only: not a pair, no pair-cost check
    for leg, side, rest in legs:
        a = asks[side]
        o_leg = other(leg)
        counter = rd[f"px_{o_leg}"] if rd[f"got_{o_leg}"] > EPS else asks[rd[o_leg]]
        if not single and (counter is None or a + counter > p.max_pair_cost + EPS):
            continue
        sent.append((leg, side, a, rest))
    if len(legs) == 2 and len(sent) < 2:
        sent = []                                         # both legs open: buy them together or not at all
    if not sent:
        tot = asks[low] + asks[high]
        d.why["taker"] = f"ladder: round {rd['i'] + 1}: Up + Down asks {tot:.3f} over {p.max_pair_cost:.2f}, trying again"
        return
    for leg, side, a, rest in sent:
        d.takes.append((side, round(a, 3), rest, 0.0))
        d.tags[f"taker:{side}"] = "ladder"
        d.ladder_kinds[side] = leg
    d.why["taker"] = (f"ladder: round {rd['i'] + 1}{' (flip)' if rd.get('flip') else ''}: buy "
                      + " + ".join(f"{rest:g} {side} @ {a:.3f}" for _, side, a, rest in sent)
                      + f" (pair cost <= {p.max_pair_cost:.2f})")
