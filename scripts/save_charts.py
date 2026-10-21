"""Save a chart image of every window a bot traded, and keep that window's data for good.

For each bot folder (data/real, data/paper/<coin>), every settled window with at least one
fill gets, in <folder>/charts/:
    <GMT+9 start>_<pnl>_<slug>.png   the console's window card: prices, fills, the coin
                                     against the strike with why each order was sent
    <slug>.json                      the window's chart data (windows/ is pruned after 7 days)

The image is the console's own page (?print=<slug>) photographed by a headless Chromium,
so it matches what the console shows. Runs as pm2 app xshark-charts; already-saved windows
are skipped, so a restart backfills whatever is missing.

    .venv/bin/python scripts/save_charts.py            # loop, every 60 s
    .venv/bin/python scripts/save_charts.py --once     # one pass and exit
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME = "/root/.cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell"
HOST = "149.33.42.112"
TZ_SHIFT_S = 9 * 3600

log = logging.getLogger("save_charts")


def targets() -> list[tuple[str, str]]:
    """(bot folder, console page URL prefix) for the real bot and each paper coin."""
    out = [(os.path.join(HERE, "data", "real"), f"http://{HOST}:8796/?theme=light")]
    paper = os.path.join(HERE, "data", "paper")
    if os.path.isdir(paper):
        for coin in sorted(os.listdir(paper)):
            d = os.path.join(paper, coin)
            if os.path.isfile(os.path.join(d, "windows.jsonl")):
                out.append((d, f"http://{HOST}:8795/?asset={coin}&theme=light"))
    return out


def traded_windows(folder: str) -> list[dict]:
    rows = []
    try:
        with open(os.path.join(folder, "windows.jsonl")) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("fills"):
                    rows.append(r)
    except OSError:
        pass
    return rows


def image_name(r: dict) -> str:
    when = time.strftime("%Y%m%d-%H%M", time.gmtime(r["start"] + TZ_SHIFT_S))
    pnl = r.get("pnl") or 0.0
    return f"{when}_{'+' if pnl >= 0 else '-'}{abs(pnl):.2f}_{r['slug']}.png"


def shoot(url: str, path: str, height: int) -> bool:
    tmp = path + ".tmp.png"
    cmd = [CHROME, "--no-sandbox", "--hide-scrollbars", "--disable-gpu", f"--window-size=1300,{height}",
           "--virtual-time-budget=12000", f"--screenshot={tmp}", url]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90, check=False)
    except subprocess.TimeoutExpired:
        log.warning("timed out: %s", url)
        return False
    if not os.path.exists(tmp) or os.path.getsize(tmp) < 5000:
        log.warning("no image for %s", url)
        return False
    os.replace(tmp, path)
    return True


def one_pass(limit: int) -> int:
    saved = 0
    for folder, base in targets():
        charts = os.path.join(folder, "charts")
        os.makedirs(charts, exist_ok=True)
        have = {n.split("_", 2)[-1][:-4] for n in os.listdir(charts) if n.endswith(".png")}
        for r in traded_windows(folder):
            slug = r["slug"]
            doc = os.path.join(folder, "windows", f"{slug}.json")
            if slug in have or not os.path.exists(doc):
                continue
            shutil.copyfile(doc, os.path.join(charts, f"{slug}.json"))
            height = min(2600, 1100 + 42 * int(r.get("fills") or 1))
            if shoot(f"{base}&print={slug}", os.path.join(charts, image_name(r)), height):
                saved += 1
                log.info("saved %s/%s", os.path.relpath(charts, HERE), image_name(r))
            if saved >= limit:
                return saved
    return saved


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--every", type=float, default=60.0, help="seconds between passes (default 60)")
    args = ap.parse_args()
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    while True:
        try:
            one_pass(limit=10_000 if args.once else 40)
        except Exception:                             # noqa: BLE001 -- the loop must survive
            log.exception("pass failed")
        if args.once:
            return
        time.sleep(args.every)


if __name__ == "__main__":
    main()
