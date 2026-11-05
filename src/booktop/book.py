"""Order book state, normalized events, and rolling statistics."""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Literal

Side = Literal["buy", "sell"]


# ---------------------------------------------------------------------------
# Events every feed is normalized into
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Snapshot:
    """Replace the whole book. Levels are (price, size)."""
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]
    ts: float = 0.0


@dataclass(frozen=True)
class Update:
    """Set one level's size; size 0 removes the level."""
    side: Literal["bid", "ask"]
    price: float
    size: float
    ts: float = 0.0


@dataclass(frozen=True)
class Trade:
    price: float
    size: float
    side: Side  # aggressor (taker) side
    ts: float = 0.0


@dataclass(frozen=True)
class Status:
    """Connection / playback status line for the header."""
    text: str
    live: bool = True


Event = Snapshot | Update | Trade | Status


# ---------------------------------------------------------------------------
# Book
# ---------------------------------------------------------------------------
class OrderBook:
    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        # price -> monotonic time of last change, for the "flash" effect
        self.changed: dict[tuple[str, float], float] = {}
        self.decimals = 0

    def _note_price(self, price: float) -> None:
        # Infer display precision from the prices we see (capped at 8).
        d = -Decimal(repr(price)).normalize().as_tuple().exponent
        if d > self.decimals:
            self.decimals = min(int(d), 8)

    def apply(self, ev: Snapshot | Update, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if isinstance(ev, Snapshot):
            new_bids = {p: s for p, s in ev.bids if s > 0}
            new_asks = {p: s for p, s in ev.asks if s > 0}
            if self.bids or self.asks:
                # Diff against the previous book so replayed snapshots still flash what changed.
                for side, old, new in (("bid", self.bids, new_bids), ("ask", self.asks, new_asks)):
                    for p in new.keys() & old.keys():  # size changes at existing levels only
                        if new[p] != old[p]:
                            self.changed[(side, p)] = now
            else:
                self.changed.clear()
            self.bids, self.asks = new_bids, new_asks
            for p, _ in list(ev.bids[:5]) + list(ev.asks[:5]):
                self._note_price(p)
            if len(self.changed) > 5000:
                self.changed = {k: t for k, t in self.changed.items() if now - t < 1.0}
            return
        book = self.bids if ev.side == "bid" else self.asks
        self._note_price(ev.price)
        if ev.size <= 0:
            book.pop(ev.price, None)
        else:
            book[ev.price] = ev.size
        self.changed[(ev.side, ev.price)] = now
        if len(self.changed) > 5000:
            self.changed = {k: t for k, t in self.changed.items() if now - t < 1.0}

    def truncate(self, depth: int) -> None:
        """Keep only the best `depth` levels per side (bounded memory for depth-limited feeds)."""
        if len(self.bids) > depth:
            for p in sorted(self.bids, reverse=True)[depth:]:
                del self.bids[p]
        if len(self.asks) > depth:
            for p in sorted(self.asks)[depth:]:
                del self.asks[p]

    # -- queries -----------------------------------------------------------
    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    def mid(self) -> float | None:
        b, a = self.best_bid(), self.best_ask()
        if b is None or a is None:
            return None
        return (a + b) / 2

    def spread(self) -> float | None:
        b, a = self.best_bid(), self.best_ask()
        if b is None or a is None:
            return None
        return a - b

    def levels(self, side: str, depth: int, group: float = 0.0) -> list[tuple[float, float]]:
        """Top `depth` levels, best first, optionally aggregated into buckets of size `group`."""
        book = self.bids if side == "bid" else self.asks
        if not book:
            return []
        if group <= 0:
            prices = sorted(book, reverse=(side == "bid"))[:depth]
            return [(p, book[p]) for p in prices]
        buckets: dict[float, float] = {}
        for p, s in book.items():
            # bids round down, asks round up, so a bucket never crosses the spread
            k = math.floor(p / group) if side == "bid" else math.ceil(p / group)
            key = round(k * group, 10)
            buckets[key] = buckets.get(key, 0.0) + s
        prices = sorted(buckets, reverse=(side == "bid"))[:depth]
        return [(p, buckets[p]) for p in prices]

    def imbalance(self, depth: int = 10) -> float | None:
        """(bid depth - ask depth) / total, over the top `depth` levels. In [-1, 1]."""
        b = sum(s for _, s in self.levels("bid", depth))
        a = sum(s for _, s in self.levels("ask", depth))
        if a + b == 0:
            return None
        return (b - a) / (a + b)

    def flashing(self, side: str, price: float, now: float, window: float = 0.35) -> bool:
        t = self.changed.get((side, price))
        return t is not None and now - t < window


# ---------------------------------------------------------------------------
# Rolling trade statistics
# ---------------------------------------------------------------------------
@dataclass
class TradeStats:
    window: float = 60.0  # seconds
    trades: deque = field(default_factory=lambda: deque(maxlen=5000))
    tape: deque = field(default_factory=lambda: deque(maxlen=200))
    last_price: float | None = None
    prev_price: float | None = None

    def add(self, t: Trade, now: float) -> None:
        if self.last_price is not None and t.price != self.last_price:
            self.prev_price = self.last_price
        self.last_price = t.price
        self.trades.append((now, t))
        self.tape.appendleft((now, t))

    def _recent(self, now: float) -> Iterable[Trade]:
        while self.trades and now - self.trades[0][0] > self.window:
            self.trades.popleft()
        return (t for _, t in self.trades)

    def flow(self, now: float) -> tuple[float, float, int]:
        """(buy volume, sell volume, trade count) over the window."""
        buy = sell = 0.0
        n = 0
        for t in self._recent(now):
            n += 1
            if t.side == "buy":
                buy += t.size
            else:
                sell += t.size
        return buy, sell, n

    def big_trade_threshold(self, now: float) -> float:
        """Size above which a trade is highlighted: 5x the recent median, at least the 95th pct."""
        sizes = sorted(t.size for t in self._recent(now))
        if len(sizes) < 20:
            return math.inf
        median = sizes[len(sizes) // 2]
        p95 = sizes[int(len(sizes) * 0.95)]
        return max(5 * median, p95)

    def tick(self) -> int:
        """+1 if the last price change was up, -1 if down, 0 if unknown."""
        if self.last_price is None or self.prev_price is None:
            return 0
        return 1 if self.last_price > self.prev_price else -1
