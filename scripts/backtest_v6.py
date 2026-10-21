"""Backtest Strategy V6 (the bot's own ``xshark.v6.decide``) on the recorded BTC 5-minute markets, with the fill
model of scripts/backtest_v3.py (calibrated latency and race odds, partial fills at the recorded best-ask size).

    nice .venv/bin/python scripts/backtest_v6.py [last N markets]
"""
import collections
import os
import sys
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_v3 import batch, load_windows, report      # noqa: E402
from xshark.v6 import V6Params                           # noqa: E402

if __name__ == "__main__":
    ws = [w for w in load_windows() if w["slug"].startswith("btc-")]
    if len(sys.argv) > 1:
        ws = ws[-int(sys.argv[1]):]
    print(f"{len(ws)} markets, {ws[0]['slug']} .. {ws[-1]['slug']}")
    P = V6Params(enabled=True)
    variants = {
        "V6 (defaults)": P,
        "fallen = any drop (0.00)": replace(P, fall_th=0.0),
        "fallen = 0.03": replace(P, fall_th=0.03),
        "either token after a hedge": replace(P, next_any=True),
        "hedge x0.5": replace(P, hedge_mult=0.5),
    }
    res = {}
    for lab, p in variants.items():
        res[lab] = batch(p, ws, procs=2, pfield="v6_params")
        report(lab, res[lab])
    full = res["V6 (defaults)"]
    cut = int(len(full) * 0.6)
    print("\nby period (nothing fitted):")
    report("first 60%", full[:cut]); report("last 40%", full[cut:])
    print("\nby market type (labels overlap):")
    for k, _ in collections.Counter(k for r in full for k in r["kind"]).most_common():
        report(k, [r for r in full if k in r["kind"]])
    print("\nfills by kind:", dict(sum((r["tags"] for r in full), collections.Counter())))
