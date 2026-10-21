from types import SimpleNamespace

from xshark.config import Settings
from xshark.copytrade import TAG, decide, fills_for_market, unseen_fills
from xshark.strategy import Decision, decide as strategy_decide
from test_strategy import M, view as strategy_view


def params(**changes):
    values = dict(copy_enabled=True, copy_max_book_age_ms=1500.0,
                  copy_cutoff_s=10.0, copy_max_spread=0.05, copy_max_price=0.99,
                  copy_max_slip=0.03, copy_limit_over=0.01, copy_max_fill_age_s=10.0,
                  copy_max_attempts=10.0, copy_retry_ms=1000.0)
    values.update(changes)
    return SimpleNamespace(**values)


def view(**changes):
    values = dict(
        market=M,
        books={
            "Up": {"bid": 0.49, "ask": 0.50, "ask_size": 20.0, "asks": [(0.50, 20.0)]},
            "Down": {"bid": 0.49, "ask": 0.50, "ask_size": 20.0, "asks": [(0.50, 20.0)]},
        },
        copy_fill={"timestamp": M.start + 20, "outcome": "Down", "side": "BUY", "size": 5.0},
        copy_done=False,
        copy_attempts=0,
        copy_last_attempt_ms=0.0,
        copy_maker_price=None,
        copy_filled_shares=0.0,
        live=False,
        halted=None,
        now_ms=(M.start + 25) * 1000.0,
        book_age_ms=10.0,
        secs_left=250.0,
        in_flight_taker={},
    )
    values.update(changes)
    return SimpleNamespace(**values)


def test_market_fills_are_sorted_and_preserve_identical_trade_rows():
    rows = [
        {"type": "TRADE", "slug": M.slug, "timestamp": M.start + 24, "outcome": "Up", "size": 8.0},
        {"type": "TRADE", "slug": M.slug, "timestamp": M.start + 19, "outcome": "Down", "size": 13.5},
        {"type": "TRADE", "slug": M.slug, "timestamp": M.start + 19, "outcome": "Down", "size": 13.5},
        {"type": "REDEEM", "slug": M.slug, "timestamp": M.start + 10, "outcome": "Up"},
        {"type": "TRADE", "slug": "eth-updown-5m-1", "timestamp": M.start + 1, "outcome": "Up"},
    ]
    fills = fills_for_market(rows, M.slug, M.start)
    assert [(f["outcome"], f["timestamp"], f["size"]) for f in fills] == [
        ("Down", M.start + 19, 13.5), ("Down", M.start + 19, 13.5), ("Up", M.start + 24, 8.0)]


def test_unseen_fills_deduplicates_repeated_polls_without_dropping_identical_rows():
    same = {"transaction": "0xtx", "timestamp": 1, "asset": "1", "outcome": "Up",
            "side": "BUY", "price": 0.5, "size": 7.0}
    seen = {}
    assert unseen_fills([same, same], seen) == [same, same]
    assert unseen_fills([same, same], seen) == []
    assert unseen_fills([same, same, same], seen) == [same]


def test_copy_uses_a_maker_order_on_a_fill_seen_within_2_seconds():
    first = {"timestamp": M.start + 20, "outcome": "Down", "side": "BUY", "size": 13.5, "price": 0.55}
    d = Decision()
    decide(view(copy_fill=first, now_ms=(M.start + 21) * 1000.0), params(), d)
    assert d.places == [("Down", 0.49, 13.5)]
    assert d.tags["maker:Down"] == TAG


def test_copy_uses_a_maker_order_when_ask_falls_below_leader_fill_price():
    first = {"timestamp": M.start + 20, "outcome": "Down", "side": "BUY", "size": 13.5, "price": 0.55}
    d = Decision()
    decide(view(copy_fill=first, now_ms=(M.start + 23) * 1000.0), params(), d)
    assert d.places == [("Down", 0.49, 13.5)]
    assert d.takes == []
    assert d.tags["maker:Down"] == TAG


def test_copy_retries_an_unfilled_maker_at_the_original_price():
    first = {"timestamp": M.start + 20, "outcome": "Down", "side": "BUY", "size": 13.5, "price": 0.55}
    d = Decision()
    decide(view(
        copy_fill=first,
        copy_attempts=1,
        copy_last_attempt_ms=(M.start + 22) * 1000.0,
        copy_maker_price=0.49,
        now_ms=(M.start + 24) * 1000.0,
        books={
            "Up": {"bid": 0.45, "ask": 0.50, "ask_size": 20.0, "asks": [(0.50, 20.0)]},
            "Down": {"bid": 0.45, "ask": 0.50, "ask_size": 20.0, "asks": [(0.50, 20.0)]},
        },
    ), params(), d)
    assert d.places == [("Down", 0.49, 13.5)]
    assert d.takes == []
    assert d.tags["maker:Down"] == TAG


def test_copy_uses_a_taker_order_when_ask_has_not_fallen_below_leader_fill_price():
    first = {"timestamp": M.start + 20, "outcome": "Down", "side": "BUY", "size": 13.5, "price": 0.45}
    d = Decision()
    decide(view(copy_fill=first, now_ms=(M.start + 23) * 1000.0), params(), d)
    assert d.takes == [("Down", 0.51, 13.5, 0.0)]
    assert d.places == []
    assert d.tags["taker:Down"] == TAG


def test_copy_buys_exact_shares_of_first_fill_outcome():
    first = {"timestamp": M.start + 20, "outcome": "Down", "side": "BUY", "size": 13.5}
    d = Decision()
    decide(view(copy_fill=first, now_ms=(M.start + 23) * 1000.0), params(), d)
    assert d.takes == [("Down", 0.51, 13.5, 0.0)]
    assert d.tags["taker:Down"] == TAG


def test_copy_respects_price_spread_and_fresh_book_limits():
    for v, s in (
        (view(books={"Down": {"bid": 0.40, "ask": 0.50, "ask_size": 10}}), params()),
        (view(books={"Down": {"bid": 0.48, "ask": 0.50, "asks": [(0.50, 10)]}}),
         params(copy_max_spread=0.01)),
        (view(book_age_ms=2000.0), params()),
    ):
        d = Decision()
        decide(v, s, d)
        assert d.takes == []


def test_copy_stops_after_max_attempts_or_in_flight_order():
    exhausted = view(copy_attempts=10)
    inflight = view(in_flight_taker={"Down": 5.0})
    done = Decision()
    decide(exhausted, params(), done)
    assert done.copy_done
    for v in (exhausted, inflight):
        d = Decision()
        decide(v, params(), d)
        assert d.takes == []


def test_copy_keeps_retrying_past_the_old_three_attempt_limit():
    v = view(copy_attempts=4, copy_last_attempt_ms=(M.start + 23) * 1000.0)
    d = Decision()
    decide(v, params(), d)
    assert d.takes == [("Down", 0.51, 5.0, 0.0)]


def test_copy_retries_an_unfilled_order_after_the_retry_delay():
    v = view(copy_attempts=1, copy_last_attempt_ms=(M.start + 23) * 1000.0)
    d = Decision()
    decide(v, params(), d)
    assert d.takes == [("Down", 0.51, 5.0, 0.0)]


def test_copy_waits_out_the_retry_delay():
    v = view(copy_attempts=1, copy_last_attempt_ms=(M.start + 24.5) * 1000.0)
    d = Decision()
    decide(v, params(), d)
    assert d.takes == [] and "waiting before retry" in d.why["taker"]


def test_copy_stops_if_a_partial_fill_leaves_less_than_venue_minimum():
    d = Decision()
    decide(view(copy_filled_shares=2.0), params(), d)
    assert d.takes == [] and d.copy_done


def test_strategy_pipeline_routes_first_fill_to_copy_order():
    market_view = strategy_view(
        copy_fill={"timestamp": M.start + 55, "outcome": "Down", "side": "BUY", "size": 7.0},
        books={"Up": {"bid": 0.58, "ask": 0.60},
               "Down": {"bid": 0.39, "ask": 0.41, "ask_size": 10.0, "asks": [(0.41, 10.0)]}},
    )
    settings = Settings(maker_enabled=False, taker_enabled=False, pair_enabled=False, fav_enabled=False,
                        late_enabled=False, scalp_enabled=False, copy_enabled=True)
    d = strategy_decide(market_view, settings)
    assert d.takes == [("Down", 0.42, 7.0, 0.0)]
    assert d.tags["taker:Down"] == TAG


def test_copy_skips_a_signal_that_the_public_api_reported_too_late():
    v = view(copy_fill={"timestamp": M.start + 20, "outcome": "Down", "size": 5.0})
    v.now_ms = (M.start + 31) * 1000.0
    d = Decision()
    decide(v, params(), d)
    assert d.takes == [] and d.copy_done
    assert d.why["taker"] == "first-fill copy signal is stale; skipped"