"""HTTP server for the xshark console, in Bo-BOTT's shape.

A stdlib ThreadingHTTPServer, JSON under /api, static files under /static.
It only READS the bot's files (data/bot/...) and writes two things: the
tuning overrides (controls.json) and the halt switch (KILL). Both writes need
the console key in data/bot/console_key (made on first start, root-only), as
compare_bot's console does, because a console may be opened beyond loopback.

    GET  /api/live?since=<ms>        state.json; the price path only after <ms>
    GET  /api/windows?page=&size=    settled windows, newest first, + summary + benchmark
    GET  /api/window?slug=           one archived window (price path, fills, result)
    GET  /api/fills?limit=           recent fills + quality by leg (edge, markouts)
    GET  /api/earnings?period=       day | week | month, on the GMT+9 calendar
    GET  /api/controls               the tunables, what is stored, what the bot runs
    POST /api/controls               {values: {...}}   (key)
    POST /api/controls/reset         back to the defaults (key)
    POST /api/restart                {balance}: the paper bot starts again with this cash (key)
    POST /api/kill                   {on: bool}        (key)
    POST /api/loss/reset             today's loss count starts from zero now (key)
"""
from __future__ import annotations

import hmac
import json
import logging
import math
import mimetypes
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import config
from ..market import ASSETS

log = logging.getLogger(__name__)

STATIC = Path(__file__).with_name("static")
SLUG_RE = re.compile(rf"^(?:{'|'.join(map(re.escape, ASSETS))})-updown-5m-\d{{9,11}}$")
TZ_SHIFT_S = 9 * 3600          # every calendar bucket is GMT+9, like every clock on the page

#: The trader this bot copies, BTC 5m only, from its full activity log
#: (data/x-MoneyForWhiskas_activity.csv, 2026-06-10 .. 2026-09-30).
BENCHMARK = {
    "name": "x-MoneyForWhiskas",
    "source": "Polymarket activity log, BTC 5m only, 2026-06-10 to 2026-09-30, 28,101 windows",
    "windows_per_day": 275.5,
    "fills_per_window": 31,
    "both_sides": 0.966,
    "hedge": 0.81,
    "pair_cost": 0.992,
    "maker_fill_share": 0.44,
    "maker_return": 0.0241,
    "taker_return": 0.0033,
    "taker_fee_share": 0.0299,
    "return_on_cost": 0.0086,
    "window_win_rate": 0.533,
    "median_window_pnl": 1.99,
    "first_fill_s": 17,
    "last_fill_s": 217,
    "monthly_return": {"2026-06": 0.018, "2026-07": 0.016, "2026-08": 0.010, "2026-09": 0.003},
}


def _clean(v):
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


class Jsonl:
    """An append-only JSONL file, parsed incrementally (only new lines are read)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.rows: list[dict] = []
        self._offset = 0
        self._ino = None
        self._lock = threading.Lock()

    def get(self) -> list[dict]:
        with self._lock:
            try:
                st = os.stat(self.path)
            except OSError:
                self.rows, self._offset, self._ino = [], 0, None
                return self.rows
            if st.st_ino != self._ino or st.st_size < self._offset:
                self.rows, self._offset, self._ino = [], 0, st.st_ino
            if st.st_size > self._offset:
                with open(self.path, "rb") as f:
                    f.seek(self._offset)
                    chunk = f.read()
                end = chunk.rfind(b"\n") + 1            # leave a half-written last line for next time
                for line in chunk[:end].splitlines():
                    try:
                        self.rows.append(json.loads(line))
                    except ValueError:
                        continue
                self._offset += end
            return self.rows


class Console:
    def __init__(self, data_dir: str, key: str | None = None, only_on: bool = False) -> None:
        self.dir = data_dir
        self.only_on = only_on                    # Bot variables: only the settings in use (web --only-on)
        os.makedirs(data_dir, exist_ok=True)
        self.windows = Jsonl(os.path.join(data_dir, "windows.jsonl"))
        self.fills = Jsonl(os.path.join(data_dir, "fills.jsonl"))
        self.key = key or self._console_key()

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def _console_key(self) -> str:
        p = self.path("console_key")
        try:
            with open(p) as f:
                k = f.read().strip()
            if k:
                return k
        except OSError:
            pass
        k = secrets.token_urlsafe(18)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(k + "\n")
        log.info("made the console write key: %s", p)
        return k

    # ------------------------------------------------------------ reads

    def state(self) -> dict | None:
        try:
            with open(self.path("state.json"), "rb") as f:
                st = json.loads(f.read())
        except (OSError, ValueError):
            return None
        age = time.time() - st.get("ts", 0) / 1000.0
        st["age_s"] = round(age, 1)
        st["alive"] = bool(st.get("running")) and age < 5.0
        return st

    def live(self, qs: dict) -> dict:
        st = self.state()
        if st is None:
            return {"running": False, "alive": False, "reason": "no state file yet: the bot has not started"}
        since = _num(qs, "since")
        slug = (qs.get("slug") or [""])[0]
        w = st.get("window") or {}
        if since is not None and slug and slug == w.get("slug"):
            st["hist"] = [p for p in st.get("hist") or [] if p[0] > since]
            st["history_partial"] = True
        return st

    def window_doc(self, slug: str) -> dict | None:
        if not SLUG_RE.match(slug or ""):
            return None
        try:
            with open(os.path.join(self.dir, "windows", f"{slug}.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def windows_page(self, qs: dict) -> dict:
        rows = self.windows.get()
        page = int(_num(qs, "page") or 0)
        size = int(min(max(_num(qs, "size") or 50, 10), 200))
        traded = [r for r in rows if r.get("fills")]
        newest = list(reversed(rows))
        curve, cum, cm, ct = [], 0.0, 0.0, 0.0
        for r in rows:
            cum += r.get("pnl") or 0.0
            cm += r.get("maker_pnl") or 0.0
            ct += r.get("taker_pnl") or 0.0
            # [close, cumulative, this window, cumulative maker, cumulative taker, slug]
            curve.append([r.get("end"), round(cum, 4), r.get("pnl"), round(cm, 4), round(ct, 4), r.get("slug")])
        both = [r for r in traded if (r.get("up_shares") or 0) > 0 and (r.get("down_shares") or 0) > 0]
        cost = sum(r.get("cost") or 0.0 for r in traded)
        mk_cost = sum(r.get("maker_cost") or 0.0 for r in traded)
        tk_cost = sum(r.get("taker_cost") or 0.0 for r in traded)
        pnls = sorted(r.get("pnl") or 0.0 for r in traded)
        med = lambda xs: xs[len(xs) // 2] if xs else None
        summary = {
            "windows": len(rows), "traded": len(traded),
            "wins": sum((r.get("pnl") or 0) > 0 for r in traded),
            "pnl": sum(r.get("pnl") or 0.0 for r in rows),
            "maker_pnl": sum(r.get("maker_pnl") or 0.0 for r in rows),
            "taker_pnl": sum(r.get("taker_pnl") or 0.0 for r in rows),
            "fees": sum(r.get("fees") or 0.0 for r in rows),
            "rebate_est": sum(r.get("rebate_est") or 0.0 for r in rows),
            "cost": cost, "maker_cost": mk_cost, "taker_cost": tk_cost,
            "return_on_cost": (sum(r.get("pnl") or 0.0 for r in traded) / cost) if cost > 0 else None,
            "maker_return": (sum(r.get("maker_pnl") or 0.0 for r in traded) / mk_cost) if mk_cost > 0 else None,
            "taker_return": (sum(r.get("taker_pnl") or 0.0 for r in traded) / tk_cost) if tk_cost > 0 else None,
            "median_window_pnl": med(pnls),
            "both_sides": len(both) / len(traded) if traded else None,
            "hedge": med(sorted(r["hedge"] for r in traded if r.get("hedge") is not None)),
            "pair_cost": med(sorted(r["pair_cost"] for r in both if r.get("pair_cost") is not None)),
            "fills_per_window": med(sorted(r.get("fills") or 0 for r in traded)),
            "maker_fill_share": (sum(r.get("maker_fills") or 0 for r in traded)
                                 / max(1, sum(r.get("fills") or 0 for r in traded))) if traded else None,
            "first_fill_s": med(sorted(r["first_fill_s"] for r in traded if r.get("first_fill_s") is not None)),
            "last_fill_s": med(sorted(r["last_fill_s"] for r in traded if r.get("last_fill_s") is not None)),
            "window_win_rate": (sum((r.get("pnl") or 0) > 0 for r in traded) / len(traded)) if traded else None,
        }
        return {"rows": newest[page * size:(page + 1) * size], "page": page, "size": size, "total": len(rows),
                "summary": summary, "curve": curve[-3000:], "benchmark": BENCHMARK}

    def fills_view(self, qs: dict) -> dict:
        rows = self.fills.get()
        limit = int(min(max(_num(qs, "limit") or 300, 10), 2000))
        by = {}
        for r in rows:
            for key in (r.get("kind"), f"{r.get('kind')}:{r.get('how')}"):
                a = by.setdefault(key, {"n": 0, "shares": 0.0, "cost": 0.0, "pnl": 0.0, "wins": 0,
                                        "edge_w": 0.0, "edge_sh": 0.0, "mo_1s": [0.0, 0.0], "mo_5s": [0.0, 0.0],
                                        "mo_30s": [0.0, 0.0], "price_w": 0.0, "fees": 0.0, "rebate_est": 0.0})
                sh = r.get("size") or 0.0
                a["n"] += 1
                a["shares"] += sh
                a["cost"] += (r.get("price") or 0.0) * sh + (r.get("fee") or 0.0)
                a["price_w"] += (r.get("price") or 0.0) * sh
                a["pnl"] += r.get("pnl") or 0.0
                a["fees"] += r.get("fee") or 0.0
                a["rebate_est"] += r.get("rebate_est") or 0.0
                a["wins"] += 1 if r.get("won") else 0
                if r.get("fair") is not None:
                    fair = r["fair"]
                    a["edge_w"] += (fair - (r.get("price") or 0.0)) * sh
                    a["edge_sh"] += sh
                for h in ("mo_1s", "mo_5s", "mo_30s"):
                    if r.get(h) is not None:
                        a[h][0] += r[h] * sh
                        a[h][1] += sh
        summary = {}
        for k, a in by.items():
            sh = a["shares"] or 1.0
            summary[k] = {
                "fills": a["n"], "shares": a["shares"], "cost": a["cost"], "pnl": a["pnl"],
                "fees": a["fees"], "rebate_est": a["rebate_est"],
                "win_rate": a["wins"] / a["n"] if a["n"] else None,
                "avg_price": a["price_w"] / sh, "pnl_per_share": a["pnl"] / sh,
                "return": a["pnl"] / a["cost"] if a["cost"] > 0 else None,
                "edge_at_fill": a["edge_w"] / a["edge_sh"] if a["edge_sh"] else None,
                **{h: (a[h][0] / a[h][1] if a[h][1] else None) for h in ("mo_1s", "mo_5s", "mo_30s")},
            }
        clips = {}
        for w in self.windows.get():
            for k, v in (w.get("counts") or {}).items():
                if k.startswith("taker_"):
                    clips[k[6:]] = clips.get(k[6:], 0) + v
        per_window = [{"slug": w.get("slug"), "start": w.get("start"), "end": w.get("end"), "winner": w.get("winner"),
                       "pnl": w.get("pnl") or 0.0, "maker_pnl": w.get("maker_pnl") or 0.0,
                       "taker_pnl": w.get("taker_pnl") or 0.0, "fees": w.get("fees") or 0.0,
                       "cost": w.get("cost") or 0.0, "maker_fills": w.get("maker_fills") or 0,
                       "taker_fills": w.get("taker_fills") or 0}
                      for w in self.windows.get()[-288:]]
        return {"rows": list(reversed(rows[-limit:])), "total": len(rows), "summary": summary,
                "clips": clips, "calibration": (self.state() or {}).get("calibration"), "per_window": per_window}

    def earnings(self, qs: dict) -> dict:
        period = (qs.get("period") or ["day"])[0]
        if period not in ("day", "week", "month"):
            period = "day"
        buckets: dict[str, dict] = {}
        for r in self.windows.get():
            t = time.gmtime((r.get("end") or 0) + TZ_SHIFT_S)
            if period == "day":
                key = time.strftime("%Y-%m-%d", t)
            elif period == "month":
                key = time.strftime("%Y-%m", t)
            else:
                monday = (r.get("end") or 0) + TZ_SHIFT_S - t.tm_wday * 86400
                key = time.strftime("%Y-%m-%d", time.gmtime(monday))
            b = buckets.setdefault(key, {"key": key, "pnl": 0.0, "maker_pnl": 0.0, "taker_pnl": 0.0,
                                         "fees": 0.0, "rebate_est": 0.0, "cost": 0.0, "windows": 0, "traded": 0})
            for k in ("pnl", "maker_pnl", "taker_pnl", "fees", "rebate_est", "cost"):
                b[k] += r.get(k) or 0.0
            b["windows"] += 1
            b["traded"] += 1 if r.get("fills") else 0
        span = {"day": 30, "week": 26, "month": 12}[period]
        rows = sorted(buckets.values(), key=lambda b: b["key"])[-span:]
        return {"period": period, "rows": rows, "tz": "GMT+9"}

    # ------------------------------------------------------------ controls

    def controls_view(self) -> dict:
        try:
            with open(self.path("controls.json")) as f:
                stored = json.load(f) or {}
        except (OSError, ValueError):
            stored = {}
        st = self.state() or {}
        c = st.get("controls") or {}
        strategy = (st.get("model") or {}).get("strategy")
        if strategy == "cross-market pairs":
            spec = [r for r in config.CONTROLS if str(r["key"]).startswith("cross_pair_")]
        elif self.only_on:
            spec = config.visible_spec({**config.defaults(), **stored})
        else:
            spec = [r for r in config.CONTROLS
                    if not str(r["key"]).startswith("cross_pair_")
                    and (r["group"] != "Triangle" or stored.get("tri_enabled"))]
        return {"spec": spec, "stored": stored, "defaults": config.defaults(),
                "running": c.get("values") if st.get("alive") else None,
                "strategy": strategy,
                "applied_at": c.get("applied_at"), "applied": c.get("applied"), "notes": c.get("notes"),
                "kill": os.path.exists(self.path("KILL")), "alive": bool(st.get("alive")),
                "today": (st.get("stats") or {}).get("today"), "loss_reset_at": (st.get("stats") or {}).get("loss_reset_at"),
                "halted": st.get("halted")}

    def set_controls(self, body: dict) -> dict:
        clean, notes = config.clamp_values((body or {}).get("values") or {})
        try:
            with open(self.path("controls.json")) as f:
                stored = json.load(f) or {}
        except (OSError, ValueError):
            stored = {}
        stored.update(clean)
        stored = {k: v for k, v in stored.items() if k in config.SPEC}
        tmp = self.path("controls.json.tmp")
        with open(tmp, "w") as f:
            json.dump(stored, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path("controls.json"))
        return {**self.controls_view(), "notes_saved": notes}

    def reset_controls(self) -> dict:
        if self.only_on:
            # only what this console shows goes back to its default: the hidden (switched-off) settings stay
            # as stored, so a reset never turns a hidden strategy back on
            view = self.controls_view()
            shown = {r["key"] for r in view["spec"]}
            stored = {k: v for k, v in view["stored"].items() if k not in shown}
            tmp = self.path("controls.json.tmp")
            with open(tmp, "w") as f:
                json.dump(stored, f, indent=1, sort_keys=True)
            os.replace(tmp, self.path("controls.json"))
            return self.controls_view()
        try:
            os.remove(self.path("controls.json"))
        except OSError:
            pass
        return self.controls_view()

    def reset_loss(self) -> dict:
        """Start today's loss count from zero now; the bot picks it up within ~2 s."""
        tmp = self.path("loss_reset.tmp")
        with open(tmp, "w") as f:
            f.write(f"{time.time():.3f}\n")
        os.replace(tmp, self.path("loss_reset"))
        return self.controls_view()

    def request_sell(self, body: dict) -> dict:
        """Ask the bot to sell what it holds on one side of the current window (it acts within ~0.2 s)."""
        o = (body or {}).get("outcome")
        if o not in ("Up", "Down"):
            return {"error": "outcome must be Up or Down"}
        req = {"outcome": o, "slug": str((body or {}).get("slug") or ""), "ts": time.time()}
        tmp = self.path("sell_request.json.tmp")
        with open(tmp, "w") as f:
            json.dump(req, f)
        os.replace(tmp, self.path("sell_request.json"))
        return {"ok": True, "requested": req}

    def request_restart(self, body: dict) -> dict:
        """Ask the PAPER bot to start again with new cash: it archives its ledger (fills, windows, charts),
        keeps the settings, saves the balance (balance.json) and exits; pm2 starts it again (~10 s)."""
        st = self.state() or {}
        if st.get("mode") != "paper":
            return {"error": "only the paper bot restarts with new cash (the real bot's cash is your Polymarket account)"}
        try:
            bal = float((body or {}).get("balance"))
        except (TypeError, ValueError):
            return {"error": "the cash must be a number"}
        if not (1.0 <= bal <= 1_000_000.0):
            return {"error": "the cash must be 1 to 1,000,000 USDC"}
        req = {"balance": round(bal, 2), "ts": time.time()}
        tmp = self.path("reset_request.json.tmp")
        with open(tmp, "w") as f:
            json.dump(req, f)
        os.replace(tmp, self.path("reset_request.json"))
        return {"ok": True, "requested": req}

    def set_kill(self, on: bool) -> dict:
        p = self.path("KILL")
        if on:
            with open(p, "w") as f:
                f.write(f"halted from the console at {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC\n")
        else:
            try:
                os.remove(p)
            except OSError:
                pass
        return self.controls_view()


class Consoles:
    """One console per coin: ``data_dir/<coin>/`` for each coin found (or one dir, no coins).

    Reads take ``?asset=``; every write (tuning, halt, loss reset) goes to ALL coins,
    because they run one strategy. One write key, ``data_dir/console_key``."""

    def __init__(self, data_dir: str, only_on: bool = False) -> None:
        subs = [a for a in ASSETS if os.path.isdir(os.path.join(data_dir, a))]
        if subs:
            root = Console.__new__(Console)
            root.dir = data_dir
            key = Console._console_key(root)
            self.by_asset = {a: Console(os.path.join(data_dir, a), key=key, only_on=only_on) for a in subs}
        else:
            c = Console(data_dir, only_on=only_on)
            self.by_asset = {"": c}
        self.key = next(iter(self.by_asset.values())).key

    def pick(self, qs: dict) -> Console:
        a = ((qs.get("asset") or [""])[0] or "").lower()
        return self.by_asset.get(a) or next(iter(self.by_asset.values()))

    def assets(self) -> dict:
        out = []
        for a, c in self.by_asset.items():
            st = c.state() or {}
            s = st.get("stats") or {}
            out.append({"asset": a, "coin": (st.get("asset") or a or "").lower(), "mode": st.get("mode"),
                        "alive": bool(st.get("alive")), "halted": st.get("halted"),
                        "equity": st.get("equity"), "balance0": st.get("balance0"), "pnl": s.get("pnl"),
                        "today": s.get("today"), "windows": s.get("windows")})
        return {"assets": out, "multi": "" not in self.by_asset}

    def window_doc(self, slug: str) -> dict | None:
        """Open the archive from the market directory named by its slug."""
        asset = (slug or "").split("-updown-5m-", 1)[0].lower()
        console = self.by_asset.get(asset)
        if console is not None:
            return console.window_doc(slug)
        for console in self.by_asset.values():
            doc = console.window_doc(slug)
            if doc is not None:
                return doc
        return None

    def overall_per_window(self, limit: int = 288) -> list[dict]:
        """Combine the seven per-market settlement ledgers by shared 5-minute window."""
        grouped: dict[int, dict] = {}
        for asset, console in self.by_asset.items():
            if not asset:
                continue
            for row in console.windows.get():
                start = row.get("start")
                if start is None:
                    continue
                try:
                    start = int(start)
                except (TypeError, ValueError):
                    continue
                overall = grouped.setdefault(start, {
                    "start": start, "end": row.get("end"), "slug": None,
                    "pnl": 0.0, "maker_pnl": 0.0, "taker_pnl": 0.0,
                    "fees": 0.0, "cost": 0.0, "maker_fills": 0, "taker_fills": 0,
                    "markets": {},
                })
                pnl = row.get("pnl") or 0.0
                overall["pnl"] += pnl
                overall["maker_pnl"] += row.get("maker_pnl") or 0.0
                overall["taker_pnl"] += row.get("taker_pnl") or 0.0
                overall["fees"] += row.get("fees") or 0.0
                overall["cost"] += row.get("cost") or 0.0
                overall["maker_fills"] += row.get("maker_fills") or 0
                overall["taker_fills"] += row.get("taker_fills") or 0
                overall["markets"][asset.upper()] = round(pnl, 4)
                if row.get("fills"):
                    overall["slug"] = row.get("slug")
        rows = [grouped[k] for k in sorted(grouped)]
        for row in rows:
            for key in ("pnl", "maker_pnl", "taker_pnl", "fees", "cost"):
                row[key] = round(row[key], 4)
        return rows[-max(1, int(limit)):]

    def write_all(self, fn) -> None:
        for c in self.by_asset.values():
            fn(c)

    def rule_pnl(self) -> dict:
        """Settled PnL by cross-pair rule, summed across every market console."""
        out = {"rule1": 0.0, "rule2": 0.0, "rule3": 0.0, "rule4": 0.0, "rule5": 0.0,
               "rule6": 0.0, "rule7": 0.0, "cset": 0.0}
        for c in self.by_asset.values():
            s = ((c.state() or {}).get("stats") or {})
            for k in out:
                out[k] += s.get(f"{k}_pnl") or 0.0
        return {k: round(v, 6) for k, v in out.items()}


def _num(qs: dict, key: str) -> float | None:
    try:
        v = float((qs.get(key) or [""])[0])
        return v if math.isfinite(v) else None
    except ValueError:
        return None


def make_handler(consoles: Consoles):
    class Handler(BaseHTTPRequestHandler):
        server_version = "xshark-console"

        def log_message(self, fmt, *args):          # quiet: the page polls twice a second
            return

        def _json(self, obj, status: int = 200) -> None:
            body = json.dumps(_clean(obj), separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _static(self, rel: str) -> None:
            p = (STATIC / rel).resolve()
            if STATIC.resolve() not in p.parents or not p.is_file():
                self.send_error(404)
                return
            body = p.read_bytes()
            ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/")
                                                       or ctype.endswith("javascript") else ""))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:                   # noqa: N802
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            console = consoles.pick(qs)
            try:
                if u.path == "/api/assets":
                    return self._json(consoles.assets())
                if u.path in ("/", "/index.html"):
                    return self._static("index.html")
                if u.path.startswith("/static/"):
                    return self._static(u.path[len("/static/"):])
                if u.path == "/api/live":
                    st = console.live(qs)
                    if (st.get("model") or {}).get("strategy") == "cross-market pairs":
                        st["rule_pnl"] = consoles.rule_pnl()
                    return self._json(st)
                if u.path == "/api/windows":
                    return self._json(console.windows_page(qs))
                if u.path == "/api/window":
                    slug = (qs.get("slug") or [""])[0]
                    doc = consoles.window_doc(slug) if "" not in consoles.by_asset else console.window_doc(slug)
                    return self._json(doc if doc else {"error": "no such window"}, 200 if doc else 404)
                if u.path == "/api/fills":
                    result = console.fills_view(qs)
                    if "" not in consoles.by_asset:
                        result["per_window"] = consoles.overall_per_window()
                        result["per_window_scope"] = "all_markets"
                    return self._json(result)
                if u.path == "/api/earnings":
                    return self._json(console.earnings(qs))
                if u.path == "/api/controls":
                    return self._json(console.controls_view())
            except Exception as exc:                # noqa: BLE001
                log.exception("GET %s", u.path)
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            self.send_error(404)

        def do_POST(self) -> None:                  # noqa: N802
            u = urlparse(self.path)
            console = consoles.pick(parse_qs(u.query))
            key = self.headers.get("X-Console-Key") or ""
            if not hmac.compare_digest(key.encode(), consoles.key.encode()):
                return self._json({"error": "console key required"}, 401)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(n, 65536)) or b"{}") if n else {}
            except ValueError:
                return self._json({"error": "bad JSON"}, 400)
            try:
                # every coin runs one strategy: a change goes to all of them
                if u.path == "/api/controls":
                    notes = []
                    consoles.write_all(lambda c: notes.append(c.set_controls(body).get("notes_saved")))
                    return self._json({**console.controls_view(), "notes_saved": notes[0] if notes else []})
                if u.path == "/api/controls/reset":
                    consoles.write_all(lambda c: c.reset_controls())
                    return self._json(console.controls_view())
                if u.path == "/api/sell":
                    res = console.request_sell(body)
                    return self._json(res, 400 if "error" in res else 200)
                if u.path == "/api/restart":
                    res = []
                    consoles.write_all(lambda c: res.append(c.request_restart(body)))
                    out = next((r for r in res if "error" in r), res[0] if res else {"error": "no bot"})
                    return self._json(out, 400 if "error" in out else 200)
                if u.path == "/api/kill":
                    consoles.write_all(lambda c: c.set_kill(bool(body.get("on"))))
                    return self._json(console.controls_view())
                if u.path == "/api/loss/reset":
                    consoles.write_all(lambda c: c.reset_loss())
                    return self._json(console.controls_view())
            except Exception as exc:                # noqa: BLE001
                log.exception("POST %s", u.path)
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            self.send_error(404)

    return Handler


def serve(host: str, port: int, data_dir: str, only_on: bool = False) -> None:
    consoles = Consoles(data_dir, only_on=only_on)
    httpd = ThreadingHTTPServer((host, port), make_handler(consoles))
    httpd.daemon_threads = True
    log.info("xshark console on http://%s:%d/ (data %s)", host, port, data_dir)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
