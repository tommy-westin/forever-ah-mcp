"""Tester mot syntetisk data: inget nätverk eller API-nycklar krävs."""

from __future__ import annotations

import asyncio
import json
import random

import httpx
import pytest

from forever_ah import analytics as a
from forever_ah import collector as c
from forever_ah import db
from forever_ah.blizzard import BlizzardClient
from forever_ah.config import Settings

HOUR = 3600
DAY = 86400
T0 = 1_795_000_000 - 1_795_000_000 % DAY  # midnatt UTC


def settings(tmp_path) -> Settings:
    return Settings(client_id="x", client_secret="y", db_path=str(tmp_path / "ah.sqlite"),
                    houses=("horde",), hourly_retention_days=14)


# --- Rena funktioner ---


def test_normalize_and_aggregate():
    payload = {"auctions": [
        {"id": 1, "item": {"id": 10}, "quantity": 20, "unit_price": 100, "time_left": "LONG"},
        {"id": 2, "item": {"id": 10}, "quantity": 5, "unit_price": 90, "time_left": "SHORT"},
        {"id": 3, "item": {"id": 10}, "quantity": 75, "unit_price": 200, "time_left": "VERY_LONG"},
        {"id": 4, "item": {"id": 11}, "quantity": 0, "unit_price": 1, "time_left": "LONG"},   # ogiltig
        {"id": 5, "item": {}, "quantity": 1, "unit_price": 1},                               # trasig
    ]}
    ls = c.normalize(payload, "commodities")
    assert [l.auction_id for l in ls] == [1, 2, 3]
    s = c.aggregate(ls)[10]
    assert s.min_price == 90 and s.qty == 100 and s.listings == 3
    assert s.p10_price == 100          # 5 st à 90 räcker inte till 10 %
    assert s.median_price == 200       # 75 av 100 ligger på 200
    assert s.top_share == 0.75


def test_normalize_auctions_uses_buyout_per_unit_and_skips_bid_only():
    payload = {"auctions": [
        {"id": 1, "item": {"id": 7}, "quantity": 4, "buyout": 1000, "time_left": "LONG"},
        {"id": 2, "item": {"id": 7}, "quantity": 1, "bid": 50, "time_left": "LONG"},
    ]}
    ls = c.normalize(payload, "auctions")
    assert len(ls) == 1 and ls[0].unit_price == 250


def test_estimate_sold():
    prev = {1: (10, 20, "LONG"), 2: (10, 5, "SHORT"), 3: (11, 8, "MEDIUM"), 4: (12, 3, "LONG")}
    new = {1: 15, 3: 8}
    # 1: delköp 5, 2: SHORT försvann = utgången, 3: oförändrad, 4: försvann med LONG = såld
    assert c.estimate_sold(prev, new) == {10: 5, 12: 3}


def test_fmt():
    assert a.fmt(12345) == "1g 23s 45c"
    assert a.fmt(5) == "5c"
    assert a.fmt(1800) == "18s 0c"


# --- Simulering: Stranglevine faller i fem dygn och planar sedan ut ---


def simulate(conn, house_id=2, item_id=249274, days=8):
    rng = random.Random(1)
    aid = 1
    live: dict[int, tuple[int, int, str]] = {}  # auction_id -> (qty, price, time_left)
    for h in range(days * 24):
        d = h / 24
        # Pris: 60s -> 18s fram till dygn 5, sedan svag uppgång
        base = 6000 - (6000 - 1800) * min(d, 5) / 5 + (0 if d < 5 else 40 * (d - 5) * 24 / 24)
        # Säljtakt: 10 st/h, ökar till 14 st/h efter dygn 5
        sell = 10 if d < 5 else 14
        # Utbud: nya listningar 14 st/h innan dygn 5, sedan 16 st/h
        new_qty = 14 if d < 5 else 16
        # Köp från billigaste
        for k in sorted(live, key=lambda k: live[k][1]):
            if sell <= 0:
                break
            q, p, tl = live[k]
            take = min(q, sell)
            sell -= take
            if take == q:
                del live[k]
            else:
                live[k] = (q - take, p, tl)
        live[aid] = (new_qty, int(base * rng.uniform(0.97, 1.05)), "VERY_LONG")
        aid += 1
        payload = {"auctions": [{"id": k, "item": {"id": item_id}, "quantity": q, "unit_price": p, "time_left": tl}
                                for k, (q, p, tl) in live.items()]}
        c.store_snapshot(conn, house_id, "commodities", c.normalize(payload, "commodities"), T0 + h * HOUR, None)
        if h % 6 == 5:
            c.rollup(conn, 14, now=T0 + h * HOUR)


@pytest.fixture
def conn(tmp_path):
    cn = db.connect(str(tmp_path / "ah.sqlite"))
    with cn:
        cn.execute("INSERT INTO houses VALUES (2, 'Horde', 'horde')")
        cn.execute("INSERT INTO items(item_id,name,name_lc,fetch_failed,item_class,item_subclass,item_level,class_id,subclass_id) "
                   "VALUES (249274,'Stranglevine','stranglevine',0,'Tradeskill','Herb',45,7,9)")
        cn.execute("INSERT INTO items(item_id,name,name_lc,fetch_failed) VALUES (249431,'Viridian Dye','viridian dye',0)")
    simulate(cn)
    yield cn
    cn.close()


def test_price_and_history(conn):
    p = a.price(conn, 2, 249274)
    assert p["qty"] > 0 and p["est_sold_24h"] > 300
    assert p["change_7d_pct"] is not None and p["change_7d_pct"] < -40
    h = a.history(conn, 2, 249274, 3, 14)
    assert h["resolution"] == "hourly" and 60 <= h["points"] <= 73
    hd = a.history(conn, 2, 249274, 30, 14)
    assert hd["resolution"] == "daily" and hd["points"] == 8


def test_signals_detect_bottom(conn):
    s = a.signals(conn, 2, 249274)
    assert s["checks"]["price_stopped_falling"]["met"] == "ja"
    assert s["checks"]["sales_rising"]["met"] == "ja"
    assert s["score"] in ("2/3", "3/3")


def test_depth_and_movers(conn):
    d = a.depth(conn, 2, 249274)
    assert d["total_qty"] == sum(b["qty"] for b in d["bands_vs_lowest"])
    assert d["buy_cheapest_25pct"]["qty"] > 0
    m = a.movers(conn, 2, hours=72, min_daily_turnover_copper=0)
    assert m["up"] or m["down"]


def test_resolve_item(conn):
    assert a.resolve_item(conn, "stranglevine") == (249274, "Stranglevine")
    assert a.resolve_item(conn, "249274")[0] == 249274
    with pytest.raises(a.NotFound):
        a.resolve_item(conn, "finns inte")


def test_retention(tmp_path):
    cn = db.connect(str(tmp_path / "r.sqlite"))
    ls = c.normalize({"auctions": [{"id": 1, "item": {"id": 5}, "quantity": 1, "unit_price": 10}]}, "commodities")
    c.store_snapshot(cn, 1, "commodities", ls, T0, None)
    c.rollup(cn, retention_days=14, now=T0 + 20 * DAY)
    assert cn.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 0


# --- Insamlare end-to-end mot mockat API ---


def test_collect_once_with_mock_api(tmp_path):
    s = settings(tmp_path)
    calls = {"commodities": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/token" or req.url.host == "oauth.battle.net":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        assert req.url.params["namespace"].endswith("forever-eu")
        if path == "/data/wow/ruleset/index":
            return httpx.Response(200, json={"rulesets": [{"id": 1}, {"id": 2}]})
        if path == "/data/wow/ruleset/1/auctions/":
            return httpx.Response(200, json={"auctions": [{"id": 110, "name": "Alliance"}, {"id": 111, "name": "Horde"},
                                                         {"id": 112, "name": "Blackwater"}]})
        if path == "/data/wow/ruleset/1/auctions/111/commodities":
            calls["commodities"] += 1
            if req.headers.get("If-Modified-Since"):
                return httpx.Response(304)
            return httpx.Response(200, headers={"Last-Modified": "Fri, 20 Nov 2026 12:00:00 GMT"}, json={"auctions": [
                {"id": 1, "item": {"id": 249274}, "quantity": 200, "unit_price": 1738, "time_left": "LONG"}]})
        if path == "/data/wow/ruleset/1/auctions/111":
            return httpx.Response(200, headers={"Last-Modified": "Fri, 20 Nov 2026 12:00:00 GMT"}, json={"auctions": [
                {"id": 9, "item": {"id": 254035}, "quantity": 1, "buyout": 29550, "time_left": "LONG"}]})
        if path.startswith("/data/wow/item/"):
            iid = int(path.rsplit("/", 1)[1])
            return httpx.Response(200, json={"id": iid, "name": {249274: "Stranglevine", 254035: "Mystery"}[iid],
                                             "quality": {"type": "UNCOMMON"}, "item_class": {"name": "Tradeskill", "id": 7},
                                             "item_subclass": {"name": "Herb", "id": 9}, "level": 50,
                                             "required_level": 40, "sell_price": 500})
        return httpx.Response(404)

    async def go():
        client = BlizzardClient(s, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        cn = db.connect(s.db_path)
        rid, houses = await c.resolve_houses(client, cn, s)
        assert (rid, houses) == (1, [111])
        await c.collect_once(client, cn, s, rid, houses)
        await c.collect_once(client, cn, s, rid, houses)  # andra gången: 304
        await client.aclose()
        return cn

    cn = asyncio.run(go())
    assert calls["commodities"] == 2
    assert cn.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 2
    assert a.resolve_item(cn, "stranglevine")[0] == 249274
    row = cn.execute("SELECT class_id, subclass_id, required_level FROM items WHERE item_id=249274").fetchone()
    assert tuple(row) == (7, 9, 40)
    p = a.price(cn, 111, 249274)
    assert p["min_price"] == "17s 38c" and p["data_from"].startswith("2026-11-20T12:00")
    st = a.status(cn)
    assert {r["kind"] for r in st["runs"]} == {"auctions", "commodities"} and st["collected_houses"] == [111]


# --- MCP-verktygen via FastMCP ---


def test_mcp_tools(conn, tmp_path, monkeypatch):
    import importlib

    monkeypatch.setenv("AH_DB_PATH", str(tmp_path / "ah.sqlite"))
    monkeypatch.setenv("AH_DEFAULT_HOUSE", "horde")
    import forever_ah.server as srv
    srv = importlib.reload(srv)

    async def go():
        tools = {t.name for t in await srv.mcp.list_tools()}
        assert {"ah_price", "ah_history", "ah_signals", "ah_depth", "ah_movers", "ah_search_items", "ah_status"} <= tools
        res = await srv.mcp.call_tool("ah_signals", {"item": "Stranglevine"})
        return res

    res = asyncio.run(go())
    blocks = res[0] if isinstance(res, tuple) else res
    text = json.loads(blocks[0].text)
    assert text["item"] == "Stranglevine" and text["house"] == "Horde"
    assert "score" in text

    async def bad():
        return await srv.mcp.call_tool("ah_price", {"item": "finns inte"})
    res = asyncio.run(bad())
    blocks = res[0] if isinstance(res, tuple) else res
    assert "error" in json.loads(blocks[0].text)


def test_resolve_houses_by_id(tmp_path):
    from dataclasses import replace

    s = replace(settings(tmp_path), houses=("112",))

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "oauth.battle.net":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if req.url.path == "/data/wow/ruleset/index":
            return httpx.Response(200, json={"rulesets": [{"id": 1}]})
        return httpx.Response(200, json={"auctions": [{"id": 111, "name": "Normal Horde"},
                                                     {"id": 112, "name": "PvP Horde"}]})

    async def go():
        client = BlizzardClient(s, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try:
            return await c.resolve_houses(client, db.connect(s.db_path), s)
        finally:
            await client.aclose()

    assert asyncio.run(go()) == (1, [112])


def test_categories_and_category_items(conn):
    cats = a.categories(conn, 2)
    assert cats["categories"]["Tradeskill"][0]["subclass"] == "Herb"
    res = a.category_items(conn, 2, "tradeskill", "herb", min_level=40, max_level=50)
    assert res["matches"] == 1 and res["items"][0]["name"] == "Stranglevine"
    assert a.category_items(conn, 2, "tradeskill", None, max_level=30)["matches"] == 0
    m = a.movers(conn, 2, hours=72, min_daily_turnover_copper=0, item_subclass="herb")
    assert all(x["category"] == "Tradeskill / Herb" for x in m["up"] + m["down"])
    assert a.price(conn, 2, 249274)["category"] == "Tradeskill / Herb"


def test_migration_adds_columns(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.sqlite")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE items (item_id INTEGER PRIMARY KEY, name TEXT, name_lc TEXT, quality TEXT, "
                "item_class TEXT, item_subclass TEXT, item_level INTEGER, vendor_sell INTEGER, fetched_at INTEGER, "
                "fetch_failed INTEGER NOT NULL DEFAULT 0)")
    old.execute("INSERT INTO items(item_id,name,name_lc) VALUES (1,'Old','old')")
    old.commit(); old.close()
    cn = db.connect(path)
    cols = {r[1] for r in cn.execute("PRAGMA table_info(items)")}
    assert {"class_id", "subclass_id", "required_level"} <= cols
    assert cn.execute("SELECT name FROM items WHERE item_id=1").fetchone()[0] == "Old"
