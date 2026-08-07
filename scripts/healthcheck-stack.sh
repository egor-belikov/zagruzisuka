#!/usr/bin/env bash
# Хостовый watchdog: прокси, API, зависший бот. Запускать из cron каждые 5 минут.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.egorvps.yml)
PROXY_CONTAINER="zagruzisuka_proxy"
BOT_CONTAINER="zagruzisuka_bot"
API_CONTAINER="zagruzisuka_api"
LOG_TAG="zagruzisuka-healthcheck"
STUCK_RESTART_MINUTES=15

log() { logger -t "$LOG_TAG" "$*"; printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Полный TLS+HTTP запрос, а не только открыть-закрыть сокет: mihomo отвечает на
# SOCKS-хендшейк ДО того, как дозвонится до выходной ноды, поэтому старая проверка
# проходила даже с мёртвой нодой — так простой 2026-08 и остался незамеченным.
telegram_via_proxy_ok() {
  docker exec "$BOT_CONTAINER" python -c "
import socket, ssl, sys, socks
socks.set_default_proxy(socks.SOCKS5, 'yt_proxy', 10808, rdns=True)
socket.socket = socks.socksocket
s = socket.create_connection(('api.telegram.org', 443), 15)
s.settimeout(15)
c = ssl.create_default_context().wrap_socket(s, server_hostname='api.telegram.org')
c.sendall(b'HEAD / HTTP/1.1\r\nHost: api.telegram.org\r\nConnection: close\r\n\r\n')
resp = c.recv(64)
c.close()
sys.exit(0 if resp.startswith(b'HTTP/') else 1)
" >/dev/null 2>&1
}

api_ok() {
  curl -sf --max-time 5 http://127.0.0.1:1984/status >/dev/null 2>&1
}

bot_stuck_in_restart() {
  local count
  count="$(docker logs "$BOT_CONTAINER" --since "${STUCK_RESTART_MINUTES}m" 2>&1 \
    | grep -c 'Session.restart()' || true)"
  [[ "$count" -ge 5 ]] && return 0
  local last_activity
  last_activity="$(docker logs "$BOT_CONTAINER" 2>&1 \
    | grep -v DbCleanup \
    | grep -E 'on_message|VideoUploadTask|Starting \"|Connection lost' \
    | tail -1 || true)"
  if [[ -z "$last_activity" ]]; then
    return 1
  fi
  if docker logs "$BOT_CONTAINER" --since "${STUCK_RESTART_MINUTES}m" 2>&1 \
    | grep -q 'Connection lost'; then
    return 0
  fi
  return 1
}

api_child_death_storm() {
  local count
  count="$(docker logs "$API_CONTAINER" --since 5m 2>&1 | grep -c 'Child process.*died' || true)"
  [[ "$count" -ge 10 ]]
}

log "healthcheck start"

if ! telegram_via_proxy_ok; then
  log "proxy/Telegram failed — refresh wizard subscription"
  bash "$ROOT/scripts/refresh-wizard-proxy.sh" || true
fi

if api_child_death_storm || ! api_ok; then
  log "API unhealthy — restarting yt_api (API_WORKERS=1 on egorvps)"
  "${COMPOSE[@]}" up -d yt_api
  sleep 8
  if ! api_ok; then
    log "API still down after restart"
    docker restart "$API_CONTAINER" || true
  fi
fi

if bot_stuck_in_restart; then
  log "bot stuck (Session.restart / connection lost) — restarting yt_bot"
  docker restart "$BOT_CONTAINER" || true
fi

log "healthcheck done"
