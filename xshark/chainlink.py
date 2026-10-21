"""The price the market settles on: Chainlink, via Polymarket's live data socket.

Ported from Bo-BOTT's feeds/chainlink.py (aiohttp there, websockets here),
plus the stream the venue actually settles on. Gamma lists the resolution
source of every BTC 5m market as ``btc-usd-twap-60s-streams`` with
``twapLookbackSeconds: 60``: the window resolves Up iff the 60-second TWAP at
the close is >= the 60-second TWAP at the open. The socket publishes that
series itself as ``crypto_prices_twap_sixty`` (one tick a second, measured
2026-09-30), so the strike is read, not reconstructed. Bo-BOTT's
reconstruction from the per-second ``crypto_prices_chainlink`` ticks (it
matched 146 of 147 official results) stays as the fallback.

Both topics are on the LEGACY socket, which Polymarket plans to retire around
late October 2026; the replacement (PolyBolt, ``price.crypto.twap``) needs CLOB
API credentials. Until then this is public and needs no key.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

RTDS_URL = "wss://ws-live-data.polymarket.com"
TOPIC_PRICE = "crypto_prices_chainlink"
TOPIC_TWAP = "crypto_prices_twap_sixty"
#: ticks kept per asset and topic; both send ~1/s, so this is ~15 minutes
HISTORY = 900
RECONNECT_MIN_S, RECONNECT_MAX_S = 2.0, 60.0
#: both topics tick every second; this long with nothing means a dead socket
SILENCE_S = 20.0
#: the composite-vs-Chainlink basis moves with USDT/USD, slowly
BASIS_HALFLIFE_S = 120.0
#: a sample this far from the running basis is the two feeds disagreeing about
#: a MOVE (Chainlink arrives ~1.4 s late), not a change of level
BASIS_OUTLIER_BPS = 15.0


@dataclass(slots=True)
class Tick:
    ts_ms: float          # Chainlink's own timestamp (whole seconds)
    price: float
    recv_ms: float        # when it reached us


def _ticks_at(q: deque | None, ts_ms: float, tolerance_ms: float) -> float | None | str:
    if not q:
        return None
    if q[-1].ts_ms < ts_ms:
        return "wait"
    if q[0].ts_ms > ts_ms:
        return None
    for t in reversed(q):
        if t.ts_ms <= ts_ms:
            return t.price if ts_ms - t.ts_ms <= tolerance_ms else None
    return None


@dataclass
class ChainlinkFeed:
    history: int = HISTORY
    prices: dict[str, deque] = field(default_factory=dict)     # crypto_prices_chainlink
    twaps: dict[str, deque] = field(default_factory=dict)      # crypto_prices_twap_sixty
    basis: dict[str, float] = field(default_factory=dict)      # log(composite) - log(chainlink)
    basis_n: dict[str, int] = field(default_factory=dict)
    _basis_ts: dict[str, float] = field(default_factory=dict)
    #: assets kept (None = all); the socket sends every asset it carries
    assets: set | None = None
    connected: bool = False
    messages: int = 0
    reconnects: int = 0
    last_error: str = ""

    # ---------------------------------------------------------------- ticks

    def on_message(self, body: dict, recv_ms: float) -> tuple[str, str] | None:
        """One socket message; returns (topic, asset) when it stored a tick."""
        topic = body.get("topic")
        if topic not in (TOPIC_PRICE, TOPIC_TWAP):
            return None
        p = body.get("payload") or {}
        sym = str(p.get("symbol") or "")
        if not sym.endswith("/usd"):
            return None
        asset = sym[:-4].upper()
        if self.assets is not None and asset not in self.assets:
            return None
        try:
            price, ts = float(p["value"]), float(p["timestamp"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (price > 0.0 and math.isfinite(price)):
            return None
        store = self.prices if topic == TOPIC_PRICE else self.twaps
        q = store.setdefault(asset, deque(maxlen=self.history))
        if q and ts <= q[-1].ts_ms:
            if ts == q[-1].ts_ms:
                q[-1] = Tick(ts, price, recv_ms)          # a resend of the same second
            return topic, asset
        q.append(Tick(ts, price, recv_ms))
        self.messages += 1
        return topic, asset

    def latest(self, asset: str) -> Tick | None:
        q = self.prices.get(asset)
        return q[-1] if q else None

    def latest_twap(self, asset: str) -> Tick | None:
        q = self.twaps.get(asset)
        return q[-1] if q else None

    def twap_at(self, asset: str, ts_ms: float, now_ms: float | None = None,
                patience_ms: float = 5000.0) -> tuple[float | None | str, str]:
        """The settling series at ``ts_ms``: (value, source).

        The official ``twap60`` tick is preferred: while it has not arrived
        (Chainlink runs ~1.4 s behind) this answers ``"wait"`` for up to
        ``patience_ms`` after ``ts_ms``, and only then falls back to the
        reconstruction from the per-second prices. None when neither covers it."""
        v = _ticks_at(self.twaps.get(asset), ts_ms, 1500.0)
        if isinstance(v, float):
            return v, "twap60"
        if v == "wait" and now_ms is not None and now_ms - ts_ms < patience_ms:
            return "wait", ""
        r = self._reconstruct(asset, ts_ms)
        if isinstance(r, float):
            return r, "reconstructed"
        if v == "wait" or r == "wait":
            return "wait", ""
        return None, ""

    def _reconstruct(self, asset: str, ts_ms: float, window_ms: float = 60_000.0) -> float | None | str:
        """Time-weighted mean of the Chainlink price over [ts - 60 s, ts]
        (Bo-BOTT's ``twap_at``)."""
        q = self.prices.get(asset)
        if not q:
            return None
        if q[-1].ts_ms < ts_ms:
            return "wait"
        start = ts_ms - window_ms
        pts = [(t.ts_ms, t.price) for t in q if start - 2000.0 <= t.ts_ms <= ts_ms]
        if len(pts) < 10 or pts[0][0] > start + 2000.0:
            return None
        total = 0.0
        prev_t, prev_p = max(pts[0][0], start), pts[0][1]
        for t, px in pts[1:]:
            t = min(max(t, start), ts_ms)
            if t > prev_t:
                total += 0.5 * (prev_p + px) * (t - prev_t)
            prev_t, prev_p = t, px
        if ts_ms > prev_t:
            total += prev_p * (ts_ms - prev_t)
        return total / window_ms

    # ---------------------------------------------------------------- basis

    def update_basis(self, asset: str, composite_at_tick: float | None, tick: Tick) -> None:
        """Fold one (composite, Chainlink) pair taken at the tick's own instant
        into the running basis (Bo-BOTT's rule, unchanged)."""
        if composite_at_tick is None or composite_at_tick <= 0.0:
            return
        x = math.log(composite_at_tick) - math.log(tick.price)
        n = self.basis_n.get(asset, 0)
        prev = self.basis.get(asset)
        if prev is not None and n >= 20 and abs(x - prev) * 1e4 > BASIS_OUTLIER_BPS:
            return
        if prev is None:
            self.basis[asset] = x
        else:
            dt = max((tick.ts_ms - self._basis_ts.get(asset, tick.ts_ms)) / 1000.0, 0.0)
            a = 1.0 - 0.5 ** (dt / BASIS_HALFLIFE_S) if dt > 0 else 1.0 / (n + 1)
            if n < 20:
                a = max(a, 1.0 / (n + 1))
            self.basis[asset] = prev + a * (x - prev)
        self._basis_ts[asset] = tick.ts_ms
        self.basis_n[asset] = n + 1

    def basis_factor(self, asset: str, min_samples: int = 10) -> float:
        """Multiply a composite price by this to put it on Chainlink's level."""
        if self.basis_n.get(asset, 0) < min_samples:
            return 1.0
        return math.exp(-self.basis.get(asset, 0.0))

    def snapshot(self, asset: str, now_ms: float) -> dict:
        t, tw = self.latest(asset), self.latest_twap(asset)
        return {
            "connected": self.connected, "messages": self.messages, "reconnects": self.reconnects,
            "error": self.last_error or None,
            "price": t.price if t else None,
            "price_age_ms": round(now_ms - t.ts_ms) if t else None,
            "delay_ms": round(t.recv_ms - t.ts_ms) if t else None,
            "twap60": tw.price if tw else None,
            "twap_age_ms": round(now_ms - tw.ts_ms) if tw else None,
            "basis_bps": round(self.basis[asset] * 1e4, 2) if asset in self.basis else None,
            "basis_n": self.basis_n.get(asset, 0),
        }

    # ---------------------------------------------------------------- socket

    async def run(self, stop: asyncio.Event, clock: Callable[[], float],
                  on_tick: Callable[[str, str, Tick], None] | None = None, url: str = RTDS_URL) -> None:
        """Listen until ``stop`` is set, reconnecting with backoff."""
        import websockets

        backoff = RECONNECT_MIN_S
        while not stop.is_set():
            try:
                async with websockets.connect(url, open_timeout=15, ping_interval=10, close_timeout=3) as ws:
                    await ws.send(json.dumps({"action": "subscribe", "subscriptions": [
                        {"topic": TOPIC_PRICE, "type": "*", "filters": ""},
                        {"topic": TOPIC_TWAP, "type": "*", "filters": ""}]}))
                    self.connected, self.last_error, backoff = True, "", RECONNECT_MIN_S
                    log.info("chainlink feed connected (%s)", url)
                    last_msg = time.monotonic()
                    while not stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), 5.0)
                        except asyncio.TimeoutError:
                            # the socket can stay open and send nothing (seen 2026-09-30:
                            # ~1 h silent, 13 strikes fell back to the composite)
                            if time.monotonic() - last_msg > SILENCE_S:
                                raise ConnectionError(f"silent for {SILENCE_S:.0f} s") from None
                            continue
                        last_msg = time.monotonic()
                        if not isinstance(raw, str) or not raw.strip():
                            continue
                        try:
                            body = json.loads(raw)
                        except ValueError:
                            continue
                        got = self.on_message(body, clock())
                        if got is not None and on_tick is not None:
                            topic, asset = got
                            q = (self.prices if topic == TOPIC_PRICE else self.twaps)[asset]
                            on_tick(topic, asset, q[-1])
            except asyncio.CancelledError:
                raise
            except Exception as exc:                          # noqa: BLE001
                self.last_error = f"{type(exc).__name__}: {exc}"[:160]
                self.reconnects += 1
                log.warning("chainlink feed dropped (%s); retry in %.0fs", self.last_error, backoff)
            self.connected = False
            if stop.is_set():
                break
            try:
                await asyncio.wait_for(stop.wait(), backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2.0, RECONNECT_MAX_S)
