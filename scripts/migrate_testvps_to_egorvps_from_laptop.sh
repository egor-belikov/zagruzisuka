#!/usr/bin/env bash
# Запуск с машины, где есть SSH к обоим серверам (локальные Host testvps / egorvps из ~/.ssh/config).
# Синхронизирует код testvps → egorvps (tar по SSH), бэкапит тома на egorvps, переносит БД yt.
#
# Использование:
#   chmod +x scripts/migrate_testvps_to_egorvps_from_laptop.sh
#   ./scripts/migrate_testvps_to_egorvps_from_laptop.sh
#
# Переменные при необходимости:
#   TESTVPS=testvps EGORVPS=egorvps ./scripts/migrate_testvps_to_egorvps_from_laptop.sh
#
set -euo pipefail

TESTVPS="${TESTVPS:-testvps}"
EGORVPS="${EGORVPS:-egorvps}"
SRC_DIR="/root/my_projects/zagruzisuka"
DST_DIR="/root/my_projects/zagruzisuka"
TS="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="/root/backups/zagruzisuka-migrate-${TS}"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.egorvps.yml"

echo "[orchestrator] backup on $EGORVPS → $BACKUP_DIR"
ssh -o BatchMode=yes "$EGORVPS" bash -s <<EOF
set -euo pipefail
mkdir -p "$BACKUP_DIR"
PG_MOUNT="\$(docker volume inspect yt_pgdata --format '{{.Mountpoint}}')"
FS_VOL="zagruzisuka_zagruzisuka_filestorage"
FS_MOUNT="\$(docker volume inspect "\$FS_VOL" --format '{{.Mountpoint}}')"
sudo tar czf "$BACKUP_DIR/yt_pgdata_before.tgz" -C "\$PG_MOUNT" .
sudo tar czf "$BACKUP_DIR/filestorage_before.tgz" -C "\$FS_MOUNT" .
{
  echo "Откат миграции zagruzisuka (${TS})"
  echo "Архивы: $BACKUP_DIR/yt_pgdata_before.tgz и filestorage_before.tgz"
  echo ""
  echo "cd $DST_DIR && $COMPOSE down --remove-orphans"
  echo "sudo rm -rf \$(docker volume inspect yt_pgdata --format '{{.Mountpoint}}')/*"
  echo "sudo tar xzf $BACKUP_DIR/yt_pgdata_before.tgz -C \$(docker volume inspect yt_pgdata --format '{{.Mountpoint}}')"
  echo "sudo rm -rf \$(docker volume inspect zagruzisuka_zagruzisuka_filestorage --format '{{.Mountpoint}}')/*"
  echo "sudo tar xzf $BACKUP_DIR/filestorage_before.tgz -C \$(docker volume inspect zagruzisuka_zagruzisuka_filestorage --format '{{.Mountpoint}}')"
  echo "cd $DST_DIR && $COMPOSE up -d"
} >"$BACKUP_DIR/ROLLBACK.txt"
echo "Rollback: cat $BACKUP_DIR/ROLLBACK.txt"
EOF

echo "[orchestrator] dump database yt from $TESTVPS"
ssh -o BatchMode=yes "$TESTVPS" "docker exec zagruzisuka_postgres pg_dump -U yt -Fc yt" \
  >"/tmp/pg_yt_${TS}.dump"

echo "[orchestrator] copy dump to $EGORVPS"
scp -o BatchMode=yes "/tmp/pg_yt_${TS}.dump" "${EGORVPS}:${BACKUP_DIR}/pg_yt.dump"
rm -f "/tmp/pg_yt_${TS}.dump"

echo "[orchestrator] sync code testvps → egorvps (tar over SSH)"
ssh -o BatchMode=yes "$TESTVPS" "cd /root/my_projects && tar czf - \
  --exclude='zagruzisuka/.git' \
  --exclude='zagruzisuka/app_bot/.venv' \
  --exclude='zagruzisuka/**/__pycache__' \
  zagruzisuka" \
  | ssh -o BatchMode=yes "$EGORVPS" "mkdir -p ${DST_DIR%/*} && tar xzf - -C ${DST_DIR%/*}"

echo "[orchestrator] run DB restore + build + up on $EGORVPS"
scp -o BatchMode=yes "$(dirname "$0")/migrate_from_testvps_to_egorvps.sh" "${EGORVPS}:${DST_DIR}/scripts/"
ssh -o BatchMode=yes "$EGORVPS" bash -s <<EOF
set -euo pipefail
cd $DST_DIR
$COMPOSE down --remove-orphans 2>/dev/null || true
PG_MOUNT="\$(docker volume inspect yt_pgdata --format '{{.Mountpoint}}')"
sudo rm -rf "\${PG_MOUNT:?}/"*
$COMPOSE up -d yt_postgres
for i in \$(seq 1 30); do
  docker exec zagruzisuka_postgres pg_isready -U yt -d postgres >/dev/null 2>&1 && break
  sleep 1
done
set +e
docker exec -i zagruzisuka_postgres pg_restore -U yt -d yt --clean --if-exists --no-owner <"$BACKUP_DIR/pg_yt.dump"
rv=\$?
set -e
[ "\$rv" -eq 0 ] || [ "\$rv" -eq 1 ] || exit "\$rv"
chmod +x scripts/build-low-resource.sh
./scripts/build-low-resource.sh
$COMPOSE up -d
$COMPOSE ps
EOF

echo "[orchestrator] done. Проверьте логи: ssh $EGORVPS 'docker logs zagruzisuka_bot --tail 80'"
echo "[orchestrator] Затем остановите источник (один токен бота):"
echo "  ssh $TESTVPS 'cd $SRC_DIR && docker compose -f docker-compose.yml -f docker-compose.egorvps.yml down'"
