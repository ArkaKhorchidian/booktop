"""Pure rendering functions: state in, Rich Text out (no Textual here, so it's easy to test)."""
from __future__ import annotations

import math
import time
from datetime import datetime

from rich.text import Text

from .book import OrderBook, TradeStats

BID = "#3fd17a"
ASK = "#ff5c6c"
BID_BAR = "#123d27"
ASK_BAR = "#4a1720"
FLASH = "#f5d76e"
DIM = "#7a8494"
ACCENT = "#7aa2f7"


def fmt_price(p: float, decimals: int) -> str:
    return f"{p:,.{decimals}f}"


def size_decimals(sizes) -> int:
    """Fewest decimals that still read well: integers stay integers (shares), small coin sizes get 4."""
    sizes = [x for x in sizes if x > 0]
    if not sizes or all(float(x).is_integer() for x in sizes):
        return 0
    if max(sizes) >= 1000:
        return 0
    base = 2 if max(sizes) >= 10 else 4
    # enough places that the smallest size still shows ~2 significant digits (max 8)
    need = math.ceil(-math.log10(min(sizes))) + 1
    return max(base, min(8, need))


def fmt_size(s: float, decimals: int) -> str:
    return f"{s:,.{decimals}f}"


def group_options(decimals: int) -> list[float]:
    """Grouping choices for the `z` key: none, then 10x, 100x, 1000x the tick."""
    tick = 10 ** -decimals if decimals else 1.0
    return [0.0, tick * 10, tick * 100, tick * 1000]


def price_decimals(book: OrderBook, group: float) -> int:
    if group <= 0:
        return book.decimals
    return max(0, -int(math.floor(math.log10(group) + 1e-9)))


def ladder(book: OrderBook, stats: TradeStats, width: int, height: int, now: float, group: float = 0.0) -> Text:
    """Asks on top (best ask lowest), a spread line, bids below. Bars show cumulative depth."""
    width = max(width, 30)
    depth = max(1, (height - 2) // 2)
    asks = book.levels("ask", depth, group)
    bids = book.levels("bid", depth, group)
    pdec = price_decimals(book, group)
    sdec = size_decimals(s for _, s in asks + bids)

    def cum(levels):
        out, c = [], 0.0
        for p, s in levels:
            c += s
            out.append((p, s, c))
        return out

    ask_rows, bid_rows = cum(asks), cum(bids)
    max_cum = max([r[2] for r in ask_rows[-1:] + bid_rows[-1:]] or [1.0]) or 1.0

    pw = max(len(fmt_price(p, pdec)) for p, _ in (asks + bids) or [(0.0, 0)])
    sw = max([len(fmt_size(c, sdec)) for _, _, c in ask_rows + bid_rows] or [4])
    cols = f"{'PRICE':>{pw}}   {'SIZE':>{sw}}   {'TOTAL':>{sw}}"

    text = Text()
    text.append(f" {cols}".ljust(width) + "\n", style=f"bold {DIM}")

    def row(side, p, s, c):
        flashing = group <= 0 and book.flashing(side, p, now)
        body = f" {fmt_price(p, pdec):>{pw}}   {fmt_size(s, sdec):>{sw}}   {fmt_size(c, sdec):>{sw}}"
        body = body.ljust(width)[:width]
        line = Text(body)
        color = ASK if side == "ask" else BID
        bar = ASK_BAR if side == "ask" else BID_BAR
        filled = int(round(width * c / max_cum))
        line.stylize(f"on {bar}", 0, filled)
        line.stylize(f"bold {color}", 1, 1 + pw)
        if flashing:
            line.stylize(f"bold {FLASH}", 1 + pw + 3, 1 + pw + 3 + sw)
        line.append("\n")
        return line

    pad = depth - len(ask_rows)
    for _ in range(pad):
        text.append(" " * width + "\n")
    for p, s, c in reversed(ask_rows):
        text.append_text(row("ask", p, s, c))

    # spread line
    spread, mid = book.spread(), book.mid()
    if spread is not None and mid:
        bps = spread / mid * 1e4
        last = stats.last_price
        arrow = {1: "▲", -1: "▼"}.get(stats.tick(), "•")
        acolor = {1: BID, -1: ASK}.get(stats.tick(), DIM)
        mid_txt = Text()
        if last is not None:
            mid_txt.append(f" {fmt_price(last, book.decimals)} {arrow} ", style=f"bold {acolor}")
        mid_txt.append(f" spread {fmt_price(spread, book.decimals)} · {bps:.2f} bps ", style=DIM)
        fill = max(0, width - mid_txt.cell_len)
        line = Text("─" * (fill // 2), style=DIM)
        line.append_text(mid_txt)
        line.append("─" * (fill - fill // 2), style=DIM)
        text.append_text(line)
    else:
        text.append(" waiting for the book…".ljust(width), style=DIM)
    text.append("\n")

    for p, s, c in bid_rows:
        text.append_text(row("bid", p, s, c))
    text.rstrip()
    return text


def _clock(ts: float, now_wall: float | None = None) -> str:
    if ts <= 0:
        return datetime.fromtimestamp(now_wall or time.time()).strftime("%H:%M:%S")
    if ts < 3 * 86400:  # replay: seconds since midnight
        h, rem = divmod(int(ts), 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def tape(book: OrderBook, stats: TradeStats, width: int, height: int, now: float) -> Text:
    text = Text()
    text.append(" TRADES".ljust(width) + "\n", style=f"bold {DIM}")
    big = stats.big_trade_threshold(now)
    rows = list(stats.tape)[: max(0, height - 1)]
    if not rows:
        text.append(" waiting for trades…", style=DIM)
        return text
    sdec = size_decimals(t.size for _, t in rows)
    pw = max(len(fmt_price(t.price, book.decimals)) for _, t in rows)
    sw = max(len(fmt_size(t.size, sdec)) for _, t in rows)
    for wall, t in rows:
        color = BID if t.side == "buy" else ASK
        is_big = t.size >= big
        mark = "◆" if is_big else ("▲" if t.side == "buy" else "▼")
        line = Text(f" {_clock(t.ts)}  ", style=DIM)
        line.append(f"{fmt_price(t.price, book.decimals):>{pw}}", style=f"bold {color}" if is_big else color)
        line.append(f"  {fmt_size(t.size, sdec):>{sw}} ", style="bold" if is_big else "")
        line.append(mark, style=f"bold {FLASH}" if is_big else color)
        age = now - wall
        if age < 0.4:
            line.stylize("reverse" if is_big else "bold")
        if line.cell_len < width:
            line.append(" " * (width - line.cell_len))
        text.append_text(line)
        text.append("\n")
    text.rstrip()
    return text


def gauge(label: str, value: float | None, width: int, left: str, right: str) -> Text:
    """A centered bar for a value in [-1, 1]: positive fills toward `left` (green)."""
    t = Text(f" {label:<10}", style=f"bold {DIM}")
    bar_w = max(10, width - len(label) - 24)
    if value is None:
        t.append("·" * bar_w, style=DIM)
        return t
    value = max(-1.0, min(1.0, value))
    share = (value + 1) / 2  # fraction that's "left" (buy/bid)
    n_left = int(round(bar_w * share))
    t.append(f"{left} {share * 100:3.0f}% ", style=BID)
    t.append("█" * n_left, style=BID)
    t.append("█" * (bar_w - n_left), style=ASK)
    t.append(f" {100 - share * 100:3.0f}% {right}", style=ASK)
    return t


def header(symbol: str, venue: str, status: str, live: bool, book: OrderBook, stats: TradeStats,
           group: float, paused: bool, width: int) -> Text:
    t = Text()
    t.append(" booktop ", style=f"bold black on {ACCENT}")
    t.append(f"  {symbol} ", style="bold")
    t.append(f"· {venue}  ", style=DIM)
    dot = "●" if live else "○"
    t.append(f"{dot} {status}", style=(BID if live else FLASH))
    if paused:
        t.append("  ❚❚ PAUSED", style=f"bold {FLASH}")
    if stats.last_price is not None:
        color = {1: BID, -1: ASK}.get(stats.tick(), "white")
        arrow = {1: " ▲", -1: " ▼"}.get(stats.tick(), "")
        t.append(f"   {fmt_price(stats.last_price, book.decimals)}{arrow}", style=f"bold {color}")
    if group > 0:
        t.append(f"   grouped by {fmt_price(group, price_decimals(book, group))}", style=DIM)
    return t
