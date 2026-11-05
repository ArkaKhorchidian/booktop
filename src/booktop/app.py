"""The Textual app: layout, refresh loop, and key bindings."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import AsyncIterator, Callable

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Sparkline, Static

from . import render
from .book import OrderBook, Snapshot, Status, Trade, TradeStats, Update
from .feeds import SymbolError


@dataclass
class Control:
    """Shared with replay feeds so the keyboard can pause / change speed."""
    paused: bool = False
    speed: float = 10.0


class BooktopApp(App):
    TITLE = "booktop"
    CSS = """
    Screen { background: #0d1117; layout: vertical; }
    #header { height: 1; background: #161b22; }
    #body { height: 1fr; }
    #ladder { width: 3fr; height: 1fr; min-width: 40; padding: 0 1 0 0; }
    #side { width: 2fr; min-width: 34; border-left: tall #21262d; }
    #chart-label { height: 1; color: #7a8494; }
    #chart { height: 7; margin: 0 1; }
    #chart > .sparkline--max-color { color: #3fd17a; }
    #chart > .sparkline--min-color { color: #ff5c6c; }
    #gauges { height: 3; margin: 1 0 0 0; }
    #tape { height: 1fr; margin: 1 0 0 0; }
    Footer { background: #161b22; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("space", "pause", "Pause"),
        Binding("z", "group", "Group levels"),
        Binding("right_square_bracket", "faster", "Faster", show=False),
        Binding("left_square_bracket", "slower", "Slower", show=False),
    ]

    def __init__(self, source: Callable[[], AsyncIterator], symbol: str, venue: str,
                 control: Control | None = None, truncate: int | None = None,
                 exit_after: float | None = None) -> None:
        super().__init__()
        self.exit_after = exit_after
        self.source = source
        self.symbol = symbol
        self.venue = venue
        self.control = control
        self.truncate = truncate
        self.book = OrderBook()
        self.stats = TradeStats()
        self.status, self.live = "starting…", False
        self.paused = False
        self.group_idx = 0
        self.mids: deque[float] = deque(maxlen=600)
        self.error: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with Horizontal(id="body"):
            yield Static(id="ladder")
            with Vertical(id="side"):
                yield Static(id="chart-label")
                yield Sparkline([0.0], id="chart", summary_function=max)
                yield Static(id="gauges")
                yield Static(id="tape")
        yield Footer()

    def on_mount(self) -> None:
        self.run_worker(self._consume(), exclusive=True)
        self.set_interval(1 / 15, self._refresh)
        self.set_interval(0.25, self._sample)
        if self.exit_after:
            self.set_timer(self.exit_after, self.exit)

    async def _consume(self) -> None:
        try:
            async for ev in self.source():
                now = time.monotonic()
                if isinstance(ev, (Snapshot, Update)):
                    self.book.apply(ev, now)
                    if self.truncate and isinstance(ev, Update):
                        self.book.truncate(self.truncate)
                elif isinstance(ev, Trade):
                    self.stats.add(ev, now)
                elif isinstance(ev, Status):
                    self.status, self.live = ev.text, ev.live
        except SymbolError as e:
            self.error = str(e)
            self.exit(message=str(e))

    def _sample(self) -> None:
        if self.paused:
            return
        m = self.book.mid()
        if m is not None:
            self.mids.append(m)

    @property
    def group(self) -> float:
        opts = render.group_options(self.book.decimals)
        return opts[self.group_idx % len(opts)]

    def _refresh(self) -> None:
        if self.paused and self.control is None:
            return
        now = time.monotonic()
        hdr = self.query_one("#header", Static)
        hdr.update(render.header(self.symbol, self.venue, self.status, self.live, self.book, self.stats,
                                 self.group, self.paused, hdr.size.width))
        lad = self.query_one("#ladder", Static)
        lad.update(render.ladder(self.book, self.stats, lad.size.width - 1, lad.size.height, now, self.group))

        side = self.query_one("#side")
        w = side.size.width - 1
        if self.mids:
            lo, hi = min(self.mids), max(self.mids)
            span = len(self.mids) * 0.25
            label = Text(f" MID · last {span / 60:.0f} min" if span >= 60 else f" MID · last {span:.0f}s", style="bold #7a8494")
            label.append(f"   {render.fmt_price(lo, self.book.decimals)} – {render.fmt_price(hi, self.book.decimals)}", style="#7a8494")
            self.query_one("#chart-label", Static).update(label)
            chart = self.query_one("#chart", Sparkline)
            n = max(2, chart.size.width)
            data = list(self.mids)[-n * 2:]
            # Sparkline draws from zero; plot the offset from the window low so moves are visible.
            chart.data = [m - lo + (hi - lo) * 0.05 + 1e-9 for m in data]

        buy, sell, n = self.stats.flow(now)
        flow = None if buy + sell == 0 else (buy - sell) / (buy + sell)
        g = Text()
        g.append_text(render.gauge("BOOK", self.book.imbalance(10), w, "bid", "ask"))
        g.append("\n")
        g.append_text(render.gauge("FLOW 60s", flow, w, "buy", "sell"))
        g.append(f"\n {n} trades in the last 60s", style="#7a8494")
        self.query_one("#gauges", Static).update(g)
        tp = self.query_one("#tape", Static)
        tp.update(render.tape(self.book, self.stats, w, tp.size.height, now))

    # -- actions -------------------------------------------------------------
    def action_pause(self) -> None:
        self.paused = not self.paused
        if self.control is not None:
            self.control.paused = self.paused
        self._refresh()

    def action_group(self) -> None:
        self.group_idx = (self.group_idx + 1) % len(render.group_options(self.book.decimals))

    def action_faster(self) -> None:
        if self.control is not None:
            self.control.speed = min(self.control.speed * 2, 1000)

    def action_slower(self) -> None:
        if self.control is not None:
            self.control.speed = max(self.control.speed / 2, 0.25)
