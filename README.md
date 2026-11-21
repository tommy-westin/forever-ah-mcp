# forever-ah-mcp

Samlar auktionsdata för WoW Forever från Blizzards officiella API varje timme och gör den sökbar för Claude via MCP.

```
Battle.net API ──(varje timme)──> collector ──> SQLite (/data/ah.sqlite) <── mcp-server ──> Claude
  ruleset/{id}/auctions/{hus}              timaggregat 14 dygn            7 verktyg
  ruleset/{id}/auctions/{hus}/commodities  dagsaggregat för alltid        stdio eller HTTP
  item/{id} (namn)                         senaste orderboken
```

## Datamodell

| Tabell | Innehåll |
|---|---|
| `snapshots` | Per item och timme: lägsta pris, 10:e percentil, median (kvantitetsviktade), mängd ute, antal auktioner, största listningens andel, uppskattat sålt |
| `daily` | Dagsaggregat per UTC-dygn, sparas för alltid |
| `listings` | Alla auktioner från senaste hämtningen, används för orderbok och säljuppskattning |
| `items` | Namn, kvalitet, klass och vendorpris från static-API:t, hämtas när ett item först syns |
| `runs`, `houses` | Senaste `Last-Modified` per hus och endpoint, husindex |

Priser lagras i koppar per styck. Auktioner med bara bud (utan buyout) ignoreras.

**Säljuppskattning.** API:t säger inte vad som såldes. En auktion som försvinner mellan två hämtningar räknas som såld om den inte hade `SHORT` kvar; en commodity-auktion vars mängd minskat räknas som delköp. Avbrutna auktioner räknas alltså som sålda, så siffran är en övre gräns. Trenden över tid är pålitligare än nivån.

## MCP-verktyg

| Verktyg | Gör |
|---|---|
| `ah_price(item, house?)` | Pris nu, förändring 24 h och 7 dygn, mängd ute, uppskattat sålt senaste dygnet |
| `ah_history(item, days, house?)` | Tidsserie, per timme upp till 14 dygn, därefter per dygn |
| `ah_signals(item, house?)` | Bottentecken 0–3: priset har slutat falla, mängden minskar, försäljningen ökar |
| `ah_depth(item, house?)` | Orderbok: mängd per prisband, billigaste listningar, kostnad att köpa billigaste 25 % |
| `ah_movers(hours, min_daily_turnover_gold, house?)` | Största prisrörelserna bland items med omsättning |
| `ah_search_items(text)` | Hitta item-id på namn |
| `ah_status()` | Senaste data per hus och hur långt bak historiken går |

`item` kan vara id eller engelskt namn. `house` är `horde`, `alliance` eller `blackwater`.

## Installation i Proxmox-LXC (utan Docker)

Rekommenderat på en liten värd. Två systemd-tjänster med härdning, hemligheter i `/etc/forever-ah/*.env` (läsbara bara för root och tjänstekontot).

```bash
git clone https://github.com/tommy-westin/forever-ah-mcp /opt/src/forever-ah-mcp
cd /opt/src/forever-ah-mcp && bash deploy/install-lxc.sh
nano /etc/forever-ah/collector.env        # fyll i BLIZZARD_CLIENT_ID och _SECRET
systemctl start forever-ah-collector forever-ah-mcp
journalctl -u forever-ah-collector -f
```

Uppdatera: `git pull && bash deploy/install-lxc.sh`. Env-filer och data behålls.

## Installation med Docker

1. Skapa en API-klient på <https://develop.battle.net/access/clients> (gratis, client credentials räcker).
2. Klona repot och skapa `.env`:
   ```bash
   cp .env.example .env
   # fyll i BLIZZARD_CLIENT_ID, BLIZZARD_CLIENT_SECRET och MCP_TOKEN
   openssl rand -hex 32   # ger en token
   ```
3. Bakom en proxy med egen CA: lägg certifikatet som `certs/proxy-ca.crt` före bygget. Annars räcker en tom mapp:
   ```bash
   mkdir -p certs && touch certs/.gitkeep
   ```
4. Starta:
   ```bash
   docker compose up -d --build
   docker compose logs -f collector
   ```
   Första körningen loggar vilket ruleset och vilka hus som valdes och hur många listningar som sparades.

Testa en hämtning utan att starta tjänsterna:
```bash
docker compose run --rm collector forever-ah-collector --once --log-level DEBUG
```

## Koppla till Claude

**Claude Code** (på valfri dator i nätet):
```bash
claude mcp add --transport http forever-ah http://PROXMOX-IP:8765/mcp \
  --header "Authorization: Bearer DIN_TOKEN"
```

**Claude Desktop** via `mcp-remote` (kräver Node), i `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "forever-ah": {
      "command": "cmd",
      "args": ["/c", "npx", "-y", "mcp-remote", "http://PROXMOX-IP:8765/mcp",
               "--allow-http", "--header", "Authorization:${AUTH}"],
      "env": { "AUTH": "Bearer DIN_TOKEN" }
    }
  }
}
```
`--allow-http` behövs eftersom `mcp-remote` annars kräver HTTPS för andra adresser än localhost. Avsluta Claude Desktop helt via systemfältet efter ändringen.

**claude.ai i webbläsaren** kräver en publik HTTPS-adress och stöder inte statiska bearer-tokens. Det kräver en reverse proxy med OAuth och ingår inte här.

**Lokalt via stdio** (utan Docker), om databasen finns på samma maskin:
```bash
pip install .
AH_DB_PATH=./ah.sqlite forever-ah-mcp
```

## Konfiguration

| Variabel | Standard | Beskrivning |
|---|---|---|
| `BLIZZARD_CLIENT_ID`, `BLIZZARD_CLIENT_SECRET` | | Krävs av insamlaren |
| `BLIZZARD_REGION` | `eu` | `eu`, `us`, `kr`, `tw` |
| `AH_HOUSES` | `horde` | Kommaseparerat, matchas mot husnamnet |
| `AH_RULESET_ID` | första | Alla rulesets i en region delar samma hus |
| `AH_POLL_MINUTES` | `20` | Hur ofta API:t frågas; oförändrad data ger 304 och kostar inget |
| `AH_HOURLY_RETENTION_DAYS` | `14` | Därefter finns bara dagsaggregat |
| `AH_ITEM_FETCH_PER_RUN` | `300` | Max antal nya itemnamn per körning |
| `MCP_TOKEN` | tom | Kräver `Authorization: Bearer`; tom betyder öppen port |

## Drift

- **Säkerhet.** Exponera inte port 8765 mot internet. Sätt alltid `MCP_TOKEN`. MCP-containern får inga API-nycklar och öppnar databasen skrivskyddad.
- **Lagring.** Ett hus ger ungefär 8 000 items per timme; med 14 dygns timdata hamnar databasen på några hundra MB. Dagsdata växer med cirka 3 MB per månad.
- **Backup.** `sqlite3 /data/ah.sqlite ".backup /backup/ah.sqlite"` fungerar medan insamlaren kör.
- **Luckor.** Missas hämtningar blir säljuppskattningen för perioden för låg, eftersom flera timmars förändringar slås ihop. Prisdata påverkas inte.

## Utveckling

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```
Testerna körs mot syntetisk data och ett mockat API, utan nätverk.
