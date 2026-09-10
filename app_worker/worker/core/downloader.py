import glob
import logging
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar
from urllib.parse import urlsplit

import ssl
import urllib.error
import urllib.request

import yt_dlp
from yt_dlp.utils import DownloadError
from yt_shared.enums import DownMediaType
from yt_shared.schemas.media import Audio, DownMedia, InbMediaPayload, Video
from yt_shared.utils.common import format_bytes, gen_random_str
from yt_shared.utils.file import file_size, list_files_human, remove_dir

from worker.bunkr_resolver import resolve_if_bunkr
from worker.sports_ru_resolver import resolve_if_sports_ru
from worker.veed_resolver import resolve_if_veed
from worker.core.config import settings
from worker.core.download_errors import format_download_failure
from worker.core.exceptions import MediaDownloaderError
from ytdl_opts.per_host._base import AbstractHostConfig

try:
    from ytdl_opts.user import FINAL_AUDIO_FORMAT, FINAL_THUMBNAIL_FORMAT
except ImportError:
    from ytdl_opts.default import FINAL_AUDIO_FORMAT, FINAL_THUMBNAIL_FORMAT

_DEFAULT_MAX_FILESIZE = (
    1024 * 1024 * 1024
)  # 1 GiB; env YTDLP_MAX_FILESIZE_BYTES или 0 = без лимита
_DEFAULT_SOCKET_TIMEOUT = 120
_DEFAULT_RETRIES = 15
_DEFAULT_FRAGMENT_RETRIES = 50
# `retries`/`fragment_retries` покрывают только фазу самой закачки файла.
# Получение метаданных (webpage/API запрос экстрактора, напр. Instagram)
# retry'ится отдельным параметром yt-dlp — `extractor_retries` (дефолт в
# самом yt-dlp — всего 3). При просадках на VPN-прокси (Mihomo/WizardVPN,
# см. proxy/mihomo.yaml) 3 попыток не хватает и extract_info тихо
# возвращает None вместо поднятия ошибки — см. "yt-dlp не вернул метаданные".
_DEFAULT_EXTRACTOR_RETRIES = 10
_DEFAULT_CONCURRENT_FRAGMENTS = 1
# YouTube душит длинные одиночные соединения (~650 КБ/с при канале ноды в 47 Мбит/с).
# Range-запросы кусками сбрасывают throttling: на замерах 2026-08 те же файлы шли
# в 1.5–5 раз быстрее. Параллелить фрагменты вместо этого нельзя — из-за прокси
# возвращаются «fragment not found» и FileNotFoundError на .part-FragN при merge,
# поэтому _DEFAULT_CONCURRENT_FRAGMENTS остаётся 1.
_DEFAULT_HTTP_CHUNK_SIZE = 10 * 1024 * 1024  # 0 = отключить чанки
_STREAMFF_HOSTS = {
    'streamff.com',
    'www.streamff.com',
    'streamff.link',
    'www.streamff.link',
    'streamff.ink',
    'www.streamff.ink',
}
_STREAMFF_PATH_RE = re.compile(r'^/v/(?P<share_id>[A-Za-z0-9_-]+)(?:/)?$')
_STREAMFF_CDN_MEDIA_TPL = 'https://cdn.streamff.one/{share_id}.mp4'


_DIRECT_YTDLP_HOST_SUFFIXES: frozenset[str] = frozenset(
    (
        'pornhub.com',
        'pornhubpremium.com',
        'phncdn.com',
        'phprcdn.com',
        'rutube.ru',
    )
)
# YouTube убран из этого списка 2026-09-10: прямое TLS-соединение с youtube.com
# с этого VPS зависает на handshake (RU-блокировка), а через Mihomo (proxy/mihomo.yaml)
# youtube.com уже матчится RuleSet(russia-inside-domain) → PROXY и работает нормально.


def _is_rutube_url(url: str) -> bool:
    host = (urlsplit(url).hostname or '').lower()
    return host == 'rutube.ru' or host.endswith('.rutube.ru')


def _format_duration_ru(seconds: float | None) -> str:
    if seconds is None or seconds != seconds or seconds < 0:
        return '—'
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    parts: list[str] = []
    if h:
        parts.append(f'{h} ч')
    if m:
        parts.append(f'{m} м')
    if sec and not h:
        parts.append(f'{sec} с')
    return ' '.join(parts) if parts else f'{sec} с'


def _estimate_download_eta_hint(filesize: int | None, duration: float | None) -> str:
    if filesize and filesize > 0:
        est_bytes = filesize
    elif duration and duration > 0:
        est_bytes = int(duration * 45_000)
    else:
        return 'время скачивания зависит от CDN'
    fast = max(60.0, est_bytes / (800 * 1024))
    slow = max(fast, est_bytes / (120 * 1024))
    return (
        f'ожидаемое скачивание: {_format_duration_ru(fast)}'
        f' – {_format_duration_ru(slow)}'
    )


def _ytdlp_should_bypass_proxy(url: str) -> bool:
    """Sites reachable from RU VPS; Mihomo proxy returns broken interstitial pages."""
    host = (urlsplit(url).hostname or '').lower()
    if not host:
        return False
    return any(
        host == suffix or host.endswith('.' + suffix)
        for suffix in _DIRECT_YTDLP_HOST_SUFFIXES
    )


def _maybe_strip_proxy_for_direct_hosts(opts: dict, *urls: str) -> None:
    if any(_ytdlp_should_bypass_proxy(u) for u in urls if u):
        opts.pop('proxy', None)


def _first_env_proxy() -> str | None:
    for key in (
        'YTDLP_PROXY',
        'ALL_PROXY',
        'HTTPS_PROXY',
        'https_proxy',
        'HTTP_PROXY',
        'http_proxy',
    ):
        raw = (os.environ.get(key) or '').strip()
        if raw:
            return raw
    return None


def _long_video_threshold_seconds() -> float | None:
    """Порог длительности (сек): дольше — качаем в низком качестве. Отключить: 0/off/never."""
    raw = (os.environ.get('YTDLP_LONG_VIDEO_SECONDS') or '').strip().lower()
    if raw in ('0', 'off', 'never', 'false', '-1'):
        return None
    if not raw:
        return 600.0
    try:
        t = float(raw)
    except ValueError:
        return 600.0
    return t if t > 0 else None


def _long_video_format_string() -> str:
    custom = (os.environ.get('YTDLP_LONG_VIDEO_FORMAT') or '').strip()
    if custom:
        return custom
    return (
        'bestvideo*[height<=360][fps<=30]+bestaudio/bestaudio+bestvideo*[height<=360]/'
        'best[height<=360]/worst'
    )


def _env_positive_int(key: str, default: int) -> int:
    raw = (os.environ.get(key) or '').strip()
    if not raw:
        return default
    try:
        n = int(raw, 10)
    except ValueError:
        return default
    return n if n > 0 else default


def _merge_global_ytdl_opts(opts: dict) -> dict:
    out = dict(opts)
    # Пробелы/юникод в имени файла ломают HLS-склейку (fragment.part-FragN) на tmpfs — см. FileNotFoundError в логах.
    if 'restrictfilenames' not in out:
        out['restrictfilenames'] = True
    if out.get('proxy') in (None, ''):
        p = _first_env_proxy()
        if p:
            out['proxy'] = p
    if out.get('max_filesize') in (None, 0):
        raw = (os.environ.get('YTDLP_MAX_FILESIZE_BYTES') or '').strip().lower()
        if raw in ('0', 'none', 'unlimited'):
            pass
        elif raw:
            try:
                out['max_filesize'] = int(raw, 10)
            except ValueError:
                out['max_filesize'] = _DEFAULT_MAX_FILESIZE
        else:
            out['max_filesize'] = _DEFAULT_MAX_FILESIZE
    # Длинный HLS + прокси: SSL/read timeout и параллельная закачка фрагментов дают
    # «fragment not found; Skipping» и затем FileNotFoundError на .part-FragN при merge.
    if 'socket_timeout' not in out:
        out['socket_timeout'] = _env_positive_int(
            'YTDLP_SOCKET_TIMEOUT', _DEFAULT_SOCKET_TIMEOUT
        )
    if 'retries' not in out:
        out['retries'] = _env_positive_int('YTDLP_RETRIES', _DEFAULT_RETRIES)
    if 'extractor_retries' not in out:
        out['extractor_retries'] = _env_positive_int(
            'YTDLP_EXTRACTOR_RETRIES', _DEFAULT_EXTRACTOR_RETRIES
        )
    if 'fragment_retries' not in out:
        out['fragment_retries'] = _env_positive_int(
            'YTDLP_FRAGMENT_RETRIES', _DEFAULT_FRAGMENT_RETRIES
        )
    if 'concurrent_fragment_downloads' not in out:
        out['concurrent_fragment_downloads'] = _env_positive_int(
            'YTDLP_CONCURRENT_FRAGMENTS', _DEFAULT_CONCURRENT_FRAGMENTS
        )
    if 'http_chunk_size' not in out:
        # Не через _env_positive_int: там 0 откатывается к дефолту, а здесь
        # 0 должен именно выключать чанки (ключ не попадает в opts).
        raw = (os.environ.get('YTDLP_HTTP_CHUNK_SIZE') or '').strip()
        try:
            chunk = int(raw, 10) if raw else _DEFAULT_HTTP_CHUNK_SIZE
        except ValueError:
            chunk = _DEFAULT_HTTP_CHUNK_SIZE
        if chunk > 0:
            out['http_chunk_size'] = chunk
    return out


def _resolve_streamff_direct_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.netloc.lower() not in _STREAMFF_HOSTS:
        return url

    match = _STREAMFF_PATH_RE.match(parsed.path or '')
    if match is None:
        return url

    return _STREAMFF_CDN_MEDIA_TPL.format(share_id=match.group('share_id'))




def _bunkr_safe_filename(title: str | None, url: str) -> str:
    base = (title or urlsplit(url).path.rsplit("/", 1)[-1] or "bunkr_media").strip()
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
    if not base.lower().endswith(".mp4"):
        base += ".mp4"
    return base[:200]


def _bunkr_cdn_proxy() -> str | None:
    raw = (os.environ.get('BUNKR_CDN_PROXY') or '').strip()
    return raw or None


def _bunkr_download_proxy_chain() -> list[str | None]:
    chain: list[str | None] = [None]
    for candidate in (
        (os.environ.get('BUNKR_CDN_PROXY') or '').strip(),
        _first_env_proxy() or '',
    ):
        if candidate and candidate not in chain:
            chain.append(candidate)
    return chain


def _http_download_to_file(
    source_url: str,
    dest_path: Path,
    headers: dict[str, str],
    proxy: str | None,
) -> int:
    handlers: list = []
    if proxy:
        handlers.append(
            urllib.request.ProxyHandler(
                {"http": proxy, "https": proxy, "socks5": proxy, "socks": proxy}
            )
        )
    handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    opener = urllib.request.build_opener(*handlers)
    req_headers = dict(headers)
    req_headers.setdefault("Accept", "*/*")
    req = urllib.request.Request(source_url, headers=req_headers, method="GET")
    with opener.open(req, timeout=300) as resp:
        total = 0
        with dest_path.open("wb") as out:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
                total += len(chunk)
    return total

class MediaDownloader:
    _PLAYLIST_TYPE = 'playlist'
    _DESTINATION_TMP_DIR_NAME_LEN = 4
    _KEEP_VIDEO_OPTION = '--keep-video'

    _EXT_TO_NAME: ClassVar[dict[str, str]] = {
        FINAL_AUDIO_FORMAT: 'audio',
        FINAL_THUMBNAIL_FORMAT: 'thumbnail',
    }

    def __init__(self) -> None:
        self._log = logging.getLogger(self.__class__.__name__)
        self._tmp_downloaded_dest_dir = (
            settings.TMP_DOWNLOAD_ROOT_PATH / settings.TMP_DOWNLOADED_DIR
        )

    def _probe_video_meta(self, opts: dict, resolved_url: str) -> dict | None:
        skip = frozenset(
            {
                'progress_hooks',
                'postprocessors',
                'forceprint',
                'print_to_file',
                'writesubtitles',
                'writeautomaticsub',
            }
        )
        probe_opts = {k: v for k, v in opts.items() if k not in skip}
        probe_opts['quiet'] = True
        probe_opts['no_warnings'] = True
        try:
            with yt_dlp.YoutubeDL(probe_opts) as ydl:
                info = ydl.extract_info(resolved_url, download=False)
        except Exception as err:
            self._log.warning('Duration probe failed for %s: %s', resolved_url, err)
            return None
        if not info:
            return None
        if info.get('_type') == 'playlist':
            entries = info.get('entries') or []
            if not entries:
                return None
            first = entries[0]
            if first is None:
                return None
            info = first if isinstance(first, dict) else {}
        duration = info.get('duration')
        filesize = info.get('filesize') or info.get('filesize_approx')
        meta: dict = {}
        if duration is not None:
            try:
                meta['duration'] = float(duration)
            except (TypeError, ValueError):
                pass
        if filesize is not None:
            try:
                meta['filesize'] = int(filesize)
            except (TypeError, ValueError):
                pass
        return meta or None

    def _maybe_apply_long_video_low_format(
        self, opts: dict, resolved_url: str
    ) -> tuple[dict | None, bool]:
        threshold = _long_video_threshold_seconds()
        meta = self._probe_video_meta(opts, resolved_url)
        if threshold is None or not meta:
            return meta, False
        duration = meta.get('duration')
        if duration is None or duration < threshold:
            return meta, False
        fmt = _long_video_format_string()
        opts['format'] = fmt
        self._log.info(
            'Long video (%.0fs >= %.0fs): using low format: %s',
            duration,
            threshold,
            fmt,
        )
        return meta, True

    def download(
        self,
        host_conf: AbstractHostConfig,
        media_payload: InbMediaPayload,
        progress_hook: Callable[[dict], None] | None = None,
        plan_hook: Callable[[dict], None] | None = None,
    ) -> DownMedia:
        try:
            return self._download(
                host_conf=host_conf,
                media_payload=media_payload,
                progress_hook=progress_hook,
                plan_hook=plan_hook,
            )
        except Exception:
            self._log.error('Failed to download %s', host_conf.url)
            raise


    def _download_bunkr_direct(
        self,
        *,
        bunker_res,
        url: str,
        curr_tmp_dir: Path,
        opts: dict,
        progress_hook: Callable[[dict], None] | None,
    ) -> dict:
        filename = _bunkr_safe_filename(bunker_res.page_title, bunker_res.direct_url)
        dest = curr_tmp_dir / filename
        if progress_hook:
            progress_hook({"status": "downloading", "_percent_str": "0%"})
        try:
            from worker.utils import get_cookies_opts_if_not_empty
            hdrs = dict(bunker_res.http_headers)
            ck = get_cookies_opts_if_not_empty()
            if ck:
                try:
                    import http.cookiejar
                    cj = http.cookiejar.MozillaCookieJar(ck[1])
                    cj.load(ignore_discard=True, ignore_expires=True)
                    hdrs['Cookie'] = '; '.join(f'{c.name}={c.value}' for c in cj)
                except Exception:
                    pass
            last_http_err: urllib.error.HTTPError | None = None
            size = 0
            for proxy in _bunkr_download_proxy_chain():
                try:
                    size = _http_download_to_file(
                        bunker_res.direct_url,
                        dest,
                        hdrs,
                        proxy,
                    )
                    last_http_err = None
                    break
                except urllib.error.HTTPError as err:
                    last_http_err = err
                    if err.code in (403, 429):
                        self._log.warning(
                            'Bunkr CDN HTTP %s via proxy=%s, trying next path',
                            err.code,
                            proxy or 'direct',
                        )
                        continue
                    raise
            if last_http_err is not None:
                raise last_http_err
        except urllib.error.HTTPError as err:
            raise MediaDownloaderError(
                format_download_failure(
                    reason=(
                        f"Bunkr CDN HTTP {err.code}: {err.reason}. "
                        "CDN prxp-b.cdn.cr блокирует IP датацентра (VPS и Wizard VPN). "
                        "Экспортируй cookies из браузера на ПК (где Bunkr открывается) "
                        "в /app/cookies/cookies.txt или укажи residential BUNKR_CDN_PROXY."
                    ),
                    url=url,
                    resolved_url=bunker_res.direct_url,
                )
            ) from err
        except Exception as err:
            raise MediaDownloaderError(
                format_download_failure(
                    reason=str(err),
                    url=url,
                    resolved_url=bunker_res.direct_url,
                )
            ) from err
        if progress_hook:
            progress_hook({"status": "finished"})
        title = bunker_res.page_title or filename
        return {
            "title": title,
            "ext": dest.suffix.lstrip(".") or "mp4",
            "_filename": str(dest),
            "requested_downloads": [{"filepath": str(dest), "_filename": str(dest), "ext": "mp4"}],
            "filesize": size,
        }

    def _download(
        self,
        host_conf: AbstractHostConfig,
        media_payload: InbMediaPayload,
        progress_hook: Callable[[dict], None] | None = None,
        plan_hook: Callable[[dict], None] | None = None,
    ) -> DownMedia:
        media_type = media_payload.download_media_type
        url = host_conf.url

        bunker_res = resolve_if_bunkr(url, self._log)
        veed_res = resolve_if_veed(url, self._log)
        sports_ru_res = resolve_if_sports_ru(url, self._log)
        resolved_url = url
        try:
            resolved_url = _resolve_streamff_direct_url(url)
        except Exception:
            self._log.warning('Failed to resolve streamff direct URL for %s', url)
        if bunker_res is not None:
            resolved_url = bunker_res.direct_url
            self._log.info('Bunkr page %s resolved to CDN URL', url)
        elif veed_res is not None:
            resolved_url = veed_res.direct_url
            self._log.info('VEED page %s resolved to CDN URL', url)
        elif sports_ru_res is not None:
            resolved_url = sports_ru_res.direct_url
            self._log.info('Sports.ru page %s resolved to HLS playlist URL', url)
        elif resolved_url != url:
            self._log.info('Resolved %s to direct URL %s', url, resolved_url)
        self._log.info('Downloading %s, media_type %s', url, media_type)
        tmp_down_path = settings.TMP_DOWNLOAD_ROOT_PATH / settings.TMP_DOWNLOAD_DIR
        with TemporaryDirectory(prefix='tmp_media_dir-', dir=tmp_down_path) as tmp_dir:
            curr_tmp_dir = tmp_down_path / tmp_dir

            ytdl_opts_model = host_conf.build_config(
                media_type=media_type, curr_tmp_dir=curr_tmp_dir
            )

            opts = _merge_global_ytdl_opts(dict(ytdl_opts_model.ytdl_opts))
            _maybe_strip_proxy_for_direct_hosts(opts, url, resolved_url)
            if bunker_res is not None:
                hdrs = dict(opts.get('http_headers') or {})
                hdrs.update(bunker_res.http_headers)
                opts['http_headers'] = hdrs
            elif veed_res is not None:
                hdrs = dict(opts.get('http_headers') or {})
                hdrs.update(veed_res.http_headers)
                opts['http_headers'] = hdrs
            elif sports_ru_res is not None:
                hdrs = dict(opts.get('http_headers') or {})
                hdrs.update(sports_ru_res.http_headers)
                opts['http_headers'] = hdrs

            hooks = list(opts.get('progress_hooks') or [])
            if progress_hook:
                hooks.append(progress_hook)
            opts['progress_hooks'] = hooks

            probe_meta: dict | None = None
            low_format = False
            if media_type in (DownMediaType.VIDEO, DownMediaType.AUDIO_VIDEO):
                probe_meta, low_format = self._maybe_apply_long_video_low_format(
                    opts, resolved_url
                )

            if plan_hook and _is_rutube_url(url):
                meta = probe_meta or {}
                duration = meta.get('duration')
                filesize = meta.get('filesize')
                try:
                    plan_hook(
                        {
                            'duration_seconds': duration,
                            'filesize': filesize,
                            'low_quality': low_format,
                            'quality_label': '360p' if low_format else 'обычное',
                            'no_proxy': not opts.get('proxy'),
                            'eta_hint': _estimate_download_eta_hint(filesize, duration),
                            'duration_label': _format_duration_ru(duration),
                        }
                    )
                except Exception:
                    self._log.debug('plan_hook failed', exc_info=True)

            if bunker_res is not None:
                self._log.info(
                    'Downloading Bunkr CDN "%s" to "%s"', resolved_url, curr_tmp_dir
                )
                meta = self._download_bunkr_direct(
                    bunker_res=bunker_res,
                    url=url,
                    curr_tmp_dir=curr_tmp_dir,
                    opts=opts,
                    progress_hook=progress_hook,
                )
                meta_sanitized = meta
            else:
                with yt_dlp.YoutubeDL(opts) as ytdl:
                    self._log.info('Downloading "%s" to "%s"', resolved_url, curr_tmp_dir)
                    self._log.info('Downloading with options: %s', opts)

                    try:
                        meta = ytdl.extract_info(resolved_url, download=True)
                    except DownloadError as err:
                        self._log.error(
                            'yt-dlp DownloadError for %s (resolved=%s): %s',
                            url,
                            resolved_url,
                            err,
                        )
                        raise MediaDownloaderError(
                            format_download_failure(
                                reason=str(err),
                                url=url,
                                resolved_url=resolved_url,
                            )
                        ) from err
                    if not meta:
                        err_msg = format_download_failure(
                            reason='yt-dlp не вернул метаданные (extract_info вернул пустой результат).',
                            url=url,
                            resolved_url=resolved_url,
                        )
                        self._log.error('%s Meta: %s', err_msg, meta)
                        raise MediaDownloaderError(err_msg)

                    if veed_res is not None and veed_res.page_title:
                        meta['title'] = veed_res.page_title
                    elif sports_ru_res is not None and sports_ru_res.page_title:
                        meta['title'] = sports_ru_res.page_title

                    meta_sanitized = ytdl.sanitize_info(meta)

            current_files = list(curr_tmp_dir.iterdir())
            if not current_files:
                err_msg = format_download_failure(
                    reason=(
                        'Загрузка завершилась без файлов в рабочей директории '
                        '(возможно, неверный URL или формат недоступен).'
                    ),
                    url=url,
                    resolved_url=resolved_url,
                )
                self._log.error(err_msg)
                raise MediaDownloaderError(err_msg)
            self._log.info('Finished downloading %s', url)
            self._log.debug('Downloaded "%s" meta: %s', url, meta_sanitized)
            self._log.info(
                'Content of "%s": %s', curr_tmp_dir, list_files_human(curr_tmp_dir)
            )

            destination_dir = self._tmp_downloaded_dest_dir / gen_random_str(
                length=self._DESTINATION_TMP_DIR_NAME_LEN
            )
            destination_dir.mkdir()

            audio, video = self._create_media_dtos(
                media_type=media_type,
                meta=meta,
                curr_tmp_dir=curr_tmp_dir,
                destination_dir=destination_dir,
                custom_video_filename=media_payload.custom_filename,
            )
            self._log.info(
                'Removing temporary download directory "%s" with leftover files %s',
                curr_tmp_dir,
                list_files_human(curr_tmp_dir),
            )

        return DownMedia(
            media_type=media_type,
            audio=audio,
            video=video,
            meta=meta_sanitized,
            root_path=destination_dir,
        )

    def _create_media_dtos(
        self,
        media_type: DownMediaType,
        meta: dict,
        curr_tmp_dir: str,
        destination_dir: str,
        custom_video_filename: str | None = None,
    ) -> tuple[Audio | None, Video | None]:
        def get_audio() -> Audio:
            return create_dto(self._create_audio_dto)

        def get_video() -> Video:
            return create_dto(self._create_video_dto)

        def create_dto(
            func: Callable[[dict, str, str, str | None], Audio | Video],
        ) -> Audio | Video:
            try:
                return func(meta, curr_tmp_dir, destination_dir, custom_video_filename)
            except Exception:
                remove_dir(destination_dir)
                raise

        match media_type:
            case DownMediaType.AUDIO:
                return get_audio(), None
            case DownMediaType.VIDEO:
                return None, get_video()
            case DownMediaType.AUDIO_VIDEO:
                return get_audio(), get_video()
            case _:
                raise RuntimeError(f'Unknown media type "{media_type}"')

    def _create_video_dto(
        self,
        meta: dict,
        curr_tmp_dir: Path,
        destination_dir: Path,
        custom_video_filename: str | None = None,
    ) -> Video:
        video_filename = self._get_video_filename(meta)
        video_filepath = curr_tmp_dir / video_filename

        if custom_video_filename:
            dest_path = destination_dir / custom_video_filename
        else:
            dest_path = destination_dir / video_filename

        self._log.info('Moving "%s" to "%s"', video_filepath, dest_path)
        shutil.move(video_filepath, dest_path)

        thumb_path: Path | None = None
        thumb_name = self._find_downloaded_file(
            root_path=curr_tmp_dir, extension=FINAL_THUMBNAIL_FORMAT
        )
        if thumb_name:
            _thumb_path = curr_tmp_dir / thumb_name
            shutil.move(_thumb_path, destination_dir)
            thumb_path = destination_dir / thumb_name

        duration, width, height = self._get_video_context(meta)
        return Video(
            title=meta['title'],
            original_filename=video_filename,
            custom_filename=custom_video_filename,
            duration=duration,
            width=width,
            height=height,
            directory_path=destination_dir,
            file_size=file_size(dest_path),
            thumb_path=thumb_path,
            thumb_name=thumb_name,
        )

    def _create_audio_dto(
        self,
        meta: dict,
        curr_tmp_dir: Path,
        destination_dir: Path,
        custom_video_filename: str | None = None,  # noqa: ARG002 # TODO: Make for audio.
    ) -> Audio:
        audio_filename = self._find_downloaded_file(
            root_path=curr_tmp_dir, extension=FINAL_AUDIO_FORMAT
        )
        audio_filepath = curr_tmp_dir / audio_filename
        self._log.info('Moving "%s" to "%s"', audio_filepath, destination_dir)
        shutil.move(audio_filepath, destination_dir)
        return Audio(
            title=meta['title'],
            original_filename=audio_filename,
            duration=None,
            directory_path=destination_dir,
            file_size=file_size(destination_dir / audio_filename),
        )

    def _find_downloaded_file(self, root_path: Path, extension: str) -> str | None:
        """Try to find downloaded audio or thumbnail file."""
        verbose_name = self._EXT_TO_NAME[extension]
        for file_name in glob.glob(f'*.{extension}', root_dir=root_path):  # noqa: PTH207
            self._log.info(
                'Found downloaded %s: "%s" [%s]',
                verbose_name,
                file_name,
                format_bytes(file_size(root_path / file_name)),
            )
            return file_name
        self._log.info('Downloaded %s not found in "%s"', verbose_name, root_path)
        return None

    def _get_video_context(
        self, meta: dict
    ) -> tuple[float | None, int | float | None, int | float | None]:
        if meta['_type'] == self._PLAYLIST_TYPE:
            if not len(meta['entries']):
                raise ValueError(
                    'Item said to be downloaded but no entries to process.'
                )
            entry: dict = meta['entries'][0]
            requested_video = self._get_requested_video(entry['requested_downloads'])
            return (
                self._to_float(entry.get('duration')),
                requested_video.get('width'),
                requested_video.get('height'),
            )
        requested_video = self._get_requested_video(meta['requested_downloads'])
        return (
            self._to_float(meta.get('duration')),
            requested_video.get('width'),
            requested_video.get('height'),
        )

    def _get_requested_video(self, requested_downloads: list[dict]) -> dict | None:
        for download_obj in requested_downloads:
            if download_obj.get('ext', '') != FINAL_AUDIO_FORMAT:
                # Attempt to handle yt-dlp glitch.
                download_obj['filepath'] = download_obj.get(
                    'filepath', download_obj.get('filename', download_obj['_filename'])
                )
                return download_obj

        # When video was converted to audio but video kept.
        for download_obj in requested_downloads:
            if download_obj['ext'] != download_obj['_filename'].rsplit('.', 1)[-1]:
                download_obj_copy = download_obj.copy()
                self._log.info(
                    'Replacing video path in meta "%s" with "%s"',
                    download_obj_copy['filepath'],
                    download_obj_copy['_filename'],
                )
                download_obj_copy['filepath'] = download_obj_copy.get(
                    'filename', download_obj_copy['_filename']
                )
                return download_obj_copy
        return None

    @staticmethod
    def _to_float(duration: float | None) -> float | None:
        try:
            return float(duration)
        except TypeError:
            return duration

    def _get_video_filename(self, meta: dict) -> str:
        return self._get_video_filepath(meta).rsplit('/', maxsplit=1)[-1]

    def _get_video_filepath(self, meta: dict) -> str:
        if meta['_type'] == self._PLAYLIST_TYPE:
            requested_downloads: list[dict] = meta['entries'][0]['requested_downloads']
            requested_video = self._get_requested_video(requested_downloads)
        else:
            requested_downloads = meta['requested_downloads']
            requested_video = self._get_requested_video(requested_downloads)

        try:
            return requested_video['filepath']
        except (AttributeError, KeyError):
            err_msg = 'Video filepath not found'
            self._log.exception('%s, meta: %s', err_msg, meta)
            raise ValueError(err_msg) from None
