import asyncio
import copy
import json
import os
import time
from pathlib import Path

import pytest

from booktop import render
from booktop.book import OrderBook, Snapshot, Status, Trade, TradeStats, Update
from booktop.feeds import (
    ChecksumError,
    KrakenBook,
    coinbase_symbol,
    demo,
    kraken_symbol,
    parse_coinbase,
    parse_kraken,
    replay,
)

REC = json.loads((Path(__file__).parent / "fixtures" / "recorded.json").read_text())


# ---------------------------------------------------------------------------
# Order book
# ---------------------------------------------------------------------------
def make_book():
    b = OrderBook()
    b.apply(Snapshot([(100.0, 1.0), (99.5, 2.0), (99.0, 3.0)], [(100.5, 1.5), (101.0, 2.5), (102.0, 4.0)]), now=0)
    return b


def test_book_basics():
    b = make_book()
    assert b.best_bid() == 100.0 and b.best_ask() == 100.5
    assert b.mid() == 100.25 and b.spread() == 0.5
    assert b.levels("bid", 2) == [(100.0, 1.0), (99.5, 2.0)]
    assert b.levels("ask", 5) == [(100.5, 1.5), (101.0, 2.5), (102.0, 4.0)]
    b.apply(Update("bid", 100.0, 0.0), now=1)
    assert b.best_bid() == 99.5
    b.apply(Update("ask", 100.25, 7.0), now=1)
    assert b.best_ask() == 100.25 and b.decimals == 2


def test_imbalance_and_grouping():
    b = make_book()
    assert b.imbalance(10) == pytest.approx((6 - 8) / 14)
    # bids round down and asks round up, so groups never cross the spread
    assert b.levels("bid", 5, group=1.0) == [(100.0, 1.0), (99.0, 5.0)]
    assert b.levels("ask", 5, group=1.0) == [(101.0, 4.0), (102.0, 4.0)]


def test_flash_only_for_size_changes():
    b = make_book()
    b.apply(Update("bid", 99.5, 5.0), now=10.0)
    assert b.flashing("bid", 99.5, 10.1) and not b.flashing("bid", 99.5, 11.0)
    # snapshot diff: changed size flashes, a brand-new level does not
    b.apply(Snapshot([(100.0, 9.0), (98.0, 1.0)], [(100.5, 1.5)]), now=20.0)
    assert b.flashing("bid", 100.0, 20.1)
    assert not b.flashing("bid", 98.0, 20.1)


def test_truncate():
    b = make_book()
    b.truncate(2)
    assert sorted(b.bids) == [99.5, 100.0] and sorted(b.asks) == [100.5, 101.0]


def test_trade_stats():
    st = TradeStats(window=10)
    for i in range(30):
        st.add(Trade(100 + (i % 3), 1.0, "buy" if i % 2 else "sell"), now=i * 0.1)
    st.add(Trade(105, 50.0, "buy"), now=3.1)
    buy, sell, n = st.flow(3.2)
    assert n == 31 and buy == 15 + 50 and sell == 15
    assert 1.0 < st.big_trade_threshold(3.2) <= 50.0
    assert st.tick() == 1
    assert st.flow(100.0) == (0.0, 0.0, 0)  # window expired


# ---------------------------------------------------------------------------
# Feed parsers, against messages recorded from the real exchanges
# ---------------------------------------------------------------------------
def test_kraken_checksums_verify_on_real_messages():
    kb = KrakenBook(depth=10, price_dp=1, qty_dp=8)
    n = 0
    for msg in REC["kraken_book"]:
        events = kb.apply(msg)  # raises ChecksumError on any mismatch
        n += len(events)
    assert n == len(REC["kraken_book"]) and len(kb.bids) == len(kb.asks) == 10
    assert max(kb.bids) < min(kb.asks)


def test_kraken_checksum_detects_corruption():
    msgs = copy.deepcopy(REC["kraken_book"])
    kb = KrakenBook(depth=10, price_dp=1, qty_dp=8)
    kb.apply(msgs[0])
    bad = next(m for m in msgs[1:] if m["data"][0]["bids"] or m["data"][0]["asks"])
    side = "bids" if bad["data"][0]["bids"] else "asks"
    bad["data"][0][side][0]["qty"] += 1.0
    with pytest.raises(ChecksumError):
        for m in msgs[1:]:
            kb.apply(m)


def test_parse_kraken_trade():
    evs = parse_kraken(REC["kraken_trade"])
    assert evs and all(isinstance(e, Trade) and e.side in ("buy", "sell") and e.price > 0 for e in evs)


def test_parse_coinbase():
    snap, upd, match = REC["coinbase"]
    s = parse_coinbase(snap)[0]
    assert isinstance(s, Snapshot) and len(s.bids) == 5 and s.bids[0][0] < s.asks[0][0]
    u = parse_coinbase(upd)
    assert u and all(isinstance(e, Update) and e.side in ("bid", "ask") for e in u)
    t = parse_coinbase(match)[0]
    # Coinbase reports the maker side; booktop shows the aggressor
    assert t.side == ("buy" if match["side"] == "sell" else "sell")


def test_symbol_normalization():
    assert kraken_symbol("btc") == "BTC/USD" and kraken_symbol("eth-eur") == "ETH/EUR"
    assert coinbase_symbol("sol") == "SOL-USD" and coinbase_symbol("BTC/USD") == "BTC-USD"


# ---------------------------------------------------------------------------
# Demo and replay feeds
# ---------------------------------------------------------------------------
def test_demo_feed_keeps_a_valid_book():
    async def run():
        b, trades = OrderBook(), 0
        agen = demo(seed=1, rate=1e6)
        async for ev in agen:
            if isinstance(ev, (Snapshot, Update)):
                b.apply(ev)
                assert b.best_bid() < b.best_ask()
            elif isinstance(ev, Trade):
                trades += 1
            if trades >= 300:
                break
        await agen.aclose()
        return trades
    assert asyncio.run(run()) == 300


def test_replay_orders_and_coalesces():
    events = [(i * 10**8, Snapshot([(10.0 + i, 1.0)], [(11.0 + i, 1.0)])) for i in range(50)]
    events.insert(10, (10 * 10**8, Trade(20.0, 5.0, "buy")))

    async def run():
        out = []
        async for ev in replay(events, speed=1000.0):
            out.append(ev)
        return out
    out = asyncio.run(run())
    snaps = [e for e in out if isinstance(e, Snapshot)]
    assert snaps[-1].bids[0][0] == 59.0  # last state always delivered
    assert len(snaps) < 50  # coalesced per frame
    assert any(isinstance(e, Trade) for e in out)
    assert isinstance(out[-1], Status) and "finished" in out[-1].text


def test_itch_extraction(tmp_path, monkeypatch):
    fastitch = pytest.importorskip("fastitch")
    pytest.importorskip("polars")
    from fastitch.synthetic import generate

    from booktop.feeds import load_itch

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    p = tmp_path / "s.itch"
    p.write_bytes(generate(40_000, symbols=["AAPL", "MSFT"], seed=5))
    events, first, last = load_itch(str(p), "AAPL", depth=5)
    snaps = [e for _, e in events if isinstance(e, Snapshot)]
    trades = [e for _, e in events if isinstance(e, Trade)]
    assert snaps and trades and first <= last
    assert all(s.bids[0][0] < s.asks[0][0] for s in snaps if s.bids and s.asks)
    assert {t.side for t in trades} <= {"buy", "sell"}
    # second call hits the cache and returns the same events
    again, _, _ = load_itch(str(p), "AAPL", depth=5)
    assert len(again) == len(events)
    assert list((tmp_path / "cache" / "booktop").iterdir())


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def test_render_functions():
    b, st = make_book(), TradeStats()
    for i in range(25):
        st.add(Trade(100.5, 1.0 + (i == 24) * 40, "buy"), now=time.monotonic())
    now = time.monotonic()
    lad = render.ladder(b, st, width=60, height=12, now=now)
    lines = lad.plain.split("\n")
    assert len(lines) <= 12 and "100.5 " in lad.plain and "spread" in lad.plain
    assert all(len(l) <= 60 for l in lines)
    tp = render.tape(b, st, width=40, height=8, now=now)
    assert "◆" in tp.plain  # the big trade is highlighted
    assert "bid" in render.gauge("BOOK", 0.2, 50, "bid", "ask").plain
    assert "BTC/USD" in render.header("BTC/USD", "kraken", "live", True, b, st, 0.0, False, 80).plain
    assert render.size_decimals([100.0, 200.0]) == 0 and render.size_decimals([0.5, 1.25]) == 4


# ---------------------------------------------------------------------------
# The Textual app, headless
# ---------------------------------------------------------------------------
def test_app_runs_and_responds_to_keys():
    from booktop.app import BooktopApp, Control

    async def run():
        app = BooktopApp(lambda: demo(seed=2, rate=500), "DEMO", "synthetic", control=Control())
        async with app.run_test(size=(120, 34)) as pilot:
            await pilot.pause(1.0)
            assert "spread" in str(app.query_one("#ladder").render())
            await pilot.press("z")
            assert app.group > 0
            await pilot.press("space")
            assert app.paused and app.control.paused
            await pilot.press("right_square_bracket")
            assert app.control.speed == 20.0
            await pilot.press("q")
    asyncio.run(run())
