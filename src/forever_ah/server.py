"""MCP-server som exponerar den insamlade auktionsdatan.

Transporter:
    forever-ah-mcp                      stdio (Claude Desktop, Claude Code lokalt)
    forever-ah-mcp --http               streamable HTTP på MCP_HOST:MCP_PORT/mcp
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import hmac
import logging
import os
import sqlite3
from typing import Any, Iterator

from mcp.server.fastmcp import FastMCP

from . import analytics as a
from . import db
from .config import load_settings

log = logging.getLogger("forever_ah.server")
S = load_settings(require_api=False)

mcp = FastMCP(
    "forever-ah",
    instructions=(
        "Auktionsdata för WoW Forever, insamlad varje timme från Blizzards API för ett eller flera auktionshus. "
        "Priser är per styck. Items anges med id eller namn (engelska). 'house' är t.ex. 'horde', 'alliance' "
        "eller 'blackwater'; utelämnat används standardhuset. Sålda mängder är uppskattningar från "
        "försvunna listningar och kan innehålla avbrutna auktioner."
    ),
    host=S.mcp_host,
    port=S.mcp_port,
)


@contextlib.contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    if not os.path.exists(S.db_path):
        raise a.NotFound(f"Databasen {S.db_path} finns inte. Har insamlaren körts?")
    conn = db.connect(S.db_path, readonly=True)
    try:
        yield conn
    finally:
        conn.close()


def _ctx(conn: sqlite3.Connection, item: str | int | None, house: str | None) -> dict[str, Any]:
    house_id, house_name = a.resolve_house(conn, house, S.default_house)
    out: dict[str, Any] = {"house": house_name, "house_id": house_id}
    if item is not None:
        item_id, name = a.resolve_item(conn, item)
        out.update(item_id=item_id, item=name)
    return out


def _safe(fn):
    """Gör uppslagsfel till läsbara svar i stället för undantag."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except a.NotFound as exc:
            return {"error": str(exc)}
    return wrapper


@mcp.tool()
@_safe
def ah_price(item: str, house: str | None = None) -> dict[str, Any]:
    """Aktuellt pris för ett item: lägsta, 10:e percentil, median, mängd ute, förändring 24 h och 7 dygn,
    samt uppskattat sålt senaste dygnet."""
    with _conn() as c:
        ctx = _ctx(c, item, house)
        return {**ctx, **a.price(c, ctx["house_id"], ctx["item_id"])}


@mcp.tool()
@_safe
def ah_history(item: str, days: int = 7, house: str | None = None) -> dict[str, Any]:
    """Prishistorik. Upp till lagringstiden för timdata (standard 14 dygn) ges per timme, längre per dygn.
    Priser i koppar."""
    with _conn() as c:
        ctx = _ctx(c, item, house)
        return {**ctx, **a.history(c, ctx["house_id"], ctx["item_id"], days, S.hourly_retention_days)}


@mcp.tool()
@_safe
def ah_signals(item: str, house: str | None = None) -> dict[str, Any]:
    """Bottentecken för ett item som fallit i pris: (1) priset har slutat falla, (2) mängden ute minskar,
    (3) försäljningen ökar. Ger poäng 0-3 med underlag, plus nivåbandets utveckling som kontext (level_band)."""
    with _conn() as c:
        ctx = _ctx(c, item, house)
        return {**ctx, **a.signals(c, ctx["house_id"], ctx["item_id"])}


@mcp.tool()
@_safe
def ah_depth(item: str, house: str | None = None) -> dict[str, Any]:
    """Orderbok för ett item just nu: mängd per prisband över lägsta pris, de billigaste listningarna,
    vad det kostar att köpa upp den billigaste fjärdedelen och hur stor den största enskilda listningen är."""
    with _conn() as c:
        ctx = _ctx(c, item, house)
        return {**ctx, **a.depth(c, ctx["house_id"], ctx["item_id"])}


@mcp.tool()
@_safe
def ah_movers(hours: int = 24, min_daily_turnover_gold: float = 5.0, item_class: str | None = None,
              item_subclass: str | None = None, house: str | None = None) -> dict[str, Any]:
    """Största prisrörelserna under perioden bland items med minst angiven daglig omsättning (uppskattad).
    Kan filtreras på kategori, t.ex. item_class='Tradeskill', item_subclass='Herb'."""
    with _conn() as c:
        ctx = _ctx(c, None, house)
        return {**ctx, **a.movers(c, ctx["house_id"], max(1, min(hours, 24 * 14)),
                                  int(min_daily_turnover_gold * 10000),
                                  item_class=item_class, item_subclass=item_subclass)}


@mcp.tool()
@_safe
def ah_categories(house: str | None = None) -> dict[str, Any]:
    """Alla kategorier (klass och subklass enligt Blizzard) som finns ute på AH, med antal items och mängd."""
    with _conn() as c:
        ctx = _ctx(c, None, house)
        return {**ctx, **a.categories(c, ctx["house_id"])}


@mcp.tool()
@_safe
def ah_category(item_class: str | None = None, item_subclass: str | None = None, min_item_level: int | None = None,
                max_item_level: int | None = None, limit: int = 30, house: str | None = None) -> dict[str, Any]:
    """Items i en kategori med pris, mängd och uppskattat sålt senaste dygnet, sorterade på omsättning.
    Klass/subklass matchas som delsträng (se ah_categories). Itemnivå avgränsar t.ex. nivåband."""
    with _conn() as c:
        ctx = _ctx(c, None, house)
        return {**ctx, **a.category_items(c, ctx["house_id"], item_class, item_subclass, min_item_level,
                                          max_item_level, max(1, min(limit, 200)))}


@mcp.tool()
@_safe
def ah_level_activity(days: int = 7, band_size: int = 10, item_class: str | None = None,
                      item_subclass: str | None = None, house: str | None = None) -> dict[str, Any]:
    """Var på nivåskalan handeln sker: uppskattad omsättning och såld mängd per nivåband (itemnivå) och dag,
    med trend i omsättningsandel. Filtrera på kategori, t.ex. item_class='Trade Goods' eller item_subclass='Herb',
    för att se när en nivåvåg når ett visst material."""
    with _conn() as c:
        ctx = _ctx(c, None, house)
        return {**ctx, **a.level_activity(c, ctx["house_id"], days, band_size, item_class, item_subclass)}


@mcp.tool()
@_safe
def ah_search_items(text: str, limit: int = 20) -> dict[str, Any]:
    """Sök item-id på namn (engelska, delsträng)."""
    with _conn() as c:
        return {"results": a.search_items(c, text, max(1, min(limit, 100)))}


@mcp.tool()
@_safe
def ah_status() -> dict[str, Any]:
    """Insamlingens status: senaste data per hus, antal rader och hur långt bak historiken går."""
    with _conn() as c:
        return a.status(c)


class BearerAuth:
    """ASGI-middleware: kräver 'Authorization: Bearer <token>' där token är en av de konfigurerade.

    MCP_TOKEN kan innehålla flera kommaseparerade tokens, t.ex. en per person, så att en kan spärras
    utan att de andra påverkas.
    """

    def __init__(self, app, tokens: list[str]):
        self.app = app
        self.expected = [b"Bearer " + t.encode() for t in tokens]

    def _ok(self, got: bytes) -> bool:
        # Jämför mot alla utan att avbryta tidigt, för konstant tid
        return any([hmac.compare_digest(got, e) for e in self.expected])

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            if not self._ok(headers.get(b"authorization", b"")):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
        await self.app(scope, receive, send)


def main() -> None:
    p = argparse.ArgumentParser(description="MCP-server för WoW Forever-auktionsdata")
    p.add_argument("--http", action="store_true", help="Kör streamable HTTP i stället för stdio")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not args.http:
        mcp.run("stdio")
        return
    import uvicorn

    app = mcp.streamable_http_app()
    tokens = [t.strip() for t in S.mcp_token.split(",") if t.strip()]
    if tokens:
        app = BearerAuth(app, tokens)
        log.info("Bearer-autentisering aktiv med %d token(s)", len(tokens))
    else:
        log.warning("MCP_TOKEN är inte satt: HTTP-servern är öppen för alla som når porten.")
    uvicorn.run(app, host=S.mcp_host, port=S.mcp_port, log_level="info")


if __name__ == "__main__":
    main()
