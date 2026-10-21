"""Backtest Strategy V5 (the bot's own ``xshark.v5.decide``) on the recorded BTC 5-minute markets, with the fill
model of scripts/backtest_v3.py (calibrated latency and race odds, partial fills at the recorded best-ask size,
nothing deeper than the best level).

    nice .venv/bin/python scripts/backtest_v5.py [last N markets]
"""
import collections
import os
import sys
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_v3 import batch, load_windows, report      # noqa: E402
from xshark.v5 import V5Params                           # noqa: E402

if __name__ == "__main__":
    ws = [w for w in load_windows() if w["slug"].startswith("btc-")]
    if len(sys.argv) > 1:
        ws = ws[-int(sys.argv[1]):]
    print(f"{len(ws)} markets, {ws[0]['slug']} .. {ws[-1]['slug']}")
    P = V5Params(enabled=True)
    variants = {
        "V5 complete (defaults)": P,
        "momentum only (no balancing)": replace(P, bal_max_delay_s=0.0),
        "pair under 0.95": replace(P, max_pair=0.95),
        "pair under 0.99": replace(P, max_pair=0.99),
        "orders 5-8": replace(P, max_order=8.0),
        "worst-case loss $10": replace(P, max_worst_loss=10.0, max_unmatched_cost=20.0),
    }
    res = {}
    for lab, p in variants.items():
        res[lab] = batch(p, ws, pfield="v5_params")
        report(lab, res[lab])
    full = res["V5 complete (defaults)"]
    cut = int(len(full) * 0.6)
    print("\nby period (nothing fitted):")
    report("first 60%", full[:cut]); report("last 40%", full[cut:])
    print("\nby market type (labels overlap):")
    for k, _ in collections.Counter(k for r in full for k in r["kind"]).most_common():
        report(k, [r for r in full if k in r["kind"]])
    print("\nfills by kind:", dict(sum((r["tags"] for r in full), collections.Counter())))
    both = sum(1 for r in full if r["fills"] and r["worst"] > 0)
    print(f"markets ending with both outcomes positive: {both} of {sum(1 for r in full if r['fills'])} traded")
