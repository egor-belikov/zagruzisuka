"""Resolve VEED view/embed pages to a direct CDN MP4 URL.

VEED's html5 embed reports ext=001; yt-dlp refuses to download it (CVE-2024-38519).
We probe once, strip the media fragment (#t=…), and download the CDN file directly.
"""

from __future__ import annotations

import logging  # noqa: TC003
import os
import re
from dataclasses import dataclass
from typing import NoReturn
from urllib.parse import urlsplit

import yt_dlp

_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
)

_VEED_ID_RE = re.compile(
    r'^/(?:view|embed|videos)/(?P<id>[0-9a-fA-F-]{36})(?:/|$|\?)',
    re.IGNORECASE,
)
_CDN_MP4_HOST_SUFFIX = '.veed.io'


def _raise_dl(msg: str, cause: BaseException | None = None) -> NoReturn:
    from worker.core.exceptions import MediaDownloaderError  # noqa: PLC0415

    if cause is None:
        raise MediaDownloaderError(msg)
    raise MediaDownloaderError(msg) from cause


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


def is_veed_media_page_url(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https'):
        return False
    host = (parsed.netloc or '').lower().removeprefix('www.')
    if host != 'veed.io':
        return False
    path = parsed.path or ''
    if not path.startswith('/'):
        path = '/' + path
    return _VEED_ID_RE.match(path) is not None


def _extract_video_id(url: str) -> str | None:
    parsed = urlsplit(url)
    path = parsed.path or ''
    if not path.startswith('/'):
        path = '/' + path
    m = _VEED_ID_RE.match(path)
    return m.group('id') if m else None


@dataclass(frozen=True)
class VeedResolved:
    direct_url: str
    http_headers: dict[str, str]
    page_title: str | None = None


def _pick_direct_mp4_url(info: dict) -> str | None:
    direct = info.get('url')
    if isinstance(direct, str) and '.mp4' in direct.lower():
        return direct.split('#', maxsplit=1)[0]

    for fmt in info.get('formats') or ():
        if not isinstance(fmt, dict):
            continue
        candidate = fmt.get('url')
        if isinstance(candidate, str) and '.mp4' in candidate.lower():
            return candidate.split('#', maxsplit=1)[0]
    return None


def resolve_if_veed(url: str, log: logging.Logger) -> VeedResolved | None:
    """If this is a VEED page — return signed CDN MP4 URL, else None."""
    if not is_veed_media_page_url(url):
        return None

    video_id = _extract_video_id(url)
    if not video_id:
        _raise_dl('Не удалось извлечь id видео из ссылки VEED.')

    embed_url = f'https://www.veed.io/embed/{video_id}'
    log.info('Probing VEED page %s for CDN URL…', url)

    probe_opts: dict = {
        'quiet': True,
        'no_warnings': True,
        'format': 'best[ext=mp4]/best/bestvideo+bestaudio',
        'noplaylist': True,
    }
    proxy = _first_env_proxy()
    if proxy:
        probe_opts['proxy'] = proxy

    try:
        with yt_dlp.YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as err:
        _raise_dl(f'Не удалось прочитать страницу VEED: {err}', cause=err)

    if not info:
        _raise_dl('VEED: пустой ответ при разборе страницы.')

    direct_url = _pick_direct_mp4_url(info)
    if not direct_url:
        _raise_dl('VEED: не найдена прямая ссылка на MP4 на CDN.')

    host = (urlsplit(direct_url).hostname or '').lower()
    if not host.endswith(_CDN_MP4_HOST_SUFFIX) or not direct_url.lower().endswith('.mp4'):
        _raise_dl(f'VEED: неожиданный CDN URL: {direct_url[:160]}')

    title = info.get('title')
    if isinstance(title, str):
        title = title.strip() or None
    else:
        title = None

    log.info(
        'VEED resolved %s → %s…',
        video_id,
        direct_url.rsplit('/', maxsplit=1)[-1][:80],
    )
    return VeedResolved(
        direct_url=direct_url,
        http_headers={
            'Referer': embed_url,
            'Origin': 'https://www.veed.io',
            'User-Agent': _USER_AGENT,
        },
        page_title=title,
    )
