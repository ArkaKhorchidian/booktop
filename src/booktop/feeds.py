"""Market data feeds, all normalized to booktop events.

Each feed is an async generator of events. Live feeds reconnect with backoff and
emit a fresh Snapshot after every reconnect, so the book can never go stale.
"""
from __future__ import annotations

import asyncio
import json
import random
import zlib
from datetime import datetime
from typing import AsyncIterator

from .book import Event, Snapshot, Status, Trade, Update

EXCHANGES = ("coinbase", "kraken")


def _iso(ts: str | None) -> float:
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Message parsers (pure functions, unit-tested against recorded messages)
# ---------------------------------------------------------------------------
def parse_kraken(msg: dict) -> list[Event]:
    """Kraken WebSocket v2: `book` and `trade` channels."""
    ch, typ = msg.get("channel"), msg.get("type")
    out: list[Event] = []
    if ch == "book":
        for d in msg.get("data", []):
            ts = _iso(d.get("timestamp"))
            bids = [(float(l["price"]), float(l["qty"])) for l in d.get("bids", [])]
            asks = [(float(l["price"]), float(l["qty"])) for l in d.get("asks", [])]
            if typ == "snapshot":
                out.append(Snapshot(bids, asks, ts))
            else:
                out += [Update("bid", p, s, ts) for p, s in bids]
                out += [Update("ask", p, s, ts) for p, s in asks]
    elif ch == "trade":
        for d in msg.get("data", []):
            out.append(Trade(float(d["price"]), float(d["qty"]), d["side"], _iso(d.get("timestamp"))))
    return out


def _ck_fmt(x: float, decimals: int) -> str:
    return f"{x:.{decimals}f}".replace(".", "").lstrip("0")


def kraken_checksum(bids: dict, asks: dict, price_dp: int, qty_dp: int) -> int:
    """CRC32 over the top 10 asks then top 10 bids, as Kraken WebSocket v2 defines it."""
    s = "".join(_ck_fmt(p, price_dp) + _ck_fmt(asks[p], qty_dp) for p in sorted(asks)[:10])
    s += "".join(_ck_fmt(p, price_dp) + _ck_fmt(bids[p], qty_dp) for p in sorted(bids, reverse=True)[:10])
    return zlib.crc32(s.encode())


class KrakenBook:
    """Kraken's book protocol: apply a whole message, truncate to depth, then verify the checksum."""

    def __init__(self, depth: int, price_dp: int, qty_dp: int) -> None:
        self.depth, self.price_dp, self.qty_dp = depth, price_dp, qty_dp
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}

    def apply(self, msg: dict) -> list[Event]:
        """Returns events for the UI; raises ChecksumError if the book diverged."""
        out: list[Event] = []
        for d in msg.get("data", []):
            if msg.get("type") == "snapshot":
                self.bids = {l["price"]: l["qty"] for l in d.get("bids", [])}
                self.asks = {l["price"]: l["qty"] for l in d.get("asks", [])}
            else:
                for key, book in (("bids", self.bids), ("asks", self.asks)):
                    for l in d.get(key, []):
                        if l["qty"] == 0:
                            book.pop(l["price"], None)
                        else:
                            book[l["price"]] = l["qty"]
                for p in sorted(self.bids, reverse=True)[self.depth:]:
                    del self.bids[p]
                for p in sorted(self.asks)[self.depth:]:
                    del self.asks[p]
            if "checksum" in d and kraken_checksum(self.bids, self.asks, self.price_dp, self.qty_dp) != d["checksum"]:
                raise ChecksumError("order book checksum mismatch")
            out.append(Snapshot(sorted(self.bids.items(), reverse=True), sorted(self.asks.items()),
                                _iso(d.get("timestamp"))))
        return out


class ChecksumError(Exception):
    pass


def parse_coinbase(msg: dict) -> list[Event]:
    """Coinbase Exchange public feed: `level2_batch` (served as level2_50) and `matches`."""
    typ = msg.get("type")
    if typ == "snapshot":
        bids = [(float(p), float(s)) for p, s in msg.get("bids", [])]
        asks = [(float(p), float(s)) for p, s in msg.get("asks", [])]
        return [Snapshot(bids, asks)]
    if typ == "l2update":
        ts = _iso(msg.get("time"))
        return [Update("bid" if side == "buy" else "ask", float(p), float(s), ts) for side, p, s in msg.get("changes", [])]
    if typ == "match":
        # Coinbase reports the *maker* side; the aggressor is the opposite.
        taker = "buy" if msg["side"] == "sell" else "sell"
        return [Trade(float(msg["price"]), float(msg["size"]), taker, _iso(msg.get("time")))]
    return []


# ---------------------------------------------------------------------------
# Live websocket feeds
# ---------------------------------------------------------------------------
def kraken_symbol(symbol: str) -> str:
    s = symbol.upper().replace("-", "/")
    return s if "/" in s else f"{s}/USD"


def coinbase_symbol(symbol: str) -> str:
    s = symbol.upper().replace("/", "-")
    return s if "-" in s else f"{s}-USD"


async def live(exchange: str, symbol: str, depth: int = 25) -> AsyncIterator[Event]:
    if exchange == "kraken":
        async for ev in _kraken(kraken_symbol(symbol), _kraken_depth(depth)):
            yield ev
    elif exchange == "coinbase":
        async for ev in _coinbase(coinbase_symbol(symbol)):
            yield ev
    else:
        raise ValueError(f"unknown exchange {exchange!r}; choose from {', '.join(EXCHANGES)}")


async def _reconnecting(name: str, session):
    """Run `session()` (an async generator) forever, reconnecting with exponential backoff."""
    import websockets

    backoff = 1.0
    while True:
        yield Status(f"connecting to {name}…", live=False)
        try:
            async for ev in session():
                if isinstance(ev, Status) and ev.live:
                    backoff = 1.0
                yield ev
            reason = "connection closed"
        except SymbolError:
            raise
        except ChecksumError:
            reason, backoff = "book checksum mismatch, resyncing", 0.0
        except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as e:
            reason = type(e).__name__
        yield Status(f"{reason}; reconnecting" + (f" in {backoff:.0f}s" if backoff else ""), live=False)
        await asyncio.sleep(backoff)
        backoff = min(max(backoff, 0.5) * 2, 30)


async def _kraken(sym: str, depth: int) -> AsyncIterator[Event]:
    import websockets

    async def session():
        async with websockets.connect("wss://ws.kraken.com/v2", max_size=None, open_timeout=15, ping_interval=20) as ws:
            await ws.send(json.dumps({"method": "subscribe", "params": {"channel": "instrument", "snapshot": True}}))
            price_dp = qty_dp = None
            async for raw in ws:  # precision is needed to verify book checksums
                msg = json.loads(raw)
                if msg.get("channel") == "instrument" and msg.get("type") == "snapshot":
                    for pair in msg["data"].get("pairs", []):
                        if pair.get("symbol") == sym:
                            price_dp, qty_dp = pair["price_precision"], pair["qty_precision"]
                    break
            if price_dp is None:
                raise SymbolError(f"Kraken has no market called {sym!r} (try BTC/USD, ETH/USD, SOL/EUR)")
            await ws.send(json.dumps({"method": "unsubscribe", "params": {"channel": "instrument"}}))
            await ws.send(json.dumps({"method": "subscribe", "params": {"channel": "book", "symbol": [sym], "depth": depth}}))
            await ws.send(json.dumps({"method": "subscribe", "params": {"channel": "trade", "symbol": [sym]}}))
            book = KrakenBook(depth, price_dp, qty_dp)
            verified = 0
            async for raw in ws:
                msg = json.loads(raw)
                err = _subscription_error("kraken", msg)
                if err:
                    raise SymbolError(err)
                ch = msg.get("channel")
                if ch == "book":
                    for ev in book.apply(msg):
                        yield ev
                    verified += 1
                    if verified == 1 or verified % 50 == 0:
                        yield Status(f"live · checksum ✓ {verified:,}", live=True)
                elif ch == "trade":
                    for ev in parse_kraken(msg):
                        yield ev

    async for ev in _reconnecting("kraken", session):
        yield ev


async def _coinbase(sym: str) -> AsyncIterator[Event]:
    import websockets

    async def session():
        async with websockets.connect("wss://ws-feed.exchange.coinbase.com", max_size=None, open_timeout=15,
                                      ping_interval=20) as ws:
            await ws.send(json.dumps({"type": "subscribe", "product_ids": [sym], "channels": ["level2_batch", "matches"]}))
            yield Status("live", live=True)
            async for raw in ws:
                msg = json.loads(raw)
                err = _subscription_error("coinbase", msg)
                if err:
                    raise SymbolError(err)
                for ev in parse_coinbase(msg):
                    yield ev

    async for ev in _reconnecting("coinbase", session):
        yield ev


class SymbolError(Exception):
    pass


def _subscription_error(exchange: str, msg: dict) -> str | None:
    if exchange == "kraken" and msg.get("method") == "subscribe" and msg.get("success") is False:
        return f"Kraken rejected the subscription: {msg.get('error', 'unknown error')}"
    if exchange == "coinbase" and msg.get("type") == "error":
        return f"Coinbase: {msg.get('message', '')} {msg.get('reason', '')}".strip()
    return None


def _kraken_depth(depth: int) -> int:
    for d in (10, 25, 100, 500, 1000):
        if depth <= d:
            return d
    return 1000


# ---------------------------------------------------------------------------
# Demo feed: a synthetic market for offline use, tests, and screenshots
# ---------------------------------------------------------------------------
async def demo(symbol: str = "DEMO", seed: int | None = None, rate: float = 40.0) -> AsyncIterator[Event]:
    rng = random.Random(seed)
    tick = 0.5
    mid = 64_000.0
    yield Status("demo market (synthetic)", live=True)

    def level_size() -> float:
        return round(rng.lognormvariate(-1.0, 1.1), 4)

    bids = {round(mid - tick * (i + 1), 1): level_size() for i in range(60)}
    asks = {round(mid + tick * (i + 1), 1): level_size() for i in range(60)}
    yield Snapshot(sorted(bids.items(), reverse=True), sorted(asks.items()))
    drift = 0.0
    while True:
        await asyncio.sleep(rng.expovariate(rate))
        r = rng.random()
        best_bid, best_ask = max(bids), min(asks)
        if r < 0.55:  # replenish / cancel somewhere in the book
            side = rng.choice(("bid", "ask"))
            k = min(int(rng.expovariate(0.15)), 59)
            price = round((best_bid - tick * k) if side == "bid" else (best_ask + tick * k), 1)
            book = bids if side == "bid" else asks
            size = 0.0 if rng.random() < 0.25 and price in book else level_size()
            if size:
                book[price] = size
            else:
                book.pop(price, None)
            yield Update(side, price, size)
        elif r < 0.85:  # trade, consuming the touch
            drift = 0.9 * drift + rng.gauss(0, 0.3)
            side = "buy" if rng.random() < 0.5 + max(-0.3, min(0.3, drift / 3)) else "sell"
            size = round(rng.lognormvariate(-2.2, 1.4), 5)
            if side == "buy":
                p = best_ask
                left = asks[p] - size
                if left > 0:
                    asks[p] = round(left, 5)
                    yield Update("ask", p, asks[p])
                else:
                    del asks[p]
                    yield Update("ask", p, 0.0)
            else:
                p = best_bid
                left = bids[p] - size
                if left > 0:
                    bids[p] = round(left, 5)
                    yield Update("bid", p, bids[p])
                else:
                    del bids[p]
                    yield Update("bid", p, 0.0)
            yield Trade(p, size, side)
        else:  # quote improvement inside the spread
            if best_ask - best_bid > tick * 1.5:
                side = rng.choice(("bid", "ask"))
                p = round(best_bid + tick, 1) if side == "bid" else round(best_ask - tick, 1)
                (bids if side == "bid" else asks)[p] = level_size()
                yield Update(side, p, (bids if side == "bid" else asks)[p])
            else:
                for book, side, ref in ((bids, "bid", best_bid), (asks, "ask", best_ask)):
                    if len(book) < 40:
                        p = round(ref - tick * len(book), 1) if side == "bid" else round(ref + tick * len(book), 1)
                        book[p] = level_size()
                        yield Update(side, p, book[p])


# ---------------------------------------------------------------------------
# NASDAQ ITCH replay (via fastitch)
# ---------------------------------------------------------------------------
def _cache_path(path: str, symbol: str, depth: int, start, end) -> str:
    import hashlib
    import os

    st = os.stat(path)
    key = f"v1|{os.path.abspath(path)}|{st.st_size}|{st.st_mtime_ns}|{symbol}|{depth}|{start}|{end}"
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    d = os.path.join(base, "booktop")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{symbol}-{hashlib.sha1(key.encode()).hexdigest()[:16]}")


def extract_itch(path: str, symbol: str, depth: int = 20, start: str | None = "09:30", end: str | None = "16:00"):
    """Build (book, trades) DataFrames for one symbol from an ITCH file, using a cache when possible."""
    try:
        import fastitch
        import polars as pl
    except ImportError as e:  # pragma: no cover
        raise SystemExit("ITCH replay needs fastitch and polars: pip install 'booktop[itch]'") from e

    sym = symbol.upper()
    cache = _cache_path(path, sym, depth, start, end)
    try:
        return pl.read_parquet(cache + ".book.parquet"), pl.read_parquet(cache + ".trades.parquet")
    except (FileNotFoundError, OSError):
        pass

    book = fastitch.book(path, sym, depth=depth, every="100ms", start=start, end=end, date=False, backend="polars")
    if book.height == 0:
        raise SymbolError(f"no order book activity for {sym} in {path}")
    f = fastitch.read(path, ["A", "F", "U", "E", "C", "P"], symbols=sym, date=False, backend="polars")

    # Executions (E/C) don't say which side traded: recover the resting order's side and
    # price by following each order through its chain of replaces (U messages).
    orders = pl.concat([f["add_order"].select("order_reference_number", "side", "price"),
                        f["add_order_mpid"].select("order_reference_number", "side", "price")])
    repl = f["order_replace"].select(
        pl.col("original_order_reference_number").alias("orig"),
        pl.col("new_order_reference_number").alias("order_reference_number"),
        pl.col("price").alias("new_price"))
    while True:
        new = repl.join(orders.select(pl.col("order_reference_number").alias("orig"), "side"), on="orig", how="inner")
        new = new.select("order_reference_number", "side", pl.col("new_price").alias("price"))
        new = new.join(orders, on="order_reference_number", how="anti")
        if new.height == 0:
            break
        orders = pl.concat([orders, new])

    aggressor = (pl.when(pl.col("side") == "B").then(pl.lit("sell"))   # resting buy hit => seller aggressed
                 .when(pl.col("side") == "S").then(pl.lit("buy")).otherwise(None).alias("aggr"))
    window = [pl.col("timestamp").dt.total_nanoseconds().alias("ns")]
    e = f["order_executed"].join(orders, on="order_reference_number", how="left")
    c = f["order_executed_with_price"].join(orders.select("order_reference_number", "side"),
                                            on="order_reference_number", how="left")
    trades = pl.concat([
        e.select(*window, "price", pl.col("executed_shares").cast(pl.Float64).alias("size"), aggressor),
        c.select(*window, pl.col("execution_price").alias("price"),
                 pl.col("executed_shares").cast(pl.Float64).alias("size"), aggressor),
        f["trade"].select(*window, "price", pl.col("shares").cast(pl.Float64).alias("size"), aggressor),
    ]).drop_nulls().sort("ns")
    lo = book["timestamp"].dt.total_nanoseconds().min()
    hi = book["timestamp"].dt.total_nanoseconds().max()
    trades = trades.filter(pl.col("ns").is_between(lo, hi))
    book = book.with_columns(pl.col("timestamp").dt.total_nanoseconds().alias("ns")).drop("timestamp", "symbol")
    try:
        book.write_parquet(cache + ".book.parquet")
        trades.write_parquet(cache + ".trades.parquet")
    except OSError:
        pass
    return book, trades


def load_itch(path: str, symbol: str, depth: int = 20, start: str | None = "09:30", end: str | None = "16:00"):
    """Precompute replay events for one symbol. Returns (events, first_ns, last_ns)."""
    book, trades = extract_itch(path, symbol, depth, start, end)
    events: list[tuple[int, Event]] = []
    cols = book.columns
    idx = {c: i for i, c in enumerate(cols)}
    bp = [idx[f"bid_price_{i}"] for i in range(1, depth + 1)]
    bs = [idx[f"bid_size_{i}"] for i in range(1, depth + 1)]
    ap = [idx[f"ask_price_{i}"] for i in range(1, depth + 1)]
    as_ = [idx[f"ask_size_{i}"] for i in range(1, depth + 1)]
    ns_i = idx["ns"]
    for r in book.iter_rows():
        bids = [(r[p], float(r[q])) for p, q in zip(bp, bs) if r[p]]
        asks = [(r[p], float(r[q])) for p, q in zip(ap, as_) if r[p]]
        events.append((r[ns_i], Snapshot(bids, asks, r[ns_i] / 1e9)))
    for ns, price, size, aggr in trades.select("ns", "price", "size", "aggr").iter_rows():
        events.append((ns, Trade(price, size, aggr, ns / 1e9)))
    events.sort(key=lambda e: e[0])
    return events, events[0][0], events[-1][0]


async def replay(events, speed: float = 10.0, control=None) -> AsyncIterator[Event]:
    """Play precomputed (ns, event) pairs in market time scaled by `speed`.

    `control` (optional) is an object with `.paused` and `.speed` attributes the UI can change.
    """
    loop = asyncio.get_running_loop()
    if not events:
        return
    t0_market = events[0][0]
    t0_wall = loop.time()
    clock = 0.0  # market seconds elapsed
    last_wall = t0_wall
    i = 0
    while i < len(events):
        now = loop.time()
        sp = control.speed if control is not None else speed
        if control is None or not control.paused:
            clock += (now - last_wall) * sp
        last_wall = now
        target = t0_market + int(clock * 1e9)
        latest_snap = None
        while i < len(events) and events[i][0] <= target:
            ns, ev = events[i]
            if isinstance(ev, Snapshot):
                latest_snap = ev  # coalesce many book changes into one per frame
            else:
                if latest_snap is not None:
                    yield latest_snap
                    latest_snap = None
                yield ev
            i += 1
        if latest_snap is not None:
            yield latest_snap
        ns_now = events[min(i, len(events) - 1)][0]
        secs = ns_now / 1e9
        h, rem = divmod(int(secs), 3600)
        m, s = divmod(rem, 60)
        state = "paused" if control is not None and control.paused else f"{sp:g}x"
        yield Status(f"replay {h:02d}:{m:02d}:{s:02d} · {state}", live=False)
        await asyncio.sleep(1 / 30)
    yield Status("replay finished", live=False)
