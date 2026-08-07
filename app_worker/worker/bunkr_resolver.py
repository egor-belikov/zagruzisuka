"""Resolve Bunkr file/album pages to direct media URLs.

Uses Bunkr download API (dl.bunkr.cr) + CDN signing (glb-apisign.cdn.cr),
aligned with gallery-dl 1.32.x (2026).
"""

from __future__ import annotations

import binascii
import html as html_stdlib
import json
import logging
import os
import random
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import NoReturn
from urllib.parse import quote, urlencode, urlsplit


def _raise_dl(msg: str, cause: BaseException | None = None) -> NoReturn:
    from worker.core.exceptions import MediaDownloaderError  # noqa: PLC0415

    if cause is None:
        raise MediaDownloaderError(msg)
    raise MediaDownloaderError(msg) from cause


_PRIMARY_MIRRORS: tuple[str, ...] = (
    'bunkr.ac',
    'bunkr.ci',
    'bunkr.cr',
    'bunkr.fi',
    'bunkr.ph',
    'bunkr.pk',
    'bunkr.ps',
    'bunkr.si',
    'bunkr.sk',
    'bunkr.ws',
    'bunkr.black',
    'bunkr.red',
    'bunkr.media',
    'bunkr.site',
)
_LEGACY_MIRRORS: frozenset[str] = frozenset(
    (
        'bunkr.ax',
        'bunkr.cat',
        'bunkr.ru',
        'bunkrr.ru',
        'bunkr.su',
        'bunkrr.su',
        'bunkr.la',
        'bunkr.is',
        'bunkr.to',
    )
)
_ALL_KNOWN: frozenset[str] = frozenset(_PRIMARY_MIRRORS) | _LEGACY_MIRRORS

_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
)

_MEDIA_PATH_RE = re.compile(r'^/[fvid]/[^/?#]+/?$', re.IGNORECASE)
_ALBUM_PATH_RE = re.compile(r'^/a/[^/?#]+/?$', re.IGNORECASE)
_ALBUM_FILE_LINK_RE = re.compile(r'href=["\'](/[fvid]/[^"\']+)["\']', re.IGNORECASE)
_ALBUM_FILE_ID_RE = re.compile(r'\bid:\s*(\d+)\s*,', re.MULTILINE)

_DATA_FILE_ID_RE = re.compile(
    r'data-file-id\s*=\s*"([^"]+)"', re.MULTILINE | re.IGNORECASE
)
_OG_TITLE_RE = re.compile(r'property="og:title"\s+content="([^"]*)"', re.IGNORECASE)

_GENERIC_BUNKR_HOST_RE = re.compile(
    r'^(?:www\.)?(?:app\.)?(?P<core>bunkr+\.[a-z0-9][a-z0-9.-]*[a-z0-9]?)$'
)

_BUNKR_API = 'https://dl.bunkr.cr/api/_001_v2'
_BUNKR_API_ROOT = 'https://dl.bunkr.cr'
_BUNKR_SIGN_API = 'https://glb-apisign.cdn.cr/sign'
_BUNKR_REFERER_ROOT = 'https://get.bunkrr.su'

_HTTP_STATUS_CLIENT_ERROR_THRESHOLD = 400
_LOG_PREVIEW_TITLE_CHARS = 80


def _strip_leading_ipv4_maybe(host: str) -> str:
    return host.strip().lower()


def _normalize_bunker_host(raw_host: str) -> str | None:
    h = _strip_leading_ipv4_maybe(raw_host).removeprefix('www.').removeprefix('app.')
    if h in _ALL_KNOWN:
        return h
    m = _GENERIC_BUNKR_HOST_RE.match(h)
    return m.group('core').lower() if m else None


def is_bunkr_media_page_url(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https'):
        return False
    if _normalize_bunker_host(parsed.netloc or '') is None:
        return False
    path = parsed.path or ''
    path = '/' + path.lstrip('/') if path else ''
    return bool(_MEDIA_PATH_RE.match(path))


def is_bunkr_album_page_url(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https'):
        return False
    if _normalize_bunker_host(parsed.netloc or '') is None:
        return False
    path = parsed.path or ''
    path = '/' + path.lstrip('/') if path else ''
    return bool(_ALBUM_PATH_RE.match(path))


@dataclass(frozen=True)
class BunkrResolved:
    direct_url: str
    http_headers: dict[str, str]
    page_title: str | None = None


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


def _urllib_opener() -> urllib.request.OpenerDirector:
    handlers: list = []
    p = _first_env_proxy()
    if p:
        handlers.append(
            urllib.request.ProxyHandler(
                {'http': p, 'https': p, 'socks5': p, 'socks': p}
            )
        )
    handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    return urllib.request.build_opener(*handlers)


def _mirror_roots_for_retries(preferred_host: str) -> list[str]:
    ph = preferred_host.lower()
    rest = [h for h in _PRIMARY_MIRRORS if h.lower() != ph]
    random.shuffle(rest)
    legacy_list = list(_LEGACY_MIRRORS - {preferred_host.lower()})
    random.shuffle(legacy_list)
    out = [preferred_host]
    seen = {preferred_host}
    for bucket in (rest, legacy_list):
        for h in bucket:
            if h not in seen:
                seen.add(h)
                out.append(h)
    return out


def _fetch_html_page(
    opener: urllib.request.OpenerDirector, page_url: str
) -> tuple[int, str]:
    req = urllib.request.Request(
        page_url,
        headers={
            'User-Agent': _USER_AGENT,
            'Accept': 'text/html,application/xhtml+xml,*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Upgrade-Insecure-Requests': '1',
        },
        method='GET',
    )
    try:
        with opener.open(req, timeout=120) as resp:
            code = getattr(resp, 'status', getattr(resp, 'code', 200))
            raw = resp.read()
            return int(code), raw.decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        try:
            body = exc.fp.read().decode('utf-8', 'replace') if exc.fp else ''
        except OSError:
            body = ''
        return int(exc.code), body


def _bunkr_file_referer(file_id: str) -> str:
    return f'{_BUNKR_REFERER_ROOT}/file/{file_id}'


def _decrypt_xor_url(encrypted_b64: str, timestamp: int) -> str:
    key = f'SECRET_KEY_{timestamp // 3600}'.encode()
    encrypted = binascii.a2b_base64(encrypted_b64)
    return bytes(
        encrypted[i] ^ key[i % len(key)] for i in range(len(encrypted))
    ).decode()


def _request_json(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    method: str = 'GET',
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> dict:
    req = urllib.request.Request(
        url,
        data=body,
        headers=headers or {},
        method=method,
    )
    try:
        with opener.open(req, timeout=120) as resp:
            raw = resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        detail = ''
        try:
            if exc.fp:
                detail = exc.fp.read().decode('utf-8', 'replace')
        except OSError:
            pass
        _raise_dl(
            f'Bunkr API недоступен (HTTP {exc.code}). Ответ: {detail[:300]}',
            cause=exc,
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        _raise_dl(f'Bunkr API вернул не-JSON: {raw[:240]}', cause=exc)
    if not isinstance(payload, dict):
        _raise_dl(f'Bunkr API: ожидался объект JSON, получено: {payload!r}')
    return payload




def _sign_bunkr_cdn_url(
    opener: urllib.request.OpenerDirector,
    *,
    media_url: str,
    path: str,
    original: str | None,
) -> str:
    sign_headers = {
        'User-Agent': _USER_AGENT,
        'Referer': f'{_BUNKR_API_ROOT}/',
    }
    sign_url = f'{_BUNKR_SIGN_API}?path={quote(path, safe="")}'
    sign = _request_json(opener, sign_url, headers=sign_headers)
    params = dict(sign)
    if original:
        params['n'] = original
    sep = '&' if '?' in media_url else '?'
    return media_url + sep + urlencode(params)


def _resolve_bunkr_file_id(
    file_id: str,
    *,
    page_title: str | None,
    opener: urllib.request.OpenerDirector,
    log: logging.Logger,
) -> BunkrResolved:
    referer = _bunkr_file_referer(file_id)
    api_headers = {
        'User-Agent': _USER_AGENT,
        'Accept': 'application/json,*/*',
        'Content-Type': 'application/json',
        'Referer': f'{_BUNKR_API_ROOT}/',
        'Origin': _BUNKR_API_ROOT,
    }
    payload = _request_json(
        opener,
        _BUNKR_API,
        method='POST',
        headers=api_headers,
        body=json.dumps({'id': file_id}).encode(),
    )

    original = payload.get('original')
    if isinstance(original, str):
        original_name = original
    else:
        original_name = None

    if payload.get('encrypted'):
        ts = int(payload['timestamp'])
        direct_url = _decrypt_xor_url(str(payload['url']), ts)
    elif payload.get('url'):
        direct_url = str(payload['url'])
    else:
        base = str(payload.get('mediafiles') or '').rstrip('/')
        path = str(payload.get('path') or '')
        if not base or not path:
            _raise_dl(f'Bunkr download API: неполный ответ: {payload!r}')
        unsigned = base + path
        direct_url = _sign_bunkr_cdn_url(
            opener,
            media_url=unsigned,
            path=path,
            original=original_name,
        )

    hdrs = {
        'Referer': referer,
        'User-Agent': _USER_AGENT,
        'Accept': '*/*',
    }
    log.info(
        'Bunkr file id=%s resolved: %s…',
        file_id,
        direct_url.split('?', maxsplit=1)[0][:100],
    )
    return BunkrResolved(
        direct_url=direct_url,
        http_headers=hdrs,
        page_title=page_title or original_name,
    )


def _extract_file_id_from_html(html: str) -> tuple[str, str | None]:
    fm = _DATA_FILE_ID_RE.search(html)
    if fm is None:
        _raise_dl(
            'Не удалось прочитать страницу Bunkr (нет id файла). '
            'Сайт изменился или требует Cloudflare-браузer.'
        )
    file_id = fm.group(1).strip()
    if not file_id:
        _raise_dl('Страница Bunkr не содержит идентификатора файла.')
    title_m = _OG_TITLE_RE.search(html)
    title = (
        html_stdlib.unescape(html_stdlib.unescape(title_m.group(1)))
        if title_m is not None
        else None
    )
    return file_id, title


def _extract_album_file_paths(html: str) -> list[str]:
    seen: set[str] = set()
    paths: list[str] = []
    for m in _ALBUM_FILE_LINK_RE.finditer(html):
        path = m.group(1).split('?')[0]
        if path not in seen:
            seen.add(path)
            paths.append(path)
    if paths:
        return paths
    for m in _ALBUM_FILE_ID_RE.finditer(html):
        fid = m.group(1)
        path = f'/f/{fid}'
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def list_bunkr_album_file_page_urls(url: str, log: logging.Logger) -> list[str]:
    """Return absolute file-page URLs for each item in a Bunkr album."""
    if not is_bunkr_album_page_url(url):
        return []

    parsed = urlsplit(url)
    host = _normalize_bunker_host(parsed.netloc or '')
    if host is None:
        return []

    album_path = parsed.path or ''
    if not album_path.startswith('/'):
        album_path = '/' + album_path
    query_suffix = '?advanced=1'

    opener = _urllib_opener()
    last_err = ''
    for root_host in _mirror_roots_for_retries(host):
        page_url = f'https://{root_host}{album_path}{query_suffix}'
        log.info('Fetching Bunkr album via mirror %s', page_url)
        code, html = _fetch_html_page(opener, page_url)
        if code >= _HTTP_STATUS_CLIENT_ERROR_THRESHOLD:
            last_err = f'{root_host}: HTTP {code}'
            continue
        paths = _extract_album_file_paths(html)
        if not paths:
            last_err = f'{root_host}: не найдены файлы в альбоме'
            continue
        out = [f'https://{root_host}{p}' for p in paths]
        log.info('Bunkr album: %s file(s) on %s', len(out), root_host)
        return out

    _raise_dl(
        'Не удалось загрузить альбом Bunkr ни с одного зеркала. '
        f'Последние попытки: {last_err or "нет"}.'
    )


def resolve_if_bunkr(url: str, log: logging.Logger) -> BunkrResolved | None:
    """If this is a Bunkr single-file page, return a signed CDN URL."""
    if not is_bunkr_media_page_url(url):
        return None

    parsed = urlsplit(url)
    host = _normalize_bunker_host(parsed.netloc or '')
    if host is None:
        return None

    raw_path_q = (parsed.path or '') + (('?' + parsed.query) if parsed.query else '')
    if not raw_path_q.startswith('/'):
        raw_path_q = '/' + raw_path_q

    opener = _urllib_opener()
    last_err = ''
    for root_host in _mirror_roots_for_retries(host):
        page_url = f'https://{root_host}{raw_path_q}'
        log.info('Fetching Bunkr page via mirror %s', page_url)
        code, html = _fetch_html_page(opener, page_url)
        if (
            code >= _HTTP_STATUS_CLIENT_ERROR_THRESHOLD
            or 'data-file-id' not in html.lower()
        ):
            last_err = f'{root_host}: HTTP {code}'
            continue
        try:
            file_id, page_title = _extract_file_id_from_html(html)
            return _resolve_bunkr_file_id(
                file_id, page_title=page_title, opener=opener, log=log
            )
        except Exception as err:
            from worker.core.exceptions import MediaDownloaderError  # noqa: PLC0415

            if isinstance(err, MediaDownloaderError):
                last_err = f'{root_host}: {err}'
                continue
            raise

    _raise_dl(
        'Не удалось загрузить страницу ни с одного зеркала Bunkr. '
        f'Последние попытки: {last_err or "нет"}.'
    )
