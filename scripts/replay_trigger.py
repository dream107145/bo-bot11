"""Which clip trigger (move size in sigma x measuring window in ms) does best?

Replays the paper bot's CURRENT settings (data/paper/btc/state.json) with the favourite and
last-minute bids off, over the recorded BTC windows (troll-poly-bot charts), through
scripts/replay.py's simulate(): the real decide(), real order latency, race odds, pair bids.
Only the trigger changes: z is recomputed from Bybit's own price (the paper bot's feed) for
each window length, as the bot does (newest price vs the price `ms` earlier, over
sigma * sqrt(ms)).

The charts sample every ~205 ms, so windows under ~400 ms cannot be tested.

    .venv/bin/python scripts/replay_trigger.py            # the grid
"""
from __future__ import annotations

import bisect
import glob
import json
import math
import os
import pickle
import sys
from array import array
from dataclasses import fields, replace
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import replay                                        # noqa: E402
from replay import CHARTS, Settings, report, simulate   # noqa: E402

CACHE = "/tmp/xshark_replay_trigger_cache.pkl"
MS = (400, 600, 800, 1000, 1400, 2000, 3000)
SIGMAS = (1.0, 1.3, 1.6, 2.0, 2.5, 3.0)


def prep(path: str) -> dict | None:
    w = replay.prep(path)
    if not w:
        return None
    d = json.load(open(path))
    pts = [p for p in d.get("points") or [] if p.get("spot")]
    lb = array("d")
    for p in pts:
        b = (p.get("srcs") or {}).get("bybit") or p["spot"]
        lb.append(math.log(b))
    # per-second vol of Bybit's price: EWMA of squared 1 s returns, 120 s half-life (xshark/vol.py)
    sig, var, n = array("d"), 0.0, 0
    t = w["t"]
    last_t, last_x = t[0], lb[0]
    alpha = 1 - 0.5 ** (1 / 120)
    for ti, xi in zip(t, lb):
        if ti - last_t >= 1000:
            r2 = (xi - last_x) ** 2 / ((ti - last_t) / 1000)
            var = (var * n + r2) / (n + 1) if n < 30 else (1 - alpha) * var + alpha * r2
            n += 1
            last_t, last_x = ti, xi
        sig.append(min(max(math.sqrt(var), 2e-5), 5e-3) if n else 2e-5)
    w["lb"], w["sig"] = lb, sig
    del w["z"]
    return w


def load() -> list[dict]:
    if os.path.exists(CACHE):
        with open(CACHE, "rb") as f:
            return pickle.load(f)
    paths = sorted(glob.glob(f"{CHARTS}/btc-updown-5m-*.json"))
    with Pool(3) as pool:
        ws = [w for w in pool.map(prep, paths, chunksize=20) if w]
    ws.sort(key=lambda w: w["open"])
    with open(CACHE, "wb") as f:
        pickle.dump(ws, f, protocol=pickle.HIGHEST_PROTOCOL)
    return ws


def with_trigger(w: dict, ms: int) -> dict:
    t, lb, sig = w["t"], w["lb"], w["sig"]
    root = math.sqrt(ms / 1000.0)
    z = []
    for i, ti in enumerate(t):
        j = bisect.bisect_right(t, ti - ms) - 1
        z.append(None if j < 0 or ti - ms - t[j] > 2000 else (lb[i] - lb[j]) / (sig[i] * root))
    out = dict(w)
    out["z"] = z
    return out


def _job(args):
    w, s, ms = args
    return simulate(with_trigger(w, ms), s)


def paper_settings() -> Settings:
    st = json.load(open(os.path.join(replay.ROOT, "data", "paper", "btc", "state.json")))
    vals = st["controls"]["values"]
    names = {f.name for f in fields(Settings)}
    s = replace(Settings(), **{k: v for k, v in vals.items() if k in names})
    return replace(s, fav_enabled=False, late_enabled=False)


def main() -> None:
    ws = load()
    split = int(len(ws) * 0.6)
    base = paper_settings()
    print(f"{len(ws)} BTC windows, TRAIN {split} / TEST {len(ws) - split}; paper settings: clips on={base.taker_enabled} "
          f"edge {base.taker_edge} price {base.min_price}-{base.max_price} pairs={base.pair_enabled} "
          f"(<{base.pair_target}) alternate={base.alternate_sides} cap {base.max_window_shares} "
          f"clips off {base.taker_stop_left_s}s left", flush=True)
    args = sys.argv[1:]
    if "--pairs-off" in args:                      # clips alone: no pair bid after a clip fills
        args.remove("--pairs-off")
        base = replace(base, pair_enabled=False)
        print("pair bids OFF", flush=True)
    grid = [(sg, ms) for ms in MS for sg in SIGMAS]
    if args:
        grid = [tuple(float(x) if "." in x else int(x) for x in a.split("x")) for a in args]
    out = []
    with Pool(3) as pool:
        for sg, ms in grid:
            s = replace(base, trigger_sigma=sg, trigger_ms=int(ms))
            res = pool.map(_job, [(w, s, int(ms)) for w in ws], chunksize=10)
            r = report(f"{sg:.1f} sigma within {int(ms)} ms", res, split)
            r.update(sigma=sg, ms=int(ms))
            out.append(r)
            with open("/tmp/xshark_replay_trigger_results" + ("_nopair" if not base.pair_enabled else "") + ".json", "w") as f:
                json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
