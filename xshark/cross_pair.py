"""Cross-market lag strategy for the six paper markets.

Six entry rules, all hold to settlement. Each rule has its own slot per
(window, asset, outcome), so every rule can fire independently when its own
conditions match (including after another rule already bought that side).

1. Leader + pair — in the first four minutes, buy five shares of a token whose
   ask is in [leader_min, leader_max] while the average ask of the same outcome
   on at least ``min_peers`` other markets is above ``peer_avg``. After that
   token's ask has risen by ``rise`` from the entry fill, rest a maker bid for
   five shares of the opposite outcome on the same market (held to a maker fill).
   If the pair is still unfilled ``pair_wait_s`` after the entry fill, pair once
   the entry ask has risen by ``pair_fallback_rise`` instead. Repeat a
   five-share clip at ``leader_every_ms`` while eligible.

2. Hold — from ``hold_from_s`` (1:30) until close, buy five shares of a token
   whose ask is in (hold_min, hold_max) while the peer average is above
   ``hold_peer_avg``, and hold to close with no pair leg. Repeat at
   ``hold_every_ms`` while eligible.

3. Gap + pair — in the first four minutes, buy five shares of a token whose ask
   is in [gap_min, gap_max] and at least ``gap`` below the peer average. After a
   ``rise`` from the entry fill, rest a maker bid for five of the opposite token.
   Same ``pair_wait_s`` / ``pair_fallback_rise`` fallback as rule 1. Repeat at
   ``gap_every_ms`` while eligible.

4. Deep gap + pair — same as rule 3 but ask in [deep_min, deep_max] (cheap band)
   and pair after ``deep_rise`` instead of ``rise`` (no time fallback). Repeat
   at ``deep_every_ms`` while eligible.

5. Majority — from ``maj_from_s`` until ``maj_until_s``, when at least
   ``maj_min_high`` other markets quote the same side above ``maj_high``, buy
   five shares of the remaining market if its ask is strictly under ``maj_max``
   (no pair leg). Repeat at ``maj_every_ms`` while eligible.

6. Sweep — from ``sweep_from_s`` (2:20) until ``sweep_until_s``, when all
   ``sweep_min_high`` other markets quote the same side above ``sweep_peer``,
   buy five shares of the remaining market if its ask is in
   [``sweep_min``, ``sweep_max``]. While that condition stays true, buy another
   five shares at its own configured interval (no pair leg).

7. Cross-asset cascade — PRIMARY. A cycle walks all **15 pairs** in a
   **distributed** order (waves of 3 disjoint pairs) so no market stacks early.
   Every ``xlead_every_ms`` (default 7 s) buy the next pair; after the 15th,
   start the next cycle from BTC×ETH:

   * BTC×ETH, SOL×XRP, DOGE×BNB
   * BTC×SOL, ETH×DOGE, XRP×BNB
   * BTC×XRP, ETH×BNB, SOL×DOGE
   * BTC×DOGE, ETH×XRP, SOL×BNB
   * BTC×BNB, ETH×SOL, XRP×DOGE

   Each step is ``xlead_max_tokens`` shares (default 10 → 5 Up + 5 Down) on
   the cheaper orientation of Up(A)+Down(B) vs Up(B)+Down(A) when sum ≤
   ``xlead_max_sum``. Both legs same tick. Statistical only.

   Secondary: same-market complete-set / balance (``cset``) may still lock a
   full Up+Down set when edge exists. Always runs after Rule 7 and rules 1–6.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field

from .market import ASSETS, Market, WINDOW_S, slug_for

log = logging.getLogger("xshark.cross_pair")

DEFAULT_SHARES = 5.0
DEFAULT_LIMIT_OFFSET = 0.02
DEFAULT_RETRY_MS = 500
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_LEADER_MIN = 0.30
DEFAULT_LEADER_MAX = 0.60
DEFAULT_PEER_AVG = 0.88
DEFAULT_RISE = 0.30
DEFAULT_BUY_UNTIL_S = 240.0
DEFAULT_HOLD_PEER_AVG = 0.90
DEFAULT_HOLD_MIN = 0.50
DEFAULT_HOLD_MAX = 0.60
DEFAULT_HOLD_FROM_S = 90.0                # 1:30 into the window
DEFAULT_GAP = 0.35                        # peer avg - ask must be at least this
DEFAULT_GAP_MIN = 0.10
DEFAULT_GAP_MAX = 0.35
DEFAULT_GAP_UNTIL_S = 240.0               # rule 3: no new buys after this many s in
DEFAULT_DEEP = 0.35                       # rule 4: peer avg - ask must be at least this
DEFAULT_DEEP_MIN = 0.01
DEFAULT_DEEP_MAX = 0.15
DEFAULT_DEEP_RISE = 0.10                  # rule 4: maker-pair after this rise from entry
DEFAULT_PAIR_WAIT_S = 40.0                # rules 1/3: after this, use pair_fallback_rise
DEFAULT_PAIR_FALLBACK_RISE = 0.10         # rules 1/3: pair rise once pair_wait_s has elapsed
DEFAULT_CHASE = 0.05                      # rules 1/3/4: no more buys once ask rose this far from signal
DEFAULT_MIN_PEERS = 5
DEFAULT_MAJ_HIGH = 0.85                   # rule 5: peer ask must be above this
DEFAULT_MAJ_MAX = 0.70                    # rule 5: buy only while ask is under this
DEFAULT_MAJ_MIN_HIGH = 5                  # rule 5: this many other markets above maj_high
DEFAULT_MAJ_FROM_S = 0.0                  # rule 5: no buys before this many s in
DEFAULT_MAJ_UNTIL_S = 300.0               # rule 5: no new buys after this many s in
DEFAULT_SWEEP_PEER = 0.90                 # rule 6: every other market must be above this
DEFAULT_SWEEP_MIN = 0.70
DEFAULT_SWEEP_MAX = 0.90
DEFAULT_SWEEP_MIN_HIGH = 5               # rule 6: all five other markets
DEFAULT_SWEEP_EVERY_MS = 5000            # rule 6: another 5 shares on this cadence
DEFAULT_RULE_REPEAT_EVERY_MS = 5000      # rules 1–5: repeat while each signal remains eligible
DEFAULT_SWEEP_FROM_S = 140.0             # rule 6: no buys before 2:20 into the window
DEFAULT_SWEEP_UNTIL_S = 300.0            # rule 6: no new buys after this many s in
DEFAULT_XLEAD_EVERY_MS = 7000            # rule 7: wait between consecutive cascade pairs
DEFAULT_XLEAD_MAX_SUM = 1.0              # rule 7: only buy when cross sum ≤ this
DEFAULT_XLEAD_MAX_TOKENS = 10.0          # rule 7: shares per pair (both legs; 5+5)
DEFAULT_XLEAD_FROM_S = 0.0
DEFAULT_XLEAD_UNTIL_S = 290.0
XLEAD_MIN_LEG = 5.0                      # venue minimum shares per leg
# Distributed 15-pair schedule: waves of 3 disjoint pairs (1-factorization of K6)
# so consecutive buys spread across markets instead of stacking BTC first.
XLEAD_CASCADE: tuple[tuple[str, str], ...] = (
    ("btc", "eth"), ("sol", "xrp"), ("doge", "bnb"),   # wave 1
    ("btc", "sol"), ("eth", "doge"), ("xrp", "bnb"),   # wave 2
    ("btc", "xrp"), ("eth", "bnb"), ("sol", "doge"),   # wave 3
    ("btc", "doge"), ("eth", "xrp"), ("sol", "bnb"),   # wave 4
    ("btc", "bnb"), ("eth", "sol"), ("xrp", "doge"),   # wave 5
)
DEFAULT_CSET_MIN_PROFIT = 0.01           # secondary complete-set: min locked $/sh
DEFAULT_CSET_MIN_ROI = 0.01
DEFAULT_CSET_RESERVE = 0.005
DEFAULT_CSET_FROM_S = 0.0
DEFAULT_CSET_UNTIL_S = 290.0
DEFAULT_CSET_MAX_LOSS = 0.02
CSET_OUTCOME = "Set"
XLEAD_OUTCOME = "Cross"                  # rule 7 position: Up on asset + Down on leg2_asset
MAX_LIMIT_PRICE = 0.99
MAX_BOOK_AGE_MS = 2000.0
EPS = 1e-8
OTH = {"Up": "Down", "Down": "Up"}
PAIR_KINDS = frozenset({"leader", "gap", "deep"})
ENTRY_KINDS = frozenset({"leader", "hold", "gap", "deep", "maj", "sweep", "xlead", "cset"})
# Fill tags crosspair:{kind}:… map to console rule buckets (pair leg joins its entry).
KIND_TO_RULE = {"leader": "rule1", "hold": "rule2", "gap": "rule3", "deep": "rule4",
                "maj": "rule5", "sweep": "rule6", "xlead": "rule7", "cset": "cset"}
RULE_KEYS = ("rule1", "rule2", "rule3", "rule4", "rule5", "rule6", "rule7", "cset")


def _node(asset: str, outcome: str) -> str:
    return f"{asset.lower()}:{outcome}"


@dataclass(slots=True)
class Quote:
    bot: object
    market: Market
    outcome: str
    ask: float
    bid: float | None
    limit: float
    all_in: float


@dataclass(frozen=True, slots=True)
class PairParams:
    shares: float = DEFAULT_SHARES
    limit_offset: float = DEFAULT_LIMIT_OFFSET
    retry_ms: int = DEFAULT_RETRY_MS
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS
    rule1: bool = True                        # leader + pair
    rule2: bool = True                        # hold
    rule3: bool = True                        # gap + pair
    rule4: bool = True                        # deep gap + pair
    rule5: bool = True                        # majority lag, no pair
    rule6: bool = True                        # all-high sweep, no pair; repeats
    rule7: bool = True                        # all OK cross-asset Up+Down every N ms (PRIMARY)
    cset: bool = True                         # same-market complete-set / balance (SECONDARY)
    leader_min: float = DEFAULT_LEADER_MIN
    leader_max: float = DEFAULT_LEADER_MAX
    peer_avg: float = DEFAULT_PEER_AVG
    leader_every_ms: int = DEFAULT_RULE_REPEAT_EVERY_MS
    rise: float = DEFAULT_RISE
    buy_until_s: float = DEFAULT_BUY_UNTIL_S
    hold_peer_avg: float = DEFAULT_HOLD_PEER_AVG
    hold_min: float = DEFAULT_HOLD_MIN
    hold_max: float = DEFAULT_HOLD_MAX
    hold_from_s: float = DEFAULT_HOLD_FROM_S
    hold_every_ms: int = DEFAULT_RULE_REPEAT_EVERY_MS
    gap: float = DEFAULT_GAP
    gap_min: float = DEFAULT_GAP_MIN
    gap_max: float = DEFAULT_GAP_MAX
    gap_until_s: float = DEFAULT_GAP_UNTIL_S
    gap_every_ms: int = DEFAULT_RULE_REPEAT_EVERY_MS
    deep: float = DEFAULT_DEEP
    deep_min: float = DEFAULT_DEEP_MIN
    deep_max: float = DEFAULT_DEEP_MAX
    deep_rise: float = DEFAULT_DEEP_RISE
    deep_every_ms: int = DEFAULT_RULE_REPEAT_EVERY_MS
    maj_high: float = DEFAULT_MAJ_HIGH
    maj_max: float = DEFAULT_MAJ_MAX
    maj_min_high: int = DEFAULT_MAJ_MIN_HIGH
    maj_from_s: float = DEFAULT_MAJ_FROM_S
    maj_until_s: float = DEFAULT_MAJ_UNTIL_S
    maj_every_ms: int = DEFAULT_RULE_REPEAT_EVERY_MS
    sweep_peer: float = DEFAULT_SWEEP_PEER
    sweep_min: float = DEFAULT_SWEEP_MIN
    sweep_max: float = DEFAULT_SWEEP_MAX
    sweep_min_high: int = DEFAULT_SWEEP_MIN_HIGH
    sweep_every_ms: int = DEFAULT_SWEEP_EVERY_MS
    sweep_from_s: float = DEFAULT_SWEEP_FROM_S
    sweep_until_s: float = DEFAULT_SWEEP_UNTIL_S
    xlead_every_ms: int = DEFAULT_XLEAD_EVERY_MS
    xlead_max_sum: float = DEFAULT_XLEAD_MAX_SUM
    xlead_max_tokens: float = DEFAULT_XLEAD_MAX_TOKENS
    xlead_from_s: float = DEFAULT_XLEAD_FROM_S
    xlead_until_s: float = DEFAULT_XLEAD_UNTIL_S
    cset_min_profit: float = DEFAULT_CSET_MIN_PROFIT
    cset_min_roi: float = DEFAULT_CSET_MIN_ROI
    cset_reserve: float = DEFAULT_CSET_RESERVE
    cset_from_s: float = DEFAULT_CSET_FROM_S
    cset_until_s: float = DEFAULT_CSET_UNTIL_S
    cset_max_loss: float = DEFAULT_CSET_MAX_LOSS
    pair_wait_s: float = DEFAULT_PAIR_WAIT_S
    pair_fallback_rise: float = DEFAULT_PAIR_FALLBACK_RISE
    chase: float = DEFAULT_CHASE
    min_peers: int = DEFAULT_MIN_PEERS


@dataclass(slots=True)
class Position:
    start: int
    asset: str
    outcome: str
    kind: str                                 # leader|hold|gap|deep|maj|sweep|xlead|cset
    entry_ask: float                          # fill avg once filled; used for +rise pair
    signal_ask: float = 0.0                   # ask at first signal; chase measured from this
    filled_at_ms: float = 0.0                 # first entry-fill time (pair wait clock)
    attempts: int = 0
    retry_at_ms: float = 0.0
    pair_attempts: int = 0
    pair_retry_at_ms: float = 0.0
    up_signal: float = 0.0                    # rule 7/cset: Up ask at signal
    down_signal: float = 0.0                  # rule 7/cset: Down ask at signal
    leg2_asset: str = ""                      # rule 7: Down-leg asset (≠ asset)
    notes: list[str] = field(default_factory=list)


def peer_average(asks: dict[str, float], asset: str, outcome: str,
                 min_peers: int = DEFAULT_MIN_PEERS) -> float | None:
    """Mean same-outcome ask across other markets; None unless enough peers quote."""
    asset = asset.lower()
    peers = [px for node, px in asks.items()
             if node.endswith(f":{outcome}") and not node.startswith(f"{asset}:")]
    if len(peers) < min_peers:
        return None
    return sum(peers) / len(peers)


def hold_eligible(ask: float, peer_avg: float | None, elapsed_s: float, p: PairParams) -> bool:
    if not p.rule2:
        return False
    if elapsed_s + EPS < p.hold_from_s:
        return False
    return (peer_avg is not None and peer_avg > p.hold_peer_avg + EPS
            and p.hold_min + EPS < ask < p.hold_max - EPS)


def leader_eligible(ask: float, peer_avg: float | None, elapsed_s: float, p: PairParams) -> bool:
    if not p.rule1:
        return False
    if elapsed_s >= p.buy_until_s - EPS:
        return False
    if peer_avg is None or peer_avg <= p.peer_avg + EPS:
        return False
    return p.leader_min - EPS <= ask <= p.leader_max + EPS


def gap_eligible(ask: float, peer_avg: float | None, elapsed_s: float, p: PairParams) -> bool:
    """Rule 3: ask in [gap_min, gap_max] and at least ``gap`` under the peer average."""
    if not p.rule3:
        return False
    if elapsed_s >= p.gap_until_s - EPS:
        return False
    if peer_avg is None:
        return False
    if not (p.gap_min - EPS <= ask <= p.gap_max + EPS):
        return False
    return peer_avg - ask + EPS >= p.gap


def deep_eligible(ask: float, peer_avg: float | None, elapsed_s: float, p: PairParams) -> bool:
    """Rule 4: ask in [deep_min, deep_max] and at least ``deep`` under the peer average."""
    if not p.rule4:
        return False
    if elapsed_s >= p.buy_until_s - EPS:
        return False
    if peer_avg is None:
        return False
    if not (p.deep_min - EPS <= ask <= p.deep_max + EPS):
        return False
    return peer_avg - ask + EPS >= p.deep


def high_peer_count(asks: dict[str, float], asset: str, outcome: str, high: float) -> int:
    """How many other markets quote the same side strictly above ``high``."""
    asset = asset.lower()
    return sum(1 for node, px in asks.items()
               if node.endswith(f":{outcome}") and not node.startswith(f"{asset}:")
               and px > high + EPS)


def maj_eligible(ask: float, asks: dict[str, float], asset: str, outcome: str,
                 elapsed_s: float, p: PairParams) -> bool:
    """Rule 5: ≥ maj_min_high peers above maj_high, and this ask strictly under maj_max."""
    if not p.rule5:
        return False
    if elapsed_s + EPS < p.maj_from_s:
        return False
    if elapsed_s >= p.maj_until_s - EPS:
        return False
    if ask + EPS >= p.maj_max:
        return False
    return high_peer_count(asks, asset, outcome, p.maj_high) >= p.maj_min_high


def sweep_eligible(ask: float, asks: dict[str, float], asset: str, outcome: str,
                   elapsed_s: float, p: PairParams) -> bool:
    """Rule 6: from sweep_from_s, all peers above sweep_peer, ask in [sweep_min, sweep_max]."""
    if not p.rule6:
        return False
    if elapsed_s + EPS < p.sweep_from_s:
        return False
    if elapsed_s >= p.sweep_until_s - EPS:
        return False
    if not (p.sweep_min - EPS <= ask <= p.sweep_max + EPS):
        return False
    return high_peer_count(asks, asset, outcome, p.sweep_peer) >= p.sweep_min_high


def pair_ready(leader_ask: float, entry_px: float, rise: float) -> bool:
    return leader_ask + EPS >= entry_px + rise


def pair_rise_for(kind: str, p: PairParams, *,
                  filled_at_ms: float = 0.0, now_ms: float = 0.0) -> float:
    """Rule 4: ``deep_rise``. Rules 1/3: ``rise``, or ``pair_fallback_rise`` after wait."""
    if kind == "deep":
        return p.deep_rise
    if (kind in ("leader", "gap") and p.pair_wait_s > EPS and filled_at_ms > EPS
            and now_ms + EPS >= filled_at_ms + p.pair_wait_s * 1000.0):
        return p.pair_fallback_rise
    return p.rise


def chased_away(ask: float, signal_ask: float, chase: float) -> bool:
    """True when the ask has risen ``chase`` or more from the first signal price."""
    return chase > EPS and ask + EPS >= signal_ask + chase


def still_cheap(ask: float, signal_ask: float) -> bool:
    """True while ask is still at or below the quote when the entry order was made."""
    return signal_ask > EPS and ask <= signal_ask + EPS


def cset_costs(up_all_in: float, down_all_in: float,
               reserve: float = DEFAULT_CSET_RESERVE) -> tuple[float, float, float]:
    """Per matched share: (all-in cost, net profit vs $1 payout, ROI)."""
    cost = float(up_all_in) + float(down_all_in) + 2.0 * max(0.0, float(reserve))
    profit = 1.0 - cost
    roi = profit / cost if cost > EPS else 0.0
    return cost, profit, roi


def xlead_pair_key(asset_a: str, asset_b: str) -> tuple[str, str]:
    """Canonical unordered pair key."""
    a, b = asset_a.lower(), asset_b.lower()
    return (a, b) if a <= b else (b, a)


def xlead_resolve_pair(quotes: dict[str, Quote], asset_a: str, asset_b: str,
                       max_sum: float = DEFAULT_XLEAD_MAX_SUM
                       ) -> tuple[str, str, Quote, Quote, float] | None:
    """Cheaper OK orientation of {A,B}, or None if sum > max / missing quotes."""
    best: tuple[str, str, Quote, Quote, float] | None = None
    for up_asset, dn_asset in ((asset_a, asset_b), (asset_b, asset_a)):
        up = quotes.get(_node(up_asset, "Up"))
        down = quotes.get(_node(dn_asset, "Down"))
        if up is None or down is None:
            continue
        pair_ask = up.ask + down.ask
        if pair_ask > max_sum + EPS:
            continue
        if best is None or pair_ask + EPS < best[4]:
            best = (up_asset, dn_asset, up, down, pair_ask)
    return best


def xlead_best_remaining(quotes: dict[str, Quote],
                         remaining: set[tuple[str, str]] | list[tuple[str, str]],
                         max_sum: float = DEFAULT_XLEAD_MAX_SUM
                         ) -> tuple[tuple[str, str], str, str, Quote, Quote, float] | None:
    """Cheapest OK pair among ``remaining`` keys; None if none qualify."""
    best: tuple[tuple[str, str], str, str, Quote, Quote, float] | None = None
    for key in remaining:
        a, b = key
        resolved = xlead_resolve_pair(quotes, a, b, max_sum)
        if resolved is None:
            continue
        up_asset, dn_asset, up, down, pair_ask = resolved
        if best is None or pair_ask + EPS < best[5]:
            best = (key, up_asset, dn_asset, up, down, pair_ask)
    return best


def xlead_cascade_pairs(quotes: dict[str, Quote],
                        max_sum: float = DEFAULT_XLEAD_MAX_SUM,
                        schedule: tuple[tuple[str, str], ...] = XLEAD_CASCADE
                        ) -> list[tuple[str, str, Quote, Quote, float]]:
    """OK pairs in fixed cascade order (diagnostics / tests)."""
    out: list[tuple[str, str, Quote, Quote, float]] = []
    for a, b in schedule:
        best = xlead_resolve_pair(quotes, a, b, max_sum)
        if best is not None:
            out.append(best)
    return out


# Alias kept for older imports / tests.
xlead_all_pairs = xlead_cascade_pairs


def best_xlead_pair(quotes: dict[str, Quote],
                    max_sum: float = DEFAULT_XLEAD_MAX_SUM
                    ) -> tuple[str, str, Quote, Quote, float] | None:
    """Cheapest OK cascade pair (diagnostics)."""
    pairs = xlead_cascade_pairs(quotes, max_sum)
    if not pairs:
        return None
    return min(pairs, key=lambda p: (p[4], p[0], p[1]))


def cset_eligible(up_all_in: float, down_all_in: float, elapsed_s: float, p: PairParams) -> bool:
    """Secondary: same-market Up+Down locks a positive net margin after fees/reserve."""
    if not p.cset:
        return False
    if elapsed_s + EPS < p.cset_from_s:
        return False
    if elapsed_s >= p.cset_until_s - EPS:
        return False
    if up_all_in <= EPS or down_all_in <= EPS:
        return False
    _cost, profit, roi = cset_costs(up_all_in, down_all_in, p.cset_reserve)
    if profit + EPS < p.cset_min_profit:
        return False
    if roi + EPS < p.cset_min_roi:
        return False
    return True


def _fill_pnl(f: dict, winner: str | None) -> float:
    """Settled PnL for one fill; uses stored ``pnl`` when present (fills.jsonl)."""
    if f.get("pnl") is not None and winner is None:
        try:
            return float(f["pnl"])
        except (TypeError, ValueError):
            pass
    try:
        size = float(f.get("size") or 0.0)
        price = float(f.get("price") or 0.0)
        fee = float(f.get("fee") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    outcome = f.get("outcome")
    if winner is None and f.get("won") is not None:
        payout = size if f.get("won") else 0.0
    elif winner is not None:
        payout = size if outcome == winner else 0.0
    else:
        return 0.0
    return payout - (price * size + fee)


def rule_pnl_from_fills(fills: list[dict], winner: str | None = None) -> dict[str, float]:
    """Attribute settled fill PnL to rules 1–7 and cset. Pair legs join their entry rule."""
    out = {k: 0.0 for k in RULE_KEYS}
    entry_kind: dict[str, str] = {}
    legacy_pairs: list[tuple[str, float]] = []
    for f in fills or []:
        tag = str(f.get("tag") or "")
        if not tag.startswith("crosspair:"):
            continue
        parts = tag.split(":")
        if len(parts) < 2:
            continue
        kind = parts[1]
        pnl = _fill_pnl(f, winner)
        if kind == "pair":
            # New: crosspair:pair:{entry_kind}:{asset}:{outcome}
            # Legacy: crosspair:pair:{asset}:{outcome}
            if len(parts) >= 5 and parts[2] in KIND_TO_RULE:
                rule = KIND_TO_RULE[parts[2]]
                out[rule] += pnl
            else:
                outcome = str(f.get("outcome") or (parts[3] if len(parts) > 3 else ""))
                legacy_pairs.append((outcome, pnl))
            continue
        outcome = str(f.get("outcome") or (parts[3] if len(parts) > 3 else ""))
        rule = KIND_TO_RULE.get(kind)
        if rule is None:
            continue
        if outcome in ("Up", "Down"):
            entry_kind[outcome] = kind
        out[rule] += pnl
    for outcome, pnl in legacy_pairs:
        other = OTH.get(outcome)
        rule = KIND_TO_RULE.get(entry_kind.get(other or "", ""))
        if rule is not None:
            out[rule] += pnl
    return {k: round(v, 6) for k, v in out.items()}


class CrossPairStrategy:
    """Coordinates paper orders across the per-asset workers and shared cash."""

    def __init__(self, bots: dict[str, object], data_dir: str) -> None:
        self.bots = bots
        self.path = os.path.join(data_dir, "cross_pair_plans.json")
        # Key includes kind so every rule has an independent slot.
        self.positions: dict[tuple[int, str, str, str], Position] = {}
        self._xlead_next_ms = 0.0
        self._xlead_clip_start: int | None = None
        self._xlead_idx = 0                       # 0..14 into XLEAD_CASCADE
        self._xlead_cycle = 1                     # 1-based cycle counter (per window)
        self._load()

    @staticmethod
    def _pos_key(start: int, asset: str, outcome: str, kind: str) -> tuple[int, str, str, str]:
        return (start, asset.lower(), outcome, kind)

    def _has_kind(self, start: int, asset: str, outcome: str, kind: str) -> bool:
        return self._pos_key(start, asset, outcome, kind) in self.positions

    @staticmethod
    def _pair_tag(entry_kind: str, asset: str, pair_outcome: str) -> str:
        return f"crosspair:pair:{entry_kind}:{asset}:{pair_outcome}"

    def _params(self) -> PairParams:
        bot = self.bots.get("btc") or next(iter(self.bots.values()), None)
        settings = getattr(bot, "s", None)
        if settings is None:
            return PairParams()
        return PairParams(
            shares=float(getattr(settings, "cross_pair_shares", DEFAULT_SHARES)),
            limit_offset=float(getattr(settings, "cross_pair_limit_offset", DEFAULT_LIMIT_OFFSET)),
            retry_ms=int(getattr(settings, "cross_pair_retry_ms", DEFAULT_RETRY_MS)),
            retry_attempts=int(getattr(settings, "cross_pair_retry_attempts", DEFAULT_RETRY_ATTEMPTS)),
            rule1=bool(getattr(settings, "cross_pair_rule1", True)),
            rule2=bool(getattr(settings, "cross_pair_rule2", True)),
            rule3=bool(getattr(settings, "cross_pair_rule3", True)),
            rule4=bool(getattr(settings, "cross_pair_rule4", True)),
            rule5=bool(getattr(settings, "cross_pair_rule5", True)),
            rule6=bool(getattr(settings, "cross_pair_rule6", True)),
            rule7=bool(getattr(settings, "cross_pair_rule7", True)),
            cset=bool(getattr(settings, "cross_pair_cset", True)),
            leader_min=float(getattr(settings, "cross_pair_leader_min", DEFAULT_LEADER_MIN)),
            leader_max=float(getattr(settings, "cross_pair_leader_max", DEFAULT_LEADER_MAX)),
            peer_avg=float(getattr(settings, "cross_pair_peer_avg", DEFAULT_PEER_AVG)),
            leader_every_ms=int(getattr(settings, "cross_pair_leader_every_ms", DEFAULT_RULE_REPEAT_EVERY_MS)),
            rise=float(getattr(settings, "cross_pair_rise", DEFAULT_RISE)),
            buy_until_s=float(getattr(settings, "cross_pair_buy_until_s", DEFAULT_BUY_UNTIL_S)),
            hold_peer_avg=float(getattr(settings, "cross_pair_hold_peer_avg", DEFAULT_HOLD_PEER_AVG)),
            hold_min=float(getattr(settings, "cross_pair_hold_min", DEFAULT_HOLD_MIN)),
            hold_max=float(getattr(settings, "cross_pair_hold_max", DEFAULT_HOLD_MAX)),
            hold_from_s=float(getattr(settings, "cross_pair_hold_from_s", DEFAULT_HOLD_FROM_S)),
            hold_every_ms=int(getattr(settings, "cross_pair_hold_every_ms", DEFAULT_RULE_REPEAT_EVERY_MS)),
            gap=float(getattr(settings, "cross_pair_gap", DEFAULT_GAP)),
            gap_min=float(getattr(settings, "cross_pair_gap_min", DEFAULT_GAP_MIN)),
            gap_max=float(getattr(settings, "cross_pair_gap_max", DEFAULT_GAP_MAX)),
            gap_until_s=float(getattr(settings, "cross_pair_gap_until_s", DEFAULT_GAP_UNTIL_S)),
            gap_every_ms=int(getattr(settings, "cross_pair_gap_every_ms", DEFAULT_RULE_REPEAT_EVERY_MS)),
            deep=float(getattr(settings, "cross_pair_deep", DEFAULT_DEEP)),
            deep_min=float(getattr(settings, "cross_pair_deep_min", DEFAULT_DEEP_MIN)),
            deep_max=float(getattr(settings, "cross_pair_deep_max", DEFAULT_DEEP_MAX)),
            deep_rise=float(getattr(settings, "cross_pair_deep_rise", DEFAULT_DEEP_RISE)),
            deep_every_ms=int(getattr(settings, "cross_pair_deep_every_ms", DEFAULT_RULE_REPEAT_EVERY_MS)),
            maj_high=float(getattr(settings, "cross_pair_maj_high", DEFAULT_MAJ_HIGH)),
            maj_max=float(getattr(settings, "cross_pair_maj_max", DEFAULT_MAJ_MAX)),
            maj_min_high=int(getattr(settings, "cross_pair_maj_min_high", DEFAULT_MAJ_MIN_HIGH)),
            maj_from_s=float(getattr(settings, "cross_pair_maj_from_s", DEFAULT_MAJ_FROM_S)),
            maj_until_s=float(getattr(settings, "cross_pair_maj_until_s", DEFAULT_MAJ_UNTIL_S)),
            maj_every_ms=int(getattr(settings, "cross_pair_maj_every_ms", DEFAULT_RULE_REPEAT_EVERY_MS)),
            sweep_peer=float(getattr(settings, "cross_pair_sweep_peer", DEFAULT_SWEEP_PEER)),
            sweep_min=float(getattr(settings, "cross_pair_sweep_min", DEFAULT_SWEEP_MIN)),
            sweep_max=float(getattr(settings, "cross_pair_sweep_max", DEFAULT_SWEEP_MAX)),
            sweep_min_high=int(getattr(settings, "cross_pair_sweep_min_high", DEFAULT_SWEEP_MIN_HIGH)),
            sweep_every_ms=int(getattr(settings, "cross_pair_sweep_every_ms", DEFAULT_SWEEP_EVERY_MS)),
            sweep_from_s=float(getattr(settings, "cross_pair_sweep_from_s", DEFAULT_SWEEP_FROM_S)),
            sweep_until_s=float(getattr(settings, "cross_pair_sweep_until_s", DEFAULT_SWEEP_UNTIL_S)),
            xlead_every_ms=int(getattr(settings, "cross_pair_xlead_every_ms", DEFAULT_XLEAD_EVERY_MS)),
            xlead_max_sum=float(getattr(settings, "cross_pair_xlead_max_sum", DEFAULT_XLEAD_MAX_SUM)),
            xlead_max_tokens=float(getattr(settings, "cross_pair_xlead_max_tokens", DEFAULT_XLEAD_MAX_TOKENS)),
            xlead_from_s=float(getattr(settings, "cross_pair_xlead_from_s", DEFAULT_XLEAD_FROM_S)),
            xlead_until_s=float(getattr(settings, "cross_pair_xlead_until_s", DEFAULT_XLEAD_UNTIL_S)),
            cset_min_profit=float(getattr(settings, "cross_pair_cset_min_profit", DEFAULT_CSET_MIN_PROFIT)),
            cset_min_roi=float(getattr(settings, "cross_pair_cset_min_roi", DEFAULT_CSET_MIN_ROI)),
            cset_reserve=float(getattr(settings, "cross_pair_cset_reserve", DEFAULT_CSET_RESERVE)),
            cset_from_s=float(getattr(settings, "cross_pair_cset_from_s", DEFAULT_CSET_FROM_S)),
            cset_until_s=float(getattr(settings, "cross_pair_cset_until_s", DEFAULT_CSET_UNTIL_S)),
            cset_max_loss=float(getattr(settings, "cross_pair_cset_max_loss", DEFAULT_CSET_MAX_LOSS)),
            pair_wait_s=float(getattr(settings, "cross_pair_pair_wait_s", DEFAULT_PAIR_WAIT_S)),
            pair_fallback_rise=float(getattr(settings, "cross_pair_pair_fallback_rise",
                                             DEFAULT_PAIR_FALLBACK_RISE)),
            chase=float(getattr(settings, "cross_pair_chase", DEFAULT_CHASE)),
            min_peers=int(getattr(settings, "cross_pair_min_peers", DEFAULT_MIN_PEERS)),
        )

    def _load(self) -> None:
        try:
            with open(self.path) as fh:
                doc = json.load(fh)
        except (OSError, ValueError, TypeError):
            return
        rows = doc.get("positions") or []
        for row in rows:
            try:
                start = int(row["start"])
                asset = str(row["asset"]).lower()
                outcome = str(row["outcome"])
                kind = str(row.get("kind") or "leader")
                if outcome not in ("Up", "Down", CSET_OUTCOME, XLEAD_OUTCOME) or kind not in ENTRY_KINDS:
                    continue
                signal = float(row.get("signal_ask") or row.get("entry_ask") or 0.0)
                leg2 = str(row.get("leg2_asset") or "").lower()
                pos = Position(
                    start=start, asset=asset, outcome=outcome, kind=kind,
                    entry_ask=float(row.get("entry_ask") or 0.0),
                    signal_ask=signal,
                    filled_at_ms=float(row.get("filled_at_ms") or 0.0),
                    attempts=max(0, int(row.get("attempts", 0))),
                    pair_attempts=max(0, int(row.get("pair_attempts", 0))),
                    up_signal=float(row.get("up_signal") or 0.0),
                    down_signal=float(row.get("down_signal") or 0.0),
                    leg2_asset=leg2,
                )
                key_asset = asset if kind != "xlead" else f"{asset}>{leg2}"
                self.positions[self._pos_key(start, key_asset, outcome, kind)] = pos
            except (KeyError, TypeError, ValueError):
                continue

    def _save(self) -> None:
        now = time.time()
        self.positions = {k: p for k, p in self.positions.items()
                          if p.start + WINDOW_S >= now - 120.0}
        rows = [{"start": p.start, "asset": p.asset, "outcome": p.outcome, "kind": p.kind,
                 "entry_ask": p.entry_ask, "signal_ask": p.signal_ask,
                 "filled_at_ms": p.filled_at_ms,
                 "attempts": p.attempts, "pair_attempts": p.pair_attempts,
                 "up_signal": p.up_signal, "down_signal": p.down_signal,
                 "leg2_asset": p.leg2_asset}
                for p in sorted(self.positions.values(),
                                key=lambda x: (x.start, x.asset, x.outcome, x.kind))]
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"version": 2, "positions": rows}, fh, separators=(",", ":"))
        os.replace(tmp, self.path)

    def pairings_for_asset(self, asset: str, start: int) -> list[dict]:
        """Current-window leader/hold entries and any opposite-token pair legs."""
        asset = asset.lower()
        out = []
        for pos in self.positions.values():
            if pos.start != int(start):
                continue
            partners = []
            if pos.kind == "xlead" and pos.outcome == XLEAD_OUTCOME:
                if pos.asset != asset and pos.leg2_asset != asset:
                    continue
                other = pos.leg2_asset if pos.asset == asset else pos.asset
                other_out = "Down" if pos.asset == asset else "Up"
                self_out = "Up" if pos.asset == asset else "Down"
                partners.append({"plan_id": f"xlead:{other_out}", "asset": other.upper(),
                                 "outcome": other_out})
                out.append({"asset": asset.upper(), "outcome": self_out, "kind": "xlead",
                            "partners": partners})
                continue
            if pos.asset != asset:
                continue
            if pos.kind == "cset":
                partners.append({"plan_id": "cset:Down", "asset": asset.upper(),
                                 "outcome": "Down"})
                out.append({"asset": asset.upper(), "outcome": "Up", "kind": "cset",
                            "partners": partners})
                continue
            if pos.kind in PAIR_KINDS:
                partners.append({"plan_id": f"{pos.kind}:{pos.outcome}",
                                 "asset": asset.upper(), "outcome": OTH[pos.outcome]})
            out.append({"asset": asset.upper(), "outcome": pos.outcome, "kind": pos.kind,
                        "partners": partners})
        return out

    @staticmethod
    def _fee_per_share(market: Market, price: float) -> float:
        return market.fees.taker_fee(price, 1.0)

    def _quotes(self, now_ms: float) -> tuple[int | None, dict[str, Quote], dict[str, float]]:
        params = self._params()
        live: dict[str, tuple[object, object]] = {}
        starts = set()
        for asset, bot in list(self.bots.items()):
            run = getattr(bot, "current", None)
            if run is None or run.closed or now_ms >= run.m.end * 1000.0:
                continue
            live[asset.lower()] = (bot, run)
            starts.add(run.m.start)
        if not starts:
            return None, {}, {}
        start = max(starts)
        quotes: dict[str, Quote] = {}
        asks: dict[str, float] = {}
        for asset, (bot, run) in live.items():
            m = run.m
            if m.start != start or getattr(bot, "halted", None):
                continue
            for outcome in ("Up", "Down"):
                token = m.token(outcome)
                book = bot.books.book(token)
                age = bot.books.age_ms(token)
                ask = book.best_ask() if book is not None else None
                bid = book.best_bid() if book is not None else None
                if ask is None or age is None or age > MAX_BOOK_AGE_MS or ask <= 0.0:
                    continue
                node = _node(asset, outcome)
                # Always record the ask for peer averages / Rule 5 counts, even at $0.99.
                asks[node] = ask
                if ask > MAX_LIMIT_PRICE + EPS:
                    continue
                # Clamp the limit so expensive legs stay executable (needed for Up+Down pairs).
                limit = min(MAX_LIMIT_PRICE, round(ask + params.limit_offset, 4))
                unit = limit + self._fee_per_share(m, limit)
                quotes[node] = Quote(bot, m, outcome, ask, bid, limit, unit)
        return start, quotes, asks

    @staticmethod
    def _xlead_pair_id(up_asset: str, dn_asset: str) -> str:
        return f"{up_asset.lower()}>{dn_asset.lower()}"

    def _fills(self, start: int, asset: str, outcome: str, kind: str,
               *, entry_kind: str | None = None, pair_id: str | None = None) -> list:
        bot = self.bots.get(asset)
        if bot is None:
            return []
        slug = slug_for(start, asset)
        if kind == "pair" and entry_kind:
            tag = self._pair_tag(entry_kind, asset, outcome)
        elif kind == "xlead" and pair_id:
            tag = f"crosspair:xlead:{pair_id}:{outcome}"
        else:
            tag = f"crosspair:{kind}:{asset}:{outcome}"
        fills = [f for f in bot.venue.fills.get(slug, []) if f.tag == tag]
        return sorted(fills, key=lambda f: f.ts_ms)

    def _filled_shares(self, start: int, asset: str, outcome: str, kind: str,
                       *, entry_kind: str | None = None,
                       pair_id: str | None = None) -> tuple[float, float]:
        fills = self._fills(start, asset, outcome, kind, entry_kind=entry_kind,
                            pair_id=pair_id)
        qty = sum(f.size for f in fills)
        if not fills:
            return 0.0, 0.0
        cost = sum(f.price * f.size for f in fills)
        return qty, cost / qty

    def _open_for_tag(self, asset: str, tag: str) -> list:
        bot = self.bots.get(asset)
        if bot is None:
            return []
        return [o for o in bot.venue.open_orders() if o.tag == tag]

    def _active_tag(self, asset: str, tag: str) -> bool:
        return bool(self._open_for_tag(asset, tag))

    @staticmethod
    def _maker_price(quote: Quote) -> float | None:
        """Post-only bid: join the best bid without crossing the ask."""
        if quote.bid is None or quote.ask is None:
            return None
        tick = float(getattr(quote.market, "tick", 0.01) or 0.01)
        px = round(min(quote.bid, quote.ask - tick), 4)
        return px if px > EPS else None

    def _submit(self, quote: Quote, shares: float, tag: str, why: str, now_ms: float,
                kind: str = "taker", price: float | None = None) -> bool:
        bankroll = next(iter(self.bots.values())).paper_bankroll if self.bots else None
        if bankroll is None:
            return False
        px = quote.limit if price is None else price
        if kind == "maker":
            cost = px * shares
        else:
            cost = (px + self._fee_per_share(quote.market, px)) * shares
        if cost > bankroll.cash - bankroll.reserved() + EPS:
            return False
        if px * shares < 1.0 - EPS:
            return False
        order = quote.bot.venue.submit(quote.market, quote.outcome, kind, px, shares,
                                       fair=None, tag=tag)
        run = quote.bot.current
        if run is not None and run.m.slug == quote.market.slug:
            quote.bot._note_decision(run, order, "crosspair", why, now_ms, None,
                                     quote.bot.vol.sigma_per_sec, None, None)
        return True

    def _entry_avg(self, pos: Position, filled: float, avg_px: float) -> float:
        if filled > EPS and avg_px > 0:
            return avg_px
        return pos.entry_ask

    @staticmethod
    def _entry_still_eligible(kind: str, ask: float, asks: dict[str, float],
                              asset: str, outcome: str, elapsed: float,
                              params: PairParams) -> bool:
        peer = peer_average(asks, asset, outcome, params.min_peers)
        if kind == "leader":
            return leader_eligible(ask, peer, elapsed, params)
        if kind == "hold":
            return hold_eligible(ask, peer, elapsed, params)
        if kind == "gap":
            return gap_eligible(ask, peer, elapsed, params)
        if kind == "deep":
            return deep_eligible(ask, peer, elapsed, params)
        if kind == "maj":
            return maj_eligible(ask, asks, asset, outcome, elapsed, params)
        if kind == "sweep":
            return sweep_eligible(ask, asks, asset, outcome, elapsed, params)
        return False

    def _open_entry(self, start: int, asset: str, outcome: str, kind: str, quote: Quote,
                    why: str, now_ms: float, *, retry_at_ms: float) -> bool:
        tag = f"crosspair:{kind}:{asset}:{outcome}"
        if self._active_tag(asset, tag):
            return False
        if not self._submit(quote, self._params().shares, tag, why, now_ms):
            return False
        pos = Position(start=start, asset=asset, outcome=outcome, kind=kind,
                       entry_ask=quote.ask, signal_ask=quote.ask)
        pos.attempts = 1
        pos.retry_at_ms = retry_at_ms
        self.positions[self._pos_key(start, asset, outcome, kind)] = pos
        return True

    def _open_dual(self, start: int, asset: str, kind: str, up: Quote, down: Quote,
                   why: str, now_ms: float, params: PairParams, *,
                   next_clip_ms: float | None = None) -> bool:
        """Submit Up and Down in the same tick; keep the slot if either lands."""
        tag_up = f"crosspair:{kind}:{asset}:Up"
        tag_dn = f"crosspair:{kind}:{asset}:Down"
        if self._active_tag(asset, tag_up) or self._active_tag(asset, tag_dn):
            return False
        ok_up = self._submit(up, params.shares, tag_up, why, now_ms)
        ok_dn = self._submit(down, params.shares, tag_dn, why, now_ms)
        if not ok_up and not ok_dn:
            return False
        cost = up.ask + down.ask
        key = self._pos_key(start, asset, CSET_OUTCOME, kind)
        pos = self.positions.get(key)
        if pos is None:
            pos = Position(start=start, asset=asset, outcome=CSET_OUTCOME, kind=kind,
                           entry_ask=cost, signal_ask=cost,
                           up_signal=up.ask, down_signal=down.ask)
            self.positions[key] = pos
        else:
            pos.entry_ask = cost
            pos.signal_ask = cost
            pos.up_signal = up.ask
            pos.down_signal = down.ask
        pos.attempts += 1
        pos.retry_at_ms = now_ms + (next_clip_ms if next_clip_ms is not None else params.retry_ms)
        return True

    def _open_cset(self, start: int, asset: str, up: Quote, down: Quote,
                   why: str, now_ms: float, params: PairParams) -> bool:
        """Submit both legs; keep the slot if at least one order is accepted."""
        if not self._open_dual(start, asset, "cset", up, down, why, now_ms, params):
            return False
        cost, profit, _roi = cset_costs(up.all_in, down.all_in, params.cset_reserve)
        log.info("cset %s Up@%.3f + Down@%.3f all-in %.3f lock %.3f/sh",
                 asset, up.ask, down.ask, cost, profit)
        return True

    def _open_xlead_cross(self, start: int, up_asset: str, dn_asset: str,
                          up: Quote, down: Quote, why: str, now_ms: float,
                          params: PairParams, *, shares: float) -> bool:
        """Submit Up on one market and Down on another in the same tick."""
        pair_id = self._xlead_pair_id(up_asset, dn_asset)
        tag_up = f"crosspair:xlead:{pair_id}:Up"
        tag_dn = f"crosspair:xlead:{pair_id}:Down"
        if self._active_tag(up_asset, tag_up) or self._active_tag(dn_asset, tag_dn):
            return False
        ok_up = self._submit(up, shares, tag_up, why, now_ms)
        ok_dn = self._submit(down, shares, tag_dn, why, now_ms)
        if not ok_up and not ok_dn:
            return False
        cost = up.ask + down.ask
        key = self._pos_key(start, pair_id, XLEAD_OUTCOME, "xlead")
        pos = self.positions.get(key)
        if pos is None:
            pos = Position(start=start, asset=up_asset.lower(), outcome=XLEAD_OUTCOME,
                           kind="xlead", entry_ask=cost, signal_ask=cost,
                           up_signal=up.ask, down_signal=down.ask,
                           leg2_asset=dn_asset.lower())
            self.positions[key] = pos
        else:
            pos.entry_ask = cost
            pos.signal_ask = cost
            pos.up_signal = up.ask
            pos.down_signal = down.ask
        pos.attempts += 1
        pos.retry_at_ms = now_ms + params.retry_ms
        return True

    def _tick_xlead_entries(self, start: int, quotes: dict[str, Quote],
                            now_ms: float, params: PairParams, elapsed: float) -> bool:
        """Rule 7 PRIMARY: next cascade pair in order every every_ms; wrap after 15."""
        if not params.rule7:
            return False
        if elapsed + EPS < params.xlead_from_s or elapsed >= params.xlead_until_s - EPS:
            return False
        if self._xlead_clip_start != start:
            self._xlead_clip_start = start
            self._xlead_next_ms = 0.0
            self._xlead_idx = 0
            self._xlead_cycle = 1
        if now_ms + EPS < self._xlead_next_ms:
            return False
        n = len(XLEAD_CASCADE)
        if n <= 0:
            return False
        # One step every every_ms through the fixed order.
        self._xlead_next_ms = now_ms + max(1, int(params.xlead_every_ms))
        idx = self._xlead_idx % n
        asset_a, asset_b = XLEAD_CASCADE[idx]
        step = idx + 1
        cycle = self._xlead_cycle
        per_pair = max(float(params.xlead_max_tokens), 2.0 * XLEAD_MIN_LEG)
        leg = max(XLEAD_MIN_LEG, float(int(per_pair / 2.0)))
        dirty = False
        best = xlead_resolve_pair(quotes, asset_a, asset_b, params.xlead_max_sum)
        if best is None:
            log.info("xlead skip %s×%s step %d/%d cycle %d (sum>$%.2f or no quote); "
                     "next in %dms",
                     asset_a, asset_b, step, n, cycle, params.xlead_max_sum,
                     params.xlead_every_ms)
        else:
            up_asset, dn_asset, up, down, pair_ask = best
            why = (f"xlead: {asset_a}×{asset_b} → {up_asset} Up ${up.ask:.2f} + "
                   f"{dn_asset} Down ${down.ask:.2f} = ${pair_ask:.3f}; "
                   f"{leg:g}+{leg:g} sh; step {step}/{n} cycle {cycle}; "
                   f"next in {params.xlead_every_ms}ms; +${params.limit_offset:.2f} limit")
            if self._open_xlead_cross(start, up_asset, dn_asset, up, down, why, now_ms,
                                      params, shares=leg):
                dirty = True
                log.info("xlead %s Up@%.3f + %s Down@%.3f sum %.3f (%g+%g sh; "
                         "step %d/%d cycle %d)",
                         up_asset, up.ask, dn_asset, down.ask, pair_ask, leg, leg,
                         step, n, cycle)
            else:
                log.info("xlead miss %s×%s step %d/%d cycle %d (busy/cash); next in %dms",
                         asset_a, asset_b, step, n, cycle, params.xlead_every_ms)
        nxt = (idx + 1) % n
        self._xlead_idx = nxt
        if nxt == 0:
            log.info("xlead cascade cycle %d complete (%d pairs); starting cycle %d",
                     cycle, n, cycle + 1)
            self._xlead_cycle = cycle + 1
        return dirty

    def _asset_directional_held(self, start: int, asset: str) -> tuple[float, float]:
        """Non-cset/xlead Up/Down filled shares for secondary balancing."""
        up = down = 0.0
        for kind in ENTRY_KINDS:
            if kind in ("cset", "xlead"):
                continue
            u, _ = self._filled_shares(start, asset, "Up", kind)
            d, _ = self._filled_shares(start, asset, "Down", kind)
            up += u
            down += d
        return up, down

    def _tick_cset_entries(self, start: int, quotes: dict[str, Quote],
                           now_ms: float, params: PairParams, elapsed: float) -> bool:
        """SECONDARY: same-market complete-set / balance — never before cross-asset."""
        dirty = False
        assets = sorted({node.split(":", 1)[0] for node in quotes})
        for asset in assets:
            if self._has_kind(start, asset, CSET_OUTCOME, "cset"):
                continue
            up = quotes.get(_node(asset, "Up"))
            down = quotes.get(_node(asset, "Down"))
            if up is None or down is None:
                continue
            if not cset_eligible(up.all_in, down.all_in, elapsed, params):
                continue
            held_up, held_dn = self._asset_directional_held(start, asset)
            mode = "balance" if (held_up > EPS or held_dn > EPS) else "cset"
            cost, profit, roi = cset_costs(up.all_in, down.all_in, params.cset_reserve)
            why = (f"{mode}: {asset} Up ${up.ask:.2f} + Down ${down.ask:.2f} "
                   f"all-in ${cost:.3f}; lock ${profit:.3f}/sh ({roi:.1%} ROI); "
                   f"secondary to cross-asset; +${params.limit_offset:.2f} limit")
            if self._open_cset(start, asset, up, down, why, now_ms, params):
                dirty = True
        return dirty

    def _tick_dual_retries(self, start: int, pos: Position, quotes: dict[str, Quote],
                           now_ms: float, params: PairParams, elapsed: float) -> bool:
        """Fill missing Up/Down legs for cset (same market) or xlead (cross market)."""
        dirty = False
        kind = pos.kind
        if kind == "xlead" and pos.outcome == XLEAD_OUTCOME and pos.leg2_asset:
            up_asset, dn_asset = pos.asset, pos.leg2_asset
            pair_id = self._xlead_pair_id(up_asset, dn_asset)
            up_q = quotes.get(_node(up_asset, "Up"))
            dn_q = quotes.get(_node(dn_asset, "Down"))
            up_f, up_avg = self._filled_shares(start, up_asset, "Up", kind, pair_id=pair_id)
            dn_f, dn_avg = self._filled_shares(start, dn_asset, "Down", kind, pair_id=pair_id)
            tag_up = f"crosspair:xlead:{pair_id}:Up"
            tag_dn = f"crosspair:xlead:{pair_id}:Down"
            asset_up, asset_dn = up_asset, dn_asset
        else:
            pair_id = None
            up_asset = dn_asset = pos.asset
            up_q = quotes.get(_node(pos.asset, "Up"))
            dn_q = quotes.get(_node(pos.asset, "Down"))
            up_f, up_avg = self._filled_shares(start, pos.asset, "Up", kind)
            dn_f, dn_avg = self._filled_shares(start, pos.asset, "Down", kind)
            tag_up = f"crosspair:{kind}:{pos.asset}:Up"
            tag_dn = f"crosspair:{kind}:{pos.asset}:Down"
            asset_up = asset_dn = pos.asset
        target = max(1, pos.attempts) * params.shares
        if up_f > EPS and dn_f > EPS and pos.filled_at_ms <= EPS:
            fills = (self._fills(start, up_asset, "Up", kind, pair_id=pair_id)
                     + self._fills(start, dn_asset, "Down", kind, pair_id=pair_id))
            if fills:
                pos.filled_at_ms = float(min(f.ts_ms for f in fills))
                dirty = True
        if now_ms < pos.retry_at_ms:
            return dirty
        up_need = min(params.shares, max(0.0, target - up_f))
        dn_need = min(params.shares, max(0.0, target - dn_f))
        both_short = up_need > EPS and dn_need > EPS
        for outcome, quote, need, other_avg, tag, leg_asset in (
            ("Up", up_q, up_need, dn_avg, tag_up, asset_up),
            ("Down", dn_q, dn_need, up_avg, tag_dn, asset_dn),
        ):
            if need <= EPS or quote is None:
                continue
            if self._active_tag(leg_asset, tag):
                continue
            if kind == "cset":
                if both_short:
                    if up_q is None or dn_q is None:
                        continue
                    if not cset_eligible(up_q.all_in, dn_q.all_in, elapsed, params):
                        continue
                else:
                    if other_avg <= EPS:
                        continue
                    est = other_avg + quote.all_in + params.cset_reserve
                    if est > 1.0 + params.cset_max_loss + EPS:
                        continue
            elif kind == "xlead":
                other_q = dn_q if outcome == "Up" else up_q
                if other_q is None:
                    continue
                if quote.ask + other_q.ask > params.xlead_max_sum + EPS and both_short:
                    continue
            else:
                continue
            why = (f"{kind} retry {outcome}: {leg_asset} {need:g} sh @ ${quote.ask:.2f}")
            if self._submit(quote, need, tag, why, now_ms):
                pos.retry_at_ms = now_ms + params.retry_ms
                dirty = True
                log.info("%s %s %s retry @ %.3f (need %.1f)", kind, leg_asset, outcome,
                         quote.ask, need)
        return dirty

    def _tick_entries(self, start: int, quotes: dict[str, Quote], asks: dict[str, float],
                      now_ms: float) -> bool:
        params = self._params()
        elapsed = now_ms / 1000.0 - start
        dirty = False
        # PRIMARY: cascade pairs in fixed order, one every 7s; next cycle after 15
        # (Rule 7), then 1–6, then SECONDARY same-market complete-set (never first).
        dirty |= self._tick_xlead_entries(start, quotes, now_ms, params, elapsed)
        # Each other rule is independent: open its own slot when eligible.
        for node, quote in sorted(quotes.items()):
            asset, outcome = node.split(":", 1)
            peer = peer_average(asks, asset, outcome, params.min_peers)
            candidates: list[tuple[str, str, float]] = []

            if (not self._has_kind(start, asset, outcome, "hold")
                    and hold_eligible(quote.ask, peer, elapsed, params)):
                why = (f"hold: {asset} {outcome} ask ${quote.ask:.2f}; "
                       f"peer avg ${peer:.3f}; +${params.limit_offset:.2f} limit")
                candidates.append(("hold", why, now_ms + params.retry_ms))

            if (not self._has_kind(start, asset, outcome, "maj")
                    and maj_eligible(quote.ask, asks, asset, outcome, elapsed, params)):
                n_high = high_peer_count(asks, asset, outcome, params.maj_high)
                why = (f"maj: {asset} {outcome} ask ${quote.ask:.2f} < ${params.maj_max:.2f}; "
                       f"{n_high} peers > ${params.maj_high:.2f}; "
                       f"+${params.limit_offset:.2f} limit")
                candidates.append(("maj", why, now_ms + params.retry_ms))

            if (not self._has_kind(start, asset, outcome, "sweep")
                    and sweep_eligible(quote.ask, asks, asset, outcome, elapsed, params)):
                n_high = high_peer_count(asks, asset, outcome, params.sweep_peer)
                why = (f"sweep: {asset} {outcome} ask ${quote.ask:.2f} in "
                       f"${params.sweep_min:.2f}-${params.sweep_max:.2f}; "
                       f"{n_high} peers > ${params.sweep_peer:.2f}; "
                       f"+${params.limit_offset:.2f} limit; every {params.sweep_every_ms}ms")
                candidates.append(("sweep", why, now_ms + params.sweep_every_ms))

            if (not self._has_kind(start, asset, outcome, "deep")
                    and deep_eligible(quote.ask, peer, elapsed, params)):
                why = (f"deep: {asset} {outcome} ask ${quote.ask:.2f} is "
                       f"${peer - quote.ask:.2f} under peer avg ${peer:.3f} "
                       f"(need ≥ ${params.deep:.2f}); +${params.limit_offset:.2f} limit")
                candidates.append(("deep", why, now_ms + params.retry_ms))

            if (not self._has_kind(start, asset, outcome, "gap")
                    and gap_eligible(quote.ask, peer, elapsed, params)):
                why = (f"gap: {asset} {outcome} ask ${quote.ask:.2f} is "
                       f"${peer - quote.ask:.2f} under peer avg ${peer:.3f} "
                       f"(need ≥ ${params.gap:.2f}); +${params.limit_offset:.2f} limit")
                candidates.append(("gap", why, now_ms + params.retry_ms))

            if (not self._has_kind(start, asset, outcome, "leader")
                    and leader_eligible(quote.ask, peer, elapsed, params)):
                why = (f"leader: {asset} {outcome} ask ${quote.ask:.2f}; "
                       f"peer avg ${peer:.3f}; +${params.limit_offset:.2f} limit")
                candidates.append(("leader", why, now_ms + params.retry_ms))

            for kind, why, retry_at in candidates:
                if not self._open_entry(start, asset, outcome, kind, quote, why, now_ms,
                                        retry_at_ms=retry_at):
                    continue
                dirty = True
                if kind == "maj":
                    n_high = high_peer_count(asks, asset, outcome, params.maj_high)
                    log.info("%s %s %s @ %.3f (%d peers > %.2f)",
                             kind, asset, outcome, quote.ask, n_high, params.maj_high)
                elif kind == "sweep":
                    n_high = high_peer_count(asks, asset, outcome, params.sweep_peer)
                    log.info("sweep %s %s @ %.3f (%d peers > %.2f; every %dms)",
                             asset, outcome, quote.ask, n_high, params.sweep_peer,
                             params.sweep_every_ms)
                else:
                    log.info("%s %s %s @ %.3f (peer avg %.3f)",
                             kind, asset, outcome, quote.ask, peer)
        # SECONDARY complete-set / balance — after all cross-asset rules.
        dirty |= self._tick_cset_entries(start, quotes, now_ms, params, elapsed)
        return dirty

    def _tick_retries_and_pairs(self, start: int, quotes: dict[str, Quote],
                                asks: dict[str, float], now_ms: float) -> bool:
        params = self._params()
        dirty = False
        for key, pos in list(self.positions.items()):
            if pos.start != start:
                continue
            if pos.kind in ("cset", "xlead"):
                dirty |= self._tick_dual_retries(start, pos, quotes, now_ms, params,
                                                 now_ms / 1000.0 - start)
                continue
            filled, avg_px = self._filled_shares(start, pos.asset, pos.outcome, pos.kind)
            # Fill average drives the +rise pair trigger; signal_ask stays the first quote.
            if filled > EPS and avg_px > 0 and abs(avg_px - pos.entry_ask) > EPS:
                pos.entry_ask = avg_px
                dirty = True
            if filled > EPS and pos.filled_at_ms <= EPS:
                fills = self._fills(start, pos.asset, pos.outcome, pos.kind)
                if fills:
                    pos.filled_at_ms = float(fills[0].ts_ms)
                    dirty = True
            entry = pos.entry_ask if filled <= EPS else (avg_px or pos.entry_ask)
            signal = pos.signal_ask or pos.entry_ask

            tag = f"crosspair:{pos.kind}:{pos.asset}:{pos.outcome}"
            node = _node(pos.asset, pos.outcome)
            quote = quotes.get(node)

            # Once the initial clip is filled, all six entry rules can add a
            # full clip on Rule 6's cadence while their own signal stays valid.
            if filled + EPS >= params.shares:
                elapsed = now_ms / 1000.0 - start
                repeat_ok = (quote is not None and self._entry_still_eligible(
                    pos.kind, quote.ask, asks, pos.asset, pos.outcome, elapsed, params))
                if (repeat_ok and pos.kind in PAIR_KINDS
                        and chased_away(quote.ask, signal, params.chase)):
                    repeat_ok = False
                interval_ms = {
                    "leader": params.leader_every_ms,
                    "hold": params.hold_every_ms,
                    "gap": params.gap_every_ms,
                    "deep": params.deep_every_ms,
                    "maj": params.maj_every_ms,
                    "sweep": params.sweep_every_ms,
                }.get(pos.kind, params.sweep_every_ms)
                repeat_at = max(pos.retry_at_ms, pos.filled_at_ms + interval_ms)
                if repeat_ok and now_ms >= repeat_at and not self._active_tag(pos.asset, tag):
                    why = (f"{pos.kind} every {interval_ms}ms: "
                           f"{pos.asset} {pos.outcome} ask ${quote.ask:.2f}; "
                           f"{params.shares:g} sh")
                    if self._submit(quote, params.shares, tag, why, now_ms):
                        pos.attempts += 1
                        pos.retry_at_ms = now_ms + interval_ms
                        dirty = True
                        log.info("%s %s %s @ %.3f again (clip %d)",
                                 pos.kind, pos.asset, pos.outcome, quote.ask, pos.attempts)
            need = max(0.0, params.shares - filled)
            if need > EPS and quote is not None:
                if (pos.kind in PAIR_KINDS and chased_away(quote.ask, signal, params.chase)):
                    # Rules 1 / 3 / 4: price ran up from the signal — do not chase.
                    if pos.attempts < params.retry_attempts:
                        pos.attempts = params.retry_attempts
                        dirty = True
                        log.info("%s %s %s chase stop: ask %.3f >= signal %.3f + %.2f",
                                 pos.kind, pos.asset, pos.outcome, quote.ask, signal, params.chase)
                    continue
                # After KILLED/miss: keep buying while ask is still ≤ the making-order ask.
                budget_left = pos.attempts < params.retry_attempts
                rebuy = (not budget_left) and still_cheap(quote.ask, signal)
                if not budget_left and not rebuy:
                    continue
                if now_ms >= pos.retry_at_ms and not self._active_tag(pos.asset, tag):
                    if rebuy:
                        why = (f"{pos.kind} rebuy after miss: {pos.asset} {pos.outcome} "
                               f"ask ${quote.ask:.2f} <= signal ${signal:.2f}; {need:g} sh")
                    else:
                        why = (f"{pos.kind} retry {pos.attempts + 1}/{params.retry_attempts}: "
                               f"{pos.asset} {pos.outcome} {need:g} sh")
                    if self._submit(quote, need, tag, why, now_ms):
                        pos.attempts = 1 if rebuy else pos.attempts + 1
                        pos.retry_at_ms = now_ms + params.retry_ms
                        dirty = True
                        if rebuy:
                            log.info("%s %s %s rebuy after miss: ask %.3f <= signal %.3f",
                                     pos.kind, pos.asset, pos.outcome, quote.ask, signal)

            if pos.kind not in PAIR_KINDS or filled + EPS < params.shares:
                continue
            rise = pair_rise_for(pos.kind, params, filled_at_ms=pos.filled_at_ms, now_ms=now_ms)
            pair_outcome = OTH[pos.outcome]
            pair_tag = self._pair_tag(pos.kind, pos.asset, pair_outcome)
            pair_filled, _ = self._filled_shares(start, pos.asset, pair_outcome, "pair",
                                                 entry_kind=pos.kind)
            # Pair the full accumulated entry size, including interval repeats.
            pair_need = filled - pair_filled
            if pair_need <= EPS:
                # Pair complete: pull any leftover resting maker.
                bot = self.bots.get(pos.asset)
                if bot is not None:
                    for o in self._open_for_tag(pos.asset, pair_tag):
                        bot.venue.cancel(o.id)
                continue
            lead_quote = quotes.get(node)
            pair_quote = quotes.get(_node(pos.asset, pair_outcome))
            if lead_quote is None or pair_quote is None:
                continue
            if not pair_ready(lead_quote.ask, entry, rise):
                continue
            maker_px = self._maker_price(pair_quote)
            if maker_px is None:
                continue
            open_pair = self._open_for_tag(pos.asset, pair_tag)
            if open_pair:
                # Keep a single resting maker; requote when the book moves.
                live = open_pair[0]
                if abs(live.price - maker_px) > EPS or abs(live.size - live.filled - pair_need) > 0.01:
                    bot = self.bots.get(pos.asset)
                    if bot is not None:
                        for o in open_pair:
                            bot.venue.cancel(o.id)
                continue
            if now_ms < pos.pair_retry_at_ms:
                continue
            wait_note = ""
            if (pos.kind in ("leader", "gap") and abs(rise - params.pair_fallback_rise) < EPS
                    and params.pair_wait_s > EPS):
                wait_note = f" (fallback after {params.pair_wait_s:.0f}s)"
            why = (f"pair maker after +{rise:.2f}{wait_note}: {pos.asset} {pos.outcome} "
                   f"{lead_quote.ask:.2f} from {entry:.2f} -> bid {pair_need:g} {pair_outcome} @ ${maker_px:.2f}")
            if self._submit(pair_quote, pair_need, pair_tag, why, now_ms, kind="maker", price=maker_px):
                pos.pair_attempts += 1
                pos.pair_retry_at_ms = now_ms + params.retry_ms
                dirty = True
                log.info("pair maker %s %s @ %.3f after %s rose to %.3f (entry %.3f, rise +%.2f%s)",
                         pos.asset, pair_outcome, maker_px, pos.outcome, lead_quote.ask, entry,
                         rise, wait_note)
        return dirty

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            now_ms = time.time() * 1000.0
            start, quotes, asks = self._quotes(now_ms)
            dirty = False
            if start is not None and quotes:
                try:
                    dirty |= self._tick_entries(start, quotes, asks, now_ms)
                    dirty |= self._tick_retries_and_pairs(start, quotes, asks, now_ms)
                except Exception:
                    log.exception("cross-pair tick failed")
            before = len(self.positions)
            self.positions = {k: p for k, p in self.positions.items()
                              if p.start + WINDOW_S >= time.time() - 120.0}
            if dirty or len(self.positions) != before:
                self._save()
            try:
                await asyncio.wait_for(stop.wait(), 0.05)
            except asyncio.TimeoutError:
                pass
