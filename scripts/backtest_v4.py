"""Backtest Strategy V4 (the bot's own ``xshark.v4.decide``) on the recorded BTC 5-minute markets, with the fill
model of scripts/backtest_v3.py (calibrated latency and race odds, partial fills at the recorded best-ask size,
nothing deeper). Compares the complete strategy with momentum-only and balancing-only baselines.

    nice .venv/bin/python scripts/backtest_v4.py
"""
import collections
import os
import sys
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest_v3 import batch, load_windows, report      # noqa: E402
from xshark.v4 import V4Params                           # noqa: E402


def flags(lab, R):
    traded = [r for r in R if r["fills"]] or [None]
    reach = sum(bool(r["flags"].get("v4_reached")) for r in R if r)
    both = sum(bool(r["flags"].get("v4_bothneg")) for r in R if r)
    nos = sum(r["flags"].get("v4_cnt_nosize") or 0 for r in R if r)
    print(f"   {lab}: target pair reached (within $3) in {reach} of {len(traded)} traded markets ({reach / max(1, len(traded)):.0%}); "
          f"both outcomes negative at some point in {both}; 'no permitted size' reports {nos}")


if __name__ == "__main__":
    ws = [w for w in load_windows() if w["slug"].startswith("btc-")]
    if len(sys.argv) > 1:
        ws = ws[-int(sys.argv[1]):]
    print(f"{len(ws)} markets, {ws[0]['slug']} .. {ws[-1]['slug']}")
    P = V4Params(enabled=True)
    OFF = 99.0
    variants = {
        "V4 complete": P,
        "momentum only": replace(P, entry_gap_s=1e9, bal_delay_s=1e9),
        "balancing only (+ entry)": replace(P, fast_th=OFF),
        "entry only (no balance, no momentum)": replace(P, fast_th=OFF, bal_delay_s=1e9),
    }
    res = {}
    for lab, p in variants.items():
        res[lab] = batch(p, ws, pfield="v4_params")
        report(lab, res[lab])
        flags(lab, res[lab])
    full = res["V4 complete"]
    cut = int(len(full) * 0.6)
    print("\nby period (nothing fitted):")
    report("first 60%", full[:cut]); report("last 40%", full[cut:])
    print("\nby market type (labels overlap):")
    for k, _ in collections.Counter(k for r in full for k in r["kind"]).most_common():
        report(k, [r for r in full if k in r["kind"]])
    print("\nfills by signal:", dict(sum((r["tags"] for r in full), collections.Counter())))
    print("\nsensitivity (first 60% / last 40%):")
    for lab, p in (("middle target x2 (+25.6/-24)", replace(P, mid_win=25.6, mid_lose=24.0)),
                   ("high target +25/-25", replace(P, high_lose=25.0)),
                   ("fast rise 0.06", replace(P, fast_th=0.06)),
                   ("min improvement $2", replace(P, min_improve=2.0))):
        R = batch(p, ws, pfield="v4_params")
        report(f"{lab} | in", R[:cut]); report(f"{lab} | out", R[cut:])
