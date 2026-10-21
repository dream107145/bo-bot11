"""Parsing, pricing, settings and the console's read paths."""
import json
import os

import pytest

from xshark import config
from xshark.book import Book, BookFeed
from xshark.chainlink import ChainlinkFeed
from xshark.market import FeeSchedule, parse_market, slug_for, window_start
from xshark.pricing import norm_cdf, t4_cdf
from xshark.twap import twap_variance
from xshark.web.server import Console, Jsonl

ROW = {
    "slug": "btc-updown-5m-1790781300", "conditionId": "0xabc", "question": "Bitcoin Up or Down",
    "endDate": "2026-09-30T15:20:00Z",
    # outcomes deliberately in the other order: tokens must follow the LABEL
    "clobTokenIds": "[\"tok-down\", \"tok-up\"]", "outcomes": "[\"Down\", \"Up\"]",
    "orderMinSize": 5, "orderPriceMinTickSize": 0.01, "feesEnabled": True,
    "feeSchedule": {"exponent": 1, "rate": 0.07, "takerOnly": True, "rebateRate": 0.2},
    "cryptoMarketConfig": {"twapLookbackSeconds": 60},
}


def test_market_tokens_follow_the_outcome_label():
    m = parse_market(ROW)
    assert m.up_token == "tok-up" and m.down_token == "tok-down"
    assert m.start == 1790781300 and m.end == 1790781600 and m.min_size == 5 and m.twap_s == 60


def test_market_with_a_wrong_end_date_is_refused():
    assert parse_market({**ROW, "endDate": "2026-09-30T16:20:00Z"}) is None


def test_fee_formula():
    f = FeeSchedule()
    assert f.taker_fee(0.5, 1) == pytest.approx(0.0175)
    assert f.taker_fee(0.95, 100) == pytest.approx(100 * 0.07 * 0.95 * 0.05)
    assert f.maker_rebate_est(0.5, 10) == pytest.approx(0.2 * 0.175)


def test_window_arithmetic():
    assert window_start(1790781359.9) == 1790781300
    assert slug_for(1790781300) == "btc-updown-5m-1790781300"
    assert slug_for(1790781300, "ETH") == "eth-updown-5m-1790781300"


def test_t4_cdf():
    assert t4_cdf(0) == pytest.approx(0.5)
    assert t4_cdf(1.3) == pytest.approx(1 - t4_cdf(-1.3))
    assert t4_cdf(3.0) < 1.0 and t4_cdf(3.0) < norm_cdf(3.0)          # fatter tail than the Gaussian
    xs = [t4_cdf(z / 10) for z in range(-50, 51)]
    assert all(b >= a for a, b in zip(xs, xs[1:]))


def test_twap_variance_is_continuous_at_the_averaging_window():
    assert twap_variance(60.0, 1e-4) == pytest.approx(twap_variance(59.9999, 1e-4), rel=1e-3)
    assert twap_variance(300.0, 1e-4) == pytest.approx(1e-8 * 260)


def test_book_trims_levels_the_venue_says_are_gone():
    b = Book("T")
    b.apply_snapshot({"bids": [{"price": "0.40", "size": "10"}, {"price": "0.45", "size": "5"}],
                      "asks": [{"price": "0.47", "size": "8"}]}, 0.0)
    assert b.best_bid() == 0.45 and b.best_ask() == 0.47 and b.mid() == pytest.approx(0.46)
    b.apply_change({"price": "0.41", "size": "3", "side": "BUY", "best_bid": "0.41", "best_ask": "0.47"}, 1.0, 1.0)
    assert b.best_bid() == 0.41                  # 0.45 left over from a missed delta is ignored
    b.apply_change({"price": "0.47", "size": "0", "side": "SELL", "best_bid": "0.41", "best_ask": "0.48"}, 2.0, 2.0)
    assert b.best_ask() is None


def test_book_feed_handles_messages_and_drops_duplicate_prints():
    trades = []
    feed = BookFeed(lambda: 1000.0, on_trade=trades.append)
    feed.set_tokens(["A", "B"])
    feed._active[1] = (None, frozenset({"A", "B"}))
    snap = {"event_type": "book", "asset_id": "A", "timestamp": "999",
            "bids": [{"price": "0.5", "size": "10"}], "asks": [{"price": "0.52", "size": "4"}]}
    feed._handle(json.dumps(snap), 1)
    feed._handle(json.dumps({"event_type": "price_change", "timestamp": "999", "price_changes": [
        {"asset_id": "A", "price": "0.51", "size": "7", "side": "BUY", "best_bid": "0.51", "best_ask": "0.52"}]}), 1)
    assert feed.book("A").best_bid() == 0.51
    pr = {"event_type": "last_trade_price", "asset_id": "A", "price": "0.52", "size": "2", "side": "BUY",
          "timestamp": "999", "transaction_hash": "0x1"}
    feed._handle(json.dumps(pr), 1)
    feed._handle(json.dumps([pr]), 1)
    assert len(trades) == 1 and trades[0].side == "BUY" and trades[0].size == 2.0


def test_chainlink_prefers_the_official_twap_and_waits_for_it():
    cl = ChainlinkFeed(assets={"BTC"})
    for s in range(100):
        cl.on_message({"topic": "crypto_prices_chainlink",
                       "payload": {"symbol": "btc/usd", "value": 100.0 + s, "timestamp": s * 1000}}, s * 1000 + 1400)
    # no official stream at all: the reconstruction is the answer straight away
    assert cl.twap_at("BTC", 99_000, now_ms=99_500)[1] == "reconstructed"
    cl.on_message({"topic": "crypto_prices_twap_sixty",
                   "payload": {"symbol": "btc/usd", "value": 168.0, "timestamp": 98_000}}, 99_400)
    assert cl.twap_at("BTC", 99_000, now_ms=99_500) == ("wait", "")        # stream alive, tick not in yet
    v, src = cl.twap_at("BTC", 99_000, now_ms=106_000)
    assert src == "reconstructed" and 168 < v < 170                        # mean of 139..199 ~ 169
    cl.on_message({"topic": "crypto_prices_twap_sixty",
                   "payload": {"symbol": "btc/usd", "value": 169.5, "timestamp": 99_000}}, 100_400)
    assert cl.twap_at("BTC", 99_000, now_ms=100_500) == (169.5, "twap60")
    assert cl.on_message({"topic": "crypto_prices_chainlink",
                          "payload": {"symbol": "eth/usd", "value": 1.0, "timestamp": 1}}, 1) is None


def test_settings_are_whitelisted_and_clamped():
    clean, notes = config.clamp_values({"maker_edge": 5, "nope": 1, "maker_enabled": 0, "trigger_ms": "250.4"})
    assert clean == {"maker_edge": 0.2, "maker_enabled": False, "trigger_ms": 250}
    assert any("nope" in n for n in notes) and any("clamped" in n for n in notes)
    st = config.Settings()
    changed = config.apply(st, clean)
    assert st.maker_edge == 0.2 and len(changed) == 3


def test_jsonl_reads_only_new_complete_lines(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"a": 1}\n{"a": 2}\n{"a":')
    j = Jsonl(str(p))
    assert [r["a"] for r in j.get()] == [1, 2]
    with open(p, "a") as f:
        f.write(' 3}\n')
    assert [r["a"] for r in j.get()] == [1, 2, 3]


def test_console_summaries(tmp_path):
    rows = [
        {"slug": "btc-updown-5m-1790783100", "start": 1790783100, "end": 1790783400, "winner": "Up", "pnl": -4.2,
         "maker_pnl": -4.2, "taker_pnl": 0.0, "cost": 59.2, "fees": 0.0, "rebate_est": 0.4, "up_shares": 55,
         "down_shares": 70, "pair_cost": 0.954, "hedge": 0.79, "fills": 27, "maker_fills": 27, "taker_fills": 0,
         "maker_cost": 59.2, "taker_cost": 0.0, "first_fill_s": 13.6, "last_fill_s": 133.8},
        {"slug": "btc-updown-5m-1790783400", "start": 1790783400, "end": 1790783700, "winner": "Down", "pnl": 3.0,
         "maker_pnl": 1.0, "taker_pnl": 2.0, "cost": 40.0, "fees": 0.5, "rebate_est": 0.2, "up_shares": 20,
         "down_shares": 30, "pair_cost": 0.97, "hedge": 0.67, "fills": 10, "maker_fills": 6, "taker_fills": 4,
         "maker_cost": 20.0, "taker_cost": 20.0, "first_fill_s": 10.0, "last_fill_s": 200.0},
    ]
    (tmp_path / "windows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp_path / "fills.jsonl").write_text(json.dumps(
        {"slug": rows[0]["slug"], "ts": 1, "kind": "maker", "how": "cross", "outcome": "Up", "price": 0.5, "size": 5,
         "fee": 0, "rebate_est": 0.01, "fair": 0.52, "won": True, "pnl": 2.5, "mo_1s": -0.01, "mo_5s": 0.0,
         "mo_30s": 0.02}) + "\n")
    c = Console(str(tmp_path))
    assert oct(os.stat(tmp_path / "console_key").st_mode & 0o777) == "0o600"
    w = c.windows_page({})
    assert w["summary"]["pnl"] == pytest.approx(-1.2) and w["rows"][0]["slug"].endswith("3400")
    assert w["curve"][-1][1] == pytest.approx(-1.2) and w["curve"][-1][3] == pytest.approx(-3.2)
    f = c.fills_view({})
    assert f["summary"]["maker"]["edge_at_fill"] == pytest.approx(0.02)
    assert f["summary"]["maker:cross"]["mo_30s"] == pytest.approx(0.02)
    e = c.earnings({"period": ["day"]})
    assert e["rows"][0]["key"] == "2026-10-01" and e["rows"][0]["pnl"] == pytest.approx(-1.2)
    view = c.set_controls({"values": {"maker_edge": 0.03, "bogus": 1}})
    assert view["stored"] == {"maker_edge": 0.03}
    assert c.set_kill(True)["kill"] is True and c.set_kill(False)["kill"] is False
    assert c.window_doc("../../etc/passwd") is None


def test_console_window_archive_accepts_eth_slugs(tmp_path):
    c = Console(str(tmp_path))
    (tmp_path / "windows").mkdir()
    for asset in ("btc", "eth"):
        slug = f"{asset}-updown-5m-1790781300"
        (tmp_path / "windows" / f"{slug}.json").write_text(json.dumps({"slug": slug, "hist": [[1], [2]]}))
        assert c.window_doc(slug)["slug"] == slug


def test_loss_reset_is_written_and_reported(tmp_path):
    c = Console(str(tmp_path))
    v = c.reset_loss()
    assert (tmp_path / "loss_reset").read_text().strip()
    assert "today" in v and "halted" in v


def test_paper_console_serves_one_bot_per_coin_and_writes_to_all(tmp_path):
    from xshark.web.server import Consoles
    for a in ("btc", "eth"):
        (tmp_path / a).mkdir()
    cs = Consoles(str(tmp_path))
    assert set(cs.by_asset) == {"btc", "eth"} and (tmp_path / "console_key").exists()
    assert cs.pick({"asset": ["eth"]}).dir.endswith("eth") and cs.pick({}).dir.endswith("btc")
    assert cs.assets()["multi"] is True
    cs.write_all(lambda c: c.set_controls({"values": {"taker_edge": 0.02}}))
    import json as _j
    for a in ("btc", "eth"):
        assert _j.load(open(tmp_path / a / "controls.json"))["taker_edge"] == 0.02
    single = Consoles(str(tmp_path / "btc"))
    assert list(single.by_asset) == [""] and single.assets()["multi"] is False


def test_book_feed_learns_the_tick_from_the_venue_or_from_sub_cent_prices():
    from xshark.book import BookFeed
    bf = BookFeed(lambda: 0.0)
    assert bf.tick_of("T") is None
    bf._handle(json.dumps({"event_type": "tick_size_change", "asset_id": "T", "new_tick_size": "0.001"}), 1)
    assert bf.tick_of("T") == 0.001


def test_console_sell_request_is_written_for_the_bot(tmp_path):
    from xshark.web.server import Console
    c = Console(str(tmp_path))
    assert "error" in c.request_sell({"outcome": "Sideways"})
    r = c.request_sell({"outcome": "Up", "slug": "btc-updown-5m-1"})
    assert r["ok"] and json.load(open(tmp_path / "sell_request.json"))["outcome"] == "Up"
