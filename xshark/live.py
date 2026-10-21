"""Real orders on Polymarket for xshark, behind three locks.

The design follows troll-poly-bot's execution/polymarket.py, the adapter this
VPS's real-money bots have used (same SDK, polymarket-client 0.10, same order
shapes), on top of xshark's paper venue so the bot, strategy and console do
not change.

1. ``--live`` alone is SHADOW: it logs in with the account in ``.env``, reads
   the real balance and allowance, and SIGNS every order the strategy wants --
   then does not post it. Fills stay simulated (the paper venue), so the
   console keeps measuring the strategy while the live path is proven.
2. ``--live --armed`` posts real orders, and only if the environment carries
   ``TPB_LIVE_ACK`` (or ``XSHARK_LIVE_ACK``) = ``I_UNDERSTAND_REAL_MONEY``.
   Fills are what the venue reports; nothing is simulated.
3. Hard caps that sit outside the strategy (``LiveCaps``): dollars per order,
   dollars open, a daily loss halt, orders per hour, a kill file re-read just
   before every POST, and a stale-decision cutoff.

Order shapes (as troll-poly-bot's live adapter):
  clip  a LIMIT order for an exact share count at the clip's limit, its unmatched rest
        cancelled at once (a market BUY spends dollars and buys extra shares when cheaper):
        create_limit_order(price=<limit>, size=5, side=BUY, expiration=now+240 s), then cancel_order
  bid   a post-only GTC LIMIT with a venue-side expiry (the crash backstop):
        create_limit_order(price, size, side=BUY, post_only=True, expiration=now+240 s)
        polled every second for size_matched; cancel_order when the strategy drops it.

Credentials come from the environment only (the CLI loads ``.env``); they are
never logged or written to the state file. Names as bo-real-bot's ``.env``:
TPB_POLY_PRIVATE_KEY, TPB_POLY_WALLET (or TPB_POLY_FUNDER),
TPB_POLY_API_KEY/_SECRET/_PASSPHRASE (else derived), TPB_POLY_RELAYER_API_KEY
+ TPB_POLY_RELAYER_API_KEY_ADDRESS (redemption). XSHARK_* variants also work.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Callable

from .paper import EPS, Fill, Order, PaperVenue, Position

log = logging.getLogger("xshark.live")

ACK_PHRASE = "I_UNDERSTAND_REAL_MONEY"
USDC_DECIMALS = 1_000_000.0
UNFILLED_CODES = {"unmatched", "fok_not_filled", "fak_not_filled"}
RESTING_BACKSTOP_S = 240          # the SDK insists on >= 3 minutes of venue-side expiry
POLL_S = 1.0                      # order-status check while the account websocket is down
POLL_S_WS = 5.0                   # ... and while it is up: only a backstop then
WS_UNCLAIMED_TTL_S = 120.0        # websocket fills for an order id the POST reply has not named yet
RECONCILE_S = 30.0
KEEP_WARM_S = 15.0
REDEEM_RETRY_S = 90.0
REDEEM_MAX_ATTEMPTS = 12
SYNC_QUIET_S = 45.0               # a balance read this long after the ledger last moved is a fair comparison
MAX_CONSECUTIVE_ERRORS = 5
MAX_UNREADABLE_POLLS = 10         # a bid whose status cannot be read for ~10 s is cancelled
WINDOW_RECONCILE_S = 10.0         # after a window closes, check its fills against the venue's trades


class LiveConfigError(Exception):
    """The account setup is wrong. Fatal at startup."""


@dataclass
class LiveCaps:
    max_order_usdc: float = 5.0
    max_open_usdc: float = 15.0
    max_daily_loss_usdc: float = 5.0
    max_orders_per_hour: int = 300
    max_decision_age_ms: float = 1000.0


def _env(*names: str, env=None) -> str | None:
    env = os.environ if env is None else env
    for n in names:
        v = (env.get(n) or "").strip()
        if v:
            return v
    return None


@dataclass
class LiveCredentials:
    private_key: str
    wallet: str | None = None
    api_key: str | None = None
    api_secret: str | None = None
    api_passphrase: str | None = None
    relayer_key: str | None = None
    relayer_address: str | None = None

    @classmethod
    def from_env(cls, env=None) -> "LiveCredentials":
        key = _env("TPB_POLY_PRIVATE_KEY", "XSHARK_POLY_PRIVATE_KEY", env=env)
        if not key:
            raise LiveConfigError("TPB_POLY_PRIVATE_KEY is not set (in /root/xshark/.env)")
        wallet = _env("TPB_POLY_WALLET", "TPB_POLY_FUNDER", "XSHARK_POLY_WALLET", env=env)
        if wallet is not None and not (wallet.startswith("0x") and len(wallet) == 42):
            raise LiveConfigError("TPB_POLY_WALLET must be the 0x-prefixed 40-hex-character account address")
        rk = _env("TPB_POLY_RELAYER_API_KEY", "XSHARK_POLY_RELAYER_API_KEY", env=env)
        ra = _env("TPB_POLY_RELAYER_API_KEY_ADDRESS", "XSHARK_POLY_RELAYER_API_KEY_ADDRESS", env=env)
        if bool(rk) != bool(ra):
            raise LiveConfigError("TPB_POLY_RELAYER_API_KEY and TPB_POLY_RELAYER_API_KEY_ADDRESS go together")
        return cls(private_key=key, wallet=wallet,
                   api_key=_env("TPB_POLY_API_KEY", "XSHARK_POLY_API_KEY", env=env),
                   api_secret=_env("TPB_POLY_API_SECRET", "XSHARK_POLY_API_SECRET", env=env),
                   api_passphrase=_env("TPB_POLY_API_PASSPHRASE", "XSHARK_POLY_API_PASSPHRASE", env=env),
                   relayer_key=rk, relayer_address=ra)

    def build_client(self):
        from polymarket import ApiKeyCreds, RelayerApiKey, SecureClient
        creds = None
        if self.api_key and self.api_secret and self.api_passphrase:
            creds = ApiKeyCreds.model_validate(
                {"apiKey": self.api_key, "secret": self.api_secret, "passphrase": self.api_passphrase})
        relayer = RelayerApiKey(key=self.relayer_key, address=self.relayer_address) if self.relayer_key else None
        return SecureClient.create(private_key=self.private_key, wallet=self.wallet,
                                   credentials=creds, api_key=relayer)


def armed_ok(env=None) -> bool:
    return _env("TPB_LIVE_ACK", "XSHARK_LIVE_ACK", env=env) == ACK_PHRASE


def parse_balance(resp: Any) -> tuple[float, float | None]:
    if hasattr(resp, "balance") and hasattr(resp, "allowances"):
        bal = float(resp.balance or 0) / USDC_DECIMALS
        vals = [float(v or 0) for v in dict(resp.allowances or {}).values()]
        return bal, (max(vals) / USDC_DECIMALS if vals else None)
    if isinstance(resp, dict):
        bal = float(resp.get("balance") or 0) / USDC_DECIMALS
        raw = resp.get("allowances")
        allow = max(float(v or 0) for v in raw.values()) / USDC_DECIMALS if isinstance(raw, dict) and raw else None
        return bal, allow
    raise LiveConfigError(f"unexpected balance reply: {resp!r}")


def _num(v) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _exc_is(exc: Exception, *names: str) -> bool:
    return any(c.__name__ in names for c in type(exc).__mro__)


def is_kill(exc: Exception) -> bool:
    """A fill-and-kill the venue killed, raised as an exception by the SDK.
    It is the order's normal other outcome, never an API fault."""
    if _exc_is(exc, "InsufficientLiquidityError"):
        return True
    if not _exc_is(exc, "RequestRejectedError"):
        return False
    if str(getattr(exc, "code", "") or "").lower() in UNFILLED_CODES:
        return True
    msg = str(exc).lower()
    return any(s in msg for s in ("fully filled", "fok", "fak", "unmatched", "no orders found to match", "no match"))


class LiveVenue(PaperVenue):
    """PaperVenue's surface; in SHADOW it simulates and signs, ARMED it trades."""

    def __init__(self, books, clock, settings, cash: float, on_fill=None, calibration=None, *,
                 armed: bool, caps: LiveCaps, kill_path: str, client=None,
                 day_of: Callable[[float], str] | None = None) -> None:
        super().__init__(books, clock, settings, cash, on_fill=on_fill, calibration=calibration)
        self.armed = armed
        self.caps = caps
        self.kill_path = kill_path
        self.client = client
        self.creds: LiveCredentials | None = None
        self.venue_balance: float | None = None
        self.allowance: float | None = None
        self.queue: deque = deque()                    # ("post" | "sign" | "cancel", order)
        self.venue_id: dict[int, str] = {}             # our order id -> venue order id (resting bids)
        self.unreadable: Counter = Counter()          # our order id -> polls the SDK could not parse
        self.last_fill_ms: dict[str, float] = {}      # slug -> when a venue fill last changed the position
        self.slug_orders: dict[str, dict[str, Order]] = {}   # slug -> venue order id -> our order
        self.reconcile_due: dict[str, float] = {}     # slug -> monotonic time to check it against the venue
        self.reconciled: list[dict] = []              # what the venue check corrected (console)
        # every source of fill news (POST reply, account websocket, status checks, trade list)
        # reports the TOTAL matched for one of our venue order ids; only the new part is booked
        self.vid_order: dict[str, Order] = {}
        self.vid_matched: dict[str, float] = {}
        self.ws_unclaimed: dict[str, tuple[float, float | None, bool, float]] = {}
        self.ws_trade_sum: dict[str, float] = {}
        self.ws_trades_seen: set[str] = set()
        self.ws_state = "off"
        self.ws_events: Counter = Counter()
        self._ws_task: asyncio.Task | None = None
        self.cancel_wanted: set[int] = set()
        self.sign_ms: deque = deque(maxlen=500)
        self.rtt_ms: deque = deque(maxlen=500)
        self.stats: Counter = Counter()
        self.sent_times: deque = deque()
        self.last_error = ""
        self.halted: str | None = None
        self.consecutive_errors = 0
        self.realised_today = 0.0
        self.day_of = day_of or (lambda ts: time.strftime("%Y-%m-%d", time.gmtime(ts + 9 * 3600)))
        self._day = self.day_of(time.time())
        self.prewarmed: set[str] = set()
        self.redeem_pending: dict[str, tuple[int, float]] = {}
        self.redeemed: list[str] = []
        # account sync: winnings booked at settlement that the venue pays only when the redeem
        # goes through (saved across restarts), and when the balance / the ledger last moved
        self.unredeemed: dict[str, float] = {}       # condition id -> payout
        self.redeem_path = os.path.join(os.path.dirname(kill_path), "redeem.json")
        self.balance_read_at = 0.0
        self.ledger_moved_at = 0.0
        self._last_poll = 0.0
        self._last_reconcile = 0.0
        self._last_warm = 0.0
        self._wake: asyncio.Event | None = None

    # ----------------------------------------------------------- setup

    @property
    def mode(self) -> str:
        return "LIVE-ARMED" if self.armed else "SHADOW"

    def connect(self) -> dict:
        """Log in and read the account (blocking: call from a thread at startup)."""
        if self.client is None:
            self.creds = LiveCredentials.from_env()
            self.client = self.creds.build_client()
        t0 = time.monotonic()
        self.venue_balance, self.allowance = parse_balance(self.client.get_balance_allowance(asset_type="COLLATERAL"))
        self.balance_read_at = t0
        self._last_reconcile = self._last_warm = time.monotonic()
        if self.armed:
            if self.allowance is not None and self.allowance < min(self.venue_balance, 1.0):
                raise LiveConfigError(f"allowance {self.allowance:.2f} USDC is below the balance; trade once "
                                      "through the Polymarket app to set it")
            if self.venue_balance < 1.0:
                raise LiveConfigError(f"the account holds only {self.venue_balance:.2f} USDC; nothing to trade with")
            # the ledger is brought to the account by the bot's account sync (Bot._sync_account),
            # which books the difference as money from outside the bot
            self._restore_unredeemed()
            # orders left resting by an earlier run are not in our book: take them off
            try:
                self.client.cancel_all()
            except Exception as exc:                  # noqa: BLE001
                log.warning("cancel_all at startup failed: %s", str(exc)[:120])
        log.info("%s: logged in, venue balance %.2f USDC, allowance %s, wallet type %s", self.mode,
                 self.venue_balance, "n/a" if self.allowance is None else f"{self.allowance:.2f}",
                 getattr(self.client, "wallet_type", "?"))
        return self.snapshot()

    # ----------------------------------------------------------- strategy surface

    def submit(self, market, outcome, kind, price, size, fair=None, tag=""):
        if not self.armed:
            o = super().submit(market, outcome, kind, price, size, fair, tag)
            self._enqueue("sign", o)                  # prove the real signing path; nothing is posted
            return o
        o = super().submit(market, outcome, kind, price, size, fair, tag)
        o.arrive_ms = o.created_ms                    # set to the venue's answer when it comes
        self._enqueue("post", o)
        return o

    def prewarm(self, market) -> None:
        """Sign a throwaway order per new token while idle: the SDK fetches the
        market's tick size and exchange the first time, off the critical path.
        Nothing is posted."""
        for tok in market.tokens:
            if tok and tok not in self.prewarmed:
                self.prewarmed.add(tok)
                self.queue.append(("prewarm", tok))

    def cancel(self, order_id: int) -> None:
        if not self.armed:
            return super().cancel(order_id)
        o = self.orders.get(order_id)
        if o is None or o.state == "done" or o.cancel_at is not None:
            return
        o.cancel_at = self.clock()
        self.counts.setdefault(o.slug, Counter())["cancels"] += 1
        if o.id in self.venue_id:
            self._enqueue("cancel", o)
        else:
            self.cancel_wanted.add(o.id)              # not posted yet: the worker will drop it

    def tick(self) -> None:
        if not self.armed:
            return super().tick()
        now = self.clock()
        for o in list(self.active.values()):
            if o.state == "sent" and now / 1000.0 >= o.end_s and o.id not in self.venue_id:
                self._finish(o, "EXPIRED", "arrived after the close")

    def next_event_ms(self):
        return None if self.armed else super().next_event_ms()

    def on_book(self, token: str) -> None:
        if not self.armed:
            super().on_book(token)

    def on_trade(self, t) -> None:
        if not self.armed:
            super().on_trade(t)

    def settle(self, slug: str, winner: str) -> dict:
        res = super().settle(slug, winner)
        day = self.day_of(time.time())
        if day != self._day:
            self._day, self.realised_today = day, 0.0
        if self.armed:
            self.realised_today += res["pnl"]
            if -self.realised_today >= self.caps.max_daily_loss_usdc and not self.halted:
                self.halt(f"daily loss {self.realised_today:+.2f} reached the {self.caps.max_daily_loss_usdc:.2f} cap")
            m = self.markets.get(slug)
            if res["payout"] > EPS and m is not None and m.condition_id and self.creds and self.creds.relayer_key:
                self.redeem_pending[m.condition_id] = (0, time.monotonic() + 20.0)
                self.unredeemed[m.condition_id] = self.unredeemed.get(m.condition_id, 0.0) + res["payout"]
                self._save_unredeemed()
        self.ledger_moved_at = time.monotonic()
        return res

    # ----------------------------------------------------------- account sync

    def _restore_unredeemed(self) -> None:
        """Winnings an earlier run booked but never redeemed: redeem them now."""
        try:
            with open(self.redeem_path) as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return
        if not (self.creds and self.creds.relayer_key):
            return
        for cid, payout in (saved or {}).items():
            self.unredeemed[cid] = float(payout)
            self.redeem_pending.setdefault(cid, (0, time.monotonic() + 20.0))
        if self.unredeemed:
            log.info("%d unredeemed win(s) from the last run ($%.2f): redeeming", len(self.unredeemed),
                     sum(self.unredeemed.values()))

    def _save_unredeemed(self) -> None:
        tmp = self.redeem_path + ".tmp"
        with contextlib.suppress(OSError):
            with open(tmp, "w") as f:
                json.dump(self.unredeemed, f)
            os.replace(tmp, self.redeem_path)

    def _redeem_done(self, condition_id: str) -> None:
        self.redeem_pending.pop(condition_id, None)
        if self.unredeemed.pop(condition_id, None) is not None:
            self._save_unredeemed()
        self.ledger_moved_at = time.monotonic()

    def account_gap(self) -> float | None:
        """What the account holds minus the ledger's cash: money moved outside the bot. None
        unless the last balance read is a fair comparison: armed, no order open or queued, no
        win waiting for its redeem (the venue may pay it before our redeem or only after it:
        seen 2026-10-06, paid ~1 min after the close, 5 min before our redeem), and read
        SYNC_QUIET_S after the ledger last moved (fill, settlement, redeem)."""
        if not self.armed or self.venue_balance is None:
            return None
        if self.queue or self.reconcile_due or self.unredeemed or self.open_orders():
            return None
        if self.balance_read_at < self.ledger_moved_at + SYNC_QUIET_S:
            return None
        return self.venue_balance - self.cash

    # ----------------------------------------------------------- worker

    def _enqueue(self, op: str, o: Order) -> None:
        self.queue.append((op, o))
        if self._wake is not None:
            self._wake.set()

    async def run(self, stop: asyncio.Event) -> None:
        self._wake = asyncio.Event()
        if self.armed and self.creds is not None:
            self._ws_task = asyncio.create_task(self._user_stream(stop))
        while not stop.is_set():
            try:
                if self.queue:
                    op, o = self.queue.popleft()
                    if op == "sign":
                        await asyncio.to_thread(self._sign_only, o)
                    elif op == "post":
                        why = self._refusal(o)        # the last look, on the loop, just before the POST
                        res = {"refused": why} if why else await asyncio.to_thread(self._post_sync, o)
                        self._apply_post(o, res)
                    elif op == "prewarm":
                        await asyncio.to_thread(self._prewarm_sync, o)
                    elif op == "cancel":
                        if o.id not in self.venue_id:
                            continue                  # already finished (a fill closed it first)
                        res = await asyncio.to_thread(self._cancel_sync, o)
                        self._apply_poll(o, res, cancelled=True)
                    continue
                now = time.monotonic()
                poll_every = POLL_S_WS if self.ws_state == "up" else POLL_S
                if self.armed and self.venue_id and now - self._last_poll >= poll_every:
                    self._last_poll = now
                    for oid in list(self.venue_id):
                        o = self.orders.get(oid)
                        if o is not None:
                            self._apply_poll(o, await asyncio.to_thread(self._poll_sync, o))
                    continue
                if now - self._last_reconcile >= RECONCILE_S:
                    self._last_reconcile = now
                    await asyncio.to_thread(self._reconcile_sync)
                    continue
                rdue = next((s for s, t in self.reconcile_due.items() if t <= now), None)
                if rdue is not None:
                    self.reconcile_due.pop(rdue, None)
                    try:
                        _, per_outcome = await asyncio.to_thread(self._venue_trades_sync, rdue)
                        self._apply_window_reconcile(rdue, per_outcome)
                    except Exception as exc:          # noqa: BLE001
                        log.warning("window check %s failed: %s", rdue[-10:], str(exc)[:120])
                    continue
                due = next((c for c, (_, t) in self.redeem_pending.items() if t <= now), None)
                if due is not None:
                    await asyncio.to_thread(self._redeem_sync, due)
                    continue
                if now - self._last_warm >= KEEP_WARM_S:
                    self._last_warm = now
                    await asyncio.to_thread(self._keep_warm)
                    continue
            except Exception:                         # noqa: BLE001 -- the worker must survive
                log.exception("live worker step failed")
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), 0.05)
        if self._ws_task is not None:
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._ws_task
        if self.armed and self.venue_id:
            log.info("stopping: cancelling %d resting order(s)", len(self.venue_id))
            for oid in list(self.venue_id):
                o = self.orders.get(oid)
                if o is not None:
                    with contextlib.suppress(Exception):
                        self._apply_poll(o, await asyncio.to_thread(self._cancel_sync, o), cancelled=True)

    # ----------------------------------------------------------- blocking venue calls (threads)

    def _sign(self, o: Order):
        price = round(o.price, 3)
        ptxt = f"{price:.3f}" if abs(price * 100 - round(price * 100)) > 1e-6 else f"{price:.2f}"
        size = math.floor(round(o.size * 100.0, 6)) / 100.0
        # Clips are LIMIT orders for an exact share count, not fill-and-kill market orders: a
        # market BUY spends dollars, so a cheaper fill bought extra shares (5 x limit 0.45 =
        # $2.25 filled at 0.35 gave 6.43 shares and broke the pair under the share cap). A
        # limit BUY for 5 shares at <= the limit matches the same asks and gets exactly 5;
        # what does not match at once is cancelled straight away (_apply_post).
        # Bids stay post-only. Both carry the venue-side expiry as the crash backstop.
        return self.client.create_limit_order(token_id=o.token, price=ptxt, size=f"{size:.2f}",
                                              side="SELL" if o.tag == "sell" else "BUY",
                                              post_only=(o.kind == "maker"),
                                              expiration=int(time.time()) + RESTING_BACKSTOP_S)

    def _sign_only(self, o: Order) -> None:
        t0 = time.perf_counter()
        try:
            self._sign(o)
        except Exception as exc:                      # noqa: BLE001
            self.stats["sign_errors"] += 1
            self.last_error = f"sign: {type(exc).__name__}: {exc}"[:200]
            log.warning("SHADOW sign failed for %s %s: %s", o.kind, o.outcome, self.last_error)
            return
        self.sign_ms.append((time.perf_counter() - t0) * 1000.0)
        self.stats["signed"] += 1
        log.info("SHADOW signed, not sent: %s BUY %s %.0f sh @ %.2f", o.kind, o.outcome, o.size, o.price)

    def _refusal(self, o: Order) -> str | None:
        """The last look before a POST."""
        if o.tag == "sell":
            # a sell from the console only lowers exposure: no kill file, halt or buy caps apply
            cutoff = time.time() - 3600.0
            while self.sent_times and self.sent_times[0] < cutoff:
                self.sent_times.popleft()
            if len(self.sent_times) >= self.caps.max_orders_per_hour:
                return f"{self.caps.max_orders_per_hour} orders this hour"
            return None
        if os.path.exists(self.kill_path):
            return "kill file"
        if self.halted:
            return f"halted: {self.halted}"
        if o.id in self.cancel_wanted:
            return "cancelled before posting"
        if self.last_fill_ms.get(o.slug, -1.0) >= o.created_ms:
            # decided before a fill changed the position (a requoted pair bid whose old bid
            # filled meanwhile doubled the leg, 2026-10-01 16:26): the strategy decides again
            return "position changed since the decision"
        notional = o.price * o.size
        if notional > self.caps.max_order_usdc + 1e-9:
            return f"order ${notional:.2f} over the ${self.caps.max_order_usdc:.2f} cap"
        open_usdc = sum(p.cost for pos in self.pos.values() for p in pos.values()) \
            + sum(x.price * x.remaining for x in self.active.values() if x.id in self.venue_id)
        if open_usdc + notional > self.caps.max_open_usdc + 1e-9:
            return f"open ${open_usdc:.2f} + ${notional:.2f} over the ${self.caps.max_open_usdc:.2f} cap"
        cutoff = time.time() - 3600.0
        while self.sent_times and self.sent_times[0] < cutoff:
            self.sent_times.popleft()
        if len(self.sent_times) >= self.caps.max_orders_per_hour:
            return f"{self.caps.max_orders_per_hour} orders this hour"
        if o.kind == "taker" and self.clock() - o.created_ms > self.caps.max_decision_age_ms:
            return f"decision {self.clock() - o.created_ms:.0f} ms old"
        if o.kind == "taker" and o.price * o.size < 1.0:
            return "under the venue's $1 floor for marketable buys"
        return None

    def spendable(self) -> float:
        """The ledger books a win at settlement, but the venue pays it only when the redeem goes
        through (minutes later): spend no more than the account held at the last balance read."""
        if self.venue_balance is None:
            return self.cash
        return min(self.cash, self.venue_balance)

    def hour_full(self) -> bool:
        """The orders-per-hour cap is reached: the bot pauses instead of re-deciding into refusals."""
        cutoff = time.time() - 3600.0
        while self.sent_times and self.sent_times[0] < cutoff:
            self.sent_times.popleft()
        return len(self.sent_times) >= self.caps.max_orders_per_hour

    def _prewarm_sync(self, token: str) -> None:
        t0 = time.perf_counter()
        try:
            self.client.create_market_order(token_id=token, side="BUY", amount="1.00",
                                            max_price="0.50", order_type="FAK")
        except Exception as exc:                      # noqa: BLE001
            self.stats["prewarm_errors"] += 1
            self.last_error = f"prewarm sign: {type(exc).__name__}: {exc}"[:200]
            log.warning("prewarm sign failed: %s", self.last_error)
            return
        self.stats["prewarmed"] += 1
        log.debug("prewarmed %s... in %.0f ms", token[:8], (time.perf_counter() - t0) * 1000.0)

    def _post_sync(self, o: Order) -> dict:
        t0 = time.perf_counter()
        try:
            signed = self._sign(o)
        except Exception as exc:                      # noqa: BLE001
            if _exc_is(exc, "InsufficientLiquidityError"):
                return {"kill": "nothing at the limit (venue)"}
            return {"error": f"sign: {type(exc).__name__}: {exc}"}
        self.sign_ms.append((time.perf_counter() - t0) * 1000.0)
        self.sent_times.append(time.time())
        t1 = time.perf_counter()
        try:
            resp = self.client.post_order(signed)
        except Exception as exc:                      # noqa: BLE001
            self.rtt_ms.append((time.perf_counter() - t1) * 1000.0)
            text = str(exc).lower()
            if is_kill(exc):
                return {"kill": "nothing at the limit (venue)"}
            if o.kind == "maker" and "post" in text and "cross" in text:
                return {"refused": "post-only: would have crossed"}
            if "invalid amount" in text:
                return {"refused": f"venue refused the size: {str(exc)[:100]}"}
            return {"error": f"{type(exc).__name__}: {exc}"}
        self.rtt_ms.append((time.perf_counter() - t1) * 1000.0)
        if not getattr(resp, "ok", False):
            code = str(getattr(resp, "code", "") or "")
            msg = str(getattr(resp, "message", "") or "")
            if code in UNFILLED_CODES:
                return {"kill": f"venue: {msg or code}"}
            if code in ("not_enough_balance", "market_not_ready", "post_only_would_cross"):
                return {"refused": f"venue: {code} {msg}".strip()}
            return {"error": f"venue rejected: {code}: {msg}"}
        out = {"ok": True, "venue_id": str(getattr(resp, "order_id", "") or ""),
               "status": str(getattr(resp, "status", "") or "").lower()}
        if o.kind == "taker":
            making, taking = _num(getattr(resp, "making_amount", None)), _num(getattr(resp, "taking_amount", None))
            shares, usdc = taking, making            # BUY: making = USDC spent, taking = shares got
            if o.tag == "sell":
                shares, usdc = making, taking        # SELL: making = shares given, taking = USDC got
            if (shares <= EPS or usdc <= EPS) and out["venue_id"] and out["status"] == "matched":
                with contextlib.suppress(Exception):
                    rec = self.client.get_order(order_id=out["venue_id"])
                    shares = _num(getattr(rec, "size_matched", None))
                    usdc = shares * _num(getattr(rec, "price", None))
            out.update(shares=shares, usdc=usdc)
        return out

    def _poll_sync(self, o: Order) -> dict:
        vid = self.venue_id.get(o.id)
        try:
            rec = self.client.get_order(order_id=vid)
        except Exception as exc:                      # noqa: BLE001
            if "404" in str(exc) or "not found" in str(exc).lower():
                return {"gone": True}
            if _exc_is(exc, "UnexpectedResponseError"):
                # the venue answered but the SDK could not parse it (seen right after
                # posting): not a connectivity fault, so it must not count toward the halt
                return {"unreadable": f"{type(exc).__name__}: {exc}"}
            return {"error": f"poll: {type(exc).__name__}: {exc}"}
        return {"matched": _num(getattr(rec, "size_matched", None)),
                "status": str(getattr(rec, "status", "") or "").upper()}

    def _cancel_sync(self, o: Order) -> dict:
        vid = self.venue_id.get(o.id)
        if vid:
            try:
                self.client.cancel_order(order_id=vid)
            except Exception as exc:                  # noqa: BLE001
                log.warning("cancel %s failed (%s); the venue-side expiry still bounds it", vid[:12], str(exc)[:80])
        res = self._poll_sync(o)                      # fills that landed before the cancel
        if "matched" not in res and vid:
            # 404 or a reply the SDK cannot parse -- seen for orders that had FILLED (2026-10-01
            # 17:54 and 18:0x: 12.97 shares bought that the bot never booked). The account's own
            # trade list says how much of this order matched.
            try:
                per_order, _ = self._venue_trades_sync(o.slug)
                res = {"matched": per_order.get(vid, 0.0), "status": "CANCELED", "via": "trades"}
            except Exception as exc:                  # noqa: BLE001
                log.warning("order %s: status unreadable and trade list failed (%s); the window check "
                            "after the close will correct it", vid[:12], str(exc)[:100])
                self.stats["fills_unknown"] += 1
        res["done"] = True
        return res

    def _venue_trades_sync(self, slug: str) -> tuple[dict[str, float], dict[str, list[float]]]:
        """This account's matched shares in one window, from the CLOB's own trade list:
        per venue order id, and per outcome as [shares, usdc]. Only our own orders count."""
        m = self.markets.get(slug)
        ours = self.slug_orders.get(slug, {})
        per_order: dict[str, float] = {}
        per_outcome: dict[str, list[float]] = {}
        if m is None or not m.condition_id or not ours:
            return per_order, per_outcome
        val = lambda x, k: x.get(k) if isinstance(x, dict) else getattr(x, k, None)   # noqa: E731
        for t in self.client.list_account_trades(market=m.condition_id).iter_items():
            if str(val(t, "status") or "").upper() == "FAILED":
                continue
            hits = []
            tid = str(val(t, "taker_order_id") or "")
            if tid in ours:
                hits.append((tid, _num(val(t, "size")), _num(val(t, "price")), ours[tid].outcome))
            for mo in val(t, "maker_orders") or []:
                oid = str(val(mo, "order_id") or "")
                if oid in ours:
                    hits.append((oid, _num(val(mo, "matched_amount")), _num(val(mo, "price")), ours[oid].outcome))
            for oid, size, price, outcome in hits:
                per_order[oid] = per_order.get(oid, 0.0) + size
                acc = per_outcome.setdefault(outcome, [0.0, 0.0])
                sign = -1.0 if ours[oid].tag == "sell" else 1.0
                acc[0] += sign * size
                acc[1] += sign * size * price
        return per_order, per_outcome

    def reconcile_window(self, slug: str) -> None:
        """Called when a window closes: check its fills against the venue a little later."""
        if self.armed and self.slug_orders.get(slug):
            self.reconcile_due[slug] = time.monotonic() + WINDOW_RECONCILE_S

    def _apply_window_reconcile(self, slug: str, per_outcome: dict[str, list[float]]) -> None:
        """Make the ledger hold what the venue says this account bought in the window."""
        m = self.markets.get(slug)
        if m is None:
            return
        pos = self.pos.get(slug, {})
        now = self.clock()
        for outcome in ("Up", "Down"):
            v_sh, v_usdc = per_outcome.get(outcome, [0.0, 0.0])
            p = pos.get(outcome)
            b_sh = p.shares if p else 0.0
            b_usdc = (p.cost - p.fees) if p else 0.0
            diff = v_sh - b_sh
            if abs(diff) <= 0.01:
                continue
            price = min(max((v_usdc - b_usdc) / diff, 0.01), 0.99)
            f = Fill(0, slug, outcome, "maker", price, diff, 0.0, 0.0, now, max(0.0, m.end - now / 1000.0),
                     "reconcile", None, None, None, "reconcile")
            if slug not in self.pos and slug not in self.fills and diff > 0:
                self.markets[slug] = m
            self.cash -= f.cost
            self.ledger_moved_at = time.monotonic()
            self.pos.setdefault(slug, {}).setdefault(outcome, Position()).add(f)
            self.fills.setdefault(slug, []).append(f)
            self.stats["reconciled"] += 1
            row = {"slug": slug, "outcome": outcome, "bot_shares": round(b_sh, 4), "venue_shares": round(v_sh, 4),
                   "booked_shares": round(diff, 4), "price": round(price, 4)}
            self.reconciled.append(row)
            del self.reconciled[:-50]
            log.warning("RECONCILE %s %s: bot had %.2f sh, the venue %.2f sh -> booked %+.2f @ %.3f",
                        slug[-10:], outcome, b_sh, v_sh, diff, price)
            if self.on_fill is not None:
                self.on_fill(f)

    def _reconcile_sync(self) -> None:
        t0 = time.monotonic()
        try:
            self.venue_balance, self.allowance = parse_balance(
                self.client.get_balance_allowance(asset_type="COLLATERAL"))
            self.balance_read_at = t0
            self.consecutive_errors = 0
        except Exception as exc:                      # noqa: BLE001
            self._api_error(f"balance: {type(exc).__name__}: {exc}")

    def _keep_warm(self) -> None:
        """A cheap GET on the order connection: 107 ms cold vs 27 ms warm from this VPS."""
        transport = getattr(getattr(self.client, "_ctx", None), "secure_clob", None)
        get_json = getattr(transport, "get_json", None)
        if callable(get_json):
            with contextlib.suppress(Exception):
                get_json("/time")

    def _redeem_sync(self, condition_id: str) -> None:
        attempts, _ = self.redeem_pending.get(condition_id, (0, 0.0))
        try:
            self.client.redeem_positions(condition_id=condition_id)
        except Exception as exc:                      # noqa: BLE001
            attempts += 1
            if attempts >= REDEEM_MAX_ATTEMPTS:
                self._redeem_done(condition_id)
                log.warning("redeem %s gave up after %d tries (redeem it in the app): %s",
                            condition_id[:10], attempts, str(exc)[:120])
            else:
                self.redeem_pending[condition_id] = (attempts, time.monotonic() + REDEEM_RETRY_S)
            return
        self._redeem_done(condition_id)
        self.redeemed.append(condition_id)
        log.info("REDEEM submitted for %s", condition_id[:10])

    # ----------------------------------------------------------- results (event loop)

    def _api_error(self, text: str) -> None:
        self.consecutive_errors += 1
        self.stats["api_errors"] += 1
        self.last_error = text[:200]
        log.error("venue API error (%d in a row): %s", self.consecutive_errors, self.last_error)
        if self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS and not self.halted:
            self.halt(f"{self.consecutive_errors} consecutive API errors")

    def halt(self, reason: str) -> None:
        self.halted = reason
        log.error("LIVE TRADING HALTED: %s", reason)

    def _apply_post(self, o: Order, res: dict) -> None:
        now = self.clock()
        if o.state == "done":
            return
        o.arrive_ms = now
        if "refused" in res:
            stale = res["refused"] == "position changed since the decision"
            if o.id in self.cancel_wanted or stale:
                self.cancel_wanted.discard(o.id)
                if stale:
                    self.stats["dropped_stale"] += 1
                    log.info("DROPPED %s %s %.0f @ %.2f: decided before the last fill", o.kind, o.outcome,
                             o.size, o.price)
                self._finish(o, "CANCELLED", res["refused"])
                return
            self.stats["refused"] += 1
            log.warning("NOT POSTED %s %s %.0f @ %.2f: %s", o.kind, o.outcome, o.size, o.price, res["refused"])
            self._finish(o, "REJECTED", res["refused"])
            return
        if "kill" in res:
            self.consecutive_errors = 0
            self.stats["killed"] += 1
            self._finish(o, "KILLED", res["kill"])
            return
        if "error" in res:
            self._api_error(res["error"])
            self._finish(o, "KILLED" if o.kind == "taker" else "REJECTED", "api error")
            return
        self.consecutive_errors = 0
        self.stats["posted"] += 1
        vid = res.get("venue_id") or ""
        if vid:
            self.slug_orders.setdefault(o.slug, {})[vid] = o
            self.vid_order[vid] = o
        if o.kind == "taker":
            shares, usdc = res.get("shares", 0.0), res.get("usdc", 0.0)
            if shares > EPS:
                avg = usdc / shares if usdc > EPS else o.price
                if vid:
                    self._record_matched(vid, shares, avg, True, "post")
                else:
                    m = self.markets[o.slug]
                    o.filled = shares
                    self._book_fill(o, -shares if o.tag == "sell" else shares, avg,
                                    m.fees.taker_fee(avg, shares), "sell" if o.tag == "sell" else "take", now)
                    self.stats["taker_fills"] += 1
                    log.info("VENUE FILL taker %s %.2f sh @ %.4f", o.outcome, shares, avg)
                if shares > o.size + 0.01:
                    log.warning("venue gave %.2f shares for a %.2f-share clip", shares, o.size)
            if vid:
                self._claim(vid)                      # websocket news that beat the POST reply
            if o.state == "done":
                return
            if o.remaining <= 0.005 or res.get("status") == "matched" or not vid:
                if o.filled > EPS:
                    self._finish(o, "FILLED" if o.remaining <= 0.005 else "PARTIAL", f"{o.filled:.2f} sh")
                else:
                    self.stats["killed"] += 1
                    self._finish(o, "KILLED", "nothing at the limit (venue)")
                return
            # the unmatched rest of the clip is resting at the limit: take it off first thing
            o.state = "live"
            self.venue_id[o.id] = vid
            self.queue.appendleft(("cancel", o))
            return
        # a resting bid is now on the book
        o.state = "live"
        self.venue_id[o.id] = vid or str(o.id)
        log.info("RESTING bid %s %.0f sh @ %.2f id %s", o.outcome, o.size, o.price, self.venue_id[o.id][:12])
        if vid:
            self._claim(vid)
        if o.state == "done":
            return
        if o.id in self.cancel_wanted:
            self.cancel_wanted.discard(o.id)
            self._enqueue("cancel", o)

    def _apply_poll(self, o: Order, res: dict, cancelled: bool = False) -> None:
        now = self.clock()
        if "error" in res:
            self._api_error(res["error"])
            return
        if "unreadable" in res and not cancelled:
            self.unreadable[o.id] += 1
            n = self.unreadable[o.id]
            log.warning("bid %s status unreadable (%d): %s", self.venue_id.get(o.id, "?")[:12], n,
                        res["unreadable"][:100])
            if n == MAX_UNREADABLE_POLLS and o.cancel_at is None:
                self.cancel(o.id)
            return
        vid = self.venue_id.get(o.id)
        if "matched" in res and vid:
            self._record_matched(vid, res["matched"], None, False,
                                 "trade list" if res.get("via") == "trades" else "status")
        status = res.get("status", "")
        if cancelled or res.get("gone") or status in ("MATCHED", "CANCELLED", "CANCELED", "EXPIRED") \
                or o.remaining <= EPS:
            self.venue_id.pop(o.id, None)
            self.unreadable.pop(o.id, None)
            if o.state != "done":
                if o.kind == "taker":                 # a clip's unmatched rest, now cancelled
                    if o.filled <= EPS:
                        self.stats["killed"] += 1
                        self._finish(o, "KILLED", "nothing at the limit (venue)")
                    else:
                        self._finish(o, "FILLED" if o.remaining <= 0.005 else "PARTIAL", f"{o.filled:.2f} sh")
                else:
                    self._finish(o, "CANCELLED" if o.filled <= EPS else "PARTIAL")

    def _book_fill(self, o: Order, size: float, price: float, fee: float, how: str, now: float) -> None:
        self.last_fill_ms[o.slug] = now
        self.ledger_moved_at = time.monotonic()
        super()._book_fill(o, size, price, fee, how, now)

    # ----------------------------------------------------------- fills: one rule for every source

    def _record_matched(self, vid: str, total: float, price: float | None, taker: bool, source: str) -> None:
        """The venue says ``total`` shares of our order ``vid`` have matched (POST reply, account
        websocket, status check or trade list). Book only what is new -- whether the order is
        still resting or already finished: a fill reported after a cancel is still a fill."""
        o = self.vid_order.get(vid)
        if o is None:
            prev = self.ws_unclaimed.get(vid)
            if prev is None or total > prev[0] + EPS:
                self.ws_unclaimed[vid] = (total, price, taker, time.monotonic())
            return
        have = self.vid_matched.get(vid, 0.0)
        new = min(total - have, o.size + 0.01 - o.filled)
        if new <= 0.005:
            return
        self.vid_matched[vid] = have + new
        px = price if price and price > 0 else o.price
        fee = self.markets[o.slug].fees.taker_fee(px, new) if taker else 0.0
        now = self.clock()
        late = o.state == "done"
        o.filled += new
        if o.tag == "sell":
            self._book_fill(o, -new, px, fee, "sell", now)
        else:
            self._book_fill(o, new, px, fee, "take" if taker else "venue", now)
        self.stats["taker_fills" if taker else "maker_fills"] += 1
        log.info("VENUE FILL %s %s %s %.2f sh @ %.3f (%s%s)", "taker" if taker else "maker", o.kind, o.outcome,
                 new, px, source, ", after the order closed" if late else "")
        if o.state == "live" and o.remaining <= 0.005:
            self.venue_id.pop(o.id, None)
            self.unreadable.pop(o.id, None)
            self._finish(o, "FILLED", f"{o.filled:.2f} sh")

    def _claim(self, vid: str) -> None:
        """Websocket fills for ``vid`` that arrived before the POST reply named it."""
        got = self.ws_unclaimed.pop(vid, None)
        if got is not None:
            total, price, taker, _ = got
            self._record_matched(vid, total, price, taker, "websocket, early")

    def _on_user_event(self, ev) -> None:
        """One event from the account websocket."""
        p = getattr(ev, "payload", None)
        kind = type(ev).__name__
        if p is None:
            return
        self.ws_events[kind] += 1
        val = lambda x, k: x.get(k) if isinstance(x, dict) else getattr(x, k, None)   # noqa: E731
        if "Order" in kind:
            vid = str(val(p, "id") or "")
            total = _num(val(p, "size_matched"))
            if vid and total > EPS:
                self._record_matched(vid, total, _num(val(p, "price")) or None, False, "websocket")
            return
        tid = str(val(p, "id") or "")
        status = str(val(p, "status") or "").upper()
        if status == "FAILED":
            if tid in self.ws_trades_seen:
                log.error("trade %s FAILED on chain after it was booked; the window check will correct it", tid[:12])
            return
        if not tid or tid in self.ws_trades_seen:
            return                                    # MATCHED, then MINED, then CONFIRMED: count it once
        self.ws_trades_seen.add(tid)
        owner = str(val(p, "owner") or "")
        hits = []
        if str(val(p, "trader_side") or "").upper() == "TAKER":
            hits.append((str(val(p, "taker_order_id") or ""), _num(val(p, "size")), _num(val(p, "price")), True))
        for mo in val(p, "maker_orders") or []:
            oid = str(val(mo, "order_id") or "")
            if oid in self.vid_order or (owner and str(val(mo, "owner") or "") == owner):
                hits.append((oid, _num(val(mo, "matched_amount")), _num(val(mo, "price")), False))
        for vid, size, price, taker in hits:
            if vid and size > EPS:
                self.ws_trade_sum[vid] = self.ws_trade_sum.get(vid, 0.0) + size
                self._record_matched(vid, self.ws_trade_sum[vid], price, taker, "websocket")
        if len(self.ws_trades_seen) > 5000:
            self.ws_trades_seen = set(list(self.ws_trades_seen)[-2000:])
        cutoff = time.monotonic() - WS_UNCLAIMED_TTL_S
        for k in [k for k, v in self.ws_unclaimed.items() if v[3] < cutoff]:
            del self.ws_unclaimed[k]

    async def _user_stream(self, stop: asyncio.Event) -> None:
        """The account websocket: every fill of our orders as it happens (the SDK reconnects)."""
        from polymarket import ApiKeyCreds, AsyncSecureClient
        from polymarket.streams import UserSpec
        while not stop.is_set():
            ac = handle = None
            try:
                c = self.creds
                creds = None
                if c.api_key and c.api_secret and c.api_passphrase:
                    creds = ApiKeyCreds.model_validate(
                        {"apiKey": c.api_key, "secret": c.api_secret, "passphrase": c.api_passphrase})
                self.ws_state = "connecting"
                ac = await AsyncSecureClient.create(private_key=c.private_key, wallet=c.wallet, credentials=creds)
                handle = await ac.subscribe(UserSpec())
                self.ws_state = "up"
                log.info("account websocket up: fills now arrive as they happen")
                async for ev in handle:
                    try:
                        self._on_user_event(ev)
                    except Exception:                 # noqa: BLE001 -- one bad event must not stop the feed
                        log.exception("account websocket event failed")
                    if stop.is_set():
                        break
                self.ws_state = "down"
            except asyncio.CancelledError:
                raise
            except Exception as exc:                  # noqa: BLE001
                self.ws_state = "down"
                log.warning("account websocket down (%s: %s); order-status checks every %.0f s meanwhile",
                            type(exc).__name__, str(exc)[:120], POLL_S)
            finally:
                for x in (handle, ac):
                    if x is not None:
                        with contextlib.suppress(Exception):
                            await x.close()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), 5.0)
        self.ws_state = "off"

    # ----------------------------------------------------------- console

    def snapshot(self) -> dict:
        def q(xs: deque, p: float):
            try:
                v = sorted(xs)                        # the worker thread may append meanwhile
            except RuntimeError:
                return None
            return round(v[int(p * (len(v) - 1))]) if v else None
        return {"mode": self.mode, "armed": self.armed, "venue_balance": self.venue_balance,
                "allowance": self.allowance, "wallet_type": str(getattr(self.client, "wallet_type", "") or ""),
                "halted": self.halted, "last_error": self.last_error or None, "stats": dict(self.stats),
                "resting": len(self.venue_id), "sign_p50_ms": q(self.sign_ms, 0.5),
                "sign_p90_ms": q(self.sign_ms, 0.9), "rtt_p50_ms": q(self.rtt_ms, 0.5),
                "rtt_p90_ms": q(self.rtt_ms, 0.9),
                "caps": {"max_order_usdc": self.caps.max_order_usdc, "max_open_usdc": self.caps.max_open_usdc,
                         "max_daily_loss_usdc": self.caps.max_daily_loss_usdc,
                         "max_orders_per_hour": self.caps.max_orders_per_hour},
                "realised_today": round(self.realised_today, 4), "redeem_pending": len(self.redeem_pending),
                "unredeemed": round(sum(self.unredeemed.values()), 4),
                "redeemed": len(self.redeemed), "kill_file": os.path.exists(self.kill_path),
                "reconciled": self.reconciled[-10:], "user_ws": self.ws_state,
                "user_ws_events": dict(self.ws_events)}
