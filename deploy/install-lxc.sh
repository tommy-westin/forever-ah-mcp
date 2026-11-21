#!/usr/bin/env bash
# Installerar forever-ah-mcp i ett Debian 13-LXC. Kör som root från repots rot:
#   bash deploy/install-lxc.sh
# Kan köras igen för uppdatering: koden installeras om, env-filer och data behålls.
set -euo pipefail

APP=/opt/forever-ah
DATA=/var/lib/forever-ah
CONF=/etc/forever-ah
REPO="$(cd "$(dirname "$0")/.." && pwd)"

[[ $EUID -eq 0 ]] || { echo "Kör som root"; exit 1; }

apt-get update -qq
apt-get install -y -qq python3 python3-venv sqlite3 ca-certificates curl >/dev/null

id forever-ah &>/dev/null || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin forever-ah
install -d -o root -g root -m 755 "$APP"
install -d -o forever-ah -g forever-ah -m 750 "$DATA"
install -d -o root -g forever-ah -m 750 "$CONF"

[[ -d "$APP/venv" ]] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q "$REPO"

# Env-filer skapas bara om de saknas, så hemligheter skrivs aldrig över
if [[ ! -f "$CONF/collector.env" ]]; then
  cat > "$CONF/collector.env" <<CFG
BLIZZARD_CLIENT_ID=
BLIZZARD_CLIENT_SECRET=
BLIZZARD_REGION=eu
BLIZZARD_LOCALE=en_GB
AH_HOUSES=horde
AH_DB_PATH=$DATA/ah.sqlite
AH_POLL_MINUTES=20
AH_HOURLY_RETENTION_DAYS=14
CFG
fi
if [[ ! -f "$CONF/mcp.env" ]]; then
  cat > "$CONF/mcp.env" <<CFG
AH_DB_PATH=$DATA/ah.sqlite
AH_DEFAULT_HOUSE=horde
AH_HOUSES=horde
MCP_HOST=0.0.0.0
MCP_PORT=8765
MCP_TOKEN=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
CFG
fi
chown root:forever-ah "$CONF"/*.env
chmod 640 "$CONF"/*.env

install -m 644 "$REPO/deploy/forever-ah-collector.service" /etc/systemd/system/
install -m 644 "$REPO/deploy/forever-ah-mcp.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable forever-ah-collector forever-ah-mcp >/dev/null

if grep -q '^BLIZZARD_CLIENT_ID=$' "$CONF/collector.env"; then
  echo
  echo "Klart, men nycklar saknas. Fyll i dem och starta:"
  echo "  nano $CONF/collector.env"
  echo "  systemctl start forever-ah-collector forever-ah-mcp"
else
  systemctl restart forever-ah-collector forever-ah-mcp
  echo "Tjänsterna är omstartade."
fi
echo "MCP-token: $(grep ^MCP_TOKEN= "$CONF/mcp.env" | cut -d= -f2)"
