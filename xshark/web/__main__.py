"""python -m xshark.web  -- the console (Bo-BOTT's UI) for the paper bot."""
from __future__ import annotations

import argparse
import logging
import os

from .server import serve

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    ap = argparse.ArgumentParser(description="xshark console: the paper bot's state, windows, fills and tuning")
    ap.add_argument("--host", default="127.0.0.1",
                    help="loopback by default; tuning writes need the key in data/bot/console_key either way")
    ap.add_argument("--port", type=int, default=8795)
    ap.add_argument("--data-dir", default=os.path.join(HERE, "data", "bot"))
    ap.add_argument("--only-on", action="store_true",
                    help="Bot variables shows only the settings in use (switched-off groups and options hidden)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    serve(args.host, args.port, args.data_dir, only_on=args.only_on)


if __name__ == "__main__":
    main()
