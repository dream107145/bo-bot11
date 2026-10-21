"""Polymarket CLOB market channel: order books and trade prints.

Measured on the live BTC 5m market (2026-09-30, 75 s, this VPS):

* The two books are MIRRORED. A BUY at 0.94 on Up arrives as a pair of
  ``price_change`` rows, Up bids 0.94 and Down asks 0.06, same size. So each
  token's book is complete on its own, and a resting BUY Up at ``b`` is hit
  either by a SELL of Up at ``b`` or by a BUY of Down at ``1 - b``.
* ``last_trade_price`` arrives ONCE per trade, on the taker's token, with the
  taker's side (524 BUY / 62 SELL of 586 prints).
* Feed delay (receipt minus venue timestamp): p50 13 ms, p90 ~25, p99 ~65.
* Up to ~800 events a second near the close, so handlers stay cheap.

The book logic follows troll-poly-bot's ``LiveBook`` (dict levels, the venue's
own best bid/ask trims levels a missed delta left crossed), and the connection
handling follows its 2026-09-28 rewrite: when the token set changes, the NEW
connection is opened first and the old one retired only once the new one has
a snapshot of every token, so there is no gap at the window roll.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

import orjson

log = logging.getLogger(__name__)

CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
MAX_LEVELS = 40


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Book:
    __slots__ = ("token", "bids", "asks", "ts", "recv_ms", "vbid", "vask", "conn")

    def __init__(self, token: str) -> None:
        self.token = token
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.ts = 0.0          # venue timestamp of the last change, ms
        self.recv_ms = 0.0     # when we processed it, ms
        self.vbid: float | None = None     # the venue's own best bid/ask, from the last change
        self.vask: float | None = None
        self.conn = 0

    # ----------------------------------------------------------- updates

    def apply_snapshot(self, msg: dict, recv_ms: float, conn: int = 0) -> None:
        self.bids = self._levels(msg.get("bids"), reverse=True)
        self.asks = self._levels(msg.get("asks"), reverse=False)
        self.ts = _f(msg.get("timestamp")) or recv_ms
        self.recv_ms = recv_ms
        self.vbid = self.vask = None
        self.conn = conn

    @staticmethod
    def _levels(raw, reverse: bool) -> dict[float, float]:
        out: dict[float, float] = {}
        for lvl in raw or []:
            p, s = _f(lvl.get("price")), _f(lvl.get("size"))
            if p is not None and s is not None and s > 0:
                out[p] = s
        if len(out) > MAX_LEVELS:
            out = dict(sorted(out.items(), key=lambda kv: -kv[0] if reverse else kv[0])[:MAX_LEVELS])
        return out

    def apply_change(self, ch: dict, ts: float, recv_ms: float) -> None:
        p, s = _f(ch.get("price")), _f(ch.get("size"))
        side = ch.get("side")
        levels = self.bids if side == "BUY" else self.asks if side == "SELL" else None
        if p is None or s is None or levels is None:
            return
        if s <= 0:
            levels.pop(p, None)
        else:
            levels[p] = s
            if len(levels) > 2 * MAX_LEVELS:
                keep = sorted(levels.items(), key=lambda kv: -kv[0] if side == "BUY" else kv[0])[:MAX_LEVELS]
                levels.clear()
                levels.update(keep)
        self.ts, self.recv_ms = ts, recv_ms
        self.vbid, self.vask = _f(ch.get("best_bid")), _f(ch.get("best_ask"))

    def set_bbo(self, bid, ask, ts: float, recv_ms: float) -> None:
        self.vbid, self.vask = _f(bid), _f(ask)
        self.ts, self.recv_ms = ts, recv_ms

    # ----------------------------------------------------------- reads

    def best_bid(self) -> float | None:
        cands = [p for p in self.bids if self.vbid is None or p <= self.vbid + 1e-9]
        return max(cands) if cands else None

    def best_ask(self) -> float | None:
        cands = [p for p in self.asks if self.vask is None or p >= self.vask - 1e-9]
        return min(cands) if cands else None

    def mid(self) -> float | None:
        b, a = self.best_bid(), self.best_ask()
        return (a + b) / 2.0 if a is not None and b is not None and a >= b else None

    def bid_size_at(self, price: float) -> float:
        return self.bids.get(round(price, 4), self.bids.get(price, 0.0))

    def bids_downto(self, limit: float) -> list[tuple[float, float]]:
        """Bid levels at or above ``limit``, best first (what a sell down to ``limit`` can hit)."""
        ceiling = self.vbid if self.vbid is not None else 1.0
        return sorted(((p, s) for p, s in self.bids.items() if limit - 1e-9 <= p <= ceiling + 1e-9), reverse=True)

    def asks_upto(self, limit: float) -> list[tuple[float, float]]:
        """Ask levels at or below ``limit``, cheapest first (trimmed at the venue's best)."""
        floor = self.vask - 1e-9 if self.vask is not None else -1.0
        return sorted((p, s) for p, s in self.asks.items() if floor <= p <= limit + 1e-9)

    def top(self) -> dict:
        b, a = self.best_bid(), self.best_ask()
        return {"bid": b, "ask": a, "bid_size": self.bids.get(b) if b is not None else None,
                "ask_size": self.asks.get(a) if a is not None else None}


@dataclass(slots=True)
class Trade:
    token: str
    price: float
    size: float
    side: str            # the TAKER's side, "BUY" or "SELL"
    ts: float            # venue ms
    recv_ms: float
    tx: str = ""


OnBook = Callable[[str], None]
OnTrade = Callable[[Trade], None]


class BookFeed:
    """Books for a changing set of tokens, kept continuous across resubscribes."""

    HANDOVER_TIMEOUT_S = 6.0
    RESUB_DEBOUNCE_S = 0.5
    PING_EVERY_S = 8.0
    #: PONGs answer our pings every 8 s, so this long with nothing is a dead socket
    SILENCE_S = 30.0
    #: the server can keep answering PINGs while sending no market data (2026-10-05 16:00 UTC:
    #: 3+ minutes frozen at 0.59/0.60, PONGs kept the socket "alive"); BTC 5m books update
    #: several times a second, so this long without a book or trade message is a dead feed
    DATA_SILENCE_S = 20.0

    def __init__(self, clock: Callable[[], float], on_book: OnBook | None = None,
                 on_trade: OnTrade | None = None, url: str = CLOB_WS) -> None:
        self.clock = clock
        self.on_book = on_book
        self.on_trade = on_trade
        self.url = url
        self.books: dict[str, Book] = {}
        self.want: frozenset[str] = frozenset()
        self._want_since = 0.0
        self._conn_id = 0
        self._last_open = 0.0
        self._active: dict[int, tuple[asyncio.Task, frozenset[str]]] = {}
        self._snapshotted: dict[int, set[str]] = {}
        self._seen_trades: deque = deque(maxlen=4000)
        self._seen_set: set = set()
        # health, for the console
        self.connected = False
        self.messages = 0
        self.reconnects = 0
        self.last_msg_ms = 0.0
        self.last_error = ""
        self.lag_ms: deque = deque(maxlen=500)      # receipt - venue timestamp
        self.ticks: dict[str, float] = {}           # token -> tick size the venue announced

    # ----------------------------------------------------------- API

    def set_tokens(self, tokens) -> None:
        tokens = frozenset(t for t in tokens if t)
        if tokens != self.want:
            self.want = tokens
            self._want_since = time.monotonic()
            for t in tokens:
                self.books.setdefault(t, Book(t))

    def tick_of(self, token: str) -> float | None:
        """The token's tick size: the venue's announcement, else 0.001 when its book already holds a
        sub-cent price (only possible on that tick), else None (assume the market's tick)."""
        t = self.ticks.get(token)
        if t:
            return t
        b = self.books.get(token)
        if b is not None:
            for px in list(b.bids)[:50] + list(b.asks)[:50]:
                if abs(px * 100 - round(px * 100)) > 1e-6:
                    return 0.001
        return None

    def book(self, token: str) -> Book | None:
        b = self.books.get(token)
        return b if b is not None and b.conn in self._active and (b.bids or b.asks) else None

    def age_ms(self, token: str) -> float | None:
        """How long since the book (or the connection) last heard anything."""
        b = self.book(token)
        if b is None:
            return None
        return max(0.0, self.clock() - max(b.recv_ms, self.last_msg_ms))

    def health(self) -> dict:
        lag = sorted(self.lag_ms)
        q = (lambda p: round(lag[int(p * (len(lag) - 1))], 1)) if lag else (lambda p: None)
        return {"connected": self.connected, "connections": len(self._active), "messages": self.messages,
                "reconnects": self.reconnects, "error": self.last_error or None,
                "last_msg_age_ms": round(self.clock() - self.last_msg_ms) if self.last_msg_ms else None,
                "lag_p50_ms": q(0.5), "lag_p90_ms": q(0.9), "tokens": len(self.want)}

    # ----------------------------------------------------------- supervisor

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        try:
            while not stop.is_set():
                current = max(self._active) if self._active else None
                cur_tokens = self._active[current][1] if current is not None else None
                if self.want and (cur_tokens != self.want) and current is not None and \
                        time.monotonic() - self._want_since >= self.RESUB_DEBOUNCE_S and \
                        time.monotonic() - self._last_open >= 2.0:
                    self._open(self.want)
                # drop finished readers; restart if the newest died
                for cid, (task, toks) in list(self._active.items()):
                    if task.done():
                        del self._active[cid]
                        self._snapshotted.pop(cid, None)
                        exc = task.exception() if not task.cancelled() else None
                        if exc is not None:
                            self.last_error = f"{type(exc).__name__}: {exc}"[:160]
                            self.reconnects += 1
                            log.warning("book feed connection %d dropped (%s)", cid, self.last_error)
                if not self._active and self.want:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2.0, 30.0)
                    self._open(self.want)
                    continue
                if self._active:
                    backoff = 1.0
                self.connected = bool(self._active)
                await asyncio.sleep(0.25)
        finally:
            for task, _ in self._active.values():
                task.cancel()
            for task, _ in list(self._active.values()):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(task, 3.0)

    def _open(self, tokens: frozenset[str]) -> None:
        self._last_open = time.monotonic()
        self._conn_id += 1
        cid = self._conn_id
        self._snapshotted[cid] = set()
        task = asyncio.create_task(self._reader(cid, tokens), name=f"book-{cid}")
        self._active[cid] = (task, tokens)
        log.info("book feed: connection %d for %d tokens", cid, len(tokens))

    def _retire_older(self, cid: int) -> None:
        for old, (task, _) in list(self._active.items()):
            if old < cid:
                task.cancel()

    async def _reader(self, cid: int, tokens: frozenset[str]) -> None:
        import websockets

        opened = time.monotonic()
        # compression off: ~800 small messages a second near the close, and
        # inflating them was ~4 % of the bot's CPU (py-spy, 2026-09-30)
        async with websockets.connect(self.url, open_timeout=15, ping_interval=10, close_timeout=2,
                                      max_queue=4096, max_size=8 << 20, compression=None) as ws:
            await ws.send(json.dumps({"assets_ids": sorted(tokens), "type": "market",
                                      "custom_feature_enabled": True}))
            last_ping = time.monotonic()
            last_msg = last_data = time.monotonic()
            handed_over = False
            while True:
                now_mono = time.monotonic()
                if now_mono - last_ping > self.PING_EVERY_S:
                    await ws.send("PING")
                    last_ping = now_mono
                if not handed_over and (self._snapshotted.get(cid, set()) >= tokens
                                        or now_mono - opened > self.HANDOVER_TIMEOUT_S):
                    self._retire_older(cid)
                    handed_over = True
                if time.monotonic() - last_data > self.DATA_SILENCE_S:
                    raise ConnectionError(f"no book data for {self.DATA_SILENCE_S:.0f} s (only PONGs)")
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                except asyncio.TimeoutError:
                    if time.monotonic() - last_msg > self.SILENCE_S:
                        raise ConnectionError(f"book channel silent for {self.SILENCE_S:.0f} s") from None
                    continue
                last_msg = time.monotonic()
                if raw == "PONG" or not raw:
                    continue
                last_data = last_msg
                self._handle(raw, cid)

    # ----------------------------------------------------------- messages

    def _handle(self, raw, cid: int) -> None:
        try:
            msg = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return
        now = self.clock()
        self.messages += 1
        self.last_msg_ms = now
        for ev in (msg if isinstance(msg, list) else (msg,)):
            if not isinstance(ev, dict):
                continue
            et = ev.get("event_type")
            ts = _f(ev.get("timestamp")) or now
            if et == "price_change":
                self.lag_ms.append(now - ts)
                touched = set()
                for ch in ev.get("price_changes") or ():
                    tok = ch.get("asset_id")
                    b = self.books.get(tok)
                    if b is not None and b.conn == cid:
                        b.apply_change(ch, ts, now)
                        touched.add(tok)
                if self.on_book is not None:
                    for tok in touched:
                        self.on_book(tok)
            elif et == "tick_size_change":
                t = _f(ev.get("new_tick_size"))
                if t and ev.get("asset_id"):
                    self.ticks[ev["asset_id"]] = t
                    log.info("tick size %s... -> %s", str(ev["asset_id"])[:8], t)
            elif et == "book":
                tok = ev.get("asset_id")
                if _f(ev.get("tick_size")) and tok:
                    self.ticks[tok] = _f(ev.get("tick_size"))
                b = self.books.get(tok)
                if b is None:
                    continue
                if b.conn != cid and cid < max(self._active, default=cid):
                    continue            # an older connection must not overwrite the newer one's book
                b.apply_snapshot(ev, now, cid)
                self._snapshotted.setdefault(cid, set()).add(tok)
                if self.on_book is not None:
                    self.on_book(tok)
            elif et == "best_bid_ask":
                b = self.books.get(ev.get("asset_id"))
                if b is not None and b.conn == cid:
                    b.set_bbo(ev.get("best_bid"), ev.get("best_ask"), ts, now)
                    if self.on_book is not None:
                        self.on_book(b.token)
            elif et == "last_trade_price":
                self.lag_ms.append(now - ts)
                tok = ev.get("asset_id")
                p, s = _f(ev.get("price")), _f(ev.get("size"))
                if tok not in self.books or p is None or s is None or s <= 0:
                    continue
                key = (tok, ev.get("timestamp"), ev.get("price"), ev.get("size"), ev.get("transaction_hash"))
                if key in self._seen_set:
                    continue            # the same print from a second connection during a handover
                if len(self._seen_trades) == self._seen_trades.maxlen:
                    self._seen_set.discard(self._seen_trades[0])
                self._seen_trades.append(key)
                self._seen_set.add(key)
                if self.on_trade is not None:
                    self.on_trade(Trade(tok, p, s, str(ev.get("side") or ""), ts, now,
                                        str(ev.get("transaction_hash") or "")))
