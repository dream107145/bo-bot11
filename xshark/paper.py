"""Paper venue: simulated fills against the live Polymarket books.

Nothing here signs or sends an order. Every decision is matched against the
real book and the real trade prints, with the rules below.

Taker clip (a fill-and-kill BUY)
    Reaches the book ``paper_latency_ms`` after the decision and buys the asks
    up to its limit, cheapest first. What is not there is dropped. Shares our
    own earlier clips took from a level are counted as gone for two seconds,
    so two clips cannot buy the same shares. Fee 0.07 * p * (1 - p) per share.

Maker bid (a post-only BUY that rests)
    Reaches the book after the same latency. Rejected if it would cross the
    ask. Otherwise it joins the BACK of the queue at its price: the size
    already resting there is ahead of it, and only shrinks as trades print at
    that price or the level itself shrinks.
    The books are mirrored (a bid for Up at b is an ask for Down at 1 - b), so
    a bid for outcome X at price b is filled by
      * a trade print that SOLD X at b or lower, or BOUGHT the other outcome
        at 1 - b or higher: below b the print went through the whole level
        (min(print size, what is left) fills); exactly at b it first eats the
        queue ahead of us;
      * the ask falling to b or below ("cross-through"): the real book only
        shows that once every real bid at that price is gone, so ours would
        have been hit too. The user's own replay study (troll-poly-bot,
        2026-09-25) found 85-95 % of maker fills are this kind, with -2 to -4
        cents of markout -- which is exactly why it is simulated, not skipped.
    A cancel takes effect ``paper_cancel_latency_ms`` after the request; fills
    in between count. Makers pay no fee. The maker rebate is only ESTIMATED
    (the venue pools 20 % of taker fees and pays makers pro rata) and is kept
    apart from the trading PnL.

What a paper venue cannot know: whether our own order would have changed who
traded (a bid at the top of the real book attracts flow and competitors), and
how fast other bots would have taken the same stale asks. Both make these
fills an upper bound, not a forecast.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Callable

from .book import BookFeed, Trade
from .calibration import Calibration
from .config import Settings
from .market import Market

log = logging.getLogger(__name__)

EPS = 1e-9
CONSUMED_TTL_MS = 2000.0


@dataclass(slots=True)
class Order:
    id: int
    slug: str
    outcome: str
    token: str
    other_token: str
    kind: str                 # "maker" | "taker"
    price: float              # limit
    size: float
    created_ms: float
    arrive_ms: float
    end_s: int                # the window's close
    filled: float = 0.0
    state: str = "sent"       # sent -> live -> done
    cancel_at: float | None = None
    queue_ahead: float = 0.0
    result: str = ""          # FILLED PARTIAL KILLED REJECTED CANCELLED EXPIRED
    fair: float | None = None
    tag: str = ""
    seen_ask: float | None = None     # the best ask when the order was decided
    race_p: float | None = None       # the real-order odds this clip had to beat

    @property
    def remaining(self) -> float:
        return max(0.0, self.size - self.filled)


@dataclass(slots=True)
class Fill:
    order_id: int
    slug: str
    outcome: str
    kind: str                 # "maker" | "taker"
    price: float              # average price of this fill
    size: float
    fee: float
    rebate_est: float
    ts_ms: float
    secs_left: float
    how: str                  # take | queue | sweep | cross
    fair: float | None        # our fair for that outcome when the order was decided
    mid: float | None         # the outcome's book mid at the fill
    race_p: float | None = None   # clips: the real-order odds of winning the race
    tag: str = ""                 # clip | bid | pair

    @property
    def cost(self) -> float:
        return self.price * self.size + self.fee


@dataclass(slots=True)
class Position:
    shares: float = 0.0
    cost: float = 0.0         # USDC paid, fees included
    fees: float = 0.0
    rebate_est: float = 0.0
    maker_shares: float = 0.0
    maker_cost: float = 0.0
    taker_shares: float = 0.0
    taker_cost: float = 0.0
    fills: int = 0

    def add(self, f: Fill) -> None:
        self.shares += f.size
        self.cost += f.cost
        self.fees += f.fee
        self.rebate_est += f.rebate_est
        self.fills += 1
        if f.kind == "maker":
            self.maker_shares += f.size
            self.maker_cost += f.cost
        else:
            self.taker_shares += f.size
            self.taker_cost += f.cost


class PaperBankroll:
    """One cash pool shared by several paper venues running in one process."""

    def __init__(self, balance: float, starting_balance: float, realized_pnl: float = 0.0) -> None:
        self.cash = float(balance)
        self.starting_balance = float(starting_balance)
        self.realized_pnl = float(realized_pnl)
        self.venues: dict[str, PaperVenue] = {}
        self.restored_assets: set[str] = set()

    def register(self, asset: str, venue: PaperVenue) -> None:
        self.venues[asset.lower()] = venue

    def charge_restored_positions(self, asset: str) -> bool:
        """Charge an asset's persisted open fills only on its first load in this process."""
        key = asset.lower()
        if key in self.restored_assets:
            return False
        self.restored_assets.add(key)
        return True

    def reserved(self) -> float:
        return sum(v._reserved_local() for v in self.venues.values())

    def open_value(self) -> float:
        total = 0.0
        for venue in self.venues.values():
            for slug, positions in venue.pos.items():
                market = venue.markets.get(slug)
                for outcome, position in positions.items():
                    book = venue.books.book(market.token(outcome)) if market is not None else None
                    mid = book.mid() if book is not None else None
                    total += position.shares * (mid if mid is not None else
                                                position.cost / position.shares if position.shares > EPS else 0.0)
        return total

    def equity(self) -> float:
        return self.cash + self.open_value()


def summarize(pos: dict[str, Position]) -> dict:
    """Position figures for one window: the shape the console shows."""
    up, dn = pos.get("Up", Position()), pos.get("Down", Position())
    cost = up.cost + dn.cost
    avg = lambda p: p.cost / p.shares if p.shares > EPS else None
    lo, hi = min(up.shares, dn.shares), max(up.shares, dn.shares)
    return {
        "Up": {**asdict(up), "avg": avg(up)}, "Down": {**asdict(dn), "avg": avg(dn)},
        "cost": cost, "fees": up.fees + dn.fees, "rebate_est": up.rebate_est + dn.rebate_est,
        "fills": up.fills + dn.fills,
        "pair_cost": (avg(up) + avg(dn)) if up.shares > EPS and dn.shares > EPS else None,
        "hedge": lo / hi if hi > EPS else None,
        "if_up": up.shares - cost, "if_down": dn.shares - cost,
        "worst": lo - cost if hi > EPS else 0.0, "best": hi - cost if hi > EPS else 0.0,
    }


class PaperVenue:
    def __init__(self, books: BookFeed, clock: Callable[[], float], settings: Settings,
                 cash: float, on_fill: Callable[[Fill], None] | None = None,
                 calibration: Calibration | None = None, bankroll: PaperBankroll | None = None,
                 asset: str = "") -> None:
        self.books = books
        self.cal = calibration
        self.clock = clock
        self.s = settings
        self.bankroll = bankroll
        self._cash = float(cash)
        if bankroll is not None:
            bankroll.register(asset, self)
        self.on_fill = on_fill
        self.orders: dict[int, Order] = {}
        self.active: dict[int, Order] = {}       # orders not done yet
        self._next_id = 1
        self.markets: dict[str, Market] = {}
        self.pos: dict[str, dict[str, Position]] = {}
        self.fills: dict[str, list[Fill]] = {}
        self.counts: dict[str, Counter] = {}
        #: every finished clip per window (filled or killed), for the chart
        self.clips: dict[str, list[dict]] = {}
        self._consumed: dict[tuple[str, float], tuple[float, float]] = {}

    @property
    def cash(self) -> float:
        return self.bankroll.cash if self.bankroll is not None else self._cash

    @cash.setter
    def cash(self, value: float) -> None:
        if self.bankroll is not None:
            self.bankroll.cash = float(value)
        else:
            self._cash = float(value)

    # ----------------------------------------------------------- orders

    def submit(self, market: Market, outcome: str, kind: str, price: float, size: float,
               fair: float | None = None, tag: str = "") -> Order:
        now = self.clock()
        self.markets[market.slug] = market
        book = self.books.book(market.token(outcome))
        o = Order(self._next_id, market.slug, outcome, market.token(outcome),
                  market.token("Down" if outcome == "Up" else "Up"), kind, round(price, 4), size,
                  now, now + self._latency(self.s.paper_latency_ms), market.end, fair=fair, tag=tag,
                  seen_ask=book.best_ask() if book is not None else None)
        self._next_id += 1
        self.orders[o.id] = o
        self.active[o.id] = o
        self.counts.setdefault(market.slug, Counter())[f"{kind}_sent"] += 1
        return o

    def cancel(self, order_id: int) -> None:
        o = self.orders.get(order_id)
        if o is None or o.state == "done" or o.cancel_at is not None:
            return
        o.cancel_at = self.clock() + self._latency(self.s.paper_cancel_latency_ms)
        self.counts.setdefault(o.slug, Counter())["cancels"] += 1

    def _latency(self, fixed_ms: float) -> float:
        """A delay measured on this VPS's real orders, or the fixed setting."""
        if self.cal is not None and self.s.paper_latency_real:
            return self.cal.latency_ms(presigned=self.s.paper_presigned)
        return fixed_ms

    def cancel_all(self, slug: str | None = None, outcome: str | None = None) -> None:
        for o in list(self.active.values()):
            if o.kind == "maker" and (slug is None or o.slug == slug) and (outcome is None or o.outcome == outcome):
                self.cancel(o.id)

    def open_orders(self, slug: str | None = None) -> list[Order]:
        return [o for o in self.active.values() if slug is None or o.slug == slug]

    def spendable(self) -> float:
        """Cash new orders may use."""
        return self.cash

    def reserved(self, slug: str | None = None) -> float:
        """USDC committed to orders not yet done (price * remaining, plus the fee for clips)."""
        if self.bankroll is not None and slug is None:
            return self.bankroll.reserved()
        return self._reserved_local(slug)

    def _reserved_local(self, slug: str | None = None) -> float:
        tot = 0.0
        for o in self.open_orders(slug):
            if o.tag == "sell":
                continue                              # a sell frees money, it does not commit any
            m = self.markets.get(o.slug)
            fee = m.fees.taker_fee(o.price, o.remaining) if (m and o.kind == "taker") else 0.0
            tot += o.price * o.remaining + fee
        return tot

    # ----------------------------------------------------------- clock

    def tick(self) -> None:
        now = self.clock()
        for o in list(self.active.values()):
            if o.state == "sent" and o.arrive_ms <= now:
                self._arrive(o, now)
            if o.state != "done" and o.cancel_at is not None and o.cancel_at <= now:
                self._finish(o, "CANCELLED" if o.filled <= EPS else "PARTIAL")
            if o.state != "done" and now / 1000.0 >= o.end_s:
                self._finish(o, "EXPIRED" if o.filled <= EPS else "PARTIAL")
        # forget finished orders after a while (the console reads counts, not orders)
        if len(self.orders) > 5000:
            for oid in [k for k, o in self.orders.items() if o.state == "done"][:2500]:
                del self.orders[oid]
        if len(self._consumed) > 2000:
            self._consumed = {k: v for k, v in self._consumed.items() if now - v[1] < CONSUMED_TTL_MS}

    def next_event_ms(self) -> float | None:
        ts = [o.arrive_ms for o in self.active.values() if o.state == "sent"]
        ts += [o.cancel_at for o in self.active.values() if o.cancel_at is not None]
        return min(ts) if ts else None

    def _finish(self, o: Order, result: str, why: str = "") -> None:
        o.state, o.result = "done", result
        self.active.pop(o.id, None)
        self.counts.setdefault(o.slug, Counter())[f"{o.kind}_{result.lower()}"] += 1
        if o.kind == "taker":
            rows = self.clips.setdefault(o.slug, [])
            rows.append({"id": o.id, "ts": round(o.created_ms), "arrive": round(o.arrive_ms), "outcome": o.outcome,
                         "limit": o.price, "size": o.size, "filled": round(o.filled, 4), "result": result,
                         "why": why, "seen_ask": o.seen_ask, "race_p": o.race_p, "fair": o.fair,
                         "latency_ms": round(o.arrive_ms - o.created_ms), "tag": o.tag})
            if len(rows) > 400:
                del rows[:100]

    def _arrive(self, o: Order, now: float) -> None:
        book = self.books.book(o.token)
        if book is None:
            self._finish(o, "REJECTED" if o.kind == "maker" else "KILLED", "no book")
            return
        if now / 1000.0 >= o.end_s:
            self._finish(o, "EXPIRED", "arrived after the close")
            return
        if o.kind == "taker" and o.tag == "sell":
            self._sell(o, book, now)
            return
        if o.kind == "taker":
            self._take(o, book, now)
            return
        ask = book.best_ask()
        if ask is not None and o.price >= ask - EPS:
            self._finish(o, "REJECTED")          # post-only: it would have crossed
            return
        o.state = "live"
        o.queue_ahead = book.bid_size_at(o.price)

    def _take(self, o: Order, book, now: float) -> None:
        m = self.markets[o.slug]
        got, spend, fee = 0.0, 0.0, 0.0
        for px, sz in book.asks_upto(o.price):
            used, ts = self._consumed.get((o.token, px), (0.0, 0.0))
            if now - ts > CONSUMED_TTL_MS:
                used = 0.0
            avail = sz - used
            if avail <= EPS:
                continue
            take = min(avail, o.size - got)
            got += take
            spend += take * px
            fee += m.fees.taker_fee(px, take)
            self._consumed[(o.token, px)] = (used + take, now)
            if got >= o.size - EPS:
                break
        if got <= EPS:
            self._finish(o, "KILLED", "nothing at the limit")
            return
        if self.cal is not None and self.s.paper_race:
            # the book says the shares were there; real orders from this VPS
            # found them gone most of the time (faster bots took them first)
            best = book.best_ask()
            depth = book.asks.get(best, 0.0) if best is not None else 0.0
            lower = best is not None and o.seen_ask is not None and best < o.seen_ask - EPS
            won, p, key = self.cal.win_race(depth / max(o.size, EPS), lower)
            o.race_p = p
            if not won:
                # faster bots took the best ask; what is left deeper, up to our limit, is still ours
                self.counts.setdefault(o.slug, Counter())["taker_lost_race"] += 1
                got, spend, fee = 0.0, 0.0, 0.0
                for px, sz in book.asks_upto(o.price):
                    if best is not None and abs(px - best) <= EPS:
                        continue
                    used, ts = self._consumed.get((o.token, px), (0.0, 0.0))
                    avail = sz - (used if now - ts <= CONSUMED_TTL_MS else 0.0)
                    if avail <= EPS:
                        continue
                    take = min(avail, o.size - got)
                    got += take
                    spend += take * px
                    fee += m.fees.taker_fee(px, take)
                    self._consumed[(o.token, px)] = ((used if now - ts <= CONSUMED_TTL_MS else 0.0) + take, now)
                    if got >= o.size - EPS:
                        break
                if got <= EPS:
                    self._finish(o, "KILLED", "lost the race")
                    return
                o.filled = got
                self._book_fill(o, got, spend / got, fee, "take", now)
                self._finish(o, "FILLED" if got >= o.size - EPS else "PARTIAL",
                             f"best ask lost to a faster bot; avg {spend / got:.3f} deeper")
                return
        o.filled = got
        self._book_fill(o, got, spend / got, fee, "take", now)
        self._finish(o, "FILLED" if got >= o.size - EPS else "PARTIAL", f"avg {spend / got:.3f}")

    def _sell(self, o: Order, book, now: float) -> None:
        """A sell of held shares from the console: hits the bids down to the limit (taker fee).
        Booked as a fill of negative size, so cash, position and settlement all follow."""
        m = self.markets[o.slug]
        held = self.pos.get(o.slug, {}).get(o.outcome)
        size = min(o.size, held.shares if held else 0.0)
        got, proceeds, fee = 0.0, 0.0, 0.0
        for px, sz in book.bids_downto(o.price):
            take = min(sz, size - got)
            got += take
            proceeds += take * px
            fee += m.fees.taker_fee(px, take)
            if got >= size - EPS:
                break
        if got <= EPS:
            self._finish(o, "KILLED", "no bid at the limit" if size > EPS else "nothing held to sell")
            return
        o.filled = got
        self._book_fill(o, -got, proceeds / got, fee, "sell", now)
        self._finish(o, "FILLED" if got >= o.size - EPS else "PARTIAL", f"sold avg {proceeds / got:.3f}")

    # ----------------------------------------------------------- maker fills

    def _live_makers(self, token: str) -> list[Order]:
        return [o for o in self.active.values()
                if o.kind == "maker" and o.state == "live" and (o.token == token or o.other_token == token)]

    def on_book(self, token: str) -> None:
        book = self.books.book(token)
        if book is None:
            return
        now = self.clock()
        for o in self._live_makers(token):
            if o.token != token:
                continue
            o.queue_ahead = min(o.queue_ahead, book.bid_size_at(o.price))
            ask = book.best_ask()
            if ask is not None and ask <= o.price + EPS:
                through = sum(s for p, s in book.asks_upto(o.price))
                self._maker_fill(o, min(o.remaining, through if through > EPS else o.remaining), "cross", now)

    def on_trade(self, t: Trade) -> None:
        now = self.clock()
        for o in self._live_makers(t.token):
            if o.token == t.token and t.side == "SELL":
                px = t.price
            elif o.other_token == t.token and t.side == "BUY":
                px = 1.0 - t.price
            else:
                continue
            if px < o.price - EPS:
                self._maker_fill(o, min(o.remaining, t.size), "sweep", now)
            elif abs(px - o.price) <= EPS:
                reach = t.size - o.queue_ahead
                o.queue_ahead = max(0.0, o.queue_ahead - t.size)
                if reach > EPS:
                    self._maker_fill(o, min(o.remaining, reach), "queue", now)

    def _maker_fill(self, o: Order, size: float, how: str, now: float) -> None:
        if size <= EPS or o.state != "live":
            return
        o.filled += size
        self._book_fill(o, size, o.price, 0.0, how, now)
        if o.remaining <= EPS:
            self._finish(o, "FILLED")

    # ----------------------------------------------------------- accounting

    def _book_fill(self, o: Order, size: float, price: float, fee: float, how: str, now: float) -> None:
        m = self.markets[o.slug]
        book = self.books.book(o.token)
        rebate = m.fees.maker_rebate_est(price, size) if o.kind == "maker" else 0.0
        f = Fill(o.id, o.slug, o.outcome, o.kind, price, size, fee, rebate, now,
                 max(0.0, m.end - now / 1000.0), how, o.fair, book.mid() if book else None, o.race_p, o.tag)
        self.cash -= f.cost
        self.pos.setdefault(o.slug, {}).setdefault(o.outcome, Position()).add(f)
        self.fills.setdefault(o.slug, []).append(f)
        self.counts.setdefault(o.slug, Counter())[f"{o.kind}_fills"] += 1
        if self.on_fill is not None:
            self.on_fill(f)

    def position(self, slug: str) -> dict:
        return summarize(self.pos.get(slug, {}))

    def settle(self, slug: str, winner: str) -> dict:
        """Pay the winning shares out ($1 each) and close the window's book."""
        for o in self.open_orders(slug):
            self._finish(o, "EXPIRED" if o.filled <= EPS else "PARTIAL")
        pos = self.pos.pop(slug, {})
        summary = summarize(pos)
        payout = pos[winner].shares if winner in pos else 0.0
        self.cash += payout
        if self.bankroll is not None:
            self.bankroll.realized_pnl += payout - summary["cost"]
        fills = self.fills.pop(slug, [])
        clips = self.clips.pop(slug, [])
        counts = dict(self.counts.pop(slug, Counter()))
        maker_pnl = taker_pnl = 0.0
        for f in fills:
            v = (f.size if f.outcome == winner else 0.0) - f.cost
            if f.kind == "maker":
                maker_pnl += v
            else:
                taker_pnl += v
        return {"slug": slug, "winner": winner, "payout": payout, "pnl": payout - summary["cost"],
                "maker_pnl": maker_pnl, "taker_pnl": taker_pnl, "position": summary,
                "fills": [asdict(f) for f in fills], "counts": counts, "clips": clips}

    # ----------------------------------------------------------- restarts

    def export_open(self) -> dict:
        return {slug: {"market": asdict(self.markets[slug]) if slug in self.markets else None,
                       "fills": [asdict(f) for f in self.fills.get(slug, [])]}
                for slug in self.pos}

    def import_open(self, data: dict, market_factory, charge_cash: bool = True) -> list[str]:
        """Restore windows that were open at the last shutdown, so they still settle."""
        out = []
        for slug, rec in (data or {}).items():
            m = market_factory(rec.get("market"))
            if m is None:
                continue
            self.markets[slug] = m
            for fd in rec.get("fills") or []:
                f = Fill(**fd)
                if charge_cash:
                    self.cash -= f.cost              # spent in the run that bought it
                self.pos.setdefault(slug, {}).setdefault(f.outcome, Position()).add(f)
                self.fills.setdefault(slug, []).append(f)
            out.append(slug)
        return out
