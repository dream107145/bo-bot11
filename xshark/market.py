"""BTC 5-minute Up/Down markets on Gamma: discovery, fees, official outcome.

The slug is deterministic (``btc-updown-5m-<window start epoch>``) and the
market is listed about a day ahead, so discovery is one GET per window. Tokens
are mapped by their outcome LABEL, never by index (troll-poly-bot's
feeds/markets.py rule: if the ordering ever flipped, assuming it would invert
every position).

Measured 2026-09-30 on the live market: tick 0.01, minimum 5 shares,
``feeSchedule {rate 0.07, exponent 1, takerOnly, rebateRate 0.2}``, resolution
source ``btc-usd-twap-60s-streams`` with ``twapLookbackSeconds 60``. Gamma
answers 403 to urllib's default User-Agent, so every request sends one.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime

log = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (xshark paper bot)", "Accept": "application/json"}
WINDOW_S = 300


class FetchFailed(RuntimeError):
    """The request itself failed -- distinct from "not listed"."""


def window_start(ts_s: float) -> int:
    return int(ts_s) // WINDOW_S * WINDOW_S


#: the coins Polymarket lists 5-minute Up/Down markets for (Gamma, 2026-10-01)
ASSETS = ("btc", "eth", "sol", "xrp", "doge", "bnb")


def slug_for(start: int, asset: str = "btc") -> str:
    return f"{asset.lower()}-updown-5m-{int(start)}"


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    rate: float = 0.07
    exponent: float = 1.0
    taker_only: bool = True
    rebate_rate: float = 0.2

    def taker_fee(self, price: float, shares: float) -> float:
        """USDC a taker pays on ``shares`` bought at ``price``."""
        p = min(max(price, 0.0), 1.0)
        return shares * self.rate * (p * (1.0 - p)) ** self.exponent

    def maker_rebate_est(self, price: float, shares: float) -> float:
        """ESTIMATE of the maker rebate on a fill: the venue pools ``rebate_rate``
        of the taker fees and pays makers pro rata, so this is the fee the
        counterparty paid on this fill times that share. Not the real payout."""
        return self.rebate_rate * self.taker_fee(price, shares)

    @classmethod
    def from_gamma(cls, row: dict) -> "FeeSchedule":
        fs = row.get("feeSchedule") or {}
        if not isinstance(fs, dict) or not row.get("feesEnabled", True):
            return cls(rate=0.0)
        try:
            return cls(rate=float(fs.get("rate", 0.07)), exponent=float(fs.get("exponent", 1.0)),
                       taker_only=bool(fs.get("takerOnly", True)), rebate_rate=float(fs.get("rebateRate", 0.2)))
        except (TypeError, ValueError):
            return cls()


@dataclass(frozen=True, slots=True)
class Market:
    slug: str
    condition_id: str
    question: str
    start: int                      # window open, epoch seconds
    end: int                        # window close, epoch seconds
    up_token: str
    down_token: str
    tick: float = 0.01
    min_size: float = 5.0
    twap_s: float = 60.0
    fees: FeeSchedule = field(default_factory=FeeSchedule)

    def token(self, outcome: str) -> str:
        return self.up_token if outcome == "Up" else self.down_token

    def outcome_of(self, token: str) -> str | None:
        return "Up" if token == self.up_token else "Down" if token == self.down_token else None

    @property
    def tokens(self) -> tuple[str, str]:
        return self.up_token, self.down_token


def _nested(v, default):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return default
    return v if v is not None else default


def _iso_s(v) -> float | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def parse_market(row: dict) -> Market | None:
    slug = str(row.get("slug") or "")
    if "-updown-5m-" not in slug or slug.split("-", 1)[0] not in ASSETS:
        return None
    try:
        start = int(slug.rsplit("-", 1)[1])
    except ValueError:
        return None
    tokens, outcomes = _nested(row.get("clobTokenIds"), []), _nested(row.get("outcomes"), [])
    if len(tokens) != 2 or len(outcomes) != 2:
        log.warning("market %s has %d tokens / %d outcomes", slug, len(tokens), len(outcomes))
        return None
    labels = [str(o).strip().lower() for o in outcomes]
    if "up" not in labels or "down" not in labels:
        log.warning("market %s outcomes are %r, not Up/Down", slug, outcomes)
        return None
    end = _iso_s(row.get("endDate"))
    if end is None or abs(end - (start + WINDOW_S)) > 60:
        log.warning("market %s: endDate %s does not close 300 s after the slug epoch", slug, row.get("endDate"))
        return None
    cfg = row.get("cryptoMarketConfig") or {}
    return Market(
        slug=slug,
        condition_id=str(row.get("conditionId") or ""),
        question=str(row.get("question") or ""),
        start=start,
        end=start + WINDOW_S,
        up_token=str(tokens[labels.index("up")]),
        down_token=str(tokens[labels.index("down")]),
        tick=float(row.get("orderPriceMinTickSize") or 0.01),
        min_size=float(row.get("orderMinSize") or 5),
        twap_s=float(cfg.get("twapLookbackSeconds") or 60) if isinstance(cfg, dict) else 60.0,
        fees=FeeSchedule.from_gamma(row),
    )


def _get_json(url: str, timeout: float = 10.0):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as exc:
        raise FetchFailed(f"{type(exc).__name__}: {exc}") from None


def fetch_market(start: int, asset: str = "btc") -> Market | None:
    """The market for the window opening at ``start``; None if not listed."""
    rows = _get_json(f"{GAMMA}/markets?slug={slug_for(start, asset)}")
    row = rows[0] if isinstance(rows, list) and rows else None
    return parse_market(row) if row else None


def fetch_outcome(slug: str) -> str | None:
    """The venue's own result, "Up" or "Down", once the market has resolved.

    The query carries a throwaway parameter: Gamma sits behind a cache that
    was measured holding rows for minutes (compare_bot README), and a stale
    row reads as "not resolved yet"."""
    rows = _get_json(f"{GAMMA}/markets?slug={slug}&_={int(time.time())}")
    row = rows[0] if isinstance(rows, list) and rows else None
    if not row:
        return None
    prices, outcomes = _nested(row.get("outcomePrices"), []), _nested(row.get("outcomes"), [])
    if len(prices) != 2 or len(outcomes) != 2:
        return None
    try:
        px = {str(o).strip().lower(): float(p) for o, p in zip(outcomes, prices)}
    except (TypeError, ValueError):
        return None
    if px.get("up", 0.0) > 0.9 and px.get("down", 1.0) < 0.1:
        return "Up"
    if px.get("down", 0.0) > 0.9 and px.get("up", 1.0) < 0.1:
        return "Down"
    return None
