#!/usr/bin/env python3
"""Pull a Polymarket user's full activity log (trades, redeems, rewards, rebates) into a CSV.

    python3 pull_activity.py x-MoneyForWhiskas
    python3 pull_activity.py 0x3048d65321be3497164cdfc2996f94f98a2e7537 --out data

The Data API caps /activity at offset 5000, so each UTC day is walked newest-to-oldest with an
`end` timestamp cursor instead of offsets. Finished days are cached, so re-runs only fetch new data.
"""
import argparse
import csv
import datetime as dt
import gzip
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
PAGE = 500  # /activity max limit
MAX_OFFSET = 5000  # /activity max offset
FIELDS = ["timestamp", "type", "side", "outcome", "outcomeIndex", "size", "price", "usdcSize",
          "title", "slug", "eventSlug", "conditionId", "asset", "transactionHash"]


def get(base, path, **params):
    url = f"{base}{path}?{urllib.parse.urlencode(params)}"
    for attempt in range(10):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "xshark/0.1"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                raise RuntimeError(f"HTTP {e.code} for {url}: {e.read().decode()[:200]}") from None
            print(f"HTTP {e.code}, retrying: {url}", file=sys.stderr, flush=True)
        except (OSError, http.client.HTTPException, ValueError) as e:
            print(f"{e!r}, retrying: {url}", file=sys.stderr, flush=True)
        time.sleep(min(2 ** attempt, 60))
    raise RuntimeError(f"giving up on {url}")


def resolve_wallet(user):
    if user.lower().startswith("0x") and len(user) == 42:
        return user.lower()
    found = get(GAMMA_API, "/public-search", q=user, search_profiles="true", limit_per_type=20)
    for profile in found.get("profiles") or []:
        if (profile.get("name") or "").lower() == user.lower():
            return profile["proxyWallet"]
    sys.exit(f"no Polymarket profile named {user!r}")


def fetch_range(wallet, start, end):
    """Every activity row with start <= timestamp <= end, newest first."""
    rows, cursor = [], end
    while cursor >= start:
        page = get(DATA_API, "/activity", user=wallet, start=start, end=cursor, limit=PAGE,
                   sortBy="TIMESTAMP", sortDirection="DESC")
        if len(page) < PAGE:
            return rows + page
        oldest = page[-1]["timestamp"]
        if page[0]["timestamp"] > oldest:
            # The oldest second may spill onto the next page, so leave all of it for the next request.
            rows += [r for r in page if r["timestamp"] > oldest]
            cursor = oldest
            continue
        # A full page from a single second: page through that second by offset instead.
        for offset in range(PAGE, MAX_OFFSET + 1, PAGE):
            more = get(DATA_API, "/activity", user=wallet, start=oldest, end=oldest, limit=PAGE,
                       offset=offset, sortBy="TIMESTAMP", sortDirection="DESC")
            page += more
            if len(more) < PAGE:
                break
        rows += page
        cursor = oldest - 1
    return rows


def utc_days(first_ts, last_ts):
    """(date, start, end) for each UTC day from first_ts through last_ts; end is inclusive."""
    day = dt.datetime.fromtimestamp(first_ts, dt.timezone.utc).date()
    while True:
        start = int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp())
        if start > last_ts:
            return
        yield day.isoformat(), start, start + 86399
        day += dt.timedelta(days=1)


def main():
    ap = argparse.ArgumentParser(description="Pull a Polymarket user's full activity log into a CSV.")
    ap.add_argument("user", help="Polymarket username or proxy wallet address")
    ap.add_argument("--out", default="data", help="output directory (default: ./data)")
    ap.add_argument("--workers", type=int, default=4, help="days fetched in parallel (default: 4)")
    args = ap.parse_args()

    wallet = resolve_wallet(args.user)
    now = int(time.time())
    first = get(DATA_API, "/activity", user=wallet, limit=1, sortBy="TIMESTAMP", sortDirection="ASC")
    if not first:
        sys.exit(f"no activity for {wallet}")
    cache_dir = os.path.join(args.out, ".cache", wallet)
    os.makedirs(cache_dir, exist_ok=True)

    def day_rows(day_range):
        day, start, end = day_range
        cached = os.path.join(cache_dir, f"{day}.json.gz")
        if os.path.exists(cached):
            with gzip.open(cached, "rt") as f:
                return json.load(f)
        newest_first = fetch_range(wallet, start, min(end, now))
        rows = [{k: r.get(k) for k in FIELDS} for r in reversed(newest_first)]
        if end < now - 3600:  # the day is over and the API has had time to index it
            with gzip.open(cached + ".tmp", "wt") as f:
                json.dump(rows, f)
            os.replace(cached + ".tmp", cached)
        return rows

    days = list(utc_days(first[0]["timestamp"], now))
    out_path = os.path.join(args.out, f"{args.user}_activity.csv")
    total = 0
    with ThreadPoolExecutor(args.workers) as pool, open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["datetime_utc"] + FIELDS)
        for (day, _, _), rows in zip(days, pool.map(day_rows, days)):
            for r in rows:
                when = dt.datetime.fromtimestamp(r["timestamp"], dt.timezone.utc)
                writer.writerow([when.strftime("%Y-%m-%d %H:%M:%S")] + [r[k] for k in FIELDS])
            total += len(rows)
            print(f"{day}  {len(rows):>8,} rows", file=sys.stderr, flush=True)
    print(f"{total:,} rows -> {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
