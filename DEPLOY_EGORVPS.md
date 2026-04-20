# Развёртывание zagruzisuka на egorvps (слабый VPS, рядом kinodolgoletie)

## Лимиты в проекте

- **tmpfs** для временных файлов yt-dlp: **10 GiB** (`docker-compose.yml`, `shared-tmpfs`).
- **Один файл** с yt-dlp: до **~9 GiB** (`envs/worker.egorvps.env`, `YTDLP_MAX_FILESIZE_BYTES`).
- **Постоянные загрузки** (`/filestorage`): именованный том `zagruzisuka_filestorage` в `docker-compose.egorvps.yml` (не общий хостовый `/data/downloads` с другими проектами).
- **CPU/RAM**: в `docker-compose.egorvps.yml` заданы `cpu_shares` (ниже у воркера — при конкуренции ядра отдаются другим контейнерам), `mem_limit`, `cpus`.

Docker не задаёт жёсткую квоту **10 GiB на диск** для named volume на overlay2; чтобы гарантировать потолок на хосте, создайте каталог на **XFS** с **project quota** или отдельный раздел и монтируйте его в том.

## Сборка без перегруза CPU/диска

```bash
chmod +x scripts/build-low-resource.sh
./scripts/build-low-resource.sh
docker compose -f docker-compose.yml -f docker-compose.egorvps.yml up -d
```

При необходимости ещё сильнее ограничить параллелизм BuildKit: `export BUILDKIT_MAX_PARALLELISM=1` (уже по умолчанию в скрипте).

## Приоритет сети и других стеков

У Docker **нет** встроенного «приоритета сети» между разными compose-проектами. Имеет смысл на хосте:

1. Держать **kinodolgoletie** и прочие критичные сервисы **без** искусственно заниженных `cpu_shares` (по умолчанию 1024).
2. Для zagruzisuka использовать только merge-файл `docker-compose.egorvps.yml` (заниженные shares и лимиты уже в нём).
3. При необходимости — **tc** / **iptables** на интерфейсе egress для IP контейнеров zagruzisuka (отдельная настройка ОС).

## Том Postgres

Как и раньше: внешний том `yt_pgdata` (`docker-compose.yml`). На новом сервере: `docker volume create yt_pgdata` или перенос данных.
