"""Konfiguration via miljövariabler (läses en gång vid start)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Miljövariabeln {name} saknas")
    return value or ""


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    region: str = "eu"
    locale: str = "en_GB"
    # Hus att samla in, matchas mot namnet i auktionshus-indexet (skiftlägesokänsligt, delsträng).
    # Forever har Alliance, Horde och det neutrala Blackwater.
    houses: tuple[str, ...] = ("horde",)
    # Alla rulesets i en region delar samma auktionshus; ID:t måste bara vara giltigt.
    ruleset_id: int | None = None
    db_path: str = "/data/ah.sqlite"
    poll_minutes: int = 20
    hourly_retention_days: int = 14
    item_fetch_per_run: int = 300
    http_timeout_s: float = 120.0
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8765
    mcp_token: str = ""
    default_house: str = field(default="")

    @property
    def api_base(self) -> str:
        return f"https://{self.region}.api.blizzard.com"

    @property
    def ns_dynamic(self) -> str:
        return f"dynamic-forever-{self.region}"

    @property
    def ns_static(self) -> str:
        return f"static-forever-{self.region}"


def load_settings(require_api: bool = True) -> Settings:
    houses = tuple(h.strip().lower() for h in _env("AH_HOUSES", "horde").split(",") if h.strip())
    ruleset = _env("AH_RULESET_ID")
    return Settings(
        client_id=_env("BLIZZARD_CLIENT_ID", required=require_api),
        client_secret=_env("BLIZZARD_CLIENT_SECRET", required=require_api),
        region=_env("BLIZZARD_REGION", "eu").lower(),
        locale=_env("BLIZZARD_LOCALE", "en_GB"),
        houses=houses,
        ruleset_id=int(ruleset) if ruleset else None,
        db_path=_env("AH_DB_PATH", "/data/ah.sqlite"),
        poll_minutes=int(_env("AH_POLL_MINUTES", "20")),
        hourly_retention_days=int(_env("AH_HOURLY_RETENTION_DAYS", "14")),
        item_fetch_per_run=int(_env("AH_ITEM_FETCH_PER_RUN", "300")),
        http_timeout_s=float(_env("AH_HTTP_TIMEOUT_S", "120")),
        mcp_host=_env("MCP_HOST", "0.0.0.0"),
        mcp_port=int(_env("MCP_PORT", "8765")),
        mcp_token=_env("MCP_TOKEN", ""),
        default_house=_env("AH_DEFAULT_HOUSE", houses[0] if houses else "horde").lower(),
    )
