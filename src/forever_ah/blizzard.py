"""Tunn asynkron klient mot Battle.net Game Data API för WoW Forever.

Forever saknar realms. Auktionshusen ligger under ett ruleset:
    /data/wow/ruleset/index
    /data/wow/ruleset/{id}/auctions/                       (avslutande snedstreck krävs)
    /data/wow/ruleset/{id}/auctions/{houseId}              (vanliga auktioner)
    /data/wow/ruleset/{id}/auctions/{houseId}/commodities  (commodities)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth.battle.net/token"


class BlizzardError(RuntimeError):
    pass


@dataclass
class FetchResult:
    status: int  # 200 eller 304
    data: dict[str, Any] | None
    last_modified: str | None

    @property
    def data_ts(self) -> int | None:
        """Datats tidpunkt (unix) enligt Last-Modified, annars None."""
        if not self.last_modified:
            return None
        try:
            return int(parsedate_to_datetime(self.last_modified).timestamp())
        except (TypeError, ValueError):
            return None


class BlizzardClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None, rate_per_s: float = 20.0):
        self.s = settings
        self._client = client or httpx.AsyncClient(timeout=settings.http_timeout_s)
        self._token: str | None = None
        self._token_exp = 0.0
        self._token_lock = asyncio.Lock()
        # Blizzard tillåter 100 anrop/s men stryper korta skurar; håll jämn takt
        self._min_interval = 1.0 / max(1.0, rate_per_s)
        self._rate_lock = asyncio.Lock()
        self._next_slot = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self._min_interval
        if wait > 0:
            await asyncio.sleep(wait)

    async def _get_token(self, force: bool = False) -> str:
        async with self._token_lock:
            if not force and self._token and time.time() < self._token_exp - 60:
                return self._token
            r = await self._client.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(self.s.client_id, self.s.client_secret),
            )
            if r.status_code != 200:
                raise BlizzardError(f"OAuth misslyckades: {r.status_code} {r.text[:200]}")
            body = r.json()
            self._token = body["access_token"]
            self._token_exp = time.time() + int(body.get("expires_in", 3600))
            return self._token

    async def get(
        self,
        path: str,
        namespace: str,
        *,
        if_modified_since: str | None = None,
        params: dict[str, str] | None = None,
        retries: int = 3,
    ) -> FetchResult:
        url = self.s.api_base + path
        q = {"namespace": namespace, "locale": self.s.locale, **(params or {})}
        backoff = 2.0
        for attempt in range(1, retries + 1):
            token = await self._get_token()
            headers = {"Authorization": f"Bearer {token}"}
            if if_modified_since:
                headers["If-Modified-Since"] = if_modified_since
            await self._throttle()
            try:
                r = await self._client.get(url, params=q, headers=headers)
            except httpx.TransportError as exc:
                if attempt == retries:
                    raise BlizzardError(f"Nätverksfel mot {path}: {exc}") from exc
                await asyncio.sleep(backoff)
                backoff *= 2
                continue

            if r.status_code == 304:
                return FetchResult(304, None, if_modified_since)
            if r.status_code == 200:
                return FetchResult(200, r.json(), r.headers.get("Last-Modified"))
            if r.status_code == 401 and attempt < retries:
                await self._get_token(force=True)
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = float(r.headers.get("Retry-After", backoff))
                (log.debug if r.status_code == 429 else log.warning)(
                    "HTTP %s från %s, försöker igen om %.0fs", r.status_code, path, wait)
                await asyncio.sleep(wait)
                backoff *= 2
                continue
            if r.status_code == 404:
                return FetchResult(404, None, None)
            raise BlizzardError(f"HTTP {r.status_code} från {path}: {r.text[:200]}")
        raise BlizzardError(f"Gav upp efter {retries} försök: {path}")

    # --- Forever-specifika anrop ---

    async def ruleset_ids(self) -> list[int]:
        res = await self.get("/data/wow/ruleset/index", self.s.ns_dynamic)
        if not res.data:
            raise BlizzardError("Tomt ruleset-index")
        return [int(r["id"]) for r in res.data.get("rulesets", [])]

    async def auction_houses(self, ruleset_id: int) -> list[dict[str, Any]]:
        res = await self.get(f"/data/wow/ruleset/{ruleset_id}/auctions/", self.s.ns_dynamic)
        if not res.data:
            raise BlizzardError(f"Inget auktionshus-index för ruleset {ruleset_id}")
        return res.data.get("auctions", [])

    async def house_auctions(self, ruleset_id: int, house_id: int, ims: str | None) -> FetchResult:
        return await self.get(
            f"/data/wow/ruleset/{ruleset_id}/auctions/{house_id}", self.s.ns_dynamic, if_modified_since=ims
        )

    async def house_commodities(self, ruleset_id: int, house_id: int, ims: str | None) -> FetchResult:
        return await self.get(
            f"/data/wow/ruleset/{ruleset_id}/auctions/{house_id}/commodities",
            self.s.ns_dynamic,
            if_modified_since=ims,
        )

    async def item(self, item_id: int) -> dict[str, Any] | None:
        res = await self.get(f"/data/wow/item/{item_id}", self.s.ns_static, retries=2)
        return res.data
