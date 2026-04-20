# Развёртывание zagruzisuka на egorvps (слабый VPS, рядом kinodolgoletie)

## Лимиты в проекте

- Раньше в compose стояло **~27 GiB tmpfs** — это был **потолок RAM-диска для временных файлов** во время скачивания/обработки, а не «вес проекта» и не склад готовых роликов на диске.
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

При необходимости ещё сильнее ограничить параллелизм BuildKit: `export BUILDKIT_MAX_PARALLELISM=1` (уже по умолчанию в скрипте). В Docker Compose v5 у `docker compose build` нет флага `--parallel`; не используйте его (иначе `1` может воспринять как имя сервиса).

## Приоритет сети и других стеков

У Docker **нет** встроенного «приоритета сети» между разными compose-проектами. Имеет смысл на хосте:

1. Держать **kinodolgoletie** и прочие критичные сервисы **без** искусственно заниженных `cpu_shares` (по умолчанию 1024).
2. Для zagruzisuka использовать только merge-файл `docker-compose.egorvps.yml` (заниженные shares и лимиты уже в нём).
3. При необходимости — **tc** / **iptables** на интерфейсе egress для IP контейнеров zagruzisuka (отдельная настройка ОС).

## Том Postgres

Как и раньше: внешний том `yt_pgdata` (`docker-compose.yml`). На новом сервере: `docker volume create yt_pgdata` или перенос данных.

## Удаление после отправки в Telegram

После успешной (или неуспешной) обработки сценария бот в `SuccessDownloadHandler._cleanup()` вызывает `remove_dir(self._body.media.root_path)` — каталог задачи с исходным видео и временными файлами **удаляется** в `finally` после `await` загрузки в Telegram (`upload_task` дожидается завершения). **Постоянных копий медиа на диске сервера нет**: воркер не копирует файлы в `STORAGE_PATH`; из бота в очередь всегда уходит `save_to_storage=False`.
