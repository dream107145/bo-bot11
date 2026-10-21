"""The paper venue's matching rules against hand-built books and prints."""
import pytest

from xshark.book import Book, Trade
from xshark.config import Settings
from xshark.market import FeeSchedule, Market
from xshark.paper import PaperVenue

M = Market(slug="btc-updown-5m-1790783100", condition_id="0xc", question="q", start=1790783100,
           end=1790783400, up_token="UP", down_token="DN", fees=FeeSchedule())


class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t


class Books:
    """Stands in for BookFeed: mirrored Up/Down books from one set of Up levels."""
    def __init__(self):
        self.b = {"UP": Book("UP"), "DN": Book("DN")}

    def set_up(self, bids: dict, asks: dict):
        self.b["UP"].bids, self.b["UP"].asks = dict(bids), dict(asks)
        self.b["DN"].bids = {round(1 - p, 4): s for p, s in asks.items()}
        self.b["DN"].asks = {round(1 - p, 4): s for p, s in bids.items()}
        for bk in self.b.values():
            bk.vbid = bk.vask = None

    def book(self, token):
        return self.b.get(token)


@pytest.fixture
def env():
    clock = Clock((M.start + 60) * 1000.0)
    books = Books()
    books.set_up({0.59: 100.0, 0.58: 50.0}, {0.61: 30.0, 0.62: 40.0})
    st = Settings()
    st.paper_latency_ms = 100
    st.paper_cancel_latency_ms = 100
    fills = []
    v = PaperVenue(books, clock, st, cash=100.0, on_fill=fills.append)
    return clock, books, v, fills


def test_taker_clip_arrives_after_latency_and_walks_the_asks(env):
    clock, books, v, fills = env
    o = v.submit(M, "Up", "taker", 0.62, 50.0)
    v.tick()
    assert not fills and o.state == "sent"
    clock.t += 100
    v.tick()
    assert o.result == "FILLED" and len(fills) == 1
    f = fills[0]
    assert f.size == 50.0 and f.price == pytest.approx((30 * 0.61 + 20 * 0.62) / 50)
    assert f.fee == pytest.approx(M.fees.taker_fee(0.61, 30) + M.fees.taker_fee(0.62, 20))
    assert v.cash == pytest.approx(100.0 - f.cost)


def test_fill_and_kill_drops_what_is_not_there_and_shares_are_not_bought_twice(env):
    clock, books, v, fills = env
    a = v.submit(M, "Up", "taker", 0.61, 20.0)
    b = v.submit(M, "Up", "taker", 0.61, 20.0)
    clock.t += 100
    v.tick()
    assert a.result == "FILLED" and a.filled == 20
    assert b.result == "PARTIAL" and b.filled == pytest.approx(10.0)    # only 30 at 0.61


def test_nothing_at_the_limit_is_a_kill(env):
    clock, books, v, fills = env
    o = v.submit(M, "Up", "taker", 0.60, 10.0)
    clock.t += 100
    v.tick()
    assert o.result == "KILLED" and not fills


def test_post_only_bid_that_would_cross_is_rejected(env):
    clock, books, v, fills = env
    o = v.submit(M, "Up", "maker", 0.61, 5.0)
    clock.t += 100
    v.tick()
    assert o.result == "REJECTED" and not fills


def test_bid_joins_the_back_of_the_queue(env):
    clock, books, v, fills = env
    o = v.submit(M, "Up", "maker", 0.59, 5.0)
    clock.t += 100
    v.tick()
    assert o.state == "live" and o.queue_ahead == 100.0
    v.on_trade(Trade("UP", 0.59, 98.0, "SELL", clock.t, clock.t))
    assert not fills and o.queue_ahead == pytest.approx(2.0)
    v.on_trade(Trade("UP", 0.59, 4.0, "SELL", clock.t, clock.t))
    assert len(fills) == 1 and fills[0].size == pytest.approx(2.0) and fills[0].how == "queue"
    assert fills[0].fee == 0.0 and fills[0].rebate_est > 0


def test_a_buy_of_the_other_outcome_fills_our_bid(env):
    clock, books, v, fills = env
    o = v.submit(M, "Up", "maker", 0.60, 5.0)          # a new best bid: nothing ahead
    clock.t += 100
    v.tick()
    assert o.queue_ahead == 0.0
    v.on_trade(Trade("DN", 0.40, 3.0, "BUY", clock.t, clock.t))    # mirrored: a sell of Up at 0.60
    assert fills and fills[0].size == 3.0 and fills[0].price == 0.60
    v.on_trade(Trade("DN", 0.45, 10.0, "BUY", clock.t, clock.t))   # a sell of Up at 0.55: went through us
    assert o.result == "FILLED" and fills[-1].how == "sweep" and fills[-1].size == 2.0


def test_prints_that_do_not_reach_our_price_do_nothing(env):
    clock, books, v, fills = env
    v.submit(M, "Up", "maker", 0.58, 5.0)
    clock.t += 100
    v.tick()
    v.on_trade(Trade("UP", 0.59, 50.0, "SELL", clock.t, clock.t))   # above our bid
    v.on_trade(Trade("UP", 0.61, 50.0, "BUY", clock.t, clock.t))    # someone lifting the ask
    v.on_trade(Trade("DN", 0.40, 50.0, "SELL", clock.t, clock.t))   # a sell of Down is not a sell of Up
    assert not fills


def test_the_ask_falling_through_the_bid_fills_it(env):
    clock, books, v, fills = env
    o = v.submit(M, "Up", "maker", 0.58, 5.0)
    clock.t += 100
    v.tick()
    books.set_up({0.57: 10.0}, {0.58: 40.0, 0.60: 5.0})
    v.on_book("UP")
    assert o.result == "FILLED" and fills[0].how == "cross" and fills[0].price == 0.58


def test_a_cancelled_bid_can_still_fill_until_the_cancel_lands(env):
    clock, books, v, fills = env
    o = v.submit(M, "Up", "maker", 0.60, 5.0)
    clock.t += 100
    v.tick()
    v.cancel(o.id)
    clock.t += 50
    v.on_trade(Trade("UP", 0.60, 5.0, "SELL", clock.t, clock.t))
    assert fills and fills[0].size == 5.0
    o2 = v.submit(M, "Down", "maker", 0.38, 5.0)
    clock.t += 100
    v.tick()
    v.cancel(o2.id)
    clock.t += 100
    v.tick()
    assert o2.result == "CANCELLED"
    v.on_trade(Trade("DN", 0.38, 50.0, "SELL", clock.t, clock.t))
    assert all(f.outcome == "Up" for f in fills)


def test_orders_expire_at_the_close(env):
    clock, books, v, fills = env
    o = v.submit(M, "Up", "maker", 0.58, 5.0)
    clock.t = M.end * 1000.0 + 1
    v.tick()
    assert o.state == "done" and o.result in ("EXPIRED", "REJECTED")


def test_settlement_pays_the_winner_and_splits_the_legs(env):
    clock, books, v, fills = env
    t = v.submit(M, "Up", "taker", 0.61, 10.0)
    b = v.submit(M, "Down", "maker", 0.38, 5.0)
    clock.t += 100
    v.tick()
    assert b.queue_ahead == 40.0                 # the Down bid level 0.38 mirrors Up's 0.62 ask (40)
    v.on_trade(Trade("DN", 0.38, 45.0, "SELL", clock.t, clock.t))
    cost_up = fills[0].cost
    cash_before = v.cash
    res = v.settle(M.slug, "Up")
    assert res["payout"] == 10.0
    assert res["taker_pnl"] == pytest.approx(10.0 - cost_up)
    assert res["maker_pnl"] == pytest.approx(-0.38 * 5.0)
    assert res["pnl"] == pytest.approx(res["maker_pnl"] + res["taker_pnl"])
    assert v.cash == pytest.approx(cash_before + 10.0)
    assert M.slug not in v.pos


def test_restart_restores_open_windows_and_their_cost(env):
    clock, books, v, fills = env
    v.submit(M, "Up", "taker", 0.61, 10.0)
    clock.t += 100
    v.tick()
    data = v.export_open()
    from xshark.bot import market_from_dict
    v2 = PaperVenue(books, clock, Settings(), cash=100.0)
    assert v2.import_open(data, market_from_dict) == [M.slug]
    assert v2.cash == pytest.approx(v.cash)
    assert v2.position(M.slug)["Up"]["shares"] == 10.0


class StubCal:
    """A calibration with fixed answers, so the rules are testable."""
    def __init__(self, win: bool, lat: float = 300.0):
        self.win, self.lat, self.seen = win, lat, []
    def latency_ms(self, presigned=False):
        return self.lat
    def win_race(self, depth_ratio, ask_lower):
        self.seen.append((depth_ratio, ask_lower))
        return self.win, 0.3, "depth_ge20"


def _venue(win: bool):
    clock = Clock((M.start + 60) * 1000.0)
    books = Books()
    books.set_up({0.59: 100.0}, {0.61: 30.0, 0.62: 40.0})
    fills = []
    cal = StubCal(win)
    v = PaperVenue(books, clock, Settings(), cash=100.0, on_fill=fills.append, calibration=cal)
    return clock, books, v, fills, cal


def test_real_latency_is_drawn_per_order():
    clock, books, v, fills, cal = _venue(True)
    o = v.submit(M, "Up", "taker", 0.61, 10.0)
    assert o.arrive_ms == clock.t + 300.0
    v.cancel(v.submit(M, "Up", "maker", 0.55, 5.0).id)
    assert all(x.cancel_at is None or x.cancel_at == clock.t + 300.0 for x in v.active.values())


def test_a_clip_that_loses_the_race_is_killed_even_when_the_book_had_it():
    clock, books, v, fills, cal = _venue(False)
    o = v.submit(M, "Up", "taker", 0.61, 10.0)
    clock.t += 300
    v.tick()
    assert o.result == "KILLED" and not fills and v.counts[M.slug]["taker_lost_race"] == 1
    assert cal.seen == [(3.0, False)]          # 30 at the ask / 10 wanted, ask not lower


def test_a_clip_that_wins_the_race_fills_and_records_its_odds():
    clock, books, v, fills, cal = _venue(True)
    o = v.submit(M, "Up", "taker", 0.62, 10.0)
    books.set_up({0.59: 100.0}, {0.60: 50.0})  # the ask came down before arrival
    clock.t += 300
    v.tick()
    assert o.result == "FILLED" and fills[0].race_p == 0.3 and cal.seen == [(5.0, True)]


def test_race_odds_do_not_touch_resting_bids():
    clock, books, v, fills, cal = _venue(False)
    o = v.submit(M, "Up", "maker", 0.60, 5.0)
    clock.t += 300
    v.tick()
    v.on_trade(Trade("UP", 0.60, 5.0, "SELL", clock.t, clock.t))
    assert o.result == "FILLED" and cal.seen == []


def test_calibration_file_and_buckets(tmp_path):
    import json
    from xshark.calibration import Calibration
    p = tmp_path / "cal.json"
    p.write_text(json.dumps({"orders": 100, "real_fill_rate": 0.1, "latency_ms": [250.0] * 60,
                             "race": {"troll-live": {"depth_lt3": {"n": 50, "filled": 5, "rate": 0.1},
                                                     "ask_lower": {"n": 3, "filled": 3, "rate": 1.0}}}}))
    cal = Calibration(str(p), seed=1)
    assert cal.latency_ms() == 250.0
    assert cal.race_prob(2.0, False) == (0.1, "depth_lt3")
    assert cal.race_prob(2.0, True)[0] < 1.0           # a 3-order cell is not trusted: built-in value kept
    assert cal.race_prob(50.0, False)[1] == "depth_ge20"
    none = Calibration(str(tmp_path / "missing.json"), seed=1)
    assert 180 <= none.latency_ms() <= 1500


def test_every_finished_clip_is_kept_with_its_reason():
    clock, books, v, fills, cal = _venue(False)
    v.submit(M, "Up", "taker", 0.61, 10.0)          # the book has it, the race is lost
    v.submit(M, "Up", "taker", 0.55, 10.0)          # nothing at 0.55
    clock.t += 300
    v.tick()
    whys = sorted(c["why"] for c in v.clips[M.slug])
    assert whys == ["lost the race", "nothing at the limit"]
    assert all(c["result"] == "KILLED" and c["latency_ms"] == 300 for c in v.clips[M.slug])


def test_presigned_latency_drops_the_pre_send_part(tmp_path):
    import json
    from xshark.calibration import Calibration
    p = tmp_path / "cal.json"
    p.write_text(json.dumps({"latency_ms": [280.0] * 60, "latency_split": [[30.0, 250.0]] * 60}))
    cal = Calibration(str(p), seed=1)
    assert cal.latency_ms() == 280.0
    assert cal.latency_ms(presigned=True) == 251.0


def test_a_lost_race_takes_only_the_best_ask_and_fills_deeper_up_to_the_limit(env):
    clock, books, v, fills = env

    class LoseAlways:
        def latency_ms(self, presigned=False): return 100.0
        def win_race(self, ratio, lower): return False, 0.1, "x"

    v.cal = LoseAlways()
    v.s.paper_latency_real = False
    v.s.paper_race = True
    o = v.submit(M, "Up", "taker", 0.62, 10.0)          # asks: 0.61 x 30, 0.62 x 40
    clock.t += 100
    v.tick()
    assert o.result == "FILLED" and fills[-1].price == pytest.approx(0.62) and fills[-1].size == 10.0
    o2 = v.submit(M, "Up", "taker", 0.61, 10.0)         # nothing left past the best ask at this limit
    clock.t += 100
    v.tick()
    assert o2.result == "KILLED"


def test_a_console_sell_hits_the_bids_and_settles_right(env):
    clock, books, v, fills = env                        # Up bids 0.59 x 100, 0.58 x 50
    v.s.paper_race = False
    buy = v.submit(M, "Up", "taker", 0.62, 10.0)
    clock.t += 100
    v.tick()
    cash_after_buy = v.cash
    sell = v.submit(M, "Up", "taker", 0.57, 10.0, None, "sell")
    assert v.reserved() == 0.0                          # a sell commits no money
    clock.t += 100
    v.tick()
    assert sell.result == "FILLED" and fills[-1].size == -10.0 and fills[-1].price == pytest.approx(0.59)
    fee = M.fees.taker_fee(0.59, 10)
    assert v.cash == pytest.approx(cash_after_buy + 5.9 - fee)
    assert v.position(M.slug)["Up"]["shares"] == pytest.approx(0.0)
    res = v.settle(M.slug, "Up")                         # nothing left to pay out
    assert res["payout"] == pytest.approx(0.0)
    assert res["pnl"] == pytest.approx(5.9 - fee - buy_cost(fills[0]))


def buy_cost(f):
    return f.price * f.size + f.fee
