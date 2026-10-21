"""Backtest Strategy V3 (the bot's own ``xshark.v3.decide``) on recorded BTC 5-minute markets.

Data: troll-poly-bot's data/charts/btc-updown-5m-*.json (loaded and cached by scripts/replay.py): best bid/ask and
the best ask's size for both tokens every ~150 ms, and the venue's result. Deeper levels were not recorded.

Fills (as the paper venue): each FAK arrives after a latency drawn from fill_calibration.json; it fills only if
the ask is then <= its limit, with the calibrated odds of beating faster bots to that level; it gets at most the
best ask's recorded size (a partial fill otherwise) -- nothing deeper (DEPTH2=1 adds one level 0.01 higher with
the same size). Fees 0.07 * p * (1 - p) a share. Settlement at $1 a winning share.

    nice .venv/bin/python scripts/backtest_v3.py            # the report
"""
from __future__ import annotations

import bisect
import collections
import os
import random
import statistics as S
import sys
import zlib
from dataclasses import replace
from multiprocessing import Pool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from replay import CAL, load_windows                 # noqa: E402
from xshark.config import Settings                    # noqa: E402
from xshark.market import FeeSchedule, Market         # noqa: E402
from xshark.strategy import View, decide              # noqa: E402
from xshark.v3 import V3Params, position              # noqa: E402

FEES = FeeSchedule()
ST = Settings(maker_enabled=False, taker_enabled=False, pair_enabled=False, fav_enabled=False, late_enabled=False,
              scalp_enabled=False)
DEPTH2 = os.environ.get("DEPTH2") == "1"
STEP = int(os.environ.get("STEP", "1"))            # decide on every STEP-th point


def simulate(w: dict, p, seed: int = 0, pfield: str = "v3_params") -> dict:
    rng = random.Random(zlib.crc32(f"{w['slug']}:{seed}".encode()))
    lat = CAL.latencies or [271.0]
    m = Market(slug=w["slug"], condition_id="", question="", start=int(w["open"] / 1000), end=int(w["close"] / 1000),
               up_token="U", down_token="D", fees=FEES)
    st, fills, clip, oid, sent, peak = {}, [], None, 0, 0, 0.0
    hist = collections.deque()                       # (t, Up mid) since the open, for the lows
    for i, t in enumerate(w["t"]):
        if t < w["open"]:
            continue
        if t >= w["close"]:
            break
        ub, ua, db, da = w["ub"][i], w["ua"][i], w["db"][i], w["da"][i]
        if None in (ub, ua, db, da):
            continue
        dep = {"Up": w["uad"][i] or 0.0, "Down": w["dad"][i] or 0.0}
        ask = {"Up": ua, "Down": da}
        if clip and clip["arrive"] <= t:             # the FAK arrives
            o = clip["side"]
            levels = [(ask[o], dep[o])] + ([(round(ask[o] + 0.01, 4), dep[o])] if DEPTH2 else [])
            got, cost = 0.0, 0.0
            for px, sz in levels:
                if px > clip["limit"] + 1e-9:
                    break
                q = min(sz, clip["size"] - got)
                if q <= 0:
                    break
                got += q
                cost += q * px
            if got > 0:
                pr, _ = CAL.race_prob(dep[o] / clip["size"], clip["seen"] is not None and ask[o] < clip["seen"] - 1e-9)
                if rng.random() < pr:
                    oid += 1
                    px = cost / got
                    fills.append(dict(outcome=o, price=px, size=round(got, 2), fee=FEES.taker_fee(px, got),
                                      tag=clip["tag"], ts=t, oid=oid))
            clip = None
        um = (ub + ua) / 2
        hist.append((t, um))
        if i % STEP:
            continue
        lo_t = max(t - getattr(p, "lookback_s", 10.0) * 1000, w["open"])
        while hist and hist[0][0] < lo_t:
            hist.popleft()
        mids = [x for _, x in hist]
        books = {"Up": {"bid": ub, "ask": ua, "bid_size": 1e6, "ask_size": dep["Up"], "asks": [(ua, dep["Up"])], "bids": [(ub, 1e6)]},
                 "Down": {"bid": db, "ask": da, "bid_size": 1e6, "ask_size": dep["Down"], "asks": [(da, dep["Down"])], "bids": [(db, 1e6)]}}
        if DEPTH2:
            for o in ("Up", "Down"):
                books[o]["asks"].append((round(books[o]["ask"] + 0.01, 4), dep[o]))
        pos = position(fills)
        v = View(now_ms=t, market=m, secs_in=(t - w["open"]) / 1000, secs_left=(w["close"] - t) / 1000, fair_up=None,
                 trigger_z=0.0, books=books, book_age_ms=0.0, spot_age_ms=0.0, shares=dict(pos["shares"]),
                 window_cost=pos["total"], reserved=0.0, resting={"Up": [], "Down": []},
                 in_flight_taker={"Up": clip["size"] if clip and clip["side"] == "Up" else 0.0,
                                  "Down": clip["size"] if clip and clip["side"] == "Down" else 0.0},
                 taker_count=0, last_taker_ms={}, last_quote_ms={}, halted=None, whisk=dict(st), **{pfield: p},
                 v2_fills=list(fills), v3_lows={"Up": min(mids), "Down": 1 - max(mids)} if mids else {},
                 v4_up_hist=list(hist))
        d = decide(v, ST)
        for k, x in d.whisk_update.items():
            if x is None:
                st.pop(k, None)
            else:
                st[k] = x
        if d.takes and clip is None:
            o, limit, n, _ = d.takes[0]
            clip = {"side": o, "limit": limit, "size": n, "arrive": t + rng.choice(lat), "seen": ask[o], "tag": d.tags.get(f"taker:{o}")}
            sent += 1
            peak = max(peak, pos["total"] + n * limit)
    pos = position(fills)
    win = w["winner"]
    return {"slug": w["slug"], "open": w["open"], "pnl": pos["if"][win] if fills else 0.0, "cost": pos["total"],
            "worst": pos["worst"] if fills else 0.0, "fills": len(fills), "sent": sent, "peak": peak,
            "shares": pos["shares"]["Up"] + pos["shares"]["Down"],
            "rec": len({f["oid"] for f in fills if f["tag"] in ("v3r", "v3s")}),
            "tags": collections.Counter(f["tag"] for f in fills), "kind": classify(w),
            "flags": {k: st.get(k) for k in ("v4_reached", "v4_bothneg", "v4_cnt_nosize")}}


def classify(w: dict) -> set:
    """Overlapping labels from the Up mid path inside the market."""
    pts = [(t, (ub + ua) / 2, ua - ub) for t, ub, ua in zip(w["t"], w["ub"], w["ua"])
           if ub is not None and ua is not None and w["open"] <= t < w["close"]]
    if len(pts) < 50:
        return {"short"}
    mids = [x for _, x, _ in pts]
    late = [x for t, x, _ in pts if t - w["open"] >= 60_000] or mids
    out = set()
    if w["winner"] == "Up" and min(late) >= 0.40:
        out.add("sustained Up trend")
    if w["winner"] == "Down" and max(late) <= 0.60:
        out.add("sustained Down trend")
    if (max(mids) >= 0.75 and w["winner"] == "Down") or (min(mids) <= 0.25 and w["winner"] == "Up"):
        out.add("sharp reversal")
    crosses = sum((a - 0.5) * (b - 0.5) < 0 for a, b in zip(mids, mids[1:]))
    if crosses >= 4:
        out.add("choppy / sideways")
    ts = [t for t, _, _ in pts]
    if any(abs(x - mids[bisect.bisect_left(ts, t - 1000)]) >= 0.15 for t, x, _ in pts):
        out.add("rapid jumps")
    if sum(s >= 0.03 for _, _, s in pts) > 0.2 * len(pts):
        out.add("wide spreads")
    return out or {"other"}


def report(lab: str, R: list) -> dict:
    P = [r["pnl"] for r in R]
    traded = [r for r in R if r["fills"]]
    cum = peak = dd = 0.0
    for x in P:
        cum += x
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    n = len(R) or 1
    out = dict(pnl=sum(P), per=sum(P) / n, win=sum(x > 0 for x in P) / max(1, len(traded)), dd=dd,
               cap=max((r["peak"] for r in R), default=0), worst=min(P, default=0),
               worst_proj=min((r["worst"] for r in R), default=0), trades=sum(r["fills"] for r in R),
               shares=sum(r["shares"] for r in R), rec=sum(r["rec"] for r in R) / max(1, len(traded)),
               traded=len(traded), n=len(R), sd=S.pstdev(P) if len(P) > 1 else 0.0)
    print(f"{lab:<34} {out['n']:>5} mkts {out['traded']:>5} traded | P/L {out['pnl']:+9.2f} ({out['per']:+6.3f}/mkt, sd {out['sd']:5.2f})"
          f" | profitable {out['win']:5.1%} | max DD {out['dd']:+8.2f} | max capital {out['cap']:6.2f}"
          f" | worst mkt {out['worst']:+7.2f} | trades {out['trades']:>6} shares {out['shares']:>8.0f} | recovery/mkt {out['rec']:.2f}")
    return out


def run_all(args):
    p, ws, seed, pfield = args
    return [simulate(w, p, seed, pfield) for w in ws]


def batch(p, ws, procs=3, pfield="v3_params"):
    chunks = [ws[i::procs] for i in range(procs)]
    with Pool(procs) as pool:
        parts = pool.map(run_all, [(p, c, 0, pfield) for c in chunks])
    R = [r for part in parts for r in part]
    R.sort(key=lambda r: r["open"])
    return R


if __name__ == "__main__":
    ws = [w for w in load_windows() if w["slug"].startswith("btc-")]
    if len(sys.argv) > 1:
        ws = ws[-int(sys.argv[1]):]
    print(f"{len(ws)} markets, {ws[0]['slug']} .. {ws[-1]['slug']}; deeper level: {'one at +0.01' if DEPTH2 else 'none'}; decide every {STEP} point(s)")
    P = V3Params(enabled=True)
    OFF = 9.0
    variants = {
        "complete, with risk limits": P,
        "complete, no risk limits": replace(P, max_spend=1e9, max_up=1e9, max_down=1e9, max_order=1e9, max_outcome_loss=1e9,
                                            max_imbalance=1e9, rec_max=1e9),
        "momentum only": replace(P, rec_rise=OFF, bal_sum=0.0, lvl_a=OFF, lvl_b=OFF, lvl_c=OFF),
        "balancing only (+ entry)": replace(P, add_step=OFF, rec_rise=OFF, lvl_a=OFF, lvl_b=OFF, lvl_c=OFF),
        "recovery only (+ entry)": replace(P, add_step=OFF, bal_sum=0.0, lvl_a=OFF, lvl_b=OFF, lvl_c=OFF),
    }
    res = {}
    for lab, p in variants.items():
        res[lab] = batch(p, ws)
        report(lab, res[lab])
    full = res["complete, with risk limits"]
    cut = int(len(full) * 0.6)
    print("\nby period (no parameter was fitted to either half):")
    report(f"first 60% ({full[0]['slug'][-10:]}..)", full[:cut])
    report(f"last 40% ({full[cut]['slug'][-10:]}..)", full[cut:])
    print("\nby market type (labels overlap):")
    kinds = collections.Counter(k for r in full for k in r["kind"])
    for k, _ in kinds.most_common():
        report(k, [r for r in full if k in r["kind"]])
    print("\nfills by rule:", dict(sum((r["tags"] for r in full), collections.Counter())))
    print("\nsensitivity (complete, with risk limits; first 60% / last 40%):")
    for lab, p in (("entry rise 0.03", replace(P, entry_rise=0.03)), ("entry rise 0.07", replace(P, entry_rise=0.07)),
                   ("recovery rise 0.08", replace(P, rec_rise=0.08)), ("recovery rise 0.12", replace(P, rec_rise=0.12)),
                   ("balance sum 0.94", replace(P, bal_sum=0.94)), ("balance sum 0.98", replace(P, bal_sum=0.98)),
                   ("levels off", replace(P, lvl_a=OFF, lvl_b=OFF, lvl_c=OFF))):
        R = batch(p, ws)
        report(f"{lab} | in", R[:cut])
        report(f"{lab} | out", R[cut:])
