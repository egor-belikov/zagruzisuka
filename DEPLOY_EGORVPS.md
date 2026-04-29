# Развёртывание zagruzisuka на egorvps (слабый VPS, рядом kinodolgoletie)

## Миграция с другого VPS (например testvps)

На **`egorvps` нет SSH-алиаса `testvps`** из твоего `~/.ssh/config`; проще гонять перенос **с ноутбука**, где есть ключи к обоим хостам:

```bash
chmod +x scripts/migrate_testvps_to_egorvps_from_laptop.sh
./scripts/migrate_testvps_to_egorvps_from_laptop.sh
# или: TESTVPS=user@test.host EGORVPS=user@egor.host ./scripts/...
```

Перед переносом создаётся **`/root/backups/zagruzisuka-migrate-<время>/`** с дампом Postgres, архивами томов и файлом **`ROLLBACK.txt`** (восстановить прежнее состояние на egorvps). После успешного запуска **остановите стек на источнике**, иначе два процесса с одним Telegram-ботом. Альтернатива, если на egorvps настроен DNS/SSH на источник: `bash scripts/migrate_from_testvps_to_egorvps.sh <хост>`.

## Лимиты в проекте

- Раньше в compose стояло **~27 GiB tmpfs** — это был **потолок RAM-диска для временных файлов** во время скачивания/обработки, а не «вес проекта» и не склад готовых роликов на диске.
- **tmpfs** для временных файлов yt-dlp: **~2 GiB** (`docker-compose.yml`, `shared-tmpfs`), в связке с лимитом размера файла.
- **Один файл** с yt-dlp: до **~1 GiB** по умолчанию (`worker`/`_merge_global_ytdl_opts`, `YTDLP_MAX_FILESIZE_BYTES` в `envs/worker.egorvps.env`). Снять лимит: `YTDLP_MAX_FILESIZE_BYTES=0`.
- **Длинные видео** (по умолчанию **≥ 600 с**): более низкое разрешение (~360p) — `YTDLP_LONG_VIDEO_SECONDS`, `YTDLP_LONG_VIDEO_FORMAT`; отключить: `YTDLP_LONG_VIDEO_SECONDS=0`.
- **Постоянные загрузки** (`/filestorage`): именованный том `zagruzisuka_filestorage` в `docker-compose.egorvps.yml` (не общий хостовый `/data/downloads` с другими проектами).
- **CPU/RAM**: в `docker-compose.egorvps.yml` заданы `cpu_shares` (ниже у воркера — при конкуренции ядра отдаются другим контейнерам), `mem_limit`, `cpus`.

Docker не задаёт жёсткую квоту **на диск** для named volume на overlay2; чтобы гарантировать потолок на хосте, создайте каталог на **XFS** с **project quota** или отдельный раздел и монтируйте его в том.

**Смена размера tmpfs:** если `shared-tmpfs` уже создан, Docker может не обновить `driver_opts` до удаления тома. На вопрос compose *«Volume … doesn't match configuration. Recreate?»* можно ответить **`y`** (это только временные файлы загрузок). Без интерактива: остановить стек или сервисы **`yt_worker`** и **`yt_bot`**, выполнить `docker volume rm zagruzisuka_shared-tmpfs`, затем `docker compose … up -d`. Имя тома совпадает с префиксом проекта (`name: zagruzisuka` в compose).

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

## VPN / Mihomo (российский IP сервера)

Сервис **`yt_proxy`** (`metacubex/mihomo`) читает [`proxy/mihomo.yaml`](proxy/mihomo.yaml). **Исходящий VPN** по-прежнему задаётся своей подпиской в `proxy-providers` (в репозитории — **wizard**, как было изначально). **Списки для правил** (что отправлять в прокси при блокировках из РФ) подтягиваются из [**itdoginfo/allow-domains**](https://github.com/itdoginfo/allow-domains) в `rule-providers`. Описание сценариев «чёрный/белый список» у оператора и подбор **публичных** подписок — справочно в [**igareck/vpn-configs-for-russia**](https://github.com/igareck/vpn-configs-for-russia); эти URL в проект в качестве нод **не подмешиваются**.

После правки `mihomo.yaml`: `docker compose -f docker-compose.yml -f docker-compose.egorvps.yml up -d yt_proxy`. **`YTDLP_PROXY`** / **`TELEGRAM_SOCKS5_PROXY`** — на `mixed-port` из YAML (по умолчанию **10808**).

## Удаление после отправки в Telegram

После успешной (или неуспешной) обработки сценария бот в `SuccessDownloadHandler._cleanup()` вызывает `remove_dir(self._body.media.root_path)` — каталог задачи с исходным видео и временными файлами **удаляется** в `finally` после `await` загрузки в Telegram (`upload_task` дожидается завершения). **Постоянных копий медиа на диске сервера нет**: воркер не копирует файлы в `STORAGE_PATH`; из бота в очередь всегда уходит `save_to_storage=False`.
