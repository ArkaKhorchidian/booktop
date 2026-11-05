"""booktop command line."""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .feeds import EXCHANGES, coinbase_symbol, demo, kraken_symbol, live, load_itch, replay

EXAMPLES = """examples:
  booktop                          BTC-USD live from Coinbase
  booktop ETH                      ETH-USD
  booktop SOL -x kraken            Kraken instead (every update checksum-verified)
  booktop --demo                   synthetic market, no network
  booktop --itch 12302019.NASDAQ_ITCH50.gz AAPL --speed 20
                                   replay a real NASDAQ day (pip install 'booktop[itch]')

keys: q quit · space pause · z group price levels · [ ] replay speed
"""


def main(argv=None) -> None:
    p = argparse.ArgumentParser(
        prog="booktop", description="A live order book in your terminal.",
        epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("symbol", nargs="?", default=None, help="e.g. BTC, ETH/USD, SOL-USD, or a NASDAQ ticker with --itch")
    p.add_argument("-x", "--exchange", choices=EXCHANGES, default="coinbase")
    p.add_argument("--demo", action="store_true", help="synthetic market, works offline")
    p.add_argument("--itch", metavar="FILE", help="replay a NASDAQ TotalView-ITCH 5.0 file")
    p.add_argument("--speed", type=float, default=10.0, help="replay speed multiplier (default 10x)")
    p.add_argument("--start", default="09:30", help="replay start time (default 09:30)")
    p.add_argument("--exit-after", type=float, metavar="SECONDS", help="quit automatically (for recordings)")
    p.add_argument("--version", action="version", version=f"booktop {__version__}")
    a = p.parse_args(argv)

    from .app import BooktopApp, Control

    if a.demo:
        app = BooktopApp(lambda: demo(), "DEMO", "synthetic", exit_after=a.exit_after)
    elif a.itch:
        if not a.symbol:
            p.error("--itch needs a ticker, e.g. booktop --itch FILE AAPL")
        print(f"Indexing {a.symbol.upper()} in {a.itch} with fastitch…", file=sys.stderr)
        events, _, _ = load_itch(a.itch, a.symbol, start=a.start)
        control = Control(speed=a.speed)
        app = BooktopApp(lambda: replay(events, control=control), a.symbol.upper(), "NASDAQ replay", control=control,
                         exit_after=a.exit_after)
    else:
        sym = a.symbol or "BTC"
        shown = kraken_symbol(sym) if a.exchange == "kraken" else coinbase_symbol(sym)
        app = BooktopApp(lambda: live(a.exchange, sym), shown, a.exchange,
                         exit_after=a.exit_after)
    app.run()
    if app.error:
        print(app.error, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
