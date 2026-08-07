#!/usr/bin/env bash
# Установить cron для scripts/healthcheck-stack.sh (каждые 5 минут).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CRON_LINE="*/5 * * * * cd $ROOT && bash $ROOT/scripts/healthcheck-stack.sh >> /var/log/zagruzisuka-healthcheck.log 2>&1"

chmod +x "$ROOT/scripts/healthcheck-stack.sh" "$ROOT/scripts/refresh-wizard-proxy.sh"

if crontab -l 2>/dev/null | grep -Fq 'healthcheck-stack.sh'; then
  echo "Cron entry already present"
else
  (crontab -l 2>/dev/null || true; echo "$CRON_LINE") | crontab -
  echo "Installed: $CRON_LINE"
fi
