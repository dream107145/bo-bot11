"""Calibrate the paper venue's taker fills on REAL orders sent from this VPS.

Read-only on the other bots' files. For every real fill-or-kill / fill-and-kill
BUY that reached Polymarket, it replays the book the bots recorded
(troll-poly-bot data/charts: best bid/ask + size at the top, every ~200 ms) and
asks: at what delay after the decision does "the ask is at or under our limit,
with our size there" reproduce what the venue actually did?

    .venv/bin/python scripts/calibrate_fills.py            # report + data/bot/fill_calibration.json

Sources (both are this VPS's own real-money bots):
  bo-bot11-real  data/exec_telemetry.jsonl   every order, FILLED / FOK_UNFILLABLE, with limit and size
  troll-poly-bot data/trade_forensics.jsonl  spot-lag entries killed by the venue (fills are logged
                                             without their send-time fields, so only kills are used)
"""
from __future__ import annotations

import bisect
import json
import os
import statistics
import sys
from collections import Counter, defaultdict

CHARTS = "/root/troll-poly-bot/data/charts"
BO_REAL = "/root/bo-polymarket/bo-bot11-real/data/exec_telemetry.jsonl"
TROLL = "/root/troll-poly-bot/data/trade_forensics.jsonl"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bot", "fill_calibration.json")
DELAYS = list(range(0, 1001, 50))
#: the live bots' current execution setup (order lanes, coincurve signing,
#: event-driven triggers) went live 2026-09-29 17:58 UTC; the latency the paper
#: bot draws comes from orders sent since then, the race odds from all orders
LATENCY_SINCE_MS = 1790704680000


def q(xs, p):
    xs = sorted(xs)
    return xs[int(p * (len(xs) - 1))] if xs else None


def load_orders() -> list[dict]:
    out = []
    for line in open(BO_REAL):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("side") != "BUY" or r.get("result") not in ("FILLED", "FOK_UNFILLABLE"):
            continue
        out.append({"src": "bo-real", "slug": r["slug"], "side": r["outcome"], "t": r["decision_clock"],
                    "limit": r["limit"], "size": r["size"], "seen_ask": r["decision_price"],
                    "filled": r["result"] == "FILLED", "fill_qty": r.get("filled") or 0.0,
                    "fill_price": r.get("fill_price"),
                    "lat": (r.get("decision_to_post_ms") or 0) + (r.get("request_ms") or 0),
                    "post": r.get("decision_to_post_ms"), "req": r.get("request_ms")})
    # troll-poly-bot: a killed entry keeps its send-time fields; a FILLED entry is
    # logged later as one "EXIT" row (entry + exit), so its limit comes from the
    # ORDER_SUBMITTED stage event with the same order id and timestamp
    submitted = {}
    rows = []
    for line in open(TROLL):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("event") == "stage" and r.get("stage") == "ORDER_SUBMITTED":
            submitted[(r["order_id"], round(r["ts"], 1))] = r.get("limit_price")
        elif r.get("event") == "trade":
            rows.append(r)
    for r in rows:
        if r.get("mode") != "LIVE-ARMED" or r.get("outcome") != "EXIT":
            continue
        stg = r.get("stages") or {}
        sub, fill = stg.get("ORDER_SUBMITTED"), stg.get("FULL_FILL") or stg.get("PARTIAL_FILL")
        limit = submitted.get((r["order_id"], round(sub, 1))) if sub else None
        if limit is None or not stg.get("DECISION") or not fill:
            continue
        out.append({"src": "troll-live", "slug": r["slug"], "side": r["side"], "t": stg["DECISION"],
                    "limit": limit, "size": r["quantity"], "seen_ask": r.get("decision_price"),
                    "filled": True, "fill_qty": r["quantity"], "fill_price": r.get("entry_price") or r.get("execution_price"),
                    "lat": fill - stg["DECISION"], "post": sub - stg["DECISION"], "req": fill - sub})
    for r in rows:
        if r.get("mode") != "LIVE-ARMED" or r.get("kind") != "entry" or r.get("outcome") != "REJECTED:fok_unfillable":
            continue
        if r.get("limit_price") is None or not r.get("t4_decision"):
            continue
        out.append({"src": "troll-live", "slug": r["slug"], "side": r["side"], "t": r["t4_decision"],
                    "limit": r["limit_price"], "size": r["quantity"], "seen_ask": r.get("sent_ask") or r.get("decision_price"),
                    "filled": False, "fill_qty": 0.0, "fill_price": None,
                    "lat": (r.get("presend_ms") or 0) + (r.get("venue_rtt_ms") or 0),
                    "post": r.get("presend_ms"), "req": r.get("venue_rtt_ms")})
    return out


_chart_cache: dict = {}


def chart(slug: str):
    if slug not in _chart_cache:
        try:
            with open(os.path.join(CHARTS, slug + ".json")) as f:
                pts = json.load(f).get("points") or []
        except (OSError, ValueError):
            pts = []
        _chart_cache[slug] = ([p["t"] for p in pts], pts)
    return _chart_cache[slug]


def book_at(slug: str, side: str, t: float):
    ts, pts = chart(slug)
    i = bisect.bisect_right(ts, t) - 1
    if i < 0 or t - ts[i] > 1500:
        return None
    p = pts[i]
    a, d = (p.get("ua"), p.get("uad")) if side == "UP" else (p.get("da"), p.get("dad"))
    return None if a is None else (a, d)


def depth_bucket(ratio: float) -> str:
    return "depth_lt3" if ratio < 3 else "depth_3_20" if ratio < 20 else "depth_ge20"


def predict(o: dict, delay: float, need_size: bool = True) -> bool | None:
    b = book_at(o["slug"], o["side"], o["t"] + delay)
    if b is None:
        return None
    ask, depth = b
    if ask > o["limit"] + 1e-9:
        return False
    return (depth or 0) >= o["size"] - 1e-9 if need_size else True


def main() -> None:
    orders = [o for o in load_orders() if chart(o["slug"])[0]]
    if not orders:
        sys.exit("no real orders with a recorded chart")
    print(f"real orders with a recorded book: {len(orders)} "
          f"({Counter(o['src'] for o in orders)}); filled {sum(o['filled'] for o in orders)}")
    real_rate = sum(o["filled"] for o in orders) / len(orders)

    # 1. a fixed delay for everyone
    table = []
    for dly in DELAYS:
        pr = [(predict(o, dly), o["filled"]) for o in orders]
        pr = [(p, r) for p, r in pr if p is not None]
        tp = sum(p and r for p, r in pr); fp = sum(p and not r for p, r in pr)
        fn = sum((not p) and r for p, r in pr); tn = sum((not p) and (not r) for p, r in pr)
        table.append({"delay_ms": dly, "n": len(pr), "pred_fill_rate": (tp + fp) / len(pr),
                      "accuracy": (tp + tn) / len(pr), "false_fills": fp, "missed_fills": fn})
    # 2. each order at its OWN measured latency (decision -> post + request), times a scale
    own = []
    for scale in (0.0, 0.25, 0.5, 0.75, 0.9, 1.0, 1.25):
        pr = [(predict(o, o["lat"] * scale), o["filled"]) for o in orders]
        pr = [(p, r) for p, r in pr if p is not None]
        tp = sum(p and r for p, r in pr); fp = sum(p and not r for p, r in pr)
        fn = sum((not p) and r for p, r in pr); tn = sum((not p) and (not r) for p, r in pr)
        own.append({"scale": scale, "n": len(pr), "pred_fill_rate": (tp + fp) / len(pr),
                    "accuracy": (tp + tn) / len(pr), "false_fills": fp, "missed_fills": fn})
    best = min(table, key=lambda r: abs(r["pred_fill_rate"] - real_rate) + (1 - r["accuracy"]))
    print(f"\nreal fill rate {real_rate:.1%}")
    print("fixed delay:  delay  pred_fill  accuracy  false_fills  missed_fills")
    for r in table:
        print(f"  {r['delay_ms']:>10} ms  {r['pred_fill_rate']:8.1%}  {r['accuracy']:8.1%}  {r['false_fills']:10}  {r['missed_fills']:11}")
    print("own latency x scale:")
    for r in own:
        print(f"  x{r['scale']:<5} pred {r['pred_fill_rate']:6.1%}  acc {r['accuracy']:6.1%}  false {r['false_fills']:4}  missed {r['missed_fills']:4}")

    # 3. what the fills looked like
    fills = [o for o in orders if o["filled"] and o["fill_price"]]
    slip = [round(o["fill_price"] - o["seen_ask"], 4) for o in fills]
    part = [o["fill_qty"] / o["size"] for o in fills if o["size"]]
    lat = [o["lat"] for o in load_orders() if o["lat"] and o["t"] >= LATENCY_SINCE_MS]
    if len(lat) < 50:                              # too few recent orders: fall back to all of them
        lat = [o["lat"] for o in orders if o["lat"]]
    # fill rate by how far the limit sits over the ask we saw
    cush = defaultdict(lambda: [0, 0])
    for o in orders:
        k = round((o["limit"] - o["seen_ask"]) / 0.01) if o["seen_ask"] is not None else None
        cush[k][0] += 1
        cush[k][1] += o["filled"]
    print(f"\nfill price minus the ask seen: p10 {q(slip, .1)} p50 {q(slip, .5)} p90 {q(slip, .9)} (n {len(slip)})")
    print(f"filled share of the order: p10 {q(part, .1):.2f} p50 {q(part, .5):.2f}")
    print(f"decision -> venue reply ms: p10 {q(lat, .1):.0f} p50 {q(lat, .5):.0f} p90 {q(lat, .9):.0f} p99 {q(lat, .99):.0f}")
    print("real fill rate by limit over the ask (ticks):",
          {k: f"{v[1]}/{v[0]}" for k, v in sorted(cush.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))})

    # 4. the race: among orders the replay says would fill at their own latency,
    # how many really did -- by the ask's depth over the order size and whether
    # the ask came DOWN by arrival. Spot-triggered snipes (troll-live) are the
    # class our taker clips belong to; bo-real's model-edge orders race less.
    race = {}
    for src in ("troll-live", "bo-real"):
        cells = defaultdict(lambda: [0, 0])
        for o in orders:
            if o["src"] != src:
                continue
            b0, b1 = book_at(o["slug"], o["side"], o["t"]), book_at(o["slug"], o["side"], o["t"] + o["lat"])
            if not b0 or not b1 or b1[0] > o["limit"] + 1e-9 or (b1[1] or 0) < o["size"] - 1e-9:
                continue
            key = "ask_lower" if b1[0] < (o["seen_ask"] or 0) - 1e-9 else depth_bucket((b1[1] or 0) / max(o["size"], 1))
            cells[key][0] += 1
            cells[key][1] += o["filled"]
        race[src] = {k: {"n": v[0], "filled": v[1], "rate": v[1] / v[0]} for k, v in cells.items()}
    print("\nrace (real fills among replay fills):")
    for src, cells in race.items():
        print(" ", src, {k: f"{v['filled']}/{v['n']}" for k, v in sorted(cells.items())})

    doc = {"built_at": __import__("time").time(), "orders": len(orders), "real_fill_rate": real_rate,
           "race": race,
           "fixed_delay": table, "own_latency": own, "best_fixed_delay_ms": best["delay_ms"],
           "latency_ms": sorted(round(x, 1) for x in lat), "latency_since_ms": LATENCY_SINCE_MS,
           # [pre-send (build + sign + queue), venue (send -> reply)] per recent order
           "latency_split": [[round(o["post"], 1), round(o["req"], 1)] for o in load_orders()
                             if o["t"] >= LATENCY_SINCE_MS and o["post"] is not None and o["req"]],
           "slippage": {"p10": q(slip, .1), "p50": q(slip, .5), "p90": q(slip, .9)},
           "sources": dict(Counter(o["src"] for o in orders))}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(doc, f)
    print(f"\nbest fixed delay {best['delay_ms']} ms -> {OUT}")


if __name__ == "__main__":
    main()
