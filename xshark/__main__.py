"""python -m xshark  -- run the bot (one coin, or several coins in one process: --asset btc,eth,...)."""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import os
import signal

from .bot import ASSET_CTX, Bot
from .cross_pair import CrossPairStrategy
from .market import ASSETS
from .paper import PaperBankroll

log = logging.getLogger("xshark")


class _CoinTag(logging.Filter):
    """Prefix each line with the coin of the bot that logged it (several bots, one log)."""

    def filter(self, record: logging.LogRecord) -> bool:
        a = ASSET_CTX.get()
        record.coin = f"[{a}] " if a else ""
        return True


def _paper_bankroll(args, coins: list[str]) -> PaperBankroll:
    """Restore one paper cash pool from the shared baseline and all settled coin PnL."""
    os.makedirs(args.data_dir, exist_ok=True)
    path = os.path.join(args.data_dir, "paper_bankroll.json")
    try:
        with open(path) as fh:
            starting = float(json.load(fh)["balance"])
    except (OSError, ValueError, TypeError, KeyError):
        starting = float(args.balance)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"balance": starting, "scope": "shared"}, fh)
        os.replace(tmp, path)

    pnl = 0.0
    for coin in coins:
        ledger = os.path.join(args.data_dir, coin, "windows.jsonl")
        try:
            with open(ledger, "rb") as fh:
                for line in fh:
                    try:
                        pnl += float(json.loads(line).get("pnl") or 0.0)
                    except (ValueError, TypeError, AttributeError):
                        continue
        except OSError:
            pass
    return PaperBankroll(starting + pnl, starting, realized_pnl=pnl)


async def run_many(args, coins: list[str]) -> None:
    """Run per-coin workers with separate state and, in paper mode, one shared bankroll."""
    if not getattr(args, "live", False):
        args.paper_bankroll = _paper_bankroll(args, coins)
    bots: dict[str, Bot] = {}
    cross_pair = CrossPairStrategy(bots, args.data_dir) if getattr(args, "cross_pair", False) else None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async def one(coin: str) -> None:
        while not stop.is_set():
            a = copy.copy(args)
            a.asset, a.data_dir = coin, os.path.join(args.data_dir, coin)
            bot = None
            try:
                bot = Bot(a)
                bot.cross_pair = cross_pair
                bots[coin] = bot
                await bot.run(stop)
                return
            except Exception:                         # noqa: BLE001 -- one coin must not take the others down
                log.exception("%s bot failed; starting it again in 10 s", coin.upper())
                try:
                    await asyncio.wait_for(stop.wait(), 10.0)
                except asyncio.TimeoutError:
                    pass
            finally:
                if bot is not None and bots.get(coin) is bot:
                    bots.pop(coin, None)

    jobs = [one(c) for c in coins]
    if cross_pair is not None:
        jobs.append(cross_pair.run(stop))
    await asyncio.gather(*jobs)

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    ap = argparse.ArgumentParser(
        description="xshark paper bot: crypto 5m, resting bids on both sides + taker clips, held to settlement. "
                    "Fills are simulated against the live Polymarket books; no key, no orders.")
    ap.add_argument("--asset", default="btc",
                    help="the coin whose 5-minute Up/Down markets to trade (default btc); several, comma-"
                         "separated (or 'all'), run in this one process with separate state in --data-dir/<coin>/; "
                         "paper markets share --balance "
                         f"(coins: {','.join(ASSETS)})")
    ap.add_argument("--balance", type=float, default=40.0,
                    help="paper starting balance in USDC (shared across selected markets; default 40)")
    ap.add_argument("--cross-pair", action="store_true",
                    help="paper only: buy lagging tokens when other markets average high, then pair after a +0.30 rise; "
                         "also hold mid-band tokens when peers average over 0.90")
    ap.add_argument("--exchanges", default="coinbase,binance,bybit",
                    help="spot feeds for the composite (coinbase,binance,bybit,kraken)")
    ap.add_argument("--lead-window-ms", type=float, default=0.0,
                    help="composite lead mode window (0 = plain median; troll-poly-bot's live bot uses 30)")
    ap.add_argument("--price-source", choices=("composite", "chainlink"), default="composite",
                    help="the BTC price the model, vol and trigger use: the exchange composite (fast) or "
                         "Chainlink, the settlement feed (1 tick/s, ~1.3 s late)")
    ap.add_argument("--data-dir", default=os.path.join(HERE, "data", "bot"))
    live = ap.add_argument_group("real account (Polymarket)")
    live.add_argument("--live", action="store_true",
                      help="log in with the account in --env-file. Alone = SHADOW: read the balance and sign "
                           "every order, post nothing (fills stay simulated)")
    live.add_argument("--armed", action="store_true",
                      help="with --live: POST REAL ORDERS. Also needs TPB_LIVE_ACK=I_UNDERSTAND_REAL_MONEY")
    live.add_argument("--env-file", default=os.path.join(HERE, ".env"),
                      help="credentials file (TPB_POLY_* names, as bo-real-bot); never printed")
    live.add_argument("--max-order-usdc", type=float, default=5.0)
    live.add_argument("--max-open-usdc", type=float, default=20.0)
    live.add_argument("--max-daily-loss-usdc", type=float, default=10.0)
    live.add_argument("--max-orders-per-hour", type=int, default=300)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    coins = list(ASSETS) if args.asset.strip().lower() == "all" else \
        [c.strip().lower() for c in args.asset.split(",") if c.strip()]
    bad = [c for c in coins if c not in ASSETS]
    if bad or not coins:
        ap.error(f"--asset: unknown coin(s) {','.join(bad) or '(none)'}; choose from {','.join(ASSETS)}")
    if args.armed and not args.live:
        ap.error("--armed needs --live")
    if args.live and len(coins) > 1:
        ap.error("--live runs one coin per process")
    if args.cross_pair and (args.live or len(coins) < 2):
        ap.error("--cross-pair needs paper mode and at least two assets")
    if args.live:
        from dotenv import load_dotenv
        if not os.path.exists(args.env_file):
            ap.error(f"--live: {args.env_file} not found")
        load_dotenv(args.env_file, override=False)
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(coin)s%(name)s: %(message)s")
    for h in logging.getLogger().handlers:
        h.addFilter(_CoinTag())
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)      # the venue client logs every request
    # per-coin folders (--data-dir/<coin>/) whenever several coins run, or the folder already
    # holds one for this coin -- also when a multi-coin paper run is cut down to one coin
    per_coin = len(coins) > 1 or (not args.live and os.path.isdir(os.path.join(args.data_dir, coins[0])))
    if per_coin:
        asyncio.run(run_many(args, coins))
    else:
        args.asset = coins[0]
        asyncio.run(Bot(args).run())

if __name__ == "__main__":
    main()
