"""A window's chart starts a minute before its open: the next window's book is sampled then and handed over."""
import types

from xshark.bot import Bot


def _bot(markets):
    book = types.SimpleNamespace(best_bid=lambda: 0.49, best_ask=lambda: 0.51)
    o = types.SimpleNamespace(markets=markets, pre_hist={}, spot_px=81000.0, current=None, runs={}, model_hist=[],
                              books=types.SimpleNamespace(book=lambda tok: book))
    o._sample_next = lambda now: Bot._sample_next(o, now)
    return o


def test_the_minute_before_the_open_is_sampled_and_becomes_the_start_of_the_chart():
    m = types.SimpleNamespace(slug="btc-updown-5m-600", question="q", start=600, end=900, up_token="u")
    o = _bot({600: m})
    Bot._sample_next(o, 530_000.0)                       # 70 s before the open: not yet
    assert o.pre_hist == {}
    Bot._sample_next(o, 545_000.0)
    Bot._sample_next(o, 599_750.0)
    rows = o.pre_hist[600]
    assert [r[0] for r in rows] == [545_000, 599_750] and rows[0][1:3] == [0.49, 0.51]
    assert rows[0][3] is None and rows[0][5] == 81000.0  # no fair value before the strike; the price is there
    Bot._roll(o, 600_100.0)                              # the open: the window's chart begins with that minute
    assert o.current.hist == rows and o.pre_hist == {}


def test_a_window_closing_with_a_loss_over_the_limit_makes_the_next_one_sit_out():
    from xshark.triangle import TriParams
    m = types.SimpleNamespace(slug="btc-updown-5m-600", start=600, end=900)
    run = types.SimpleNamespace(m=m, hist=[[899_500, 0.02, 0.03, None, None, 1.0, None, None]])   # Down leads at the close
    pos = {"if_up": 1.45, "if_down": -2.70}
    o = types.SimpleNamespace(tri=TriParams(skip_after_loss=2.0), skip=None,
                              venue=types.SimpleNamespace(pos={m.slug: {}}, position=lambda slug: pos))
    Bot._skip_check(o, run)
    assert o.skip == (900, -2.70)                                       # the 900 window trades nothing
    o.skip, pos["if_down"] = None, -1.20
    Bot._skip_check(o, run)
    assert o.skip is None                                               # a $1.20 loss: no skip


def test_a_chart_point_on_every_change_of_the_up_best_bid_ask_at_most_every_50_ms():
    top = {"b": 0.50, "a": 0.51}
    book = types.SimpleNamespace(best_bid=lambda: top["b"], best_ask=lambda: top["a"])
    clock = {"ms": 1000.0}
    m = types.SimpleNamespace(slug="s", up_token="u")
    o = types.SimpleNamespace(current=types.SimpleNamespace(m=m), _last_hist=0.0, _last_top=(0.50, 0.51),
                              clock=lambda: clock["ms"], books=types.SimpleNamespace(book=lambda tok: book),
                              venue=types.SimpleNamespace(on_book=lambda tok: None))
    taken = []
    o._sample = lambda now: (taken.append(now), setattr(o, "_last_top", (top["b"], top["a"])))
    Bot._on_book(o, "u")
    assert taken == []                                     # no change: no point
    top["a"] = 0.52
    clock["ms"] = 1030.0
    Bot._on_book(o, "u")
    assert taken == [1030.0]                               # changed: a point
    top["b"] = 0.51
    clock["ms"] = 1060.0
    Bot._on_book(o, "u")
    assert taken == [1030.0]                               # changed again 30 ms later: waits for 50 ms
    clock["ms"] = 1085.0
    Bot._on_book(o, "u")
    assert taken == [1030.0, 1085.0]
    Bot._on_book(o, "d")
    assert len(taken) == 2                                 # the Down token's messages do not count
