"""Replay the strategy (the real ``xshark.strategy.decide``) over recorded BTC windows.

Data: troll-poly-bot's data/charts/btc-updown-5m-*.json -- every window it
watched since 2026-09-22: best bid/ask and top size for both tokens and the
composite spot every ~200 ms, its 60 s TWAP strike, and the venue's result.

Per window, every ~200 ms point:
  fair value   the Up mid 1 s earlier + the TWAP model's move since (as the bot)
  decisions    strategy.decide() with the given Settings
  latency      each order / cancel draws a real VPS latency (fill_calibration.json)
  bids         fill only when the token's ask falls to the bid (cross-through);
               trade-print fills cannot be replayed (no prints here), so bid
               fills are UNDER-counted relative to the paper bot
  clips        at arrival the ask must be <= limit with the size there; then
               they fill with the real race odds (expected value: p * size)
  settlement   the venue's result, $1 per winning share

    .venv/bin/python scripts/replay.py                 # baseline + variants, TRAIN/TEST
"""
from __future__ import annotations

import bisect
import glob
import json
import math
import os
import pickle
import random
import statistics
import sys
import zlib
from dataclasses import replace
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from xshark.calibration import Calibration          # noqa: E402
from xshark.config import Settings                  # noqa: E402
from xshark.market import FeeSchedule, Market       # noqa: E402
from xshark.pricing import t4_cdf                   # noqa: E402
from xshark.strategy import Resting, View, decide   # noqa: E402
from xshark.twap import twap_variance               # noqa: E402

CHARTS = "/root/troll-poly-bot/data/charts"
CACHE = "/tmp/xshark_replay_cache.pkl"
CAL = Calibration(os.path.join(ROOT, "data", "bot", "fill_calibration.json"))
FEES = FeeSchedule()


# ---------------------------------------------------------------- windows

def prep(path: str) -> dict | None:
    try:
        d = json.load(open(path))
    except (OSError, ValueError):
        return None
    if d.get("outcome") not in ("UP", "DOWN") or not d.get("strike"):
        return None
    pts = [p for p in d.get("points") or [] if p.get("spot")]
    if len(pts) < 600:
        return None
    t = [p["t"] for p in pts]
    open_ms, close_ms = d["open_ts"], d["close_ts"]
    lx = [math.log(p["spot"]) for p in pts]
    # per-second vol: EWMA of squared 1 s returns, 120 s half-life, plain mean for 30 samples (vol.py)
    sig, var, n, last_t, last_x = [], 0.0, 0, t[0], lx[0]
    alpha = 1 - 0.5 ** (1 / 120)
    for ti, xi in zip(t, lx):
        if ti - last_t >= 1000:
            r2 = (xi - last_x) ** 2 / ((ti - last_t) / 1000)
            var = (var * n + r2) / (n + 1) if n < 30 else (1 - alpha) * var + alpha * r2
            n += 1
            last_t, last_x = ti, xi
        sig.append(min(max(math.sqrt(var), 2e-5), 5e-3) if n else 2e-5)
    # trapezoid prefix integral of log spot (log-price * s)
    cum = [0.0]
    for i in range(1, len(t)):
        cum.append(cum[-1] + 0.5 * (lx[i] + lx[i - 1]) * (t[i] - t[i - 1]) / 1000)

    def integral(a, b):
        ia, ib = bisect.bisect_left(t, a), bisect.bisect_right(t, b) - 1
        if ib <= ia:
            return 0.0
        return cum[ib] - cum[ia]

    lk = math.log(d["strike"])
    pm, mid, z500 = [], [], []
    for i, ti in enumerate(t):
        u = (close_ms - ti) / 1000
        if u <= 0 or ti < open_ms:
            pm.append(None)
        else:
            if u >= 60:
                mean = lx[i]
            else:
                mean = (integral(close_ms - 60_000, ti) + u * lx[i]) / 60.0
            sd = math.sqrt(max(twap_variance(u, sig[i]), 1e-18))
            pm.append(t4_cdf((mean - lk) / sd))
        p = pts[i]
        ub, ua = p.get("ub"), p.get("ua")
        mid.append((ub + ua) / 2 if ub is not None and ua is not None and 0 <= ua - ub <= 0.05 else None)
        j = bisect.bisect_right(t, ti - 500) - 1
        z500.append(None if j < 0 else (lx[i] - lx[j]) / (sig[i] * math.sqrt(0.5)))
    fair = []
    for i, ti in enumerate(t):
        j = bisect.bisect_right(t, ti - 1000) - 1
        if j < 0 or pm[i] is None or pm[j] is None or mid[j] is None:
            fair.append(None)
        else:
            fair.append(min(max(mid[j] + pm[i] - pm[j], 0.01), 0.99))
    return {"slug": d["slug"], "open": open_ms, "close": close_ms, "winner": "Up" if d["outcome"] == "UP" else "Down",
            "t": t, "fair": fair, "z": z500,
            "ub": [p.get("ub") for p in pts], "ua": [p.get("ua") for p in pts],
            "db": [p.get("db") for p in pts], "da": [p.get("da") for p in pts],
            "uad": [p.get("uad") for p in pts], "dad": [p.get("dad") for p in pts]}


def load_windows() -> list[dict]:
    if os.path.exists(CACHE) and os.path.getmtime(CACHE) > max(os.path.getmtime(p) for p in glob.glob(f"{CHARTS}/btc-updown-5m-*.json")) - 1e9:
        with open(CACHE, "rb") as f:
            ws = pickle.load(f)
        known = {w["slug"] for w in ws}
    else:
        ws, known = [], set()
    new = [p for p in sorted(glob.glob(f"{CHARTS}/btc-updown-5m-*.json"))
           if os.path.basename(p)[:-5] not in known]
    if new:
        with Pool(3) as pool:
            ws += [w for w in pool.map(prep, new, chunksize=20) if w]
        ws.sort(key=lambda w: w["open"])
        with open(CACHE, "wb") as f:
            pickle.dump(ws, f)
    return ws


# ---------------------------------------------------------------- simulation

def simulate(w: dict, s: Settings, seed: int = 0) -> dict:
    rng = random.Random(zlib.crc32(f"{w['slug']}:{seed}".encode()))
    lat = CAL.latencies or [271.0]
    L = lambda: rng.choice(lat)                                  # noqa: E731
    m = Market(slug=w["slug"], condition_id="", question="", start=int(w["open"] / 1000),
               end=int(w["close"] / 1000), up_token="U", down_token="D", fees=FEES)
    shares = {"Up": 0.0, "Down": 0.0}
    cost = {"Up": 0.0, "Down": 0.0}
    leg = {"maker": 0.0, "taker": 0.0}          # cost per leg, and payout per leg below
    legpay = {"maker": {"Up": 0.0, "Down": 0.0}, "taker": {"Up": 0.0, "Down": 0.0}}
    bids = {"Up": [], "Down": []}               # dicts: id, price, size, live, cancel
    clips = []                                  # dicts: side, limit, size, arrive, seen
    last_q, last_t = {}, {}
    ntake, nfill_m, nfill_t, oid = 0, 0, 0.0, 0
    last_side = None
    feepaid = {"Up": 0.0, "Down": 0.0}
    tick = 0.01
    for i, t in enumerate(w["t"]):
        if t < w["open"]:
            continue
        if t >= w["close"]:
            break
        book = {"Up": {"bid": w["ub"][i], "ask": w["ua"][i]}, "Down": {"bid": w["db"][i], "ask": w["da"][i]}}
        depth = {"Up": w["uad"][i] or 0.0, "Down": w["dad"][i] or 0.0}
        # resting bids: live between placement and cancel; fill when the ask comes down to them
        for side in ("Up", "Down"):
            keep = []
            for b in bids[side]:
                if b["cancel"] is not None and t >= b["cancel"]:
                    continue
                ask = book[side]["ask"]
                if b["live"] <= t and ask is not None and ask <= b["price"] + 1e-9:
                    shares[side] += b["size"]
                    cost[side] += b["price"] * b["size"]
                    leg["maker"] += b["price"] * b["size"]
                    legpay["maker"][side] += b["size"]
                    nfill_m += 1
                    last_side = side
                    continue
                keep.append(b)
            bids[side] = keep
        # clips arriving now
        rest = []
        for c in clips:
            if c["arrive"] > t:
                rest.append(c)
                continue
            ask = book[c["side"]]["ask"]
            if ask is None or ask > c["limit"] + 1e-9 or depth[c["side"]] < c["size"]:
                continue
            p, _ = CAL.race_prob(depth[c["side"]] / c["size"], c["seen"] is not None and ask < c["seen"] - 1e-9)
            if not s.paper_race:
                p = 1.0
            # a whole clip or nothing, drawn like the live paper venue (an expected
            # p * size fill left fractional shares that pairing then over-bought)
            if rng.random() >= p:
                continue
            q = c["size"]
            shares[c["side"]] += q
            cst = q * ask + FEES.taker_fee(ask, q)
            feepaid[c["side"]] += FEES.taker_fee(ask, q)
            cost[c["side"]] += cst
            leg["taker"] += cst
            legpay["taker"][c["side"]] += q
            nfill_t += 1
            last_side = c["side"]
        clips = rest
        # decide
        fair = w["fair"][i]
        v = View(now_ms=t, market=m, secs_in=(t - w["open"]) / 1000, secs_left=(w["close"] - t) / 1000,
                 fair_up=fair, trigger_z=w["z"][i], books=book, book_age_ms=0.0, spot_age_ms=0.0,
                 shares=dict(shares), window_cost=cost["Up"] + cost["Down"],
                 reserved=sum(b["price"] * b["size"] for sd in bids.values() for b in sd)
                 + sum(c["limit"] * c["size"] for c in clips),
                 resting={sd: [Resting(b["id"], b["price"], b["size"], b["live"], b["cancel"] is not None)
                               for b in bids[sd]] for sd in bids},
                 in_flight_taker={sd: sum(c["size"] for c in clips if c["side"] == sd) for sd in ("Up", "Down")},
                 taker_count=ntake, last_taker_ms=last_t, last_quote_ms=last_q, halted=None,
                 costs=dict(cost), last_fill_side=last_side, fees=dict(feepaid))
        d = decide(v, s)
        for cid in d.cancels:
            for sd in bids:
                for b in bids[sd]:
                    if b["id"] == cid and b["cancel"] is None:
                        b["cancel"] = t + L()
        for side, px, size in d.places:
            oid += 1
            bids[side].append({"id": oid, "price": px, "size": size, "live": t + L(), "cancel": None})
            last_q[side] = t
        for side, limit, size, _ in d.takes:
            clips.append({"side": side, "limit": limit, "size": size, "arrive": t + L(), "seen": book[side]["ask"]})
            ntake += 1
            last_t[side] = t
    win = w["winner"]
    pnl = shares[win] - cost["Up"] - cost["Down"]
    maker_pnl = legpay["maker"][win] - leg["maker"]
    taker_pnl = legpay["taker"][win] - leg["taker"]
    return {"slug": w["slug"], "open": w["open"], "pnl": pnl, "maker": maker_pnl, "taker": taker_pnl,
            "cost": cost["Up"] + cost["Down"], "up": shares["Up"], "down": shares["Down"],
            "fills_m": nfill_m, "fills_t": nfill_t, "traded": (cost["Up"] + cost["Down"]) > 0}


def _job(args):
    w, s = args
    return simulate(w, s)


def run(ws, s: Settings, pool) -> list[dict]:
    return pool.map(_job, [(w, s) for w in ws], chunksize=10)


def report(name: str, res: list[dict], split: int) -> dict:
    out = {"name": name}
    for part, rows in (("TRAIN", res[:split]), ("TEST", res[split:])):
        p = [r["pnl"] for r in rows]
        tr = [r for r in rows if r["traded"]]
        cost = sum(r["cost"] for r in rows)
        mean = statistics.mean(p) if p else 0.0
        sd = statistics.pstdev(p) if len(p) > 1 else 0.0
        out[part] = {"windows": len(rows), "traded": len(tr), "pnl": sum(p), "per_window": mean,
                     "t": mean / (sd / math.sqrt(len(p))) if sd > 0 else 0.0,
                     "maker": sum(r["maker"] for r in rows), "taker": sum(r["taker"] for r in rows),
                     "ret": sum(p) / cost if cost else 0.0, "fills_m": sum(r["fills_m"] for r in rows) / max(1, len(rows)),
                     "fills_t": sum(r["fills_t"] for r in rows) / max(1, len(rows))}
    a, b = out["TRAIN"], out["TEST"]
    print(f"{name:34} TRAIN {a['pnl']:+8.2f} ({a['per_window']:+.3f}/w t{a['t']:+5.1f} ret{100*a['ret']:+5.1f}% "
          f"m{a['maker']:+7.1f} t{a['taker']:+6.1f} bids/w {a['fills_m']:.1f} clips/w {a['fills_t']:.2f})  |  TEST {b['pnl']:+8.2f} "
          f"({b['per_window']:+.3f}/w t{b['t']:+5.1f} ret{100*b['ret']:+5.1f}% clips/w {b['fills_t']:.2f})",
          flush=True)
    return out


VARIANTS = {
    "now: clips, alternate, cap 20": {"maker_enabled": False, "taker_edge": 0.03, "taker_max_per_window": 20, "alternate_sides": True, "max_window_shares": 20, "pair_enabled": False},
    "+ maker bid, taker+maker < 0.95": {"maker_enabled": False, "taker_edge": 0.03, "taker_max_per_window": 20, "alternate_sides": True, "max_window_shares": 20, "pair_enabled": True, "pair_take": False, "pair_bid": True,
                                        "pair_target": 0.95, "pair_on_price": True},
}


def main() -> None:
    ws = load_windows()
    split = int(len(ws) * 0.6)
    print(f"{len(ws)} BTC windows, TRAIN {split} (to {ws[split - 1]['slug']}), TEST {len(ws) - split}", flush=True)
    only = sys.argv[1:]
    results = []
    with Pool(3) as pool:
        for name, over in VARIANTS.items():
            if only and not any(o in name for o in only):
                continue
            s = replace(Settings(), **over)
            results.append(report(name, run(ws, s, pool), split))
    with open(os.path.join(ROOT, "data", "bot", "replay_results.json"), "w") as f:
        json.dump(results, f, indent=1)


if __name__ == "__main__":
    main()
