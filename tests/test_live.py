"""The live adapter against a fake SDK client: nothing here reaches Polymarket."""
import asyncio
import types
from types import SimpleNamespace

import pytest

from xshark.config import Settings
from xshark.paper import Order
from xshark.live import ACK_PHRASE, LiveCaps, LiveConfigError, LiveCredentials, LiveVenue, armed_ok, \
    is_kill, parse_balance

from test_paper import M, Books, Clock


class RequestRejectedError(Exception):
    def __init__(self, msg, code=""):
        super().__init__(msg)
        self.code = code


class FakeClient:
    wallet_type = "proxy"

    def __init__(self, balance=40.0, allowance=1e9):
        self.balance, self.allowance = balance, allowance
        self.signed, self.posted, self.cancelled, self.orders = [], [], [], {}
        self.post_reply = None                      # callable(signed) -> reply, or an exception to raise
        self.cancel_all_calls = 0

    def get_balance_allowance(self, *, asset_type):
        return {"balance": str(int(self.balance * 1e6)), "allowances": {"x": str(int(self.allowance * 1e6))}}

    def cancel_all(self):
        self.cancel_all_calls += 1

    def create_market_order(self, **kw):
        self.signed.append(("market", kw))
        return ("market", kw)

    def create_limit_order(self, **kw):
        self.signed.append(("limit", kw))
        return ("limit", kw)

    def post_order(self, signed):
        self.posted.append(signed)
        if isinstance(self.post_reply, Exception):
            raise self.post_reply
        return self.post_reply(signed)

    def get_order(self, *, order_id):
        return self.orders[order_id]

    def cancel_order(self, *, order_id):
        self.cancelled.append(order_id)


def make(armed, client=None, tmp_path=None, **caps):
    clock = Clock((M.start + 60) * 1000.0)
    books = Books()
    books.set_up({0.39: 100.0}, {0.41: 50.0})
    st = Settings()
    st.paper_latency_ms = 100
    st.paper_cancel_latency_ms = 100
    fills = []
    v = LiveVenue(books, clock, st, cash=40.0, on_fill=fills.append, armed=armed,
                  caps=LiveCaps(**caps), kill_path=str(tmp_path / "KILL"), client=client or FakeClient())
    v.connect()
    return clock, books, v, fills


async def drain(v, until, timeout=2.0):
    stop = asyncio.Event()
    task = asyncio.create_task(v.run(stop))
    t0 = asyncio.get_running_loop().time()
    while not until() and asyncio.get_running_loop().time() - t0 < timeout:
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 2.0)


def test_credentials_come_from_env_and_are_checked():
    with pytest.raises(LiveConfigError):
        LiveCredentials.from_env(env={})
    with pytest.raises(LiveConfigError):
        LiveCredentials.from_env(env={"TPB_POLY_PRIVATE_KEY": "k", "TPB_POLY_WALLET": "nope"})
    with pytest.raises(LiveConfigError):
        LiveCredentials.from_env(env={"TPB_POLY_PRIVATE_KEY": "k", "TPB_POLY_RELAYER_API_KEY": "r"})
    c = LiveCredentials.from_env(env={"TPB_POLY_PRIVATE_KEY": "k", "TPB_POLY_FUNDER": "0x" + "a" * 40})
    assert c.wallet == "0x" + "a" * 40 and c.api_key is None
    assert not armed_ok(env={}) and armed_ok(env={"TPB_LIVE_ACK": ACK_PHRASE})


def test_balance_parse_and_kill_detection():
    assert parse_balance({"balance": "12500000", "allowances": {"a": "0", "b": "99000000"}}) == (12.5, 99.0)
    assert is_kill(RequestRejectedError("order couldn't be fully filled. FAK orders are partially filled or killed"))
    assert is_kill(RequestRejectedError("x", code="fak_not_filled"))
    assert not is_kill(RequestRejectedError("not enough balance"))
    assert not is_kill(ValueError("fak"))


def test_shadow_signs_but_never_posts(tmp_path):
    client = FakeClient()
    clock, books, v, fills = make(False, client, tmp_path)
    assert v.mode == "SHADOW" and v.venue_balance == 40.0 and client.cancel_all_calls == 0
    o = v.submit(M, "Up", "taker", 0.41, 5.0)
    asyncio.run(drain(v, lambda: v.stats["signed"] >= 1))
    assert client.posted == [] and v.stats["signed"] == 1
    kind, kw = client.signed[-1]
    assert kind == "limit" and kw["side"] == "BUY" and kw["post_only"] is False
    assert kw["size"] == "5.00" and kw["price"] == "0.41"
    # the fill itself is still the paper simulation
    clock.t += 5000
    v.tick()
    assert o.state == "done" and o.result in ("FILLED", "KILLED")


def test_armed_clip_books_the_venue_fill(tmp_path):
    client = FakeClient()
    client.post_reply = lambda s: SimpleNamespace(ok=True, order_id="0xabc", status="matched",
                                                  making_amount="2.0", taking_amount="5")
    clock, books, v, fills = make(True, client, tmp_path)
    assert client.cancel_all_calls == 1
    o = v.submit(M, "Up", "taker", 0.41, 5.0, fair=0.45)
    v.tick()                                      # the paper clock must not fill it
    assert not fills
    asyncio.run(drain(v, lambda: o.state == "done"))
    assert o.result == "FILLED" and len(fills) == 1
    f = fills[0]
    assert f.size == 5.0 and f.price == pytest.approx(0.40) and f.fee == pytest.approx(M.fees.taker_fee(0.40, 5))
    assert v.cash == pytest.approx(40.0 - f.cost)
    assert v.clips[M.slug][-1]["result"] == "FILLED"


def test_armed_clip_kill_is_not_an_error(tmp_path):
    client = FakeClient()
    client.post_reply = RequestRejectedError("FAK order couldn't be fully filled", code="")
    clock, books, v, fills = make(True, client, tmp_path)
    o = v.submit(M, "Up", "taker", 0.41, 5.0)
    asyncio.run(drain(v, lambda: o.state == "done"))
    assert o.result == "KILLED" and not fills and v.consecutive_errors == 0 and not v.halted


def test_caps_and_kill_file_stop_the_post(tmp_path):
    client = FakeClient()
    client.post_reply = lambda s: SimpleNamespace(ok=True, order_id="x", making_amount="2", taking_amount="5")
    clock, books, v, fills = make(True, client, tmp_path, max_order_usdc=1.5)
    o = v.submit(M, "Up", "taker", 0.41, 5.0)     # $2.05 > $1.50
    asyncio.run(drain(v, lambda: o.state == "done"))
    assert o.result == "REJECTED" and client.posted == []
    v.caps.max_order_usdc = 5.0
    (tmp_path / "KILL").write_text("")
    o2 = v.submit(M, "Up", "taker", 0.41, 5.0)
    asyncio.run(drain(v, lambda: o2.state == "done"))
    assert o2.result == "REJECTED" and client.posted == []


def test_repeated_api_errors_halt(tmp_path):
    client = FakeClient()
    client.post_reply = RuntimeError("502 bad gateway")
    clock, books, v, fills = make(True, client, tmp_path)
    for _ in range(5):
        o = v.submit(M, "Up", "taker", 0.41, 5.0)
        asyncio.run(drain(v, lambda: o.state == "done"))
    assert v.halted and "consecutive" in v.halted


def test_armed_bid_rests_fills_by_poll_and_cancels(tmp_path):
    client = FakeClient()
    client.post_reply = lambda s: SimpleNamespace(ok=True, order_id="0xbid")
    client.orders["0xbid"] = SimpleNamespace(size_matched="0", status="LIVE", price="0.35")
    clock, books, v, fills = make(True, client, tmp_path)

    async def wait(cond):
        for _ in range(300):
            if cond():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("timed out")

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(v.run(stop))
        o = v.submit(M, "Down", "maker", 0.35, 5.0)
        await wait(lambda: o.state == "live")
        kind, kw = client.signed[-1]
        assert kind == "limit" and kw["post_only"] is True and kw["price"] == "0.35" and kw["size"] == "5.00"
        client.orders["0xbid"] = SimpleNamespace(size_matched="2", status="LIVE", price="0.35")
        v._last_poll = 0.0
        await wait(lambda: len(fills) == 1)
        assert fills[0].size == 2.0 and fills[0].kind == "maker" and fills[0].fee == 0.0
        v.cancel(o.id)
        await wait(lambda: o.state == "done")
        assert client.cancelled == ["0xbid"] and o.result == "PARTIAL" and not v.venue_id
        # a second bid still resting at shutdown is taken off the venue
        client.post_reply = lambda s: SimpleNamespace(ok=True, order_id="0xbid2")
        client.orders["0xbid2"] = SimpleNamespace(size_matched="0", status="LIVE", price="0.30")
        clock.t += 50                                # decided after the fill above
        o2 = v.submit(M, "Down", "maker", 0.30, 5.0)
        await wait(lambda: o2.state == "live")
        stop.set()
        await asyncio.wait_for(task, 2.0)
        assert "0xbid2" in client.cancelled and o2.result == "CANCELLED"

    asyncio.run(scenario())


def test_post_only_cross_is_rejected_not_error(tmp_path):
    client = FakeClient()
    client.post_reply = RuntimeError("invalid post-only order: order crosses book")
    clock, books, v, fills = make(True, client, tmp_path)
    o = v.submit(M, "Up", "maker", 0.41, 5.0)
    asyncio.run(drain(v, lambda: o.state == "done"))
    assert o.result == "REJECTED" and v.consecutive_errors == 0


def test_armed_refuses_an_empty_account(tmp_path):
    with pytest.raises(LiveConfigError):
        make(True, FakeClient(balance=0.5), tmp_path)


def test_unreadable_poll_is_not_an_api_error(tmp_path):
    class UnexpectedResponseError(Exception):
        pass

    client = FakeClient()
    client.post_reply = lambda s: SimpleNamespace(ok=True, order_id="0xnew")
    clock, books, v, fills = make(True, client, tmp_path)

    def bad(*, order_id):
        raise UnexpectedResponseError("OpenOrder response did not match expected shape")

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(v.run(stop))
        client.get_order = bad
        o = v.submit(M, "Down", "maker", 0.35, 5.0)
        for _ in range(300):
            if o.state == "live":
                break
            await asyncio.sleep(0.01)
        for _ in range(12):
            v._last_poll = 0.0
            await asyncio.sleep(0.06)
        assert v.consecutive_errors == 0 and not v.halted
        # after MAX_UNREADABLE_POLLS the bid is taken off the venue
        assert client.cancelled == ["0xnew"] and o.state == "done" and not v.venue_id
        stop.set()
        await asyncio.wait_for(task, 2.0)

    asyncio.run(scenario())


def test_clip_rest_is_cancelled_at_once_and_late_fills_count(tmp_path):
    client = FakeClient()
    # 3 of the 5 shares match at once; the other 2 rest at the limit until our cancel
    client.post_reply = lambda s: SimpleNamespace(ok=True, order_id="0xclip", status="live",
                                                  making_amount="1.05", taking_amount="3")
    client.orders["0xclip"] = SimpleNamespace(size_matched="4", status="CANCELED", price="0.41")
    clock, books, v, fills = make(True, client, tmp_path)
    o = v.submit(M, "Up", "taker", 0.41, 5.0)
    asyncio.run(drain(v, lambda: o.state == "done"))
    assert client.cancelled == ["0xclip"]
    assert [round(f.size, 2) for f in fills] == [3.0, 1.0] and o.filled == pytest.approx(4.0)
    assert o.result == "PARTIAL" and not v.venue_id
    assert fills[0].fee == pytest.approx(M.fees.taker_fee(0.35, 3)) and fills[1].fee == 0.0


def test_clip_with_nothing_matched_is_killed_after_the_cancel(tmp_path):
    client = FakeClient()
    client.post_reply = lambda s: SimpleNamespace(ok=True, order_id="0xnone", status="live",
                                                  making_amount="0", taking_amount="0")
    client.orders["0xnone"] = SimpleNamespace(size_matched="0", status="CANCELED", price="0.41")
    clock, books, v, fills = make(True, client, tmp_path)
    o = v.submit(M, "Up", "taker", 0.41, 5.0)
    asyncio.run(drain(v, lambda: o.state == "done"))
    assert o.result == "KILLED" and not fills and client.cancelled == ["0xnone"]
    assert v.clips[M.slug][-1]["why"] == "nothing at the limit (venue)"


def test_post_only_cross_code_is_refused_not_an_error(tmp_path):
    client = FakeClient()
    client.post_reply = lambda s: SimpleNamespace(ok=False, code="post_only_would_cross", message="crosses")
    clock, books, v, fills = make(True, client, tmp_path)
    o = v.submit(M, "Up", "maker", 0.40, 5.0)
    asyncio.run(drain(v, lambda: o.state == "done"))
    assert o.result == "REJECTED" and v.consecutive_errors == 0


def test_bid_decided_before_a_fill_is_dropped_not_posted(tmp_path):
    """16:26 on 2026-10-01: the pair bid was requoted 0.57 -> 0.56, the 0.57 filled while
    its cancel was in flight, and the 0.56 was still posted and filled: Up 10 vs Down 5."""
    client = FakeClient()
    client.post_reply = lambda s: SimpleNamespace(ok=True, order_id="0xold")
    client.orders["0xold"] = SimpleNamespace(size_matched="0", status="LIVE", price="0.57")
    clock, books, v, fills = make(True, client, tmp_path)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(v.run(stop))
        old = v.submit(M, "Up", "maker", 0.37, 5.0)
        for _ in range(300):
            if old.state == "live":
                break
            await asyncio.sleep(0.01)
        # requote: cancel the old bid, decide a new one -- the old one has filled meanwhile
        client.orders["0xold"] = SimpleNamespace(size_matched="5", status="MATCHED", price="0.37")
        clock.t += 10
        v.cancel(old.id)
        new = v.submit(M, "Up", "maker", 0.36, 5.0)
        clock.t += 10
        for _ in range(300):
            if new.state == "done":
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 2.0)
        assert len(fills) == 1 and fills[0].size == 5.0
        assert new.result == "CANCELLED" and len(client.posted) == 1

    asyncio.run(scenario())


class _MO:
    def __init__(self, order_id, matched_amount, price):
        self.order_id, self.matched_amount, self.price = order_id, matched_amount, price


class _Trade:
    def __init__(self, taker_order_id="", size="0", price="0", maker_orders=(), status="CONFIRMED"):
        self.taker_order_id, self.size, self.price = taker_order_id, size, price
        self.maker_orders, self.status = list(maker_orders), status


class _Pages:
    def __init__(self, items):
        self.items = items

    def iter_items(self):
        return iter(self.items)


def test_cancel_with_unreadable_status_takes_fills_from_the_trade_list(tmp_path):
    """2026-10-01 18:0x: a clip's rest filled before the cancel, get_order could not be read,
    and the bot booked it as killed -- 5 shares bought that the ledger never saw."""
    class UnexpectedResponseError(Exception):
        pass

    client = FakeClient()
    client.post_reply = lambda s: SimpleNamespace(ok=True, order_id="0xclip", status="live",
                                                  making_amount="0", taking_amount="0")

    def unreadable(*, order_id):
        raise UnexpectedResponseError("OpenOrder response did not match expected shape")

    client.get_order = unreadable
    client.list_account_trades = lambda **kw: _Pages([
        _Trade(maker_orders=[_MO("0xother", "3", "0.40"), _MO("0xclip", "5", "0.41")])])
    clock, books, v, fills = make(True, client, tmp_path)
    o = v.submit(M, "Up", "taker", 0.41, 5.0)
    asyncio.run(drain(v, lambda: o.state == "done"))
    assert o.result == "FILLED" and len(fills) == 1 and fills[0].size == 5.0


def test_window_check_books_what_the_venue_says_was_bought(tmp_path):
    client = FakeClient()
    clock, books, v, fills = make(True, client, tmp_path)
    v.markets[M.slug] = M
    v.slug_orders[M.slug] = {"0xa": Order(1, M.slug, "Up", "UP", "DN", "maker", 0.69, 5.0, 0, 0, M.end),
                             "0xb": Order(2, M.slug, "Down", "DN", "UP", "taker", 0.25, 5.0, 0, 0, M.end)}
    client.list_account_trades = lambda **kw: _Pages([
        _Trade(taker_order_id="0xb", size="5", price="0.25"),
        _Trade(maker_orders=[_MO("0xa", "5", "0.69")])])
    _, per_outcome = v._venue_trades_sync(M.slug)
    assert per_outcome["Down"] == pytest.approx([5.0, 1.25]) and per_outcome["Up"] == pytest.approx([5.0, 3.45])
    cash0 = v.cash
    v._apply_window_reconcile(M.slug, per_outcome)       # the bot had booked nothing
    pos = v.position(M.slug)
    assert pos["Up"]["shares"] == 5.0 and pos["Down"]["shares"] == 5.0
    assert v.cash == pytest.approx(cash0 - 4.70) and len(v.reconciled) == 2
    v._apply_window_reconcile(M.slug, per_outcome)       # nothing left to correct
    assert len(v.reconciled) == 2


class UserOrderEvent:
    def __init__(self, **payload):
        self.payload = SimpleNamespace(**payload)


class UserTradeEvent:
    def __init__(self, **payload):
        payload.setdefault("maker_orders", [])
        payload.setdefault("owner", "me")
        self.payload = SimpleNamespace(**payload)


def _rest_bid(v, client, vid="0xbid", price=0.35):
    client.post_reply = lambda s: SimpleNamespace(ok=True, order_id=vid, status="live")
    client.orders[vid] = SimpleNamespace(size_matched="0", status="LIVE", price=str(price))
    o = v.submit(M, "Down", "maker", price, 5.0)

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(v.run(stop))
        for _ in range(300):
            if o.state == "live":
                break
            await asyncio.sleep(0.01)
        v.queue.clear()
        stop.set()
        v.venue_id[o.id] = vid                        # keep it resting: the test drives the events
        o.state = "live"
        v.armed = False                               # no cancel-on-stop in this harness
        await asyncio.wait_for(task, 2.0)
        v.armed = True
    asyncio.run(go())
    return o


def test_websocket_fills_are_booked_once_whatever_reports_them(tmp_path):
    client = FakeClient()
    clock, books, v, fills = make(True, client, tmp_path)
    o = _rest_bid(v, client)
    v._on_user_event(UserOrderEvent(id="0xbid", size_matched="3", price="0.35"))
    assert [f.size for f in fills] == [3.0]
    # the same match again from the trade feed, the status check and the trade list: nothing new
    v._on_user_event(UserTradeEvent(id="t1", status="MATCHED", trader_side="MAKER",
                                    maker_orders=[_MO("0xbid", "3", "0.35")]))
    v._apply_poll(o, {"matched": 3.0, "status": "LIVE"})
    assert [f.size for f in fills] == [3.0]
    # MINED / CONFIRMED of the same trade are not new shares
    v._on_user_event(UserTradeEvent(id="t1", status="CONFIRMED", trader_side="MAKER",
                                    maker_orders=[_MO("0xbid", "3", "0.35")]))
    assert len(fills) == 1
    v._on_user_event(UserTradeEvent(id="t2", status="MATCHED", trader_side="MAKER",
                                    maker_orders=[_MO("0xother", "9", "0.35"), _MO("0xbid", "2", "0.35")]))
    assert [f.size for f in fills] == [3.0, 2.0] and o.state == "done" and o.result == "FILLED"
    assert all(f.fee == 0.0 for f in fills)


def test_websocket_fill_before_the_post_reply_is_claimed_once(tmp_path):
    client = FakeClient()
    clock, books, v, fills = make(True, client, tmp_path)
    # the trade feed reports our clip's match before the POST reply has named the order
    v._on_user_event(UserTradeEvent(id="t9", status="MATCHED", trader_side="TAKER", taker_order_id="0xclip",
                                    size="5", price="0.41"))
    assert fills == [] and "0xclip" in v.ws_unclaimed
    client.post_reply = lambda s: SimpleNamespace(ok=True, order_id="0xclip", status="matched",
                                                  making_amount="2.05", taking_amount="5")
    o = v.submit(M, "Up", "taker", 0.41, 5.0)
    asyncio.run(drain(v, lambda: o.state == "done"))
    assert [f.size for f in fills] == [5.0] and o.result == "FILLED" and not v.ws_unclaimed


def test_a_fill_reported_after_the_order_closed_is_still_booked(tmp_path):
    """18:00 on 2026-10-01: the clip's rest filled while it was being cancelled."""
    client = FakeClient()
    client.post_reply = lambda s: SimpleNamespace(ok=True, order_id="0xlate", status="live",
                                                  making_amount="0", taking_amount="0")
    client.orders["0xlate"] = SimpleNamespace(size_matched="0", status="CANCELED", price="0.41")
    clock, books, v, fills = make(True, client, tmp_path)
    o = v.submit(M, "Up", "taker", 0.41, 5.0)
    asyncio.run(drain(v, lambda: o.state == "done"))
    assert o.result == "KILLED" and fills == []
    v._on_user_event(UserOrderEvent(id="0xlate", size_matched="5", price="0.41"))
    assert [f.size for f in fills] == [5.0] and o.filled == 5.0
    assert v.position(M.slug)["Up"]["shares"] == 5.0


def test_sub_cent_bid_is_signed_with_three_decimals(tmp_path):
    client = FakeClient()
    client.post_reply = lambda s: SimpleNamespace(ok=True, order_id="0xsub", status="live")
    clock, books, v, fills = make(True, client, tmp_path)
    o = v.submit(M, "Down", "maker", 0.015, 5.0)
    asyncio.run(drain(v, lambda: o.state == "live"))
    kind, kw = client.signed[-1]
    assert kind == "limit" and kw["price"] == "0.015" and kw["post_only"] is True
    o2 = v.submit(M, "Down", "maker", 0.35, 5.0)
    asyncio.run(drain(v, lambda: o2.state == "live"))
    assert client.signed[-1][1]["price"] == "0.35"


def test_console_sell_is_a_sell_order_and_books_a_negative_fill(tmp_path):
    client = FakeClient()
    client.post_reply = lambda s: SimpleNamespace(ok=True, order_id="0xbuy", status="matched",
                                                  making_amount="2.05", taking_amount="5")
    clock, books, v, fills = make(True, client, tmp_path)
    b = v.submit(M, "Up", "taker", 0.41, 5.0)
    asyncio.run(drain(v, lambda: b.state == "done"))
    # SELL: making = shares given, taking = USDC received
    client.post_reply = lambda s: SimpleNamespace(ok=True, order_id="0xsell", status="matched",
                                                  making_amount="5", taking_amount="1.90")
    (tmp_path / "KILL").write_text("")                  # a sell still goes out while halted
    s_ = v.submit(M, "Up", "taker", 0.37, 5.0, None, "sell")
    asyncio.run(drain(v, lambda: s_.state == "done"))
    kind, kw = client.signed[-1]
    assert kw["side"] == "SELL" and kw["post_only"] is False and kw["price"] == "0.37"
    assert fills[-1].size == -5.0 and fills[-1].price == pytest.approx(0.38)
    assert v.position(M.slug)["Up"]["shares"] == pytest.approx(0.0)


def test_spendable_waits_for_the_redeem(tmp_path):
    _, _, v, _ = make(True, tmp_path=tmp_path)
    v.cash, v.venue_balance = 9.19, 3.18          # a $5 win booked, not yet redeemed
    assert v.spendable() == 3.18
    v.venue_balance = 16.89                       # redeemed (or a deposit): the ledger still rules
    assert v.spendable() == 9.19
    v.venue_balance = None
    assert v.spendable() == 9.19


def test_account_gap_waits_for_a_quiet_read_and_no_pending_redeem(tmp_path):
    _, _, v, _ = make(True, tmp_path=tmp_path)
    v.cash, v.venue_balance = 14.81, 10.94
    v.ledger_moved_at, v.balance_read_at = 100.0, 120.0
    assert v.account_gap() is None                    # read too soon after the ledger moved
    v.balance_read_at = 150.0
    v.unredeemed = {"0xwin": 5.0}                     # a win booked; the venue pays it when it likes
    assert v.account_gap() is None
    v._redeem_done("0xwin")
    assert v.unredeemed == {} and v.account_gap() is None   # the redeem moved the ledger: wait again
    v.balance_read_at = v.ledger_moved_at + 50
    assert v.account_gap() == pytest.approx(10.94 - 14.81)


def test_unredeemed_wins_survive_a_restart(tmp_path):
    _, _, v, _ = make(True, tmp_path=tmp_path)
    v.unredeemed = {"0xwin": 5.0}
    v._save_unredeemed()
    _, _, w, _ = make(True, tmp_path=tmp_path)
    w.creds = types.SimpleNamespace(relayer_key="k")
    w._restore_unredeemed()
    assert w.unredeemed == {"0xwin": 5.0} and "0xwin" in w.redeem_pending
