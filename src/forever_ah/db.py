"""SQLite-lagring. WAL-läge så att insamlaren kan skriva medan MCP-servern läser."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS houses (
    house_id      INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    key           TEXT NOT NULL            -- normaliserat namn, t.ex. 'horde'
);

CREATE TABLE IF NOT EXISTS items (
    item_id       INTEGER PRIMARY KEY,
    name          TEXT,
    name_lc       TEXT,
    quality       TEXT,
    item_class    TEXT,
    item_subclass TEXT,
    item_level    INTEGER,
    class_id      INTEGER,
    subclass_id   INTEGER,
    required_level INTEGER,
    vendor_sell   INTEGER,
    fetched_at    INTEGER,
    fetch_failed  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_items_name ON items(name_lc);

-- Timaggregat per item och hus. Priser i koppar per styck.
CREATE TABLE IF NOT EXISTS snapshots (
    house_id      INTEGER NOT NULL,
    item_id       INTEGER NOT NULL,
    ts            INTEGER NOT NULL,        -- datats tid (Last-Modified), unix
    min_price     INTEGER NOT NULL,
    p10_price     INTEGER NOT NULL,        -- kvantitetsviktad 10:e percentil
    median_price  INTEGER NOT NULL,        -- kvantitetsviktad median
    qty           INTEGER NOT NULL,        -- total mängd ute
    listings      INTEGER NOT NULL,        -- antal auktioner
    top_share     REAL NOT NULL,           -- största enskilda auktionens andel av mängden
    est_sold      INTEGER NOT NULL DEFAULT 0, -- uppskattat sålt sedan förra snapshot
    PRIMARY KEY (house_id, item_id, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_snap_ts ON snapshots(house_id, ts);

-- Dagsaggregat (UTC-dygn), behålls för alltid.
CREATE TABLE IF NOT EXISTS daily (
    house_id      INTEGER NOT NULL,
    item_id       INTEGER NOT NULL,
    day           TEXT NOT NULL,           -- YYYY-MM-DD
    min_price     INTEGER NOT NULL,        -- median av timmarnas lägsta pris
    low_price     INTEGER NOT NULL,        -- dygnets lägsta pris
    median_price  INTEGER NOT NULL,
    avg_qty       INTEGER NOT NULL,
    avg_listings  INTEGER NOT NULL,
    est_sold      INTEGER NOT NULL,
    samples       INTEGER NOT NULL,
    PRIMARY KEY (house_id, item_id, day)
) WITHOUT ROWID;

-- Senaste snapshotens enskilda auktioner, används för orderbok och säljuppskattning.
CREATE TABLE IF NOT EXISTS listings (
    house_id      INTEGER NOT NULL,
    auction_id    INTEGER NOT NULL,
    kind          TEXT NOT NULL,           -- 'auctions' eller 'commodities'
    item_id       INTEGER NOT NULL,
    qty           INTEGER NOT NULL,
    unit_price    INTEGER NOT NULL,
    time_left     TEXT NOT NULL,
    PRIMARY KEY (house_id, auction_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_list_item ON listings(house_id, item_id, unit_price);

CREATE TABLE IF NOT EXISTS runs (
    house_id      INTEGER NOT NULL,
    kind          TEXT NOT NULL,           -- 'auctions' eller 'commodities'
    last_modified TEXT,
    data_ts       INTEGER,
    fetched_at    INTEGER NOT NULL,
    n_listings    INTEGER NOT NULL,
    PRIMARY KEY (house_id, kind)
);
"""


MIGRATIONS = {
    "items": {"class_id": "INTEGER", "subclass_id": "INTEGER", "required_level": "INTEGER"},
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_items_class ON items(class_id, subclass_id)")
    conn.commit()


def connect(path: str, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
        conn.executescript(SCHEMA.replace(
            "CREATE INDEX IF NOT EXISTS ix_items_name ON items(name_lc);", ""))
        _migrate(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS ix_items_name ON items(name_lc)")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn
