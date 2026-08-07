#!/usr/bin/env bash
# Обновить подписку WizardVPN в Mihomo и проверить доступ к Telegram через прокси.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.egorvps.yml)
PROXY_CONTAINER="zagruzisuka_proxy"
BOT_CONTAINER="zagruzisuka_bot"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# URL подписки читаем из mihomo.yaml, чтобы не держать вторую копию секрета.
SUB_URL="$(sed -nE 's#^[[:space:]]*url:[[:space:]]*"?(https://sub\.[^"[:space:]]+)"?.*#\1#p' \
  proxy/mihomo.yaml | head -1)"
if [[ -z "$SUB_URL" ]]; then
  log "ERROR: не нашёл url подписки (proxy-providers) в proxy/mihomo.yaml"
  exit 1
fi

log "Checking Wizard subscription URL"
http_code="$(curl -sS -o /tmp/wizard-sub.txt -w '%{http_code}' "$SUB_URL" || true)"
if [[ "$http_code" != "200" ]]; then
  log "ERROR: subscription HTTP $http_code"
  exit 1
fi
sub_size="$(wc -c </tmp/wizard-sub.txt | tr -d ' ')"
if [[ "$sub_size" -lt 100 ]]; then
  log "ERROR: subscription too small ($sub_size bytes)"
  exit 1
fi
log "Subscription OK ($sub_size bytes, 2 nodes expected after decode)"

log "Refreshing Mihomo provider"
"${COMPOSE[@]}" up -d yt_proxy
sleep 3
docker exec "$PROXY_CONTAINER" wget -qO- --method=PUT \
  'http://127.0.0.1:9090/providers/proxies/wizard' >/dev/null 2>&1 \
  || docker restart "$PROXY_CONTAINER"

sleep 5
if ! docker exec "$BOT_CONTAINER" python -c "
import socket, socks
socks.set_default_proxy(socks.SOCKS5, 'yt_proxy', 10808, rdns=True)
socket.socket = socks.socksocket
s = socket.create_connection(('api.telegram.org', 443), 15)
s.close()
print('telegram-ok')
" 2>/dev/null | grep -q telegram-ok; then
  log "WARN: Telegram via proxy failed after refresh, restarting proxy+bot"
  docker restart "$PROXY_CONTAINER"
  sleep 5
  docker restart "$BOT_CONTAINER"
  exit 1
fi

log "Telegram via proxy: OK"
