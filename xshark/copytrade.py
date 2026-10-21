"""Copy a target wallet's first confirmed fill in each ETH 5-minute market."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass

from .v2 import buy_price

TRADER_WALLET = "0x0c7c5204404e9d5402d258fedac59c7212bae4cb"
ACTIVITY_API = "https://data-api.polymarket.com/activity"
TAG = "leader-first"
MAKER_FILL_MAX_AGE_S = 2.0


@dataclass(frozen=True)
class _PriceRules:
    max_slip: float
    limit_over: float


def fill_identity(fill: dict) -> tuple:
    return (fill.get("transaction"), fill["timestamp"], fill.get("asset"), fill["outcome"],
            fill.get("side"), fill.get("price"), fill["size"])


def unseen_fills(fills: list[dict], seen_counts: dict[tuple, int]) -> list[dict]:
    """Return new occurrences, preserving identical fills in one API response."""
    counts, fresh = Counter(), []
    for fill in fills:
        identity = fill_identity(fill)
        counts[identity] += 1
        if counts[identity] > seen_counts.get(identity, 0):
            fresh.append(fill)
    for identity, count in counts.items():
        seen_counts[identity] = max(seen_counts.get(identity, 0), count)
    return fresh


def fills_for_market(rows: list[dict], slug: str, market_start: int) -> list[dict]:
    """Return all confirmed trades for this market in timestamp order."""
    eligible = [r for r in rows if r.get("type") == "TRADE" and r.get("slug") == slug
                and r.get("outcome") in ("Up", "Down") and int(r.get("timestamp", 0)) >= market_start]
    return [{"timestamp": int(row["timestamp"]), "outcome": row["outcome"], "size": float(row["size"]),
             "side": row.get("side"), "price": row.get("price"), "transaction": row.get("transactionHash"),
             "asset": row.get("asset")}
            for row in sorted(eligible, key=lambda r: (int(r["timestamp"]), str(r.get("transactionHash") or ""),
                                                       r["outcome"], float(r.get("price") or 0.0),
                                                       float(r.get("size") or 0.0)))]


def fetch_market_fills(market, now_s: int) -> list[dict]:
    """Fetch this wallet's confirmed trades for one market from the public Data API."""
    query = urllib.parse.urlencode({
        "user": TRADER_WALLET,
        "type": "TRADE",
        "start": market.start,
        "end": min(now_s, market.end),
        "limit": 500,
        "sortBy": "TIMESTAMP",
        "sortDirection": "ASC",
    })
    request = urllib.request.Request(f"{ACTIVITY_API}?{query}", headers={"User-Agent": "xshark/0.1"})
    with urllib.request.urlopen(request, timeout=3.0) as response:
        rows = json.load(response)
    return fills_for_market(rows, market.slug, market.start)


def decide(v, s, d) -> None:
    """Submit bounded retries for the wallet's first confirmed fill size.

    Copy with a resting bid while the signal is under two seconds old, or when
    the current ask has fallen below the target's reported fill price.
    """
    fill = getattr(v, "copy_fill", None)
    if not s.copy_enabled or not fill or v.copy_done:
        return
    outcome = fill["outcome"]
    target = max(0.0, float(fill.get("size") or 0.0))
    filled = v.copy_filled_shares
    remaining = max(0.0, target - filled)
    if remaining < v.market.min_size - 1e-9:
        d.copy_done = True
        d.why["taker"] = ("first-fill copy filled" if remaining <= 1e-9 else
                           "first-fill size under venue minimum" if filled <= 1e-9 else
                           "first-fill copy partial fill; remainder under venue minimum")
        return
    if (v.in_flight_taker or {}).get(outcome, 0.0) > 1e-9:
        d.why["taker"] = "first-fill copy waiting for its order in flight"
        return
    if v.now_ms / 1000.0 - fill["timestamp"] > s.copy_max_fill_age_s:
        d.copy_done = True
        d.why["taker"] = "first-fill copy signal is stale; skipped"
        return
    if v.copy_attempts >= s.copy_max_attempts:
        d.copy_done = True
        d.why["taker"] = "first-fill copy attempts exhausted"
        return
    if v.copy_attempts and v.now_ms - v.copy_last_attempt_ms < s.copy_retry_ms:
        d.why["taker"] = "first-fill copy waiting before retry"
        return
    if v.live:
        d.copy_done = True
        d.why["taker"] = "first-fill copy is paper-only"
        return
    if v.halted:
        d.why["taker"] = v.halted
        return
    if v.book_age_ms is None or v.book_age_ms > s.copy_max_book_age_ms:
        d.why["taker"] = "first-fill copy waiting for a fresh book"
        return
    if v.secs_left <= s.copy_cutoff_s:
        d.why["taker"] = "first-fill copy past its order cutoff"
        return
    if any(q > 1e-9 for q in (v.in_flight_taker or {}).values()):
        d.why["taker"] = "first-fill copy waiting for an order in flight"
        return

    book = v.books.get(outcome) or {}
    bid, ask = book.get("bid"), book.get("ask")
    if bid is None or ask is None:
        d.why["taker"] = f"first-fill copy waiting for {outcome} quotes"
        return
    if ask - bid > s.copy_max_spread + 1e-9:
        d.why["taker"] = f"first-fill copy {outcome} spread over ${s.copy_max_spread:.2f}"
        return
    if ask > s.copy_max_price + 1e-9:
        d.why["taker"] = f"first-fill copy {outcome} ask over ${s.copy_max_price:.2f}"
        return

    shares = remaining
    age_s = v.now_ms / 1000.0 - fill["timestamp"]
    try:
        leader_price = float(fill.get("price"))
    except (TypeError, ValueError):
        leader_price = None
    price_fell = leader_price is not None and leader_price > 0.0 and ask < leader_price - 1e-9

    if v.copy_maker_price is not None:
        maker_price = v.copy_maker_price
        if maker_price * shares < 1.0 - 1e-9:
            d.why[f"maker_{outcome}"] = "first-fill copy maker order is below the venue's $1 minimum"
            return
        d.places.append((outcome, maker_price, shares))
        d.tags[f"maker:{outcome}"] = TAG
        d.why[f"maker_{outcome}"] = (f"first-fill copy maker retry {v.copy_attempts + 1}/{s.copy_max_attempts}: "
                                     f"{outcome} {shares:g} shares at original bid ${maker_price:.2f}")
        return

    priced = buy_price(v, _PriceRules(s.copy_max_slip, s.copy_limit_over), outcome, shares,
                       cap=s.copy_max_price)
    if priced is None:
        d.why["taker"] = f"first-fill copy cannot fill {shares:g} {outcome} within slippage limit"
        return
    limit, vwap, _ = priced

    if age_s <= MAKER_FILL_MAX_AGE_S or price_fell:
        maker_price = round(min(bid, ask - v.market.tick), 4)
        if maker_price * shares < 1.0 - 1e-9:
            d.why[f"maker_{outcome}"] = "first-fill copy maker order is below the venue's $1 minimum"
            return
        d.places.append((outcome, maker_price, shares))
        d.tags[f"maker:{outcome}"] = TAG
        maker_reason = "ask below trader fill price" if price_fell else "fill seen within 2 seconds"
        d.why[f"maker_{outcome}"] = (f"first-fill copy maker attempt {v.copy_attempts + 1}/{s.copy_max_attempts}: "
                                     f"{outcome} {shares:g} shares ({maker_reason}), trader filled at "
                                     f"{fill['timestamp'] - v.market.start}s; bid ${maker_price:.2f} "
                                     f"(depth ${vwap:.3f})")
        return

    if limit * shares < 1.0 - 1e-9:
        d.why["taker"] = "first-fill copy is below the venue's $1 minimum"
        return

    d.takes.append((outcome, limit, shares, 0.0))
    d.tags[f"taker:{outcome}"] = TAG
    d.why["taker"] = (f"first-fill copy attempt {v.copy_attempts + 1}/{s.copy_max_attempts}: "
                       f"{outcome} {shares:g} shares, trader filled at "
                       f"{fill['timestamp'] - v.market.start}s; limit ${limit:.2f} (depth ${vwap:.3f})")