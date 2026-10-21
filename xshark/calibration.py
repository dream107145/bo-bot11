"""Execution realism measured on REAL orders from this VPS (scripts/calibrate_fills.py).

Two things the book alone gets wrong, both measured on 844 real fill-or-kill /
fill-and-kill buys sent by the VPS's real-money bots (bo-bot11-real and
troll-poly-bot spot-lag, 2026-09-24..30), replayed against the books they
recorded:

1. Latency. From the decision to the venue's reply: p50 271 ms, p90 434 ms,
   p99 ~1.05 s on the orders sent since the live bots' current execution setup
   (2026-09-29 17:58 UTC; before it the tail was p90 556 / p99 1.3 s). Each paper
   order draws its own delay from those measurements.

2. The race. Even at each order's own real latency, "the ask was at or under
   the limit, with the size there" said FILL for 38 % of the orders; only 10.7 %
   filled. Faster bots take stale asks first. Among spot-triggered snipes
   (the class the taker clips belong to), a replay-fill really filled:
       ask came DOWN by arrival     7/13  (54 %)
       ask depth >= 20x our size   16/53  (30 %)
       3-20x                       14/122 (11 %)
       under 3x                     5/71  ( 7 %)
   A paper clip that finds its price is kept with that probability, else it is
   killed as the real ones were.

Resting (post-only) bids have NO real counterpart here -- the VPS's real bots
never rested an order -- so only the latency applies to them.
"""
from __future__ import annotations

import json
import logging
import os
import random

log = logging.getLogger(__name__)

#: the measured values, used when data/bot/fill_calibration.json is absent
DEFAULT_LATENCY_Q = [(0.0, 180.0), (0.1, 221.0), (0.5, 271.0), (0.9, 534.0), (0.99, 1198.0), (1.0, 1500.0)]
DEFAULT_RACE = {"ask_lower": 7 / 13, "depth_ge20": 16 / 53, "depth_3_20": 14 / 122, "depth_lt3": 5 / 71}


def depth_bucket(ratio: float) -> str:
    return "depth_lt3" if ratio < 3 else "depth_3_20" if ratio < 20 else "depth_ge20"


class Calibration:
    def __init__(self, path: str | None = None, seed: int | None = None) -> None:
        self.rng = random.Random(seed)
        self.latencies: list[float] = []
        self.split: list[tuple[float, float]] = []      # (pre-send, venue) per real order
        self.race = dict(DEFAULT_RACE)
        self.source = "built-in (2026-09-30 measurement)"
        self.orders = 844
        self.real_fill_rate = 0.107
        if path and os.path.exists(path):
            try:
                with open(path) as f:
                    doc = json.load(f)
                lat = [float(x) for x in doc.get("latency_ms") or [] if x and x > 0]
                if len(lat) >= 50:
                    self.latencies = sorted(lat)
                split = [(float(a), float(b)) for a, b in doc.get("latency_split") or [] if b and b > 0]
                if len(split) >= 50:
                    self.split = split
                cells = (doc.get("race") or {}).get("troll-live") or {}
                for k, v in cells.items():
                    if k in self.race and v.get("n", 0) >= 10:
                        self.race[k] = float(v["rate"])
                self.orders = int(doc.get("orders") or self.orders)
                self.real_fill_rate = float(doc.get("real_fill_rate") or self.real_fill_rate)
                self.source = f"{os.path.basename(path)} ({self.orders} real orders, latency from {len(self.latencies)})"
            except (OSError, ValueError, TypeError) as exc:
                log.warning("fill calibration unreadable (%s); using the built-in values", exc)

    #: what is left of pre-send with the orders already signed: pick one, hand it to the socket
    PRESIGNED_SEND_MS = 1.0

    def latency_ms(self, presigned: bool = False) -> float:
        """One order's delay from decision to the book it is matched against.

        ``presigned``: the order was built and signed before the decision (a
        ladder of ready orders), so only the venue part of a real order's
        delay remains -- drawn from the same real orders, pre-send dropped."""
        if presigned and self.split:
            return self.PRESIGNED_SEND_MS + self.rng.choice(self.split)[1]
        if self.latencies:
            return self.rng.choice(self.latencies)
        u = self.rng.random()
        for (q0, v0), (q1, v1) in zip(DEFAULT_LATENCY_Q, DEFAULT_LATENCY_Q[1:]):
            if u <= q1:
                return v0 + (v1 - v0) * (u - q0) / (q1 - q0)
        return DEFAULT_LATENCY_Q[-1][1]

    def race_prob(self, depth_ratio: float, ask_lower: bool) -> tuple[float, str]:
        key = "ask_lower" if ask_lower else depth_bucket(depth_ratio)
        return self.race[key], key

    def win_race(self, depth_ratio: float, ask_lower: bool) -> tuple[bool, float, str]:
        p, key = self.race_prob(depth_ratio, ask_lower)
        return self.rng.random() < p, p, key

    def summary(self) -> dict:
        lat = self.latencies
        pick = (lambda p: round(lat[int(p * (len(lat) - 1))])) if lat else (lambda p: None)
        pre = sorted(a for a, _ in self.split)
        ven = sorted(self.PRESIGNED_SEND_MS + b for _, b in self.split)
        q = (lambda xs, p: round(xs[int(p * (len(xs) - 1))]) if xs else None)
        return {"source": self.source, "orders": self.orders, "real_fill_rate": self.real_fill_rate,
                "presend_p50_ms": q(pre, 0.5), "presigned_p50_ms": q(ven, 0.5), "presigned_p90_ms": q(ven, 0.9),
                "latency_p50_ms": pick(0.5) or 271, "latency_p90_ms": pick(0.9) or 534,
                "race": {k: round(v, 3) for k, v in self.race.items()}}
