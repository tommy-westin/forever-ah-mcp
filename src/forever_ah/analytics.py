"""Frågor och analyser ovanpå databasen. Rena funktioner mot en sqlite3-anslutning."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

DAY = 86400


class NotFound(LookupError):
    pass


def fmt(copper: int | float | None) -> str | None:
    """Koppar till '1g 23s 45c'."""
    if copper is None:
        return None
    c = int(round(copper))
    sign = "-" if c < 0 else ""
    c = abs(c)
    g, rest = divmod(c, 10000)
    s, cu = divmod(rest, 100)
    parts = [f"{g}g"] if g else []
    if s or g:
        parts.append(f"{s}s")
    parts.append(f"{cu}c")
    return sign + " ".join(parts)


def _pct(new: float | None, old: float | None) -> float | None:
    if not new or not old:
        return None
    return round((new / old - 1) * 100, 1)


def _iso(ts: int | None) -> str | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="minutes") if ts else None


# --- Uppslag ---


def resolve_house(conn: sqlite3.Connection, house: str | int | None, default: str) -> tuple[int, str]:
    key = str(house if house not in (None, "") else default).strip().lower()
    if key.isdigit():
        row = conn.execute("SELECT house_id, name FROM houses WHERE house_id=?", (int(key),)).fetchone()
    else:
        # Flera hus har samma namn (ett per ruleset); välj bland dem som faktiskt samlas in
        row = conn.execute(
            "SELECT h.house_id, h.name FROM houses h WHERE h.key LIKE ? "
            "ORDER BY EXISTS(SELECT 1 FROM runs r WHERE r.house_id=h.house_id) DESC, h.house_id LIMIT 1",
            (f"%{key}%",),
        ).fetchone()
    if not row:
        raise NotFound(f"Okänt auktionshus: {key}")
    return int(row[0]), row[1]


def resolve_item(conn: sqlite3.Connection, query: str | int) -> tuple[int, str | None]:
    q = str(query).strip()
    if q.isdigit():
        row = conn.execute("SELECT name FROM items WHERE item_id=?", (int(q),)).fetchone()
        return int(q), (row[0] if row else None)
    ql = q.lower()
    row = conn.execute("SELECT item_id, name FROM items WHERE name_lc=?", (ql,)).fetchone()
    if row:
        return int(row[0]), row[1]
    rows = conn.execute(
        "SELECT item_id, name FROM items WHERE name_lc LIKE ? ORDER BY length(name_lc) LIMIT 6", (f"%{ql}%",)
    ).fetchall()
    if len(rows) == 1:
        return int(rows[0][0]), rows[0][1]
    if not rows:
        raise NotFound(f"Hittar inget item som matchar '{q}'. Namn hämtas först när itemet setts på AH.")
    options = ", ".join(f"{r[1]} ({r[0]})" for r in rows)
    raise NotFound(f"Flera träffar för '{q}': {options}. Ange item-id eller exakt namn.")


def search_items(conn: sqlite3.Connection, text: str, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT item_id, name, quality, item_class, item_subclass, item_level FROM items "
        "WHERE name_lc LIKE ? ORDER BY length(name_lc) LIMIT ?",
        (f"%{text.strip().lower()}%", limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _latest_ts(conn: sqlite3.Connection, house_id: int) -> int:
    row = conn.execute("SELECT max(ts) FROM snapshots WHERE house_id=?", (house_id,)).fetchone()
    if not row or row[0] is None:
        raise NotFound("Ingen data insamlad för huset än.")
    return int(row[0])


def _snapshot_at_or_before(conn, house_id, item_id, ts) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM snapshots WHERE house_id=? AND item_id=? AND ts<=? AND qty>0 ORDER BY ts DESC LIMIT 1",
        (house_id, item_id, ts),
    ).fetchone()


def _daily_at_or_before(conn, house_id, item_id, day: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM daily WHERE house_id=? AND item_id=? AND day<=? AND avg_qty>0 ORDER BY day DESC LIMIT 1",
        (house_id, item_id, day),
    ).fetchone()


def _sold_between(conn, house_id, item_id, t0, t1) -> int:
    row = conn.execute(
        "SELECT coalesce(sum(est_sold),0) FROM snapshots WHERE house_id=? AND item_id=? AND ts>? AND ts<=?",
        (house_id, item_id, t0, t1),
    ).fetchone()
    return int(row[0])


# --- Verktyg ---


def item_info(conn: sqlite3.Connection, item_id: int) -> dict[str, Any]:
    r = conn.execute(
        "SELECT item_class, item_subclass, quality, item_level, required_level, vendor_sell FROM items WHERE item_id=?",
        (item_id,),
    ).fetchone()
    if not r:
        return {}
    return {"category": " / ".join(x for x in (r["item_class"], r["item_subclass"]) if x) or None,
            "quality": r["quality"], "item_level": r["item_level"], "required_level": r["required_level"],
            "vendor_sell": fmt(r["vendor_sell"]) if r["vendor_sell"] else None}


def price(conn: sqlite3.Connection, house_id: int, item_id: int) -> dict[str, Any]:
    now = _latest_ts(conn, house_id)
    cur = _snapshot_at_or_before(conn, house_id, item_id, now)
    if not cur:
        raise NotFound("Itemet finns inte ute på AH just nu och saknar historik.")
    d1 = _snapshot_at_or_before(conn, house_id, item_id, cur["ts"] - DAY)
    week_day = datetime.fromtimestamp(cur["ts"] - 7 * DAY, tz=timezone.utc).strftime("%Y-%m-%d")
    d7 = _daily_at_or_before(conn, house_id, item_id, week_day)
    sold24 = _sold_between(conn, house_id, item_id, cur["ts"] - DAY, cur["ts"])
    return {
        **item_info(conn, item_id),
        "data_from": _iso(cur["ts"]),
        "stale_hours": round((now - cur["ts"]) / 3600, 1),
        "min_price": fmt(cur["min_price"]),
        "p10_price": fmt(cur["p10_price"]),
        "median_price": fmt(cur["median_price"]),
        "qty": cur["qty"],
        "listings": cur["listings"],
        "largest_listing_share": cur["top_share"],
        "change_24h_pct": _pct(cur["min_price"], d1["min_price"] if d1 else None),
        "change_7d_pct": _pct(cur["min_price"], d7["min_price"] if d7 else None),
        "qty_change_24h_pct": _pct(cur["qty"], d1["qty"] if d1 else None),
        "est_sold_24h": sold24,
        "raw_copper": {"min": cur["min_price"], "p10": cur["p10_price"], "median": cur["median_price"]},
    }


def history(conn: sqlite3.Connection, house_id: int, item_id: int, days: int, retention_days: int) -> dict[str, Any]:
    days = max(1, min(days, 365))
    now = _latest_ts(conn, house_id)
    if days <= retention_days:
        rows = conn.execute(
            "SELECT ts, min_price, median_price, qty, listings, est_sold FROM snapshots "
            "WHERE house_id=? AND item_id=? AND ts>? ORDER BY ts",
            (house_id, item_id, now - days * DAY),
        ).fetchall()
        series = [{"time": _iso(r["ts"]), "min": r["min_price"], "median": r["median_price"], "qty": r["qty"],
                   "listings": r["listings"], "est_sold": r["est_sold"]} for r in rows]
        res = "hourly"
    else:
        start = datetime.fromtimestamp(now - days * DAY, tz=timezone.utc).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT day, min_price, low_price, median_price, avg_qty, avg_listings, est_sold FROM daily "
            "WHERE house_id=? AND item_id=? AND day>=? ORDER BY day",
            (house_id, item_id, start),
        ).fetchall()
        series = [{"day": r["day"], "min": r["min_price"], "low": r["low_price"], "median": r["median_price"],
                   "qty": r["avg_qty"], "listings": r["avg_listings"], "est_sold": r["est_sold"]} for r in rows]
        res = "daily"
    return {"resolution": res, "prices_in": "copper", "points": len(series), "series": series}


def signals(conn: sqlite3.Connection, house_id: int, item_id: int) -> dict[str, Any]:
    """De tre bottentecknen: priset har slutat falla, utbudet minskar, försäljningen ökar."""
    now = _latest_ts(conn, house_id)
    cur = _snapshot_at_or_before(conn, house_id, item_id, now)
    if not cur:
        raise NotFound("Ingen aktuell data för itemet.")
    t = cur["ts"]
    p2 = _snapshot_at_or_before(conn, house_id, item_id, t - 2 * DAY)
    q3 = _snapshot_at_or_before(conn, house_id, item_id, t - 3 * DAY)
    sold_last = _sold_between(conn, house_id, item_id, t - DAY, t)
    sold_prev = _sold_between(conn, house_id, item_id, t - 4 * DAY, t - DAY)
    first = conn.execute(
        "SELECT min(ts) FROM snapshots WHERE house_id=? AND item_id=?", (house_id, item_id)
    ).fetchone()[0]
    days_of_data = round((t - (first or t)) / DAY, 1)

    def verdict(ok: bool | None) -> str:
        return "ja" if ok else ("nej" if ok is False else "okänt")

    price_ok = None if not p2 else cur["min_price"] >= 0.97 * p2["min_price"]
    supply_ok = None if not q3 else cur["qty"] <= 0.9 * q3["qty"]
    prev_daily = sold_prev / 3 if days_of_data >= 4 else None
    demand_ok = None if prev_daily is None else (sold_last >= 1.1 * prev_daily if prev_daily > 0 else sold_last > 0)

    checks = {
        "price_stopped_falling": {
            "met": verdict(price_ok), "now": fmt(cur["min_price"]), "two_days_ago": fmt(p2["min_price"]) if p2 else None,
            "change_pct": _pct(cur["min_price"], p2["min_price"] if p2 else None), "rule": "lägsta pris >= 97 % av för 2 dygn sedan",
        },
        "supply_falling": {
            "met": verdict(supply_ok), "qty_now": cur["qty"], "qty_three_days_ago": q3["qty"] if q3 else None,
            "change_pct": _pct(cur["qty"], q3["qty"] if q3 else None), "rule": "mängd ute <= 90 % av för 3 dygn sedan",
        },
        "sales_rising": {
            "met": verdict(demand_ok), "est_sold_24h": sold_last,
            "avg_per_day_prev_3d": round(prev_daily, 1) if prev_daily is not None else None,
            "rule": "uppskattat sålt senaste dygnet >= 110 % av snittet för de tre dygnen innan",
        },
    }
    met = sum(1 for c in checks.values() if c["met"] == "ja")
    known = sum(1 for c in checks.values() if c["met"] != "okänt")
    return {
        "score": f"{met}/3",
        "evaluated": known,
        "days_of_history": days_of_data,
        "checks": checks,
        "note": "Försäljning uppskattas från försvunna listningar och kan inkludera avbrutna auktioner."
        if known else "För lite historik: signalerna kräver minst fyra dygns data.",
    }


def depth(conn: sqlite3.Connection, house_id: int, item_id: int, top: int = 10) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT unit_price, qty, time_left FROM listings WHERE house_id=? AND item_id=? ORDER BY unit_price, qty DESC",
        (house_id, item_id),
    ).fetchall()
    if not rows:
        raise NotFound("Itemet ligger inte ute just nu.")
    low = rows[0]["unit_price"]
    total = sum(r["qty"] for r in rows)
    bands = [(1.05, "<= +5 %"), (1.15, "+5-15 %"), (1.30, "+15-30 %"), (1.50, "+30-50 %"), (float("inf"), "> +50 %")]
    out_bands = []
    prev = 0.0
    for limit, label in bands:
        sel = [r for r in rows if prev * low < r["unit_price"] <= limit * low] if prev else \
              [r for r in rows if r["unit_price"] <= limit * low]
        q = sum(r["qty"] for r in sel)
        out_bands.append({"band": label, "qty": q, "listings": len(sel), "share": round(q / total, 3)})
        prev = limit
    # Kostnad för att köpa upp de billigaste N procenten av mängden
    def cost_to_buy(fraction: float) -> dict[str, Any]:
        target = total * fraction
        acc, spent = 0, 0
        for r in rows:
            take = min(r["qty"], target - acc)
            if take <= 0:
                break
            acc += take
            spent += take * r["unit_price"]
        return {"qty": int(acc), "cost": fmt(spent), "avg_price": fmt(spent / acc) if acc else None}

    return {
        "total_qty": total,
        "listings": len(rows),
        "lowest": fmt(low),
        "bands_vs_lowest": out_bands,
        "cheapest_listings": [{"price": fmt(r["unit_price"]), "qty": r["qty"], "time_left": r["time_left"]}
                              for r in rows[:top]],
        "buy_cheapest_25pct": cost_to_buy(0.25),
        "largest_listing_share": round(max(r["qty"] for r in rows) / total, 3),
    }


def _category_match(item_class: str | None, item_subclass: str | None):
    """SQL-villkor mot items-tabellen (alias i) för klass/subklass, skiftlägesokänsligt delsträng."""
    where, args = [], []
    if item_class:
        where.append("lower(i.item_class) LIKE ?")
        args.append(f"%{item_class.strip().lower()}%")
    if item_subclass:
        where.append("lower(i.item_subclass) LIKE ?")
        args.append(f"%{item_subclass.strip().lower()}%")
    return where, args


def movers(conn: sqlite3.Connection, house_id: int, hours: int = 24, min_daily_turnover_copper: int = 50000,
           limit: int = 15, item_class: str | None = None, item_subclass: str | None = None) -> dict[str, Any]:
    now = _latest_ts(conn, house_id)
    then = now - hours * 3600
    rows = conn.execute(
        """
        WITH cur AS (
            SELECT s.item_id, s.min_price, s.qty FROM snapshots s
            JOIN (SELECT item_id, max(ts) ts FROM snapshots WHERE house_id=:h AND ts<=:now AND qty>0 GROUP BY item_id) m
              ON m.item_id=s.item_id AND m.ts=s.ts WHERE s.house_id=:h),
        old AS (
            SELECT s.item_id, s.min_price, s.qty FROM snapshots s
            JOIN (SELECT item_id, max(ts) ts FROM snapshots WHERE house_id=:h AND ts<=:then AND qty>0 GROUP BY item_id) m
              ON m.item_id=s.item_id AND m.ts=s.ts WHERE s.house_id=:h),
        sold AS (
            SELECT item_id, sum(est_sold) n FROM snapshots WHERE house_id=:h AND ts>:then AND ts<=:now GROUP BY item_id)
        SELECT cur.item_id, i.name, i.item_class, i.item_subclass, cur.min_price AS now_p, old.min_price AS old_p, cur.qty AS now_q, old.qty AS old_q,
               coalesce(sold.n,0) AS sold
        FROM cur JOIN old ON old.item_id=cur.item_id
        LEFT JOIN sold ON sold.item_id=cur.item_id
        LEFT JOIN items i ON i.item_id=cur.item_id
        """,
        {"h": house_id, "now": now, "then": then},
    ).fetchall()
    scale = 24 / hours
    cand = []
    cls_l = item_class.strip().lower() if item_class else None
    sub_l = item_subclass.strip().lower() if item_subclass else None
    for r in rows:
        if cls_l and cls_l not in (r["item_class"] or "").lower():
            continue
        if sub_l and sub_l not in (r["item_subclass"] or "").lower():
            continue
        turnover = r["sold"] * r["now_p"] * scale
        if turnover < min_daily_turnover_copper or not r["old_p"]:
            continue
        cand.append({
            "item_id": r["item_id"], "name": r["name"],
            "category": " / ".join(x for x in (r["item_class"], r["item_subclass"]) if x) or None,
            "now": fmt(r["now_p"]), "before": fmt(r["old_p"]),
            "change_pct": _pct(r["now_p"], r["old_p"]), "qty_change_pct": _pct(r["now_q"], r["old_q"]),
            "est_sold": r["sold"], "est_daily_turnover": fmt(turnover),
        })
    cand.sort(key=lambda x: x["change_pct"])
    return {"window_hours": hours, "up": list(reversed(cand[-limit:])), "down": cand[:limit]}


def categories(conn: sqlite3.Connection, house_id: int) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT i.item_class, i.item_subclass, count(DISTINCT l.item_id) AS items, sum(l.qty) AS qty
           FROM listings l JOIN items i ON i.item_id=l.item_id
           WHERE l.house_id=? AND i.item_class IS NOT NULL
           GROUP BY 1,2 ORDER BY 1, 3 DESC""",
        (house_id,),
    ).fetchall()
    missing = conn.execute(
        """SELECT count(DISTINCT l.item_id) FROM listings l LEFT JOIN items i ON i.item_id=l.item_id
           WHERE l.house_id=? AND i.item_class IS NULL""", (house_id,)).fetchone()[0]
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["item_class"], []).append(
            {"subclass": r["item_subclass"], "items_listed": r["items"], "qty": r["qty"]})
    return {"categories": out, "items_without_category": missing}


def category_items(conn: sqlite3.Connection, house_id: int, item_class: str | None, item_subclass: str | None,
                   min_level: int | None = None, max_level: int | None = None, limit: int = 30) -> dict[str, Any]:
    """Items i en kategori sorterade på uppskattad omsättning senaste dygnet."""
    now = _latest_ts(conn, house_id)
    where, args = _category_match(item_class, item_subclass)
    if min_level is not None:
        where.append("coalesce(i.item_level,0) >= ?")
        args.append(min_level)
    if max_level is not None:
        where.append("coalesce(i.item_level,0) <= ?")
        args.append(max_level)
    if not where:
        raise NotFound("Ange item_class och/eller item_subclass. Se ah_categories för giltiga värden.")
    rows = conn.execute(
        f"""
        WITH cur AS (
            SELECT s.item_id, s.min_price, s.median_price, s.qty, s.listings FROM snapshots s
            JOIN (SELECT item_id, max(ts) ts FROM snapshots WHERE house_id=? AND qty>0 GROUP BY item_id) m
              ON m.item_id=s.item_id AND m.ts=s.ts WHERE s.house_id=?),
        sold AS (SELECT item_id, sum(est_sold) n FROM snapshots WHERE house_id=? AND ts>? GROUP BY item_id)
        SELECT i.item_id, i.name, i.item_class, i.item_subclass, i.item_level, i.quality,
               cur.min_price, cur.median_price, cur.qty, cur.listings, coalesce(sold.n,0) AS sold
        FROM items i JOIN cur ON cur.item_id=i.item_id LEFT JOIN sold ON sold.item_id=i.item_id
        WHERE {' AND '.join(where)}
        """,
        (house_id, house_id, house_id, now - DAY, *args),
    ).fetchall()
    items = [{
        "item_id": r["item_id"], "name": r["name"], "subclass": r["item_subclass"], "item_level": r["item_level"],
        "quality": r["quality"], "min_price": fmt(r["min_price"]), "median_price": fmt(r["median_price"]),
        "qty": r["qty"], "listings": r["listings"], "est_sold_24h": r["sold"],
        "est_turnover_24h": fmt(r["sold"] * r["min_price"]), "_t": r["sold"] * r["min_price"],
    } for r in rows]
    items.sort(key=lambda x: -x["_t"])
    for x in items:
        x.pop("_t")
    return {"matches": len(items), "items": items[:limit]}


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    runs = [dict(r) for r in conn.execute(
        "SELECT r.house_id, h.name AS house, r.kind, r.data_ts, r.fetched_at AS last_checked, r.n_listings FROM runs r "
        "JOIN houses h ON h.house_id=r.house_id ORDER BY h.name, r.kind")]
    for r in runs:
        r["data_ts"] = _iso(r["data_ts"])
        r["last_checked"] = _iso(r["last_checked"])
    counts = conn.execute(
        "SELECT (SELECT count(*) FROM items WHERE name IS NOT NULL), (SELECT count(*) FROM snapshots), "
        "(SELECT count(*) FROM daily), (SELECT min(day) FROM daily)").fetchone()
    return {"runs": runs, "named_items": counts[0], "hourly_rows": counts[1], "daily_rows": counts[2],
            "history_since": counts[3],
            "collected_houses": sorted({r["house_id"] for r in runs}),
            "note": "Husnamn är inte unika (ett hus per ruleset och fraktion). Ange hus-id för exakthet."}
