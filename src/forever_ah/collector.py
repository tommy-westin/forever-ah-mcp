"""Insamlare: hämtar auktionsdata varje timme, aggregerar per item och sparar i SQLite.

Kör som långlivad process (`forever-ah-collector`) eller en gång (`forever-ah-collector --once`).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from . import db
from .blizzard import BlizzardClient, BlizzardError
from .config import Settings, load_settings

log = logging.getLogger("forever_ah.collector")

KINDS = ("commodities", "auctions")


@dataclass(frozen=True)
class Listing:
    auction_id: int
    item_id: int
    qty: int
    unit_price: int
    time_left: str


@dataclass(frozen=True)
class ItemStats:
    min_price: int
    p10_price: int
    median_price: int
    qty: int
    listings: int
    top_share: float


# --- Rena funktioner (testbara utan nätverk) ---


def normalize(payload: dict[str, Any] | None, kind: str) -> list[Listing]:
    """Plattar ut API-svaret till listningar med pris per styck. Auktioner utan buyout hoppas över."""
    out: list[Listing] = []
    for a in (payload or {}).get("auctions") or []:
        try:
            qty = int(a.get("quantity") or 0)
            if qty <= 0:
                continue
            if kind == "commodities":
                unit = int(a["unit_price"])
            else:
                buyout = a.get("buyout")
                if not buyout:
                    continue  # endast bud, inget direktköpspris
                unit = int(buyout) // qty
            out.append(Listing(int(a["id"]), int(a["item"]["id"]), qty, unit, str(a.get("time_left", ""))))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _weighted_quantile(pairs: list[tuple[int, int]], q: float) -> int:
    """pairs = (pris, mängd) sorterat på pris. Returnerar priset där kumulativ mängd når q."""
    total = sum(n for _, n in pairs)
    target = q * total
    acc = 0
    for price, n in pairs:
        acc += n
        if acc >= target:
            return price
    return pairs[-1][0]


def aggregate(listings: Iterable[Listing]) -> dict[int, ItemStats]:
    by_item: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for l in listings:
        by_item[l.item_id].append((l.unit_price, l.qty))
    out: dict[int, ItemStats] = {}
    for item_id, pairs in by_item.items():
        pairs.sort()
        total = sum(n for _, n in pairs)
        out[item_id] = ItemStats(
            min_price=pairs[0][0],
            p10_price=_weighted_quantile(pairs, 0.10),
            median_price=_weighted_quantile(pairs, 0.50),
            qty=total,
            listings=len(pairs),
            top_share=round(max(n for _, n in pairs) / total, 3),
        )
    return out


def estimate_sold(prev: dict[int, tuple[int, int, str]], new: dict[int, int]) -> dict[int, int]:
    """Uppskattar såld mängd per item mellan två snapshots.

    prev: auction_id -> (item_id, qty, time_left)   new: auction_id -> qty
    - Auktion som försvunnit och inte hade SHORT kvar räknas som såld (kan också vara avbruten).
    - Commodity-auktion vars mängd minskat räknas som delköp.
    SHORT-auktioner som försvinner antas ha löpt ut.
    """
    sold: dict[int, int] = defaultdict(int)
    for aid, (item_id, qty, time_left) in prev.items():
        if aid in new:
            if new[aid] < qty:
                sold[item_id] += qty - new[aid]
        elif time_left != "SHORT":
            sold[item_id] += qty
    return dict(sold)


# --- Databassteg ---


def _prev_listings(conn: sqlite3.Connection, house_id: int, kind: str) -> dict[int, tuple[int, int, str]]:
    rows = conn.execute(
        "SELECT auction_id, item_id, qty, time_left FROM listings WHERE house_id=? AND kind=?", (house_id, kind)
    )
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def store_snapshot(
    conn: sqlite3.Connection,
    house_id: int,
    kind: str,
    listings: list[Listing],
    data_ts: int,
    last_modified: str | None,
) -> int:
    prev = _prev_listings(conn, house_id, kind)
    stats = aggregate(listings)
    sold = estimate_sold(prev, {l.auction_id: l.qty for l in listings}) if prev else {}

    with conn:
        conn.executemany(
            """INSERT OR REPLACE INTO snapshots
               (house_id,item_id,ts,min_price,p10_price,median_price,qty,listings,top_share,est_sold)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [
                (house_id, i, data_ts, s.min_price, s.p10_price, s.median_price, s.qty, s.listings, s.top_share,
                 sold.get(i, 0))
                for i, s in stats.items()
            ],
        )
        # Item som sålt slut helt: spara en rad med noll i mängd så att försäljningen inte tappas.
        gone = [i for i in sold if i not in stats]
        conn.executemany(
            """INSERT OR REPLACE INTO snapshots
               (house_id,item_id,ts,min_price,p10_price,median_price,qty,listings,top_share,est_sold)
               VALUES (?,?,?,0,0,0,0,0,0,?)""",
            [(house_id, i, data_ts, sold[i]) for i in gone],
        )
        conn.execute("DELETE FROM listings WHERE house_id=? AND kind=?", (house_id, kind))
        conn.executemany(
            "INSERT OR REPLACE INTO listings VALUES (?,?,?,?,?,?,?)",
            [(house_id, l.auction_id, kind, l.item_id, l.qty, l.unit_price, l.time_left) for l in listings],
        )
        conn.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?)",
            (house_id, kind, last_modified, data_ts, int(time.time()), len(listings)),
        )
    return len(stats)


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def rollup(conn: sqlite3.Connection, retention_days: int, now: int | None = None) -> None:
    """Räknar om dagsaggregat för de två senaste dygnen och rensar gamla timrader."""
    now = now or int(time.time())
    start_of_yesterday = (now // 86400 - 1) * 86400
    rows = conn.execute(
        """SELECT house_id,item_id,ts,min_price,median_price,qty,listings,est_sold
           FROM snapshots WHERE ts >= ? ORDER BY house_id,item_id,ts""",
        (start_of_yesterday,),
    ).fetchall()
    groups: dict[tuple[int, int, str], list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        groups[(r["house_id"], r["item_id"], _day(r["ts"]))].append(r)

    out = []
    for (house_id, item_id, day), rs in groups.items():
        priced = [r for r in rs if r["qty"] > 0]
        if priced:
            mins = [r["min_price"] for r in priced]
            meds = [r["median_price"] for r in priced]
            out.append((house_id, item_id, day, int(statistics.median(mins)), min(mins), int(statistics.median(meds)),
                        int(sum(r["qty"] for r in rs) / len(rs)), int(sum(r["listings"] for r in rs) / len(rs)),
                        sum(r["est_sold"] for r in rs), len(rs)))
        else:
            out.append((house_id, item_id, day, 0, 0, 0, 0, 0, sum(r["est_sold"] for r in rs), len(rs)))
    with conn:
        conn.executemany("INSERT OR REPLACE INTO daily VALUES (?,?,?,?,?,?,?,?,?,?)", out)
        conn.execute("DELETE FROM snapshots WHERE ts < ?", (now - retention_days * 86400,))


# --- Asynkrona steg ---


async def resolve_houses(client: BlizzardClient, conn: sqlite3.Connection, s: Settings) -> tuple[int, list[int]]:
    ruleset_id = s.ruleset_id or (await client.ruleset_ids())[0]
    index = await client.auction_houses(ruleset_id)
    chosen: list[int] = []
    with conn:
        for h in index:
            name = str(h.get("name") or h.get("key", {}).get("href", ""))
            key = name.strip().lower()
            conn.execute("INSERT OR REPLACE INTO houses VALUES (?,?,?)", (int(h["id"]), name, key))
            if any(wanted in key for wanted in s.houses):
                chosen.append(int(h["id"]))
    if not chosen:
        names = ", ".join(str(h.get("name")) for h in index)
        raise BlizzardError(f"Inget hus matchar AH_HOUSES={','.join(s.houses)}. Tillgängliga: {names}")
    log.info("Ruleset %s, hus %s", ruleset_id, chosen)
    return ruleset_id, chosen


async def fetch_item_names(client: BlizzardClient, conn: sqlite3.Connection, limit: int) -> int:
    missing = [
        r[0]
        for r in conn.execute(
            """SELECT DISTINCT l.item_id FROM listings l
               LEFT JOIN items i ON i.item_id = l.item_id
               WHERE i.item_id IS NULL OR (i.name IS NULL AND i.fetch_failed < 3)
               LIMIT ?""",
            (limit,),
        )
    ]
    if not missing:
        return 0
    sem = asyncio.Semaphore(10)

    async def one(item_id: int):
        async with sem:
            try:
                return item_id, await client.item(item_id)
            except BlizzardError as exc:
                log.debug("Item %s: %s", item_id, exc)
                return item_id, None

    results = await asyncio.gather(*(one(i) for i in missing))
    now = int(time.time())
    with conn:
        for item_id, d in results:
            if d:
                name = d.get("name")
                conn.execute(
                    "INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,0)",
                    (item_id, name, (name or "").lower(), (d.get("quality") or {}).get("type"),
                     (d.get("item_class") or {}).get("name"), (d.get("item_subclass") or {}).get("name"),
                     d.get("level"), d.get("sell_price"), now),
                )
            else:
                conn.execute(
                    """INSERT INTO items(item_id, fetched_at, fetch_failed) VALUES (?,?,1)
                       ON CONFLICT(item_id) DO UPDATE SET fetch_failed = fetch_failed + 1, fetched_at = excluded.fetched_at""",
                    (item_id, now),
                )
    return sum(1 for _, d in results if d)


async def collect_once(client: BlizzardClient, conn: sqlite3.Connection, s: Settings,
                       ruleset_id: int, house_ids: list[int]) -> None:
    for house_id in house_ids:
        for kind in KINDS:
            row = conn.execute(
                "SELECT last_modified FROM runs WHERE house_id=? AND kind=?", (house_id, kind)
            ).fetchone()
            ims = row[0] if row else None
            fetch = client.house_commodities if kind == "commodities" else client.house_auctions
            res = await fetch(ruleset_id, house_id, ims)
            if res.status == 304:
                log.debug("Hus %s/%s oförändrat", house_id, kind)
                continue
            if res.status != 200 or res.data is None:
                log.warning("Hus %s/%s gav status %s", house_id, kind, res.status)
                continue
            listings = normalize(res.data, kind)
            data_ts = res.data_ts or int(time.time())
            n = store_snapshot(conn, house_id, kind, listings, data_ts, res.last_modified)
            log.info("Hus %s/%s: %d listningar, %d items, data från %s", house_id, kind, len(listings), n,
                     datetime.fromtimestamp(data_ts, tz=timezone.utc).isoformat())
    rollup(conn, s.hourly_retention_days)
    named = await fetch_item_names(client, conn, s.item_fetch_per_run)
    if named:
        log.info("Hämtade namn för %d items", named)


async def run(once: bool) -> None:
    s = load_settings()
    conn = db.connect(s.db_path)
    client = BlizzardClient(s)
    try:
        ruleset_id, house_ids = await resolve_houses(client, conn, s)
        while True:
            try:
                await collect_once(client, conn, s, ruleset_id, house_ids)
            except BlizzardError as exc:
                log.error("Insamling misslyckades: %s", exc)
            if once:
                break
            await asyncio.sleep(s.poll_minutes * 60)
    finally:
        await client.aclose()
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Samlar WoW Forever-auktionsdata")
    p.add_argument("--once", action="store_true", help="Kör en insamling och avsluta")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run(args.once))


if __name__ == "__main__":
    main()
