#!/usr/bin/env bash
# Перенос zagruzisuka с TESTVPS на этот хост (egorvps), с бэкапом и инструкцией отката.
# Запускать НА ЦЕЛЕВОМ СЕРВЕРЕ (egorvps), от root:
#   bash scripts/migrate_from_testvps_to_egorvps.sh testvps
#
# Перед этим: chmod +x scripts/build-low-resource.sh scripts/migrate_from_testvps_to_egorvps.sh
#
# Что делает:
#   1) Бэкап томов yt_pgdata и zagruzisuka_zagruzisuka_filestorage в BACKUP_DIR
#   2) docker compose down
#   3) Очистка данных Postgres в yt_pgdata (том не удаляем — external)
#   4) pg_dump -Fc базы yt с источника → pg_restore (без дублирования ролей кластера)
#   5) rsync кода и env с источника (без затирать .git опционально)
#   6) Сборка и up через docker-compose + docker-compose.egorvps.yml
#
# Откат (если egorvps «поплыл»): см. ROLLBACK.txt внутри BACKUP_DIR.
#
set -euo pipefail

SRC_HOST="${1:-testvps}"
SRC_DIR="/root/my_projects/zagruzisuka"
DST_DIR="/root/my_projects/zagruzisuka"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.egorvps.yml"
TS="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="/root/backups/zagruzisuka-migrate-${TS}"
PG_MOUNT="$(docker volume inspect yt_pgdata --format '{{.Mountpoint}}')"
FS_VOL="zagruzisuka_zagruzisuka_filestorage"

mkdir -p "$BACKUP_DIR"

rollback_txt() {
  {
    echo "Откат миграции zagruzisuka (${TS})"
    echo "Архивы: ${BACKUP_DIR}/yt_pgdata_before.tgz и filestorage_before.tgz"
    echo ""
    echo "cd ${DST_DIR} && ${COMPOSE} down --remove-orphans"
    echo "sudo rm -rf \$(docker volume inspect yt_pgdata --format '{{.Mountpoint}}')/*"
    echo "sudo tar xzf ${BACKUP_DIR}/yt_pgdata_before.tgz -C \$(docker volume inspect yt_pgdata --format '{{.Mountpoint}}')"
    echo "sudo rm -rf \$(docker volume inspect ${FS_VOL} --format '{{.Mountpoint}}')/*"
    echo "sudo tar xzf ${BACKUP_DIR}/filestorage_before.tgz -C \$(docker volume inspect ${FS_VOL} --format '{{.Mountpoint}}')"
    echo "cd ${DST_DIR} && ${COMPOSE} up -d"
    echo ""
    echo "Дамп БД: ${BACKUP_DIR}/pg_yt_from_${SRC_HOST}.dump"
  } >"$BACKUP_DIR/ROLLBACK.txt"
}

echo "[migrate] backup dir: $BACKUP_DIR"
rollback_txt

echo "[migrate] archiving current volumes → $BACKUP_DIR"
sudo tar czf "$BACKUP_DIR/yt_pgdata_before.tgz" -C "$PG_MOUNT" .
FS_MOUNT="$(docker volume inspect "$FS_VOL" --format '{{.Mountpoint}}')"
sudo tar czf "$BACKUP_DIR/filestorage_before.tgz" -C "$FS_MOUNT" .

echo "[migrate] pulling DB dump (database yt, custom format) from $SRC_HOST"
ssh -o BatchMode=yes "$SRC_HOST" "docker exec zagruzisuka_postgres pg_dump -U yt -Fc yt" \
  >"$BACKUP_DIR/pg_yt_from_${SRC_HOST}.dump"

echo "[migrate] rsync project from $SRC_HOST (excludes .git to keep local history optional)"
mkdir -p "$DST_DIR"
rsync -avz --delete \
  --exclude '.git' \
  --exclude 'app_bot/.venv' \
  --exclude '**/__pycache__' \
  --exclude '*.pyc' \
  -e ssh \
  "root@${SRC_HOST}:${SRC_DIR}/" "${DST_DIR}/"

echo "[migrate] compose down"
cd "$DST_DIR"
$COMPOSE down --remove-orphans 2>/dev/null || true

echo "[migrate] wipe postgres volume data (volume yt_pgdata kept)"
sudo rm -rf "${PG_MOUNT:?}/"*

echo "[migrate] start postgres only"
$COMPOSE up -d yt_postgres
for i in $(seq 1 30); do
  if docker exec zagruzisuka_postgres pg_isready -U yt -d postgres >/dev/null 2>&1; then
    echo "[migrate] postgres ready"
    break
  fi
  sleep 1
  if [ "$i" -eq 30 ]; then
    echo "[migrate] FATAL: postgres not ready"; exit 1
  fi
done

echo "[migrate] restore dump into database yt"
set +e
docker exec -i zagruzisuka_postgres pg_restore -U yt -d yt --clean --if-exists --no-owner \
  <"$BACKUP_DIR/pg_yt_from_${SRC_HOST}.dump"
rv=$?
set -e
# pg_restore: 0 ok, 1 ok with warnings
if [ "$rv" -ne 0 ] && [ "$rv" -ne 1 ]; then
  echo "[migrate] FATAL: pg_restore exit $rv"; exit "$rv"
fi

echo "[migrate] low-resource build + full stack"
chmod +x scripts/build-low-resource.sh
./scripts/build-low-resource.sh
$COMPOSE up -d

echo "[migrate] done. Verify: $COMPOSE ps && docker logs zagruzisuka_bot --tail 50"
echo "[migrate] Then stop source to avoid duplicate Telegram bot:"
echo "    ssh $SRC_HOST 'cd $SRC_DIR && docker compose -f docker-compose.yml -f docker-compose.egorvps.yml down'"
echo "[migrate] Rollback: cat $BACKUP_DIR/ROLLBACK.txt"
