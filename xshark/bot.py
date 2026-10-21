"""xshark paper bot: x-MoneyForWhiskas's BTC 5-minute playbook on live books.

One process, one asyncio loop:

    spot feeds (Coinbase, Binance, Bybit)  -> composite price, vol, TWAP state
    Chainlink (Polymarket RTDS)            -> strike (the 60 s TWAP at the open)
    CLOB market channel                    -> books + trade prints -> paper venue
    discovery (Gamma, every 5 s)           -> current and next window's market
    trade loop (event-driven, <= 20 Hz)    -> fair value -> strategy.decide -> venue
    settle loop                            -> the venue's official result -> ledger
    state loop (2 Hz)                      -> data/bot/state.json for the console

It never signs or sends an order: fills are simulated (paper.py).

Files (all under --data-dir, default data/bot):
    state.json        live snapshot the console polls
    windows.jsonl     one row per settled window
    fills.jsonl       one row per fill, with markouts and the result
    windows/<slug>.json  the window's price path + fills (kept 7 days)
    open.json         positions of unsettled windows, so a restart still settles them
    controls.json     console overrides (re-read every 2 s)
    KILL              present = halt: bids come off, nothing new is sent
"""
from __future__ import annotations

import asyncio
import bisect
import contextlib
import contextvars
import json
import logging
import math
import os
import shutil
import signal
import time
from collections import Counter, deque
from dataclasses import asdict

import orjson

from . import config
from .copytrade import TAG as COPY_TAG, fetch_market_fills, unseen_fills
from .book import BookFeed, Trade
from .calibration import Calibration
from .chainlink import TOPIC_PRICE, ChainlinkFeed
from .market import (WINDOW_S, FeeSchedule, FetchFailed, Market, fetch_market, fetch_outcome,
                     window_start)
from .paper import Fill, PaperVenue
from .spot import CompositeSpot, FeedStatus, run_all
from .strategy import OUTCOMES, Resting, View, decide
from .ladder import LadderParams
from .triangle import TriParams, pair_goal
from .v2 import V2Params
from .v3 import V3Params, summary as v3_summary
from .v4 import V4Params
from .v5 import V5Params
from .v6 import V6Params
from .whisk import WhiskParams
from .twap import TwapState, twap_fair_value
from .vol import EwmaVol

#: the coin of the bot whose task is running: log lines carry it when one process runs several
ASSET_CTX: contextvars.ContextVar[str] = contextvars.ContextVar("xshark_asset", default="")

log = logging.getLogger("xshark")

#: the console's calendar: GMT+9, no DST (the user reads the dashboards there)
TZ_SHIFT_S = 9 * 3600
HIST_EVERY_MS = 250.0          # a chart point at least this often (the coin's price keeps moving)
HIST_MIN_MS = 50.0             # ... and on every change of the Up best bid/ask, at most this often (user, 2026-10-12)
STATE_EVERY_S = 0.25           # state.json for the console (it polls every 250 ms)
PRE_OPEN_S = 60.0          # a window's chart starts a minute before its open (6 minutes in all)
SELL_SLIP = 0.02                 # a console sell's limit: this far under the best bid, so it fills at once
SELL_REQUEST_MAX_AGE_S = 15.0
ACCOUNT_SYNC_MIN = 0.02          # live: an account vs ledger gap at least this big is booked as money from outside
#: the decision runs at least this often, and right after a spot move (but no
#: closer than SPOT_EVAL_GAP_MS). Book messages (~800/s near the close) update
#: the books and the paper fills directly and do not wake the loop.
EVAL_EVERY_MS = 50.0
SPOT_EVAL_GAP_MS = 5.0          # was 20 ms; a spot move is acted on almost at once
SETTLE_AFTER_S = 40.0
SETTLE_FALLBACK_S = 600.0
ARCHIVE_DAYS = 7


def day_key(ts_s: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts_s + TZ_SHIFT_S))


def _r(v, n=4):
    return None if v is None else round(v, n)


def market_from_dict(d: dict | None) -> Market | None:
    if not d:
        return None
    try:
        fees = FeeSchedule(**d.get("fees", {})) if isinstance(d.get("fees"), dict) else FeeSchedule()
        return Market(**{**d, "fees": fees})
    except TypeError:
        return None


class WindowRun:
    """Per-window bookkeeping around the venue's position."""

    def __init__(self, m: Market) -> None:
        self.m = m
        self.strike: float | None = None
        self.strike_src = ""
        self.hist: list[list] = []
        self.fills: list[dict] = []
        self.taker_count = 0
        self.last_taker_ms: dict[str, float] = {}
        self.last_quote_ms: dict[str, float] = {}
        self.why: dict[str, Counter] = {"maker_Up": Counter(), "maker_Down": Counter(), "taker": Counter()}
        self.closed = False
        self.fav_side: str | None = None
        self.decisions: list[dict] = []           # every order sent, with the reason (price chart markers)
        self.scalp: dict | None = None            # the open scalp cycle (see strategy._scalp_*)
        self.scalp_closed_ms = -1e18
        self.scalp_pairs = 0
        self.copy_seen_counts: dict[tuple, int] = {}
        self.copy_queue: list[dict] = []
        self.copy_fill: dict | None = None
        self.copy_done = False
        self.copy_attempts = 0
        self.copy_last_attempt_ms = 0.0
        self.copy_maker_price: float | None = None
        self.copy_order_floor = 0
        self.manual_sold = False                  # sold from the console: no more orders this window
        self.tri: dict = {}                       # the triangle cycle (see xshark/triangle.py)
        self.ladder: dict = {}                    # the pair ladder's rounds (see xshark/ladder.py)
        self.whisk: dict = {}                     # the Whiskas bot's legs (see xshark/whisk.py)
        self.seen_ms = time.time() * 1000.0


class Bot:
    def __init__(self, args) -> None:
        self.args = args
        self.asset = str(getattr(args, "asset", "btc") or "btc").upper()
        ASSET_CTX.set(self.asset)
        self.dir = args.data_dir
        self.paper_bankroll = getattr(args, "paper_bankroll", None) if not getattr(args, "live", False) else None
        self.reset_balance: float | None = None   # a console "restart with new cash" (paper)
        if not getattr(args, "live", False) and self.paper_bankroll is None:
            # the cash of the last console restart (balance.json) stands for --balance until the next one
            with contextlib.suppress(OSError, ValueError, TypeError, KeyError):
                with open(os.path.join(self.dir, "balance.json")) as fh:
                    args.balance = float(json.load(fh)["balance"])
        os.makedirs(os.path.join(self.dir, "windows"), exist_ok=True)
        self.s = config.Settings()
        self.defaults = config.defaults()
        self.clock = lambda: time.time() * 1000.0
        self.spot = CompositeSpot(lead_window_ms=args.lead_window_ms)
        self.spot_health: dict[str, FeedStatus] = {}
        self.vol = EwmaVol()
        self.twap = TwapState()
        self.chain = ChainlinkFeed(assets={self.asset})
        self.books = BookFeed(self.clock, on_book=self._on_book, on_trade=self._on_trade)
        self.cal = Calibration(os.path.join(self.dir, "fill_calibration.json"))
        self.live = bool(getattr(args, "live", False))
        if self.live:
            from .live import LiveCaps, LiveVenue, armed_ok
            armed = bool(getattr(args, "armed", False))
            if armed and not armed_ok():
                raise SystemExit("--armed needs TPB_LIVE_ACK=I_UNDERSTAND_REAL_MONEY in the environment")
            caps = LiveCaps(max_order_usdc=args.max_order_usdc, max_open_usdc=args.max_open_usdc,
                            max_daily_loss_usdc=args.max_daily_loss_usdc,
                            max_orders_per_hour=args.max_orders_per_hour)
            self.venue = LiveVenue(self.books, self.clock, self.s, cash=args.balance, on_fill=self._on_fill,
                                   calibration=self.cal, armed=armed, caps=caps, kill_path=self._path("KILL"),
                                   day_of=day_key)
        else:
            self.venue = PaperVenue(self.books, self.clock, self.s, cash=args.balance, on_fill=self._on_fill,
                                    calibration=self.cal, bankroll=self.paper_bankroll, asset=self.asset)
        self.markets: dict[int, Market] = {}
        self.runs: dict[str, WindowRun] = {}
        self.pending: dict[str, int] = {}              # slug -> close (s), awaiting the result
        self.pre_hist: dict[int, list] = {}            # window start -> chart rows of its minute before the open
        self.skip: tuple[int, float] | None = None     # (window start, last window's loss): that window trades nothing
        self.current: WindowRun | None = None
        self._last_copy_error_ms = 0.0
        self.spot_raw: float | None = None
        self.spot_px: float | None = None              # composite on Chainlink's level
        self.spot_ms = 0.0
        self.price_source = getattr(args, "price_source", "composite")
        self.spot_hist: deque = deque(maxlen=6000)     # (ms, the price the model trades on)
        self.comp_hist: deque = deque(maxlen=6000)     # (ms, raw composite), for the Chainlink basis
        self.model_hist: deque = deque(maxlen=400)     # (ms, mid_up, p_model)
        self.recent_fills: deque = deque(maxlen=60)
        self.sell_log: deque = deque(maxlen=20)        # sells requested from the console
        self.target_hit: tuple[float, float] | None = None   # (target, equity when it was reached)
        self._last_sell_check = 0.0
        self.stats = self._load_stats()
        self.ctrl = {"mtime": None, "applied_at": None, "applied": [], "notes": [], "stored": {}}
        self.tri = TriParams()                    # the triangle strategy, from the tri_* settings
        self.ladder = LadderParams()              # the pair ladder, from the pl_* settings
        self.whisk = WhiskParams()                # the Whiskas bot, from the wk_* settings
        self.v2 = V2Params()                      # Strategy V2, from the v2_* settings
        self.v3 = V3Params()                      # Strategy V3, from the v3_* settings
        self.v4 = V4Params()                      # Strategy V4, from the v4_* settings
        self.v5 = V5Params()                      # Strategy V5, from the v5_* settings
        self.v6 = V6Params()                      # Strategy V6, from the v6_* settings
        self.model: dict = {}
        self.why: dict = {}
        self.halted: str | None = None
        self.started = time.time()
        self._last_twap_ms = 0.0
        self._last_eval = 0.0
        self._last_hist = 0.0
        self._last_top = None                          # the Up best bid/ask of the last chart point
        self._last_why = 0.0
        self._open_dirty = False
        self.stop: asyncio.Event | None = None
        self._dirty: asyncio.Event | None = None
        # restore cash and the windows that were open at the last shutdown
        self.outside = self._load_outside()
        if self.paper_bankroll is None:
            self.venue.cash = args.balance + self.outside + self.stats["pnl"]
        try:
            with open(self._path("open.json"), "rb") as f:
                saved_open = orjson.loads(f.read())
                if self.paper_bankroll is None:
                    restored = self.venue.import_open(saved_open, market_from_dict)
                else:
                    charge_cash = self.paper_bankroll.charge_restored_positions(self.asset)
                    restored = self.venue.import_open(saved_open, market_from_dict, charge_cash=charge_cash)
        except (OSError, ValueError):
            restored = []
        for slug in restored:
            m = self.venue.markets[slug]
            run = self.runs[slug] = WindowRun(m)
            run.closed = True
            run.fills = [self._fill_view(f) for f in self.venue.fills.get(slug, [])]
            self.pending[slug] = m.end
        if restored:
            log.info("restored %d unsettled window(s): %s", len(restored), ", ".join(restored))
        self._restore_runs()

    # ------------------------------------------------------------- helpers

    def _path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def _load_outside(self) -> float:
        """Money moved outside the bot (deposits, withdrawals, trades by hand), from outside.jsonl."""
        tot = 0.0
        try:
            with open(self._path("outside.jsonl"), "rb") as f:
                for line in f:
                    with contextlib.suppress(ValueError, KeyError, TypeError):
                        tot += float(orjson.loads(line)["amount"])
        except OSError:
            pass
        return tot

    def _sync_account(self) -> None:
        """Live: book what the account holds beyond (or short of) the ledger as money moved
        outside the bot, so the console matches Polymarket. The bot's own PnL is left alone."""
        gap = self.venue.account_gap()
        if gap is None or abs(gap) < ACCOUNT_SYNC_MIN:
            return
        before = self.venue.cash
        self.venue.cash += gap
        self.outside += gap
        row = {"ts": round(time.time(), 3), "amount": round(gap, 6), "account": round(self.venue.venue_balance, 6),
               "bot_cash_before": round(before, 6), "unredeemed": round(sum(self.venue.unredeemed.values()), 6)}
        with open(self._path("outside.jsonl"), "ab") as f:
            f.write(orjson.dumps(row) + b"\n")
        log.warning("ACCOUNT SYNC: the account holds $%.2f, the bot expected $%.2f -> %+.2f booked as money "
                    "moved outside the bot (deposits, withdrawals, trades by hand)",
                    self.venue.venue_balance, self.venue.venue_balance - gap, gap)

    def _loss_reset_ts(self) -> float:
        """When the daily loss count was last reset from the console (data/bot/loss_reset)."""
        try:
            with open(self._path("loss_reset")) as f:
                return float(f.read().strip() or 0)
        except (OSError, ValueError):
            return 0.0

    def _load_stats(self) -> dict:
        st = {"windows": 0, "wins": 0, "losses": 0, "pnl": 0.0, "maker_pnl": 0.0, "taker_pnl": 0.0,
              "cost": 0.0, "fees": 0.0, "rebate_est": 0.0, "traded": 0, "today": 0.0, "today_key": day_key(time.time()),
              "today_windows": 0, "loss_reset_at": self._loss_reset_ts(),
              "rule1_pnl": 0.0, "rule2_pnl": 0.0, "rule3_pnl": 0.0, "rule4_pnl": 0.0,
              "rule5_pnl": 0.0, "rule6_pnl": 0.0, "rule7_pnl": 0.0, "cset_pnl": 0.0}
        try:
            with open(self._path("windows.jsonl"), "rb") as f:
                for line in f:
                    try:
                        self._fold(st, orjson.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        # Rule buckets come from fill tags (covers older windows that lack rule_pnl).
        st.update(self._rule_pnl_from_fills_file())
        return st

    def _rule_pnl_from_fills_file(self) -> dict:
        """Sum per-rule settled PnL from fills.jsonl (crosspair tags)."""
        from .cross_pair import RULE_KEYS, rule_pnl_from_fills
        by_slug: dict[str, list] = {}
        try:
            with open(self._path("fills.jsonl"), "rb") as f:
                for line in f:
                    try:
                        row = orjson.loads(line)
                    except ValueError:
                        continue
                    slug = row.get("slug")
                    if not slug or not str(row.get("tag") or "").startswith("crosspair:"):
                        continue
                    by_slug.setdefault(slug, []).append(row)
        except OSError:
            return {f"{k}_pnl": 0.0 for k in RULE_KEYS}
        tot = {k: 0.0 for k in RULE_KEYS}
        for fills in by_slug.values():
            for k, v in rule_pnl_from_fills(fills, winner=None).items():
                tot[k] += v
        return {f"{k}_pnl": round(v, 6) for k, v in tot.items()}

    @staticmethod
    def _fold(st: dict, w: dict) -> None:
        st["windows"] += 1
        if w.get("fills"):
            st["traded"] += 1
            st["wins" if w["pnl"] > 0 else "losses"] += 1 if abs(w["pnl"]) > 1e-9 else 0
        for k in ("pnl", "maker_pnl", "taker_pnl", "cost", "fees", "rebate_est"):
            st[k] += w.get(k) or 0.0
        # rule*_pnl is rebuilt from fills.jsonl in _load_stats; still fold live settles.
        rp = w.get("rule_pnl") or {}
        for k in ("rule1", "rule2", "rule3", "rule4", "rule5", "rule6", "rule7", "cset"):
            st[f"{k}_pnl"] = st.get(f"{k}_pnl", 0.0) + (rp.get(k) or 0.0)
        if day_key(w.get("end", 0)) == st["today_key"] and (w.get("settled_at") or 0) >= st.get("loss_reset_at", 0):
            st["today"] += w.get("pnl") or 0.0
            st["today_windows"] += 1

    # ------------------------------------------------------------- feeds

    def _on_quote(self, q) -> None:
        self.spot.update(q)
        now = self.clock()
        px = self.spot.price(self.asset, now)
        if px is None:
            return
        self.comp_hist.append((now, px))
        if self.price_source != "composite":
            return                      # Chainlink drives the price; the composite only feeds the basis
        moved = px != self.spot_raw
        self.spot_raw = px
        self.spot_hist.append((now, px))
        cl = px * self.chain.basis_factor(self.asset)
        self.spot_px, self.spot_ms = cl, now
        self.vol.update(cl, now)
        if now - self._last_twap_ms >= 100.0:
            self.twap.update(cl, now)
            self._last_twap_ms = now
        if moved:
            self._dirty.set()

    def _on_source_latency(self, source: str, lag_ms: float) -> None:
        prev = self.spot.lag.get(source)
        self.spot.lag[source] = lag_ms if prev is None else prev + 0.1 * (lag_ms - prev)
        lag = self.spot.lag[source]
        if lag > 750.0:
            self.spot.lagging.add(source)
        elif lag < 400.0:
            self.spot.lagging.discard(source)

    def _spot_at(self, ts_ms: float, h=None) -> float | None:
        h = self.spot_hist if h is None else h
        if not h:
            return None
        i = bisect.bisect_right(h, (ts_ms, math.inf)) - 1
        if i < 0 or ts_ms - h[i][0] > 2000.0:
            return None
        return h[i][1]

    def _on_chain(self, topic: str, asset: str, tick) -> None:
        if asset != self.asset or topic != TOPIC_PRICE:
            return
        self.chain.update_basis(self.asset, self._spot_at(tick.ts_ms, self.comp_hist), tick)
        if self.price_source == "chainlink":
            # the settlement feed itself: one tick a second, stamped with Chainlink's
            # own time (it arrives ~1.3 s later, which the pricer counts as info lag)
            px, ts = tick.price, tick.ts_ms
            self.spot_raw = self.spot_px = px
            self.spot_ms = ts
            self.spot_hist.append((ts, px))
            self.vol.update(px, ts)
            self.twap.update(px, ts)
            self._dirty.set()

    def _on_book(self, token: str) -> None:
        self.venue.on_book(token)
        run = self.current
        if run is None or token != run.m.up_token:
            return
        now = self.clock()
        if now - self._last_hist < HIST_MIN_MS:
            return                                     # (the cheap check first: ~670 book messages a second)
        ub = self.books.book(token)
        if ub is not None and (_r(ub.best_bid()), _r(ub.best_ask())) != self._last_top:
            self._last_hist = now                      # the best bid/ask moved: a chart point now
            self._sample(now)

    def _on_trade(self, t: Trade) -> None:
        self.venue.on_trade(t)

    def _tri_sent(self, run: WindowRun, order, kind: str, now: float) -> None:
        """A triangle order went out: remember its kind (for its fills) and count it."""
        t = run.tri
        t.setdefault("kinds", {})[str(order.id)] = kind
        if kind == "open":
            if order.kind == "taker":                     # a resting open bid is requoted, not retried
                t["open_sent"] = t.get("open_sent", 0) + 1
        elif kind == "pair":
            pass
        elif kind == "btc":
            t[f"btc_ms_{order.outcome}"] = now           # its own cooldown, per token (the others' is untouched)
            t["btc_n"] = t.get("btc_n", 0) + 1
        elif kind == "last":
            t["last_buy_ms"] = now
        else:
            t["last_ms"] = now                    # recovery / lock / late share one cooldown
            if kind in ("rec", "lock"):
                t["actions"] = t.get("actions", 0) + 1
            elif kind == "late":
                t["late_n"] = t.get("late_n", 0) + 1

    def _whisk_fill(self, run: WindowRun, f: Fill, wk: str) -> None:
        """A Whiskas-bot fill: it counts for its token's buy (kind:id) if that buy is still the one running;
        a rising fill is owed to the other token (the balance matches it)."""
        kind, _, leg_id = wk.partition(":")
        t = run.whisk
        t[f"sh_{kind}"] = t.get(f"sh_{kind}", 0.0) + f.size
        leg = t.get(f"leg_{f.outcome}")
        if leg and str(leg.get("id")) == leg_id:
            t[f"leg_{f.outcome}"] = {**leg, "got": round(leg.get("got", 0.0) + f.size, 4)}
        if kind == "rise":
            other = "Down" if f.outcome == "Up" else "Up"
            t[f"owe_{other}"] = round(t.get(f"owe_{other}", 0.0) + f.size, 4)
            t[f"pair_owed_{other}"] = round(t.get(f"pair_owed_{other}", 0.0) + f.size, 4)
            t[f"pair_cost_{other}"] = round(t.get(f"pair_cost_{other}", 0.0) + f.price * f.size, 6)
            t[f"rise_fill_ms_{f.outcome}"] = f.ts_ms
        elif kind == "pair":
            owed_key, cost_key = f"pair_owed_{f.outcome}", f"pair_cost_{f.outcome}"
            owed = t.get(owed_key, 0.0)
            avg = t.get(cost_key, 0.0) / owed if owed > 1e-9 else 0.0
            paid = min(owed, f.size)
            t[owed_key] = round(max(0.0, owed - paid), 4)
            t[cost_key] = round(max(0.0, t.get(cost_key, 0.0) - avg * paid), 6)
        log.info("whiskas %s fill: %s %.2f @ %.3f", kind, f.outcome, f.size, f.price)

    def _tri_fill(self, run: WindowRun, f: Fill, kind: str) -> None:
        t = run.tri
        if kind == "open":
            if not t.get("opened"):
                t["open_ms"] = f.ts_ms                    # the pair sum rises from here (pair_sum_step_s)
            sh = t.get("open_sh", 0.0) + f.size
            t.update(opened=True, open_side=f.outcome, open_sh=sh,
                     open_px=(t.get("open_px", 0.0) * t.get("open_sh", 0.0) + f.price * f.size) / sh)
            # a taker open is done at once; an open bid as soon as it holds the venue's minimum (5 sh):
            # the rest is not chased, the pair bid pairs what filled (user, 2026-10-07)
            if f.kind != "maker" or sh >= run.m.min_size - 0.01:
                t["open_done"] = True
        elif kind == "pair":
            t["pair_sh"] = t.get("pair_sh", 0.0) + f.size
            if t["pair_sh"] >= pair_goal(self.tri, t) - 0.01 and t.get("open_done", True):
                t.update(pair="filled", pair_done_ms=f.ts_ms)    # the cycle is locked; a repeat may follow
        elif kind == "last":
            t["last_usd"] = t.get("last_usd", 0.0) + f.cost
        elif kind == "add":
            t["add_sh"] = t.get("add_sh", 0.0) + f.size
        elif kind == "btc":
            t["btc_sh"] = t.get("btc_sh", 0.0) + f.size
        elif kind == "cb":
            t[f"cb_sh_{f.outcome}"] = t.get(f"cb_sh_{f.outcome}", 0.0) + f.size
        elif kind == "rec":
            if not (t.get("rec_open") and t.get("rec_side") == f.outcome):
                t.update(recs=t.get("recs", 0) + 1, rec_open=True, rec_side=f.outcome)   # a new episode
            t.update(rec_cap=t.get("rec_cap", 0.0) + f.cost, had_rec=True)
        elif kind == "lock":
            t.update(rec_cap=t.get("rec_cap", 0.0) + f.cost, had_rec=True)
        elif kind == "late":
            t["late_usd"] = t.get("late_usd", 0.0) + f.cost
        log.info("triangle %s fill: %s %.2f sh @ %.3f (recoveries %d)", kind, f.outcome, f.size, f.price, t.get("recs", 0))

    def _scalp_fill(self, run: WindowRun, f: Fill) -> None:
        """Track the scalp cycle: the first buy opens it, a full pair or the hedge closes it."""
        c = run.scalp
        if f.tag == "scalp":
            if c is None or c["side"] != f.outcome:
                run.scalp = {"side": f.outcome, "price": f.price, "shares": f.size, "pair": 0.0}
            else:                                     # another piece of the same first buy
                tot = c["shares"] + f.size
                c["price"] = (c["price"] * c["shares"] + f.price * f.size) / tot
                c["shares"] = tot
        elif c is not None and f.tag == "scalp_pair":
            c["pair"] = c.get("pair", 0.0) + f.size
            if c["pair"] >= c["shares"] - 0.01:
                run.scalp, run.scalp_closed_ms = None, f.ts_ms
                run.scalp_pairs += 1
                log.info("scalp cycle closed: pair complete")
        elif c is not None and f.tag == "sell" and f.outcome == c["side"]:
            run.scalp, run.scalp_closed_ms = None, f.ts_ms
            log.info("scalp cycle closed: first buy sold (%.2f sh @ %.2f)", -f.size, f.price)
        elif c is not None and f.tag == "scalp_hedge":
            run.scalp, run.scalp_closed_ms = None, f.ts_ms
            log.info("scalp cycle closed: hedged %.0f %s @ %.2f", f.size, f.outcome, f.price)

    def _fill_view(self, f: Fill) -> dict:
        return {"ts": f.ts_ms, "slug": f.slug, "outcome": f.outcome, "kind": f.kind, "how": f.how,
                "price": _r(f.price), "size": _r(f.size, 2), "fee": _r(f.fee, 5), "rebate_est": _r(f.rebate_est, 5),
                "race_p": _r(f.race_p, 3), "tag": f.tag,
                "fair": _r(f.fair), "mid": _r(f.mid), "secs_left": _r(f.secs_left, 1), "order": f.order_id}

    def _v3_ledger(self, f: Fill) -> None:
        """Strategy V3's trade ledger: one JSON line after every V3 fill (data/<dir>/v3_ledger.jsonl)."""
        fills = [dict(outcome=x.outcome, price=x.price, size=x.size, fee=x.fee) for x in self.venue.fills.get(f.slug, [])]
        books = {}
        for o in OUTCOMES:
            bk = self.books.book(self.current.m.token(o)) if self.current is not None and self.current.m.slug == f.slug else None
            if bk is not None:
                t = bk.top()
                books[o] = {**t, "bids": bk.bids_downto(max(0.0, (t["bid"] or 0.0) - 0.20))}

        class _V:                                      # what v3.summary reads to mark at the bids
            pass
        vv = _V()
        vv.books = books
        vv.market = self.current.m if self.current is not None else None
        run = self.runs.get(f.slug)
        seen = (run.whisk.get("v3_seen_ask") if run is not None else None) or next(
            (c.get("seen_ask") for c in getattr(self.venue, "clips", {}).get(f.slug, []) if c.get("id") == f.order_id), None)
        row = v3_summary(fills, vv if vv.market is not None and books else None, self.v3.max_spend)
        row = {k: (_r(x, 4) if isinstance(x, float) else x) for k, x in row.items()}
        rec = {"ts": round(f.ts_ms), "slug": f.slug, "tag": f.tag, "outcome": f.outcome, "price": _r(f.price),
               "size": _r(f.size, 2), "fee": _r(f.fee, 5), "ask_when_sent": seen,
               "slippage": None if seen is None else _r(f.price - seen, 4), "realized": 0.0, **row}
        with open(self._path("v3_ledger.jsonl"), "a") as fh:
            fh.write(json.dumps(rec) + "\n")
        log.info("v3 fill %s %s %.2f @ %.3f | Up %g Down %g | if Up %+.2f if Down %+.2f | worst %+.2f",
                 f.tag, f.outcome, f.size, f.price, row["up_shares"], row["down_shares"], row["if_up_wins"],
                 row["if_down_wins"], row["max_loss"])

    def _v4_write(self, rows: list, name: str = "v4_ledger.jsonl") -> None:
        try:
            with open(self._path(name), "a") as fh:
                for r in rows:
                    fh.write(json.dumps(r, default=float) + "\n")
        except OSError:
            log.exception("v4 ledger")

    def _v4_fill(self, f: Fill) -> None:
        """V4's ledger line after a fill: expected vs actual price and the position after it."""
        run = self.runs.get(f.slug)
        fills = [dict(outcome=x.outcome, price=x.price, size=x.size, fee=x.fee) for x in self.venue.fills.get(f.slug, [])]
        row = v3_summary(fills)
        exp = run.whisk.get("v4_expect") if run is not None else None
        tgt = run.whisk.get("v4_target") if run is not None else None
        self._v4_write([{"ts": round(f.ts_ms), "slug": f.slug, "event": "fill", "tag": f.tag, "outcome": f.outcome,
                         "size": _r(f.size, 2), "price": _r(f.price), "expected": exp,
                         "slippage": None if exp is None else _r(f.price - exp, 4), "fee": _r(f.fee, 5), "target": tgt,
                         "partial": None, **{k: (_r(x, 4) if isinstance(x, float) else x) for k, x in row.items()}}])
        log.info("v4 fill %s %s %.2f @ %.3f (expected %s) | if Up %+.2f if Down %+.2f",
                 f.tag, f.outcome, f.size, f.price, exp, row["if_up_wins"], row["if_down_wins"])

    def _v5_fill(self, f: Fill, name: str = "v5") -> None:
        """V5's (or V6's) ledger line after every fill (once per fill: the venue calls this once for each)."""
        run = self.runs.get(f.slug)
        fills = [dict(outcome=x.outcome, price=x.price, size=x.size, fee=x.fee) for x in self.venue.fills.get(f.slug, [])]
        row = v3_summary(fills)
        cyc = (run.whisk.get("v5_cycle") or {}) if run is not None else {}
        self._v4_write([{"ts": round(f.ts_ms), "slug": f.slug, "strategy": name, "event": "fill", "tag": f.tag,
                         "order": f.order_id, "cycle": cyc.get("id"), "outcome": f.outcome, "size": _r(f.size, 2),
                         "price": _r(f.price), "fee": _r(f.fee, 5), "how": f.how,
                         **{k: (_r(x, 4) if isinstance(x, float) else x) for k, x in row.items()}}], f"{name}_ledger.jsonl")
        log.info("%s fill %s %s %.2f @ %.3f fee %.4f | if Up %+.2f if Down %+.2f", name,
                 f.tag, f.outcome, f.size, f.price, f.fee, row["if_up_wins"], row["if_down_wins"])

    def _on_fill(self, f: Fill) -> None:
        v = self._fill_view(f)
        if f.tag.startswith("v4"):
            self._v4_fill(f)
        if f.tag.startswith("v5"):
            self._v5_fill(f)
        if f.tag.startswith("v6"):
            self._v5_fill(f, "v6")
        if f.tag.startswith("v3"):
            try:
                self._v3_ledger(f)
            except Exception:                          # the ledger never stops trading
                log.exception("v3 ledger")
        run = self.runs.get(f.slug)
        if run is not None:
            run.fills.append(v)
        self.recent_fills.append(v)
        self._open_dirty = True
        kind = run.tri.get("kinds", {}).get(str(f.order_id)) if run is not None else None
        leg = run.ladder.get("kinds", {}).get(str(f.order_id)) if run is not None else None
        if leg and run.ladder.get("rd"):
            rd = run.ladder["rd"]                         # the round's leg: shares and average price
            got = rd.get(f"got_{leg}", 0.0)
            rd[f"px_{leg}"] = ((rd.get(f"px_{leg}") or 0.0) * got + f.price * f.size) / (got + f.size)
            rd[f"got_{leg}"] = got + f.size
        wk = run.whisk.get("kinds", {}).get(str(f.order_id)) if run is not None else None
        if wk:
            self._whisk_fill(run, f, wk)
        if kind:
            self._tri_fill(run, f, kind)
        elif run is not None and (f.tag.startswith("scalp") or f.tag == "sell"):
            self._scalp_fill(run, f)
        if run is not None and run.closed and f.slug not in self.pending:
            self.pending[f.slug] = run.m.end          # a venue fill reported after the roll still settles
        elif run is None and f.slug not in self.pending and f.slug in self.venue.markets:
            # a fill the venue check found in a window the bot thought it had not traded
            self.pending[f.slug] = self.venue.markets[f.slug].end
        log.info("fill %s %s %s %.2f @ %.3f (%s)", f.slug[-10:], f.kind, f.outcome, f.size, f.price, f.how)

    # ------------------------------------------------------------- loops

    async def discover_loop(self) -> None:
        while not self.stop.is_set():
            w = window_start(time.time())
            for start in (w, w + WINDOW_S, w + 2 * WINDOW_S):
                if start in self.markets:
                    continue
                try:
                    m = await asyncio.to_thread(fetch_market, start, self.asset.lower())
                except FetchFailed as exc:
                    log.warning("gamma: %s", exc)
                    break
                if m is not None:
                    self.markets[start] = m
                    if self.live:
                        self.venue.prewarm(m)
            for start in [s for s in self.markets if s < w - 2 * WINDOW_S]:
                del self.markets[start]
            toks = []
            for start in (w, w + WINDOW_S):
                if start in self.markets:
                    toks += self.markets[start].tokens
            self.books.set_tokens(toks)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.stop.wait(), 5.0)

    async def trade_loop(self) -> None:
        spot_pending = False
        while not self.stop.is_set():
            now = self.clock()
            due = [self._last_eval + EVAL_EVERY_MS - now, self._last_hist + HIST_EVERY_MS - now]
            if spot_pending:
                due.append(self._last_eval + SPOT_EVAL_GAP_MS - now)
            nxt = self.venue.next_event_ms()
            if nxt is not None:
                due.append(nxt - now)
            timeout = max(0.0, min(due)) / 1000.0
            if timeout > 0:
                if spot_pending:
                    await asyncio.sleep(timeout)           # a move is already queued: wait out the gap
                else:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._dirty.wait(), timeout)
            if self._dirty.is_set():
                spot_pending = True
                self._dirty.clear()
            try:
                self.venue.tick()
                now = self.clock()
                self._roll(now)
                gap = now - self._last_eval
                if gap >= EVAL_EVERY_MS or (spot_pending and gap >= SPOT_EVAL_GAP_MS):
                    self._last_eval = now
                    spot_pending = False
                    self._evaluate(now)
                if now - self._last_hist >= HIST_EVERY_MS:
                    self._last_hist = now
                    self._sample(now)
            except Exception:                              # noqa: BLE001 -- never let one tick kill the bot
                log.exception("trade loop tick failed")

    async def copy_activity_loop(self) -> None:
        while not self.stop.is_set():
            run = self.current
            if (self.asset == "ETH" and not self.live and self.s.copy_enabled and run is not None
                    and not run.closed):
                now_s = int(self.clock() / 1000.0)
                try:
                    fills = await asyncio.to_thread(fetch_market_fills, run.m, now_s)
                except Exception as exc:                  # noqa: BLE001 -- activity API may be temporarily unavailable
                    now_ms = self.clock()
                    if now_ms - self._last_copy_error_ms >= 30_000.0:
                        self._last_copy_error_ms = now_ms
                        log.warning("first-fill activity poll failed: %s", exc)
                else:
                    now_s = int(self.clock() / 1000.0)
                    for fill in unseen_fills(fills, run.copy_seen_counts):
                        age_s = now_s - fill["timestamp"]
                        if age_s > self.s.copy_max_fill_age_s:
                            log.info("trader fill %s %.4g shares at +%d s is stale by %d s; skipping",
                                     fill["outcome"], fill["size"], fill["timestamp"] - run.m.start, age_s)
                            continue
                        run.copy_queue.append(fill)
                        log.info("trader fill detected: %s %s %.4g shares at +%d s",
                                 fill["outcome"], fill.get("side") or "TRADE", fill["size"],
                                 fill["timestamp"] - run.m.start)
                    if run.copy_fill is None and run.copy_queue:
                        run.copy_fill = run.copy_queue.pop(0)
                        run.copy_done = False
                        run.copy_attempts = 0
                        run.copy_last_attempt_ms = 0.0
                        run.copy_maker_price = None
                        run.copy_order_floor = max(
                            (f.order_id for f in self.venue.fills.get(run.m.slug, []) if f.tag == COPY_TAG),
                            default=0)
                        if self._dirty is not None:
                            self._dirty.set()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.stop.wait(), self.s.copy_poll_ms / 1000.0)

    def _roll(self, now: float) -> None:
        """Track the current window; hand finished ones to settlement."""
        w = window_start(now / 1000.0)
        m = self.markets.get(w)
        cur = self.current
        if cur is not None and now / 1000.0 >= cur.m.end:
            self.venue.cancel_all(cur.m.slug)
            if self.live:
                self.venue.reconcile_window(cur.m.slug)
            cur.closed = True
            self._skip_check(cur)
            if cur.m.slug in self.venue.pos:
                self.pending[cur.m.slug] = cur.m.end
                self._open_dirty = True
            else:
                self._archive(cur, None)              # watched only: keep the path, no result needed
                self.runs.pop(cur.m.slug, None)
                self.venue.clips.pop(cur.m.slug, None)
            self.current = None
        if self.current is None and m is not None and m.start <= now / 1000.0 < m.end:
            run = self.runs.get(m.slug) or WindowRun(m)
            if not run.hist:
                run.hist = self.pre_hist.pop(m.start, [])   # the minute before the open, sampled ahead
            self.runs[m.slug] = run
            self.current = run
            self.model_hist.clear()
            log.info("window %s open (%s)", m.slug, m.question)

    def _skip_check(self, run: WindowRun) -> None:
        """tri_skip_after_loss: a window closing with a loss over it -> the next window sits out. The official
        result comes ~40 s later, so the winner is the side its closing book leads (right ~99.9% at 0:01 left)."""
        lim = self.tri.skip_after_loss
        if lim <= 0 or run.m.slug not in self.venue.pos:
            return
        last = next((r for r in reversed(run.hist) if r[1] is not None or r[2] is not None), None)
        if last is None:
            return
        up = (last[1] + last[2]) / 2 if last[1] is not None and last[2] is not None else (last[2] if last[2] is not None else last[1])
        pos = self.venue.position(run.m.slug)
        est = pos["if_up"] if up > 0.5 else pos["if_down"]
        if est < -lim:
            self.skip = (run.m.end, est)
            log.info("window %s lost about %.2f (over %.2f): the next window trades nothing", run.m.slug[-10:], est, lim)

    def _strike(self, run: WindowRun, now: float) -> None:
        if run.strike is not None or run.strike_src == "missed":
            return
        if now / 1000.0 - run.m.start > 10.0 and not self._twap_covers(run.m.start):
            # we started after this window opened: its strike printed before we
            # listened, and nothing reconstructs it. Sit the window out.
            run.strike_src = "missed"
            log.info("window %s: strike printed before the bot was listening; sitting it out", run.m.slug[-10:])
            return
        v, src = self.chain.twap_at(self.asset, run.m.start * 1000.0, now)
        if isinstance(v, float):
            run.strike, run.strike_src = v, src
            log.info("strike %s = %.2f (%s)", run.m.slug[-10:], v, src)
            return
        # no Chainlink answer 5 s after the open: our composite's own 60 s TWAP at the open
        if now / 1000.0 - run.m.start > 5.0:
            integral, span = self.twap.observed_integral(run.m.start * 1000.0 - 60_000.0, run.m.start * 1000.0)
            if span >= 55.0:
                run.strike, run.strike_src = math.exp(integral / span), "composite"
                log.warning("strike %s from the composite (Chainlink silent)", run.m.slug[-10:])

    def _twap_covers(self, start: int) -> bool:
        """Could the strike at ``start`` still be known: an official tick for it, or
        Chainlink / composite history reaching back to 60 s before it."""
        v, _ = self.chain.twap_at(self.asset, start * 1000.0, start * 1000.0 + 60_000.0)
        if isinstance(v, float):
            return True
        _, span = self.twap.observed_integral(start * 1000.0 - 60_000.0, start * 1000.0)
        return span >= 55.0

    def _halt_reason(self, now: float) -> str | None:
        if os.path.exists(self._path("KILL")):
            return "halted from the console"
        st = self.stats
        if st["today_key"] != day_key(now / 1000.0):
            st["today_key"], st["today"], st["today_windows"] = day_key(now / 1000.0), 0.0, 0
        if self.s.daily_loss_stop_usdc > 0 and st["today"] <= -self.s.daily_loss_stop_usdc:
            return f"daily loss stop (${-st['today']:.2f} today)"
        if self.live and self.venue.halted:
            return f"live halted: {self.venue.halted}"
        tgt = self.s.stop_at_equity_usdc
        if self.target_hit and self.target_hit[0] != tgt:
            self.target_hit = None                    # the target was changed or cleared: trade again
        if tgt > 0 and self.target_hit is None:
            eq = self._equity()
            if eq >= tgt:
                self.target_hit = (tgt, eq)
                log.warning("equity $%.2f reached the $%.2f target: trading stopped", eq, tgt)
        if self.target_hit:
            return f"equity reached the ${self.target_hit[0]:.2f} target (${self.target_hit[1]:.2f}); stopped"
        paused = pause_reason(self.s, now / 1000.0)
        if paused:
            return paused
        if self.live and getattr(self.venue, "armed", False) and self.venue.hour_full():
            return f"{self.venue.caps.max_orders_per_hour} orders in the last hour; waiting"
        if self.venue.cash < 1.0:
            # orders that do not fit the free cash are dropped one by one in _evaluate (_affordable);
            # a halt on "cash minus our own resting bids" flapped: bid -> halt -> pull -> unhalt -> bid
            return "cash used up" if self.live and self.venue.armed else "paper cash used up"
        return None

    def _evaluate(self, now: float) -> None:
        run = self.current
        self.halted = self._halt_reason(now)
        if getattr(self, "cross_pair", None) is not None:
            self.model = {"strategy": "cross-market pairs",
                          "pair_shares": self.s.cross_pair_shares,
                          "limit_offset": self.s.cross_pair_limit_offset,
                          "rule1": bool(self.s.cross_pair_rule1),
                          "rule2": bool(self.s.cross_pair_rule2),
                          "rule3": bool(self.s.cross_pair_rule3),
                          "rule4": bool(self.s.cross_pair_rule4),
                          "rule5": bool(self.s.cross_pair_rule5),
                          "rule6": bool(self.s.cross_pair_rule6),
                          "rule7": bool(self.s.cross_pair_rule7),
                          "cset": bool(self.s.cross_pair_cset),
                          "xlead_every_ms": self.s.cross_pair_xlead_every_ms,
                          "xlead_max_sum": self.s.cross_pair_xlead_max_sum,
                          "xlead_max_tokens": self.s.cross_pair_xlead_max_tokens,
                          "xlead_from_s": self.s.cross_pair_xlead_from_s,
                          "xlead_until_s": self.s.cross_pair_xlead_until_s,
                          "leader_min": self.s.cross_pair_leader_min,
                          "leader_max": self.s.cross_pair_leader_max,
                          "peer_avg": self.s.cross_pair_peer_avg,
                          "leader_every_ms": self.s.cross_pair_leader_every_ms,
                          "rise": self.s.cross_pair_rise,
                          "buy_until_s": self.s.cross_pair_buy_until_s,
                          "hold_peer_avg": self.s.cross_pair_hold_peer_avg,
                          "hold_min": self.s.cross_pair_hold_min,
                          "hold_max": self.s.cross_pair_hold_max,
                          "hold_from_s": self.s.cross_pair_hold_from_s,
                          "hold_every_ms": self.s.cross_pair_hold_every_ms,
                          "gap": self.s.cross_pair_gap,
                          "gap_min": self.s.cross_pair_gap_min,
                          "gap_max": self.s.cross_pair_gap_max,
                          "gap_until_s": self.s.cross_pair_gap_until_s,
                          "gap_every_ms": self.s.cross_pair_gap_every_ms,
                          "deep": self.s.cross_pair_deep,
                          "deep_min": self.s.cross_pair_deep_min,
                          "deep_max": self.s.cross_pair_deep_max,
                          "deep_rise": self.s.cross_pair_deep_rise,
                          "deep_every_ms": self.s.cross_pair_deep_every_ms,
                          "maj_high": self.s.cross_pair_maj_high,
                          "maj_max": self.s.cross_pair_maj_max,
                          "maj_min_high": self.s.cross_pair_maj_min_high,
                          "maj_from_s": self.s.cross_pair_maj_from_s,
                          "maj_until_s": self.s.cross_pair_maj_until_s,
                          "maj_every_ms": self.s.cross_pair_maj_every_ms,
                          "sweep_peer": self.s.cross_pair_sweep_peer,
                          "sweep_min": self.s.cross_pair_sweep_min,
                          "sweep_max": self.s.cross_pair_sweep_max,
                          "sweep_min_high": self.s.cross_pair_sweep_min_high,
                          "sweep_every_ms": self.s.cross_pair_sweep_every_ms,
                          "sweep_from_s": self.s.cross_pair_sweep_from_s,
                          "sweep_until_s": self.s.cross_pair_sweep_until_s,
                          "cset_min_profit": self.s.cross_pair_cset_min_profit,
                          "cset_min_roi": self.s.cross_pair_cset_min_roi,
                          "cset_reserve": self.s.cross_pair_cset_reserve,
                          "cset_from_s": self.s.cross_pair_cset_from_s,
                          "cset_until_s": self.s.cross_pair_cset_until_s,
                          "cset_max_loss": self.s.cross_pair_cset_max_loss,
                          "pair_wait_s": self.s.cross_pair_pair_wait_s,
                          "pair_fallback_rise": self.s.cross_pair_pair_fallback_rise,
                          "chase": self.s.cross_pair_chase,
                          "min_peers": self.s.cross_pair_min_peers,
                          "retry_ms": self.s.cross_pair_retry_ms,
                          "retry_attempts": self.s.cross_pair_retry_attempts}
            if run is None:
                self.why = {"cross_pair": "no open window"}
            elif self.halted:
                self.why = {"cross_pair": self.halted}
            else:
                on = lambda n, flag: n if flag else f"{n} off"
                bits = [
                    on(f"leader ${self.s.cross_pair_leader_min:.2f}-{self.s.cross_pair_leader_max:.2f} "
                       f"when peers > ${self.s.cross_pair_peer_avg:.2f} (first {self.s.cross_pair_buy_until_s:.0f}s); "
                       f"repeat every {self.s.cross_pair_leader_every_ms}ms; "
                       f"pair after +${self.s.cross_pair_rise:.2f} "
                       f"(or +${self.s.cross_pair_pair_fallback_rise:.2f} after "
                       f"{self.s.cross_pair_pair_wait_s:.0f}s)", self.s.cross_pair_rule1),
                    on(f"hold ${self.s.cross_pair_hold_min:.2f}-{self.s.cross_pair_hold_max:.2f} "
                       f"when peers > ${self.s.cross_pair_hold_peer_avg:.2f} "
                       f"(from {self.s.cross_pair_hold_from_s:.0f}s to close); "
                       f"repeat every {self.s.cross_pair_hold_every_ms}ms", self.s.cross_pair_rule2),
                    on(f"gap ${self.s.cross_pair_gap_min:.2f}-{self.s.cross_pair_gap_max:.2f} "
                       f"when ≥${self.s.cross_pair_gap:.2f} under peers "
                       f"(first {self.s.cross_pair_gap_until_s:.0f}s); "
                       f"repeat every {self.s.cross_pair_gap_every_ms}ms; "
                       f"pair after +${self.s.cross_pair_rise:.2f} "
                       f"(or +${self.s.cross_pair_pair_fallback_rise:.2f} after "
                       f"{self.s.cross_pair_pair_wait_s:.0f}s)", self.s.cross_pair_rule3),
                    on(f"deep ${self.s.cross_pair_deep_min:.2f}-{self.s.cross_pair_deep_max:.2f} "
                       f"when ≥${self.s.cross_pair_deep:.2f} under peers, "
                       f"repeat every {self.s.cross_pair_deep_every_ms}ms; "
                       f"pair after +${self.s.cross_pair_deep_rise:.2f}", self.s.cross_pair_rule4),
                    on(f"maj ask < ${self.s.cross_pair_maj_max:.2f} when "
                       f"≥{self.s.cross_pair_maj_min_high} peers > ${self.s.cross_pair_maj_high:.2f} "
                       f"(from {self.s.cross_pair_maj_from_s:.0f}s to "
                       f"{self.s.cross_pair_maj_until_s:.0f}s); "
                       f"repeat every {self.s.cross_pair_maj_every_ms}ms",
                       self.s.cross_pair_rule5),
                    on(f"sweep ${self.s.cross_pair_sweep_min:.2f}-{self.s.cross_pair_sweep_max:.2f} "
                       f"when ≥{self.s.cross_pair_sweep_min_high} peers > "
                       f"${self.s.cross_pair_sweep_peer:.2f}; "
                       f"every {self.s.cross_pair_sweep_every_ms}ms "
                       f"(from {self.s.cross_pair_sweep_from_s:.0f}s to "
                       f"{self.s.cross_pair_sweep_until_s:.0f}s)",
                       self.s.cross_pair_rule6),
                    on(f"xlead 15-pair order ≤{self.s.cross_pair_xlead_max_tokens:g} tok/pair "
                       f"every {self.s.cross_pair_xlead_every_ms}ms "
                       f"when sum ≤${self.s.cross_pair_xlead_max_sum:.2f} "
                       f"(from {self.s.cross_pair_xlead_from_s:.0f}s to "
                       f"{self.s.cross_pair_xlead_until_s:.0f}s; primary)",
                       self.s.cross_pair_rule7),
                    on(f"cset lock ≥${self.s.cross_pair_cset_min_profit:.3f}/sh "
                       f"ROI ≥{self.s.cross_pair_cset_min_roi:.1%} "
                       f"reserve ${self.s.cross_pair_cset_reserve:.3f}/leg "
                       f"(from {self.s.cross_pair_cset_from_s:.0f}s to "
                       f"{self.s.cross_pair_cset_until_s:.0f}s; secondary)",
                       self.s.cross_pair_cset),
                    f"no retry after +${self.s.cross_pair_chase:.2f}",
                ]
                self.why = {"cross_pair": "; ".join(bits)}
            return
        self._check_sell(run, now)
        if run is None:
            self.model = {}
            self.why = {"maker_Up": "no open window", "maker_Down": "no open window", "taker": "no open window"}
            return
        m = run.m
        self._strike(run, now)
        end_ms = m.end * 1000.0
        p_model = None
        sigma = self.vol.sigma_per_sec
        spot_age = now - self.spot_ms if self.spot_ms else None
        if run.strike and self.spot_px and self.vol.ready and spot_age is not None \
                and spot_age <= self.s.max_spot_age_ms:
            fv = twap_fair_value(self.twap, run.strike, sigma, now, end_ms, info_lag_s=spot_age / 1000.0)
            p_model = fv.p_up
        ub = self.books.book(m.up_token)
        mid_up = None
        if ub is not None:
            b, a = ub.best_bid(), ub.best_ask()
            if b is not None and a is not None and 0.0 <= a - b <= self.s.max_anchor_spread + 1e-9:
                mid_up = (a + b) / 2.0
        self.model_hist.append((now, mid_up, p_model))
        fair_up, fair_src = None, "no anchor"
        target = now - self.s.anchor_s * 1000.0
        anchor = None
        for t, mu, pm in reversed(self.model_hist):
            if t <= target:
                anchor = (mu, pm)
                break
        if anchor and anchor[0] is not None and anchor[1] is not None and p_model is not None:
            fair_up = min(max(anchor[0] + (p_model - anchor[1]), 0.01), 0.99)
            fair_src = "anchor"
        elif p_model is None:
            fair_src = ("no model" if run.strike else
                        "strike missed: the bot started after this window opened" if run.strike_src == "missed"
                        else "no strike yet")
        trig = trig_pts = None
        if self.spot_hist and sigma > 0:
            # the move over trigger_ms up to the newest price we hold (Chainlink's
            # newest tick is ~1.3 s old; the composite's is fresh)
            end_ts, end_px = self.spot_hist[-1]
            then = self._spot_at(end_ts - self.s.trigger_ms)
            if then:
                trig = math.log(end_px / then) / (sigma * math.sqrt(self.s.trigger_ms / 1000.0))
                # the two prices the move was measured between, on the chart's (Chainlink) level
                lvl = self.spot_px / end_px if self.spot_px else 1.0
                trig_pts = (end_ts - self.s.trigger_ms, then * lvl, end_ts, end_px * lvl)

        btc_bps = btc_pts = None                      # the BTC move over tri btc_ms (the BTC-move buy)
        if self.spot_hist and self.tri.btc_shares > 0:
            end_ts, end_px = self.spot_hist[-1]
            then = self._spot_at(end_ts - self.tri.btc_ms)
            if then:
                btc_bps = math.log(end_px / then) * 1e4
                lvl = self.spot_px / end_px if self.spot_px else 1.0
                btc_pts = (end_ts - self.tri.btc_ms, then * lvl, end_ts, end_px * lvl)

        wk_bps = wk_pts = None                        # the BTC move over wk trig_ms (the Whiskas bot)
        if self.spot_hist and self.whisk.enabled:
            end_ts, end_px = self.spot_hist[-1]
            then = self._spot_at(end_ts - self.whisk.trig_ms)
            if then:
                wk_bps = math.log(end_px / then) * 1e4
                lvl = self.spot_px / end_px if self.spot_px else 1.0
                wk_pts = (end_ts - self.whisk.trig_ms, then * lvl, end_ts, end_px * lvl)

        books, ages = {}, []
        for o in OUTCOMES:
            bk = self.books.book(m.token(o))
            if bk is not None:
                books[o] = bk.top()
                if (self.tri.enabled or self.v2.enabled or self.v3.enabled or self.v4.enabled or self.v5.enabled
                        or self.v6.enabled or self.s.copy_enabled) and books[o]["ask"] is not None:
                    books[o]["asks"] = bk.asks_upto(books[o]["ask"] + 0.05)
                if (self.v2.enabled or self.v3.enabled or self.v4.enabled or self.v5.enabled
                    or self.v6.enabled or self.s.copy_enabled) \
                        and books[o]["bid"] is not None:
                    books[o]["bids"] = bk.bids_downto(max(0.0, books[o]["bid"] - 0.20))
                ages.append(self.books.age_ms(m.token(o)) or 0.0)
        wk_mid = None                                 # the Up mid's move over wk pair_ms (the Whiskas pair mode)
        up_t = books.get("Up") or {}
        if self.whisk.enabled and self.whisk.pair_on and run.hist and up_t.get("bid") is not None and up_t.get("ask") is not None:
            i = bisect.bisect_right(run.hist, now - self.whisk.pair_ms, key=lambda r: r[0]) - 1
            if i >= 0 and run.hist[i][1] is not None and run.hist[i][2] is not None:
                wk_mid = (up_t["bid"] + up_t["ask"]) / 2.0 - (run.hist[i][1] + run.hist[i][2]) / 2.0
        v2_low = None                                 # Up's lowest mid over v2 lookback_s (Strategy V2's local low),
        if self.v2.enabled and run.hist:              # from the open only: a rise in the minute before is no signal
            j = bisect.bisect_left(run.hist, max(now - self.v2.lookback_s * 1000.0, m.start * 1000.0), key=lambda r: r[0])
            mids = [(r[1] + r[2]) / 2.0 for r in run.hist[j:] if r[1] is not None and r[2] is not None]
            if up_t.get("bid") is not None and up_t.get("ask") is not None:
                mids.append((up_t["bid"] + up_t["ask"]) / 2.0)
            v2_low = min(mids) if mids else None
        v3_lows = {}                                  # each token's lowest mid since the open within v3 lookback_s
        if self.v3.enabled and run.hist:
            j = bisect.bisect_left(run.hist, max(now - self.v3.lookback_s * 1000.0, m.start * 1000.0), key=lambda r: r[0])
            mids = [(r[1] + r[2]) / 2.0 for r in run.hist[j:] if r[1] is not None and r[2] is not None]
            if up_t.get("bid") is not None and up_t.get("ask") is not None:
                mids.append((up_t["bid"] + up_t["ask"]) / 2.0)
            if mids:
                v3_lows = {"Up": min(mids), "Down": 1.0 - max(mids)}
        v4_hist = []                                  # [(ts, Up mid)] over the last 10 s (V4's velocities)
        if self.v4.enabled and run.hist:
            j = bisect.bisect_left(run.hist, now - max(10_000.0, 1000.0 * (max(self.v4.slow_lookback_s, self.v4.fast_lookback_s) + 1)),
                                   key=lambda r: r[0])
            v4_hist = [(r[0], (r[1] + r[2]) / 2.0) for r in run.hist[j:] if r[1] is not None and r[2] is not None]
        if run.fav_side is None and now / 1000.0 - m.start >= self.s.fav_start_s:
            for o in OUTCOMES:
                t = books.get(o) or {}
                b, a = t.get("bid"), t.get("ask")
                if b is not None and a is not None and a - b <= 0.05 + 1e-9 \
                        and (a + b) / 2.0 > self.s.fav_trigger + 1e-9 and a > self.s.fav_trigger + 1e-9:
                    run.fav_side = o
                    break
        dist_bps = ((self.spot_px - run.strike) / run.strike * 1e4) if (
            run.strike and self.spot_px and spot_age is not None and spot_age <= self.s.max_spot_age_ms) else None
        dist_z = None
        if dist_bps is not None and sigma > 0 and self.vol.ready:
            dist_z = math.log(self.spot_px / run.strike) / (sigma * math.sqrt(max(m.end - now / 1000.0, 1.0)))
        pos = self.venue.pos.get(m.slug, {})
        resting = {o: [] for o in OUTCOMES}
        in_flight = {o: 0.0 for o in OUTCOMES}
        for o_ in self.venue.open_orders(m.slug):
            if o_.kind == "maker":
                resting[o_.outcome].append(Resting(o_.id, o_.price, o_.remaining, o_.created_ms,
                                                   o_.cancel_at is not None))
            elif o_.state == "sent":
                in_flight[o_.outcome] += o_.remaining
        view = View(
            now_ms=now, market=m, secs_in=now / 1000.0 - m.start, secs_left=m.end - now / 1000.0,
            fair_up=fair_up, trigger_z=trig, books=books,
            book_age_ms=max(ages) if len(ages) == 2 else None, spot_age_ms=spot_age,
            shares={o: pos[o].shares if o in pos else 0.0 for o in OUTCOMES},
            window_cost=sum(p.cost for p in pos.values()), reserved=self.venue.reserved(m.slug),
            resting=resting, in_flight_taker=in_flight, taker_count=run.taker_count,
            last_taker_ms=run.last_taker_ms, last_quote_ms=run.last_quote_ms,
            halted=self.halted or ("sold from the console; no more orders this window" if run.manual_sold else None)
            or (f"skipping this window: the last one lost about ${-self.skip[1]:.2f} (over ${self.tri.skip_after_loss:.2f})"
                if self.skip and self.skip[0] == m.start and self.tri.skip_after_loss > 0 else None),
            costs={o: pos[o].cost if o in pos else 0.0 for o in OUTCOMES},
            fees={o: pos[o].fees if o in pos else 0.0 for o in OUTCOMES},
            last_fill_side=(self.venue.fills.get(m.slug) or [None])[-1].outcome if self.venue.fills.get(m.slug) else None,
            fav_side=run.fav_side, dist_bps=dist_bps, dist_z=dist_z,
            dist_usd=None if dist_bps is None else self.spot_px - run.strike,
            fav_shares=sum(f.size for f in self.venue.fills.get(m.slug, []) if f.tag == "fav"),
            fav_dog_shares=sum(f.size for f in self.venue.fills.get(m.slug, []) if f.tag == "dog"),
            late_shares=sum(f.size for f in self.venue.fills.get(m.slug, []) if f.tag == "late"),
            scalp=dict(run.scalp) if run.scalp else None, scalp_closed_ms=run.scalp_closed_ms,
            scalp_pairs=run.scalp_pairs,
            fav_pair_shares=sum(f.size for f in self.venue.fills.get(m.slug, []) if f.tag == "fav_pair"),
            ticks={o: self.books.tick_of(m.token(o)) for o in OUTCOMES},
            tri=dict(run.tri), tri_params=self.tri if self.tri.enabled else None, btc_move_bps=btc_bps,
            ladder=dict(run.ladder), ladder_params=self.ladder if self.ladder.enabled else None,
            whisk=dict(run.whisk, grid_ids=[o_.id for o_ in self.venue.open_orders(m.slug) if o_.tag == "ladder"]),
            whisk_params=self.whisk if self.whisk.enabled else None, wk_move_bps=wk_bps, wk_mid_move=wk_mid,
            v2_params=self.v2 if self.v2.enabled else None, v2_up_low=v2_low,
            v3_params=self.v3 if self.v3.enabled else None, v3_lows=v3_lows,
            v4_params=self.v4 if self.v4.enabled else None, v4_up_hist=v4_hist, live=bool(self.live),
            v5_params=self.v5 if self.v5.enabled else None, v6_params=self.v6 if self.v6.enabled else None,
            copy_fill=run.copy_fill,
            copy_done=run.copy_done,
            copy_attempts=run.copy_attempts,
            copy_last_attempt_ms=run.copy_last_attempt_ms,
            copy_maker_price=run.copy_maker_price,
            copy_filled_shares=sum(f.size for f in self.venue.fills.get(m.slug, [])
                                   if f.tag == COPY_TAG and f.order_id > run.copy_order_floor),
            v2_fills=[dict(outcome=f_.outcome, price=f_.price, size=f_.size, fee=f_.fee, tag=f_.tag, ts=f_.ts_ms, oid=f_.order_id)
                      for f_ in self.venue.fills.get(m.slug, [])]
            if (self.v2.enabled or self.v3.enabled or self.v4.enabled or self.v5.enabled or self.v6.enabled) else [],
            free_cash=self.venue.spendable() - sum(o.price * o.remaining for o in self.venue.open_orders()
                                                   if o.tag != "sell"))
        d = decide(view, self.s)
        if d.copy_done:
            run.copy_fill = None
            run.copy_done = False
            run.copy_attempts = 0
            run.copy_last_attempt_ms = 0.0
            run.copy_maker_price = None
            run.copy_order_floor = max(
                (f.order_id for f in self.venue.fills.get(m.slug, []) if f.tag == COPY_TAG), default=0)
            if run.copy_queue:
                run.copy_fill = run.copy_queue.pop(0)
                if self._dirty is not None:
                    self._dirty.set()
        self._affordable(d, m)
        fair = {"Up": fair_up, "Down": None if fair_up is None else 1.0 - fair_up}
        for oid in d.cancels:
            self.venue.cancel(oid)
        for oid in d.grid_cancels:
            self.venue.cancel(oid)
        free_l = self.venue.spendable() - sum(o_.price * o_.remaining for o_ in self.venue.open_orders() if o_.tag != "sell")
        for o, px, size in d.grid_places:                  # the Whiskas ladder: bids at several prices
            if px * size > free_l + 1e-9:
                continue
            order = self.venue.submit(m, o, "maker", px, size, fair[o], "ladder")
            free_l -= px * size
            self._note_decision(run, order, "ladder", d.why.get(f"maker_{o}", f"ladder bid {px:.2f}"), now, None, sigma,
                                dist_bps, fair[o])
        for o, px, size in d.places:
            tag = d.tags.get(f"maker:{o}") or ("pair" if o in d.pair_sides else "fav" if o in d.fav_sides
                   else "dog" if o in d.dog_sides else "late" if o in d.late_sides else "bid")
            order = self.venue.submit(m, o, "maker", px, size, fair[o], tag)
            if tag == COPY_TAG and order is not None:
                run.copy_attempts += 1
                run.copy_last_attempt_ms = now
                if run.copy_maker_price is None:
                    run.copy_maker_price = order.price
            run.last_quote_ms[o] = now
            if d.tri_kinds.get(f"maker:{o}"):
                self._tri_sent(run, order, d.tri_kinds[f"maker:{o}"], now)
            if d.whisk_kinds.get(f"maker:{o}") and order is not None:
                run.whisk.setdefault("kinds", {})[str(order.id)] = d.whisk_kinds[f"maker:{o}"]
            self._note_decision(run, order, tag, d.why.get(f"maker_{o}", ""), now, trig, sigma, dist_bps, fair[o])
        for o, limit, size in d.sells:
            order = self.venue.submit(m, o, "taker", limit, size, None, "sell")
            self._note_decision(run, order, "sell", d.why.get("taker", "stop-loss"), now, None, 0.0, dist_bps, None)
            log.warning("SELL %s %.2f sh, limit %.2f (%s)", o, size, limit, d.why.get("taker", ""))
        if d.tri_update:
            run.tri.update(d.tri_update)
        if d.ladder_update:
            run.ladder.update(d.ladder_update)
        for k_, v_ in d.whisk_update.items():
            if v_ is None:
                run.whisk.pop(k_, None)
            else:
                run.whisk[k_] = v_
        if d.ledger:
            for name in sorted({x.get("strategy", "v4") for x in d.ledger}):
                self._v4_write([{"ts": round(now), "slug": m.slug, **x} for x in d.ledger if x.get("strategy", "v4") == name],
                               f"{name}_ledger.jsonl")
        for o, limit, size, edge in d.takes:
            tag = d.tags.get(f"taker:{o}") or ("pair" if o in d.pair_sides else "clip")
            order = self.venue.submit(m, o, "taker", limit, size, fair[o], tag)
            if tag == COPY_TAG and order is not None:
                run.copy_attempts += 1
                run.copy_last_attempt_ms = now
            run.taker_count += 1
            run.last_taker_ms[o] = now
            if d.tri_kinds.get(o):
                self._tri_sent(run, order, d.tri_kinds[o], now)
            if d.ladder_kinds.get(o):
                run.ladder.setdefault("kinds", {})[str(order.id)] = d.ladder_kinds[o]
            wk_kind = d.whisk_kinds.get(o)
            if wk_kind and order is not None:
                run.whisk.setdefault("kinds", {})[str(order.id)] = wk_kind
            self._note_decision(run, order, tag, d.why.get("taker", ""), now, None if tag == "btc" else trig, sigma,
                                dist_bps, fair[o], edge,
                                trig_pts if tag == "clip" else (wk_pts if wk_kind else btc_pts) if tag == "btc" else None)
            if tag == "btc" and order is not None and run.decisions:
                if wk_kind:
                    run.decisions[-1].update(btc_bps=_r(wk_bps, 2), btc_need=self.whisk.trig_bps, btc_ms=self.whisk.trig_ms)
                else:
                    run.decisions[-1].update(btc_bps=_r(btc_bps, 2), btc_need=self.tri.btc_bps, btc_ms=self.tri.btc_ms)
        self.why = d.why
        if now - self._last_why >= 1000.0:          # sample reasons once a second, not per event
            self._last_why = now
            for k, reason in d.why.items():
                run.why[k][_reason_key(reason)] += 1
        self.model = {"p_model": _r(p_model), "fair_up": _r(fair_up), "fair_src": fair_src,
                      "price_source": self.price_source, "spot_age_ms": None if spot_age is None else round(spot_age),
                      "trigger_z": _r(trig, 2), "sigma_bps": _r(sigma * 1e4, 3), "mid_up": _r(mid_up),
                      "targets": d.targets}

    def _affordable(self, d, m) -> None:
        """Drop new orders the free cash cannot pay for (cash minus what open orders already hold,
        plus the bids this decision cancels). The window's own decision stays; nothing flaps."""
        cancelled = {oid for oid in d.cancels}
        if self.paper_bankroll is not None:
            # Shared cash remains reserved until each cancellation actually takes effect.
            held = self.paper_bankroll.reserved()
        else:
            held = sum(o.price * o.remaining for o in self.venue.open_orders()
                       if o.id not in cancelled and o.tag != "sell")
        free = self.venue.spendable() - held
        keep_places, keep_takes = [], []
        for o, px, size in d.places:
            cost = px * size
            if cost <= free + 1e-9:
                keep_places.append((o, px, size)); free -= cost
            else:
                d.why[f"maker_{o}"] = f"not enough cash: ${free:.2f} free for a ${cost:.2f} bid"
        for o, limit, size, edge in d.takes:
            cost = limit * size + m.fees.taker_fee(limit, size)
            if cost <= free + 1e-9:
                keep_takes.append((o, limit, size, edge)); free -= cost
            else:
                d.why["taker"] = f"not enough cash: ${free:.2f} free for a ${cost:.2f} buy"
        d.places, d.takes = keep_places, keep_takes

    def _equity(self) -> float:
        """Cash + open shares at their current mid (at cost when there is no book)."""
        if self.paper_bankroll is not None:
            return self.paper_bankroll.equity()
        val = self.venue.cash
        for slug, pos_map in self.venue.pos.items():
            r = self.runs.get(slug)
            for o, p in pos_map.items():
                bk = self.books.book(r.m.token(o)) if r else None
                mid = bk.mid() if bk is not None else None
                val += p.shares * (mid if mid is not None else (p.cost / p.shares if p.shares > 1e-9 else 0.0))
        return val

    def _check_sell(self, run: WindowRun | None, now: float) -> None:
        """A sell requested from the console (data dir/sell_request.json): sell everything held on
        that side of the current window at the best bid, at once; then no more orders this window."""
        if now - self._last_sell_check < 200.0:
            return
        self._last_sell_check = now
        p = self._path("sell_request.json")
        if not os.path.exists(p):
            return
        try:
            with open(p) as fh:
                req = json.load(fh) or {}
        except (OSError, ValueError):
            req = {}
        with contextlib.suppress(OSError):
            os.remove(p)
        o = req.get("outcome")
        rec = {"ts": round(now), "outcome": o, "slug": run.m.slug if run else None, "status": ""}

        def done(status: str) -> None:
            rec["status"] = status
            self.sell_log.append(rec)
            log.warning("console sell %s: %s", o, status)

        if run is None or run.closed or now / 1000.0 >= run.m.end:
            return done("no open window")
        if req.get("slug") and req["slug"] != run.m.slug:
            return done("that window has already closed")
        if time.time() - float(req.get("ts") or 0) > SELL_REQUEST_MAX_AGE_S:
            return done("request too old")
        if o not in OUTCOMES:
            return done("unknown side")
        p_ = self.venue.pos.get(run.m.slug, {}).get(o)
        held = p_.shares if p_ else 0.0
        size = math.floor(held * 100 + 1e-6) / 100
        if size <= 0.0:
            return done("nothing held on that side")
        bk = self.books.book(run.m.token(o))
        bid = bk.best_bid() if bk is not None else None
        if bid is None:
            return done("no bid in the book")
        limit = max(0.01, round(bid - SELL_SLIP, 2))
        for x in self.venue.open_orders(run.m.slug):
            if x.kind == "maker":
                self.venue.cancel(x.id)               # bids of this window come off first
        order = self.venue.submit(run.m, o, "taker", limit, size, None, "sell")
        run.manual_sold = True
        rec.update(shares=size, bid=bid, limit=limit, order=order.id, status="sent")
        self.sell_log.append(rec)
        self._note_decision(run, order, "sell", f"sold from the console: {size:.2f} sh at <= {limit:.2f} "
                            f"(bid {bid:.2f})", now, None, 0.0, None, None)
        log.warning("CONSOLE SELL %s %.2f sh, limit %.2f (bid %.2f)", o, size, limit, bid)

    def _sell_rows(self) -> list[dict]:
        out = []
        for r in self.sell_log:
            row = dict(r)
            o = self.venue.orders.get(r.get("order")) if r.get("order") else None
            if o is not None:
                row["result"] = o.result or ("SENT" if o.state != "done" else "")
                row["filled"] = round(o.filled, 2)
            out.append(row)
        return out

    def _note_decision(self, run: WindowRun, order, tag: str, why: str, now: float, z, sigma: float,
                       dist_bps, fair, edge=None, trig_pts=None) -> None:
        """One order the strategy sent, with what it saw: drawn on the coin's price chart."""
        if order is None:
            return
        run.decisions.append({
            "id": order.id, "ts": round(now), "tag": tag, "kind": order.kind, "outcome": order.outcome,
            "price": order.price, "size": order.size, "spot": _r(self.spot_px, 6), "why": why,
            "z": _r(z, 2), "trigger": self.s.trigger_sigma, "trigger_ms": self.s.trigger_ms,
            "sigma_bps": _r(sigma * 1e4, 3), "dist_bps": _r(dist_bps, 2), "fair": _r(fair), "edge": _r(edge),
            "secs_left": _r(run.m.end - now / 1000.0, 1)})
        if trig_pts is not None:
            run.decisions[-1].update(trig_from_ts=round(trig_pts[0]), trig_from_px=_r(trig_pts[1], 6),
                                     trig_to_ts=round(trig_pts[2]), trig_to_px=_r(trig_pts[3], 6))
        if len(run.decisions) > 400:
            del run.decisions[:100]

    def _decision_rows(self, run: WindowRun) -> list[dict]:
        """The decisions with what became of each order (filled, killed, cancelled, still resting)."""
        out = []
        for dec in run.decisions:
            o = self.venue.orders.get(dec["id"])
            row = dict(dec)
            if o is not None:
                row["result"] = o.result or ("RESTING" if o.state == "live" else "SENT")
                row["filled"] = _r(o.filled, 2)
            elif "result" not in row:
                row["result"] = "?"
            out.append(row)
        return out

    def _sample_next(self, now: float) -> None:
        """The minute before a window opens: its Up book and the price, kept for its chart. No fair value,
        model or bids yet (the strike prints at the open)."""
        start = window_start(now / 1000.0) + WINDOW_S
        for k in [k for k in self.pre_hist if k < start]:
            del self.pre_hist[k]                          # opened (taken by _roll) or never listed
        m = self.markets.get(start)
        if m is None or now / 1000.0 < start - PRE_OPEN_S:
            return
        ub = self.books.book(m.up_token)
        self.pre_hist.setdefault(start, []).append(
            [round(now), _r(ub.best_bid() if ub else None), _r(ub.best_ask() if ub else None),
             None, None, _r(self.spot_px, 2), None, None])

    def _sample(self, now: float) -> None:
        self._sample_next(now)
        run = self.current
        if run is None:
            return
        ub = self.books.book(run.m.up_token)
        q = {o: None for o in OUTCOMES}
        for o_ in self.venue.open_orders(run.m.slug):
            if o_.kind == "maker" and o_.state == "live" and o_.cancel_at is None:
                q[o_.outcome] = o_.price
        self._last_top = (_r(ub.best_bid() if ub else None), _r(ub.best_ask() if ub else None))
        run.hist.append([round(now), self._last_top[0], self._last_top[1],
                         self.model.get("fair_up"), self.model.get("p_model"),
                         _r(self.spot_px, 2), q["Up"], q["Down"]])

    async def settle_loop(self) -> None:
        while not self.stop.is_set():
            now_s = time.time()
            for slug, end in sorted(self.pending.items(), key=lambda kv: kv[1]):
                if now_s < end + SETTLE_AFTER_S:
                    continue
                run = self.runs.get(slug)
                src = "venue"
                try:
                    winner = await asyncio.to_thread(fetch_outcome, slug)
                except FetchFailed as exc:
                    log.warning("gamma outcome %s: %s", slug, exc)
                    winner = None
                if winner is None and now_s > end + SETTLE_FALLBACK_S and run is not None and run.strike:
                    v, _ = self.chain.twap_at(self.asset, end * 1000.0, time.time() * 1000.0)
                    if isinstance(v, float):
                        winner, src = ("Up" if v >= run.strike else "Down"), "estimated (Chainlink TWAP)"
                if winner is None:
                    continue
                self._settle(slug, winner, src)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.stop.wait(), 10.0)

    def _settle(self, slug: str, winner: str, src: str) -> None:
        run = self.runs.get(slug)
        res = self.venue.settle(slug, winner)
        pos = res["position"]
        fills = res["fills"]
        rule_pnl = {}
        if getattr(self, "cross_pair", None) is not None or any(
                str(f.get("tag") or "").startswith("crosspair:") for f in fills):
            from .cross_pair import rule_pnl_from_fills
            rule_pnl = rule_pnl_from_fills(fills, winner=winner)
        rec = {
            "slug": slug, "start": run.m.start if run else None, "end": self.pending.get(slug),
            "winner": winner, "winner_src": src,
            "strike": run.strike if run else None, "strike_src": run.strike_src if run else None,
            "pnl": round(res["pnl"], 6), "maker_pnl": round(res["maker_pnl"], 6), "taker_pnl": round(res["taker_pnl"], 6),
            "rule_pnl": rule_pnl,
            "payout": res["payout"], "cost": round(pos["cost"], 6), "fees": round(pos["fees"], 6),
            "rebate_est": round(pos["rebate_est"], 6),
            "up_shares": round(pos["Up"]["shares"], 4), "down_shares": round(pos["Down"]["shares"], 4),
            "up_avg": _r(pos["Up"]["avg"]), "down_avg": _r(pos["Down"]["avg"]),
            "pair_cost": _r(pos["pair_cost"]), "hedge": _r(pos["hedge"]),
            "fills": len(fills), "maker_fills": sum(f["kind"] == "maker" for f in fills),
            "taker_fills": sum(f["kind"] == "taker" for f in fills),
            "maker_cost": round(pos["Up"]["maker_cost"] + pos["Down"]["maker_cost"], 6),
            "taker_cost": round(pos["Up"]["taker_cost"] + pos["Down"]["taker_cost"], 6),
            "first_fill_s": round(min(f["ts_ms"] for f in fills) / 1000.0 - run.m.start, 1) if fills and run else None,
            "last_fill_s": round(max(f["ts_ms"] for f in fills) / 1000.0 - run.m.start, 1) if fills and run else None,
            "counts": res["counts"], "settled_at": round(time.time(), 3),
        }
        with open(self._path("windows.jsonl"), "ab") as fh:
            fh.write(orjson.dumps(rec) + b"\n")
        rec = {**rec, "clips": res.get("clips") or []}
        if fills:
            with open(self._path("fills.jsonl"), "ab") as fh:
                for f in fills:
                    fh.write(orjson.dumps(self._fill_row(f, winner, run)) + b"\n")
        self._fold(self.stats, rec)
        if run is not None:
            self._archive(run, rec)
        self.runs.pop(slug, None)
        self.pending.pop(slug, None)
        self._open_dirty = True
        log.info("settled %s: %s (%s) pnl %+.2f (maker %+.2f, taker %+.2f), %d fills",
                 slug, winner, src, rec["pnl"], rec["maker_pnl"], rec["taker_pnl"], rec["fills"])

    @staticmethod
    def _fill_row(f: dict, winner: str, run: WindowRun | None) -> dict:
        won = f["outcome"] == winner
        row = {"slug": f["slug"], "ts": round(f["ts_ms"]), "secs_left": round(f["secs_left"], 1),
               "outcome": f["outcome"], "kind": f["kind"], "how": f["how"], "price": round(f["price"], 4),
               "size": round(f["size"], 4), "fee": round(f["fee"], 6), "rebate_est": round(f["rebate_est"], 6),
               "fair": f["fair"], "mid": f["mid"], "won": won, "race_p": f.get("race_p"), "tag": f.get("tag", ""),
               "pnl": round((f["size"] if won else 0.0) - (f["price"] * f["size"] + f["fee"]), 6)}
        # markouts: the outcome's book mid 1 s / 5 s / 30 s after the fill, minus the price paid
        if run is not None and run.hist:
            ts = [p[0] for p in run.hist]
            for h in (1, 5, 30):
                i = bisect.bisect_left(ts, f["ts_ms"] + h * 1000.0)
                mid = None
                if i < len(run.hist):
                    p = run.hist[i]
                    if p[1] is not None and p[2] is not None:
                        mid = (p[1] + p[2]) / 2.0
                if mid is not None and f["outcome"] == "Down":
                    mid = 1.0 - mid
                row[f"mo_{h}s"] = None if mid is None else round(mid - f["price"], 4)
        return row

    def _archive(self, run: WindowRun, rec: dict | None) -> None:
        cross_pair = getattr(self, "cross_pair", None)
        doc = {"slug": run.m.slug, "question": run.m.question, "start": run.m.start, "end": run.m.end,
               "strike": run.strike, "strike_src": run.strike_src, "result": rec,
               "cols": ["t", "up_bid", "up_ask", "fair_up", "p_model", "spot", "bid_up", "bid_down"],
               "hist": run.hist, "fills": run.fills,
               "clips": (rec or {}).get("clips") or self.venue.clips.get(run.m.slug, []),
               "pairings": cross_pair.pairings_for_asset(self.asset, run.m.start) if cross_pair else None,
               "decisions": self._decision_rows(run),
               "why": {k: dict(c) for k, c in run.why.items()}}
        path = os.path.join(self.dir, "windows", f"{run.m.slug}.json")
        try:
            with open(path + ".tmp", "wb") as fh:
                fh.write(orjson.dumps(doc))
            os.replace(path + ".tmp", path)
        except OSError as exc:
            log.warning("archive %s: %s", run.m.slug, exc)

    def _take_reset_request(self) -> float | None:
        """The console's 'restart with new cash' (paper only): the balance, the request consumed."""
        if self.live:
            return None
        p = self._path("reset_request.json")
        if not os.path.exists(p):
            return None
        try:
            with open(p) as fh:
                bal = float((json.load(fh) or {}).get("balance"))
        except (OSError, ValueError, TypeError):
            bal = None
        with contextlib.suppress(OSError):
            os.remove(p)
        return bal if bal is not None and bal >= 1.0 else None

    def _archive_and_reset(self, bal: float) -> None:
        """After the last writes: move this run's ledger to <data root>/../paper-archive, keep the settings,
        save the new cash; the process then exits and pm2 starts it again with it."""
        root = os.path.dirname(self.dir) if os.path.basename(os.path.normpath(self.dir)) == self.asset.lower() else self.dir
        dest = os.path.join(os.path.dirname(os.path.abspath(root)), "paper-archive",
                            f"{self.asset.lower()}-reset-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}")
        os.makedirs(dest, exist_ok=True)
        for name in ("fills.jsonl", "open.json", "runs.json", "state.json", "windows.jsonl", "windows", "charts"):
            src = self._path(name)
            if os.path.exists(src):
                shutil.move(src, os.path.join(dest, name))
        with contextlib.suppress(OSError):
            shutil.copy2(self._path("controls.json"), os.path.join(dest, "controls.json"))
        with open(self._path("balance.json"), "w") as fh:
            json.dump({"balance": bal, "ts": round(time.time()), "archived_to": dest}, fh)
        if self.paper_bankroll is not None:
            shared_path = os.path.join(root, "paper_bankroll.json")
            tmp = shared_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"balance": bal, "scope": "shared", "ts": round(time.time())}, fh)
            os.replace(tmp, shared_path)
            self.paper_bankroll.starting_balance = bal
            self.paper_bankroll.cash = bal
        log.warning("RESTART WITH NEW CASH: ledger archived to %s; the bot starts again with $%.2f", dest, bal)

    async def controls_loop(self) -> None:
        path = self._path("controls.json")
        while not self.stop.is_set():
            bal = self._take_reset_request()
            if bal is not None:
                self.reset_balance = bal
                log.warning("console asked for a restart with $%.2f: stopping", bal)
                self.stop.set()
                break
            cal_path = os.path.join(self.dir, "fill_calibration.json")
            try:
                cal_mtime = os.path.getmtime(cal_path)
            except OSError:
                cal_mtime = None
            if cal_mtime != getattr(self, "_cal_mtime", cal_mtime):
                self.cal = Calibration(cal_path)          # a re-run of scripts/calibrate_fills.py
                self.venue.cal = self.cal
                log.info("fill calibration reloaded: %s", self.cal.summary())
            self._cal_mtime = cal_mtime
            reset = self._loss_reset_ts()
            if reset > self.stats.get("loss_reset_at", 0):
                self.stats.update(today=0.0, today_windows=0, loss_reset_at=reset)
                log.info("daily loss count reset from the console")
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = None
            if mtime != self.ctrl["mtime"]:
                self.ctrl["mtime"] = mtime
                stored = {}
                if mtime is not None:
                    try:
                        with open(path) as fh:
                            stored = json.load(fh) or {}
                    except (OSError, ValueError) as exc:
                        self.ctrl["notes"] = [f"controls.json unreadable: {exc}"]
                        stored = None
                if stored is not None:
                    clean, notes = config.clamp_values(stored)
                    changed = config.apply(self.s, {**self.defaults, **clean})
                    self.ctrl.update(applied_at=time.time(), applied=changed or ["no change"], notes=notes,
                                     stored=clean)
                    if changed:
                        log.info("controls applied: %s", "; ".join(changed))
                    self.tri = TriParams.from_settings(self.s)
                    self.ladder = LadderParams.from_settings(self.s)
                    self.whisk = WhiskParams.from_settings(self.s)
                    self.v2 = V2Params.from_settings(self.s)
                    self.v3 = V3Params.from_settings(self.s)
                    self.v4 = V4Params.from_settings(self.s)
                    self.v5 = V5Params.from_settings(self.s)
                    self.v6 = V6Params.from_settings(self.s)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.stop.wait(), 2.0)

    async def state_loop(self) -> None:
        last_prune = 0.0
        while not self.stop.is_set():
            try:
                if self.live:
                    self._sync_account()
                self._write_state(running=True)
                if self._open_dirty:
                    self._open_dirty = False
                    self._write_open()
                if time.time() - last_prune > 3600:
                    last_prune = time.time()
                    await asyncio.to_thread(self._prune_archive)
            except Exception:                              # noqa: BLE001
                log.exception("state write failed")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.stop.wait(), STATE_EVERY_S)

    def _prune_archive(self) -> None:
        cutoff = time.time() - ARCHIVE_DAYS * 86400
        d = os.path.join(self.dir, "windows")
        for name in os.listdir(d):
            p = os.path.join(d, name)
            with contextlib.suppress(OSError):
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)

    def _write_runs(self) -> None:
        """At shutdown: the chart history of the current and unsettled windows
        (price path, strike, clips), so a restart does not blank their charts."""
        keep = {}
        for slug, run in self.runs.items():
            if run is self.current or slug in self.pending:
                keep[slug] = {"market": asdict(run.m), "strike": run.strike, "strike_src": run.strike_src,
                              "hist": run.hist, "taker_count": run.taker_count,
                              "clips": self.venue.clips.get(slug, []), "decisions": self._decision_rows(run),
                              "scalp": run.scalp, "scalp_closed_ms": run.scalp_closed_ms,
                              "scalp_pairs": run.scalp_pairs,
                              "tri": {k: v for k, v in run.tri.items() if k != "kinds"},
                              "ladder": {k: v for k, v in run.ladder.items() if k != "kinds"},
                              "whisk": {k: v for k, v in run.whisk.items() if k != "kinds"}}
        with open(self._path("runs.json.tmp"), "wb") as fh:
            fh.write(orjson.dumps(keep))
        os.replace(self._path("runs.json.tmp"), self._path("runs.json"))

    def _restore_runs(self) -> None:
        try:
            with open(self._path("runs.json"), "rb") as f:
                saved = orjson.loads(f.read())
        except (OSError, ValueError):
            return
        now_s = time.time()
        n = 0
        for slug, rec in (saved or {}).items():
            run = self.runs.get(slug)
            if run is None:
                m = market_from_dict(rec.get("market"))
                if m is None or now_s >= m.end:       # nothing held and already over: nothing to resume
                    continue
                run = self.runs[slug] = WindowRun(m)
            run.hist = rec.get("hist") or []
            run.decisions = rec.get("decisions") or []
            run.scalp = rec.get("scalp")
            run.tri = dict(rec.get("tri") or {})
            run.ladder = dict(rec.get("ladder") or {})
            run.whisk = dict(rec.get("whisk") or {})
            run.scalp_pairs = int(rec.get("scalp_pairs") or 0)
            run.scalp_closed_ms = rec.get("scalp_closed_ms") if rec.get("scalp_closed_ms") is not None else -1e18
            run.taker_count = int(rec.get("taker_count") or 0)
            if rec.get("strike") is not None:
                run.strike, run.strike_src = float(rec["strike"]), rec.get("strike_src") or ""
            if rec.get("clips"):
                self.venue.clips[slug] = rec["clips"]
            n += 1
        if n:
            log.info("restored the chart history of %d window(s)", n)

    def _write_open(self) -> None:
        data = self.venue.export_open()
        with open(self._path("open.json.tmp"), "wb") as fh:
            fh.write(orjson.dumps(data))
        os.replace(self._path("open.json.tmp"), self._path("open.json"))

    def _write_state(self, running: bool) -> None:
        now = self.clock()
        run = self.current
        window = None
        if run is not None:
            m = run.m
            pos = self.venue.position(m.slug)
            books = {}
            for o in OUTCOMES:
                bk = self.books.book(m.token(o))
                books[o] = bk.top() if bk is not None else None
            orders = [{"id": o.id, "outcome": o.outcome, "kind": o.kind, "price": o.price, "size": o.size,
                       "filled": _r(o.filled, 2), "state": o.state, "cancelling": o.cancel_at is not None,
                       "queue_ahead": _r(o.queue_ahead, 1), "age_ms": round(now - o.created_ms)}
                      for o in self.venue.open_orders(m.slug)]
            window = {"slug": m.slug, "question": m.question, "start": m.start, "end": m.end,
                      "secs_left": round(m.end - now / 1000.0, 1), "strike": run.strike, "strike_src": run.strike_src,
                      "books": books, "position": pos, "orders": orders, "why": self.why,
                      "counts": dict(self.venue.counts.get(m.slug, {})), "taker_count": run.taker_count,
                      "fills": run.fills[-300:], "clips": self.venue.clips.get(m.slug, [])[-300:],
                      "decisions": self._decision_rows(run)[-300:],
                      "why_counts": {k: dict(c) for k, c in run.why.items()}}
        nxt = None
        nm = self.markets.get(window_start(now / 1000.0) + WINDOW_S)   # the next window: listed, books subscribed
        if nm is not None:
            nbooks = {}
            for o in OUTCOMES:
                bk = self.books.book(nm.token(o))
                nbooks[o] = bk.top() if bk is not None else None
            tw = self.chain.latest_twap(self.asset)
            nxt = {"slug": nm.slug, "question": nm.question, "start": nm.start, "end": nm.end,
                   "secs_to_open": round(nm.start - now / 1000.0, 1), "books": nbooks,
                   "strike_now": tw.price if tw else None}     # its strike if it opened now (TWAP-60 now)
        open_value = 0.0
        pending = []
        for slug, pos_map in self.venue.pos.items():
            r = self.runs.get(slug)
            val = 0.0
            for o, p in pos_map.items():
                bk = self.books.book(r.m.token(o)) if r else None
                mid = bk.mid() if bk is not None else None
                val += p.shares * (mid if mid is not None else p.cost / p.shares if p.shares else 0.0)
            open_value += val
            if slug in self.pending:
                s = self.venue.position(slug)
                pending.append({"slug": slug, "end": self.pending[slug], "cost": s["cost"],
                                "up": s["Up"]["shares"], "down": s["Down"]["shares"]})
        if self.paper_bankroll is not None:
            open_value = self.paper_bankroll.open_value()
        comp = self.spot.view(self.asset, now)
        model = dict(self.model)
        cross_pair = getattr(self, "cross_pair", None)
        if model.get("strategy") == "cross-market pairs":
            model["pairings"] = (cross_pair.pairings_for_asset(self.asset, run.m.start)
                                 if cross_pair is not None and run is not None else [])
        state = {
            "v": 1, "ts": round(now), "running": running,
            "mode": self.venue.mode if self.live else "paper", "asset": self.asset,
            "data_dir": os.path.relpath(os.path.abspath(self.dir), os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "pid": os.getpid(),
            "started_at": round(self.started * 1000), "uptime_s": round(time.time() - self.started),
            "balance0": round(self.paper_bankroll.starting_balance if self.paper_bankroll is not None
                               else self.args.balance + self.outside, 4), "outside": round(self.outside, 4),
            "cash": round(self.venue.cash, 4),
            "reserved": round(self.venue.reserved(), 4), "open_value": round(open_value, 4),
            "equity": round(self.venue.cash + open_value, 4),
            "shared_bankroll": self.paper_bankroll is not None,
            "shared_pnl": round(self.paper_bankroll.realized_pnl, 6) if self.paper_bankroll is not None else None,
            "shared_markets": len(self.paper_bankroll.venues) if self.paper_bankroll is not None else 1,
            "stats": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in self.stats.items()},
            "halted": self.halted, "kill": os.path.exists(self._path("KILL")),
            "model": model, "window": window, "next": nxt, "hist": run.hist if run else [],
            "pending": pending, "recent_fills": list(self.recent_fills)[-40:],
            "feeds": {
                "spot": {k: v.as_dict(now) for k, v in self.spot_health.items()},
                "composite": None if comp is None else {
                    "price": round(comp.price, 2), "cl_price": _r(self.spot_px, 2), "n": comp.n_sources,
                    "age_ms": round(comp.age_ms), "sources": comp.sources, "basis_bps": comp.basis_bps,
                    "lag_ms": {k: round(v) for k, v in self.spot.lag.items()},
                    "lagging": sorted(self.spot.lagging)},
                "vol_ready": self.vol.ready,
                "chainlink": self.chain.snapshot(self.asset, now),
                "book": self.books.health(),
            },
            "calibration": self.cal.summary(),
            "live": self.venue.snapshot() if self.live else None,
            "sells": self._sell_rows(),
            "controls": {"values": asdict(self.s), "defaults": self.defaults, "stored": self.ctrl["stored"],
                         "applied_at": self.ctrl["applied_at"], "applied": self.ctrl["applied"],
                         "notes": self.ctrl["notes"]},
        }
        tmp = self._path("state.json.tmp")
        with open(tmp, "wb") as fh:
            fh.write(orjson.dumps(state))
        os.replace(tmp, self._path("state.json"))

    # ------------------------------------------------------------- main

    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Run until SIGINT/SIGTERM -- or until ``stop`` is set, when one process runs several bots."""
        ASSET_CTX.set(self.asset)                    # inherited by every task and thread started below
        self.stop = stop or asyncio.Event()
        self._dirty = asyncio.Event()
        loop = asyncio.get_running_loop()
        if stop is None:
            for sig in (signal.SIGINT, signal.SIGTERM):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(sig, self.stop.set)
        exchanges = tuple(x.strip() for x in self.args.exchanges.split(",") if x.strip())
        venue_task = None
        if self.live:
            from .live import LiveConfigError
            try:
                snap = await asyncio.to_thread(self.venue.connect)
            except LiveConfigError as exc:
                log.error("live: %s", exc)
                raise SystemExit(2) from None
            log.info("live venue: %s", {k: snap[k] for k in ("mode", "venue_balance", "allowance", "caps")})
            venue_task = asyncio.create_task(self.venue.run(self.stop))
        tasks = [
            asyncio.create_task(run_all((self.asset,), self._on_quote, self.clock, self.stop, exchanges=exchanges,
                                        health=self.spot_health, on_source_latency=self._on_source_latency)),
            asyncio.create_task(self.chain.run(self.stop, self.clock, on_tick=self._on_chain)),
            asyncio.create_task(self.books.run(self.stop)),
            asyncio.create_task(self.discover_loop()),
            asyncio.create_task(self.trade_loop()),
            asyncio.create_task(self.settle_loop()),
            asyncio.create_task(self.controls_loop()),
            asyncio.create_task(self.state_loop()),
            asyncio.create_task(self.copy_activity_loop()),
        ]
        log.info("xshark bot started (%s): balance $%.2f, price %s, exchanges %s, data %s",
                 self.venue.mode if self.live else "paper", self.venue.cash, self.price_source,
                 ",".join(exchanges), self.dir)
        log.info("fill calibration: %s", self.cal.summary())
        await self.stop.wait()
        log.info("stopping")
        self.venue.cancel_all()
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                await asyncio.wait_for(t, 5.0)
        if venue_task is not None:
            # the live worker sees the stop itself and cancels any resting orders on the venue
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                await asyncio.wait_for(venue_task, 20.0)
        # a window still open keeps its position: the next start settles it
        if self.current is not None and self.current.m.slug in self.venue.pos:
            self.pending[self.current.m.slug] = self.current.m.end
        self._write_open()
        with contextlib.suppress(OSError):
            self._write_runs()
        self._write_state(running=False)
        if not self.live:
            bal = self.reset_balance or self._take_reset_request()   # also a coin stopped by another's request
            if bal is not None:
                self._archive_and_reset(bal)


def _hhmm(h: float) -> str:
    m = int(round(h * 60)) % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def pause_reason(s, ts_s: float) -> str | None:
    """The pause range (GMT+9) that ``ts_s`` falls in, or None."""
    h = ((ts_s + TZ_SHIFT_S) % 86400) / 3600.0
    for a, b in ((s.pause_a_from, s.pause_a_to), (s.pause_b_from, s.pause_b_to)):
        if abs(a - b) < 1e-9:
            continue
        inside = (a <= h < b) if a < b else (h >= a or h < b)
        if inside:
            return f"paused {_hhmm(a)}-{_hhmm(b)} GMT+9 (pause hours)"
    return None


def _reason_key(reason: str) -> str:
    """Group reasons that carry numbers ("bidding 0.47") under one label."""
    for prefix in ("paused", "bidding", "resting at", "clip ", "waiting for a move", "holding", "window budget",
                   "bid ", "clip under", "Up edge", "Down edge", "Up ask", "Down ask", "Up cooling", "Down cooling",
                   "daily loss stop"):
        if reason.startswith(prefix):
            return prefix.strip()
    return reason.split(" (")[0]
