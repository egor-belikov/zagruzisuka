"""Resolve Bunkr single-file pages to a direct media URL.

Bunker uses many mirror domains (.si, .fi, bunkr+.TLD etc.); if one mirror is blocked
by Cloudflare, we retry other mirrors with the same path (same behaviour as gallery-dl).

Logic aligned with gallery-dl's bunkr extractor (page jsCDN + signed CDN token, 2026+).
"""

from __future__ import annotations

import html as html_stdlib
import json
import logging  # noqa: TC003 — runtime logger, not type-only
import os
import random
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import NoReturn
from urllib.parse import quote, urlencode, urlsplit, urlparse


def _raise_dl(msg: str, cause: BaseException | None = None) -> NoReturn:
    from worker.core.exceptions import MediaDownloaderError  # noqa: PLC0415

    if cause is None:
        raise MediaDownloaderError(msg)
    raise MediaDownloaderError(msg) from cause


# Current + legacy bunker frontends from gallery_dl/extractor/bunkr.py (2026-ish).
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
_DATA_FILE_ID_RE = re.compile(
    r'data-file-id\s*=\s*"([^"]+)"', re.MULTILINE | re.IGNORECASE
)
_OG_TITLE_RE = re.compile(r'property="og:title"\s+content="([^"]*)"', re.IGNORECASE)
_JS_CDN_RE = re.compile(r'var\s+jsCDN\s*=\s*"([^"]+)"', re.IGNORECASE)
_JS_SIGN_URL_RE = re.compile(r'var\s+signUrl\s*=\s*"([^"]+)"', re.IGNORECASE)

_GENERIC_BUNKR_HOST_RE = re.compile(
    r'^(?:www\.)?(?:app\.)?(?P<core>bunkr+\.[a-z0-9][a-z0-9.-]*[a-z0-9]?)$'
)

_HTTP_STATUS_CLIENT_ERROR_THRESHOLD = 400
_LOG_PREVIEW_TITLE_CHARS = 80


def _strip_leading_ipv4_maybe(host: str) -> str:
    # urlsplit preserves netloc; ignore bracketed ipv6 exotic cases here.
    return host.strip().lower()


def _normalize_bunker_host(raw_host: str) -> str | None:
    """Return registrable bunker host without www/app, or None if not bunker-like."""
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


@dataclass(frozen=True)
class BunkrResolved:
    direct_url: str
    """Headers bunkr CDN expects alongside the GET to the CDN URL."""
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


def _unescape_js_string(raw: str) -> str:
    return raw.replace('\\/', '/')


def _extract_player_vars(html: str) -> tuple[str, str] | None:
    """Parse jsCDN + signUrl embedded in the Bunkr file page (2026+ token flow)."""
    js_m = _JS_CDN_RE.search(html)
    sign_m = _JS_SIGN_URL_RE.search(html)
    if js_m is None or sign_m is None:
        return None
    js_cdn = _unescape_js_string(js_m.group(1).strip())
    sign_url = _unescape_js_string(sign_m.group(1).strip())
    if not js_cdn.startswith(('http://', 'https://')):
        return None
    if not sign_url.startswith(('http://', 'https://')):
        return None
    return js_cdn, sign_url


def _sign_cdn_url(
    opener: urllib.request.OpenerDirector,
    js_cdn: str,
    sign_url: str,
    page_referer: str,
) -> str:
    """Exchange CDN path for short-lived token query params (gallery-dl gh#9554)."""
    path = urlparse(js_cdn).path
    if not path:
        _raise_dl('Bunkr: пустой путь CDN в jsCDN.')

    sign_req_url = sign_url + '?path=' + quote(path, safe='')
    req = urllib.request.Request(
        sign_req_url,
        headers={
            'User-Agent': _USER_AGENT,
            'Accept': 'application/json,*/*',
            'Referer': page_referer,
        },
        method='GET',
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
            f'Bunkr sign API недоступен (HTTP {exc.code}). Ответ: {detail[:300]}',
            cause=exc,
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        _raise_dl(f'Bunkr sign API вернул не-JSON: {raw[:240]}', cause=exc)

    if not isinstance(payload, dict):
        _raise_dl(f'Bunkr sign API: ожидался объект JSON, получено: {payload!r}')

    query = urlencode({str(k): str(v) for k, v in payload.items()})
    if not query:
        _raise_dl(f'Bunkr sign API: пустой ответ: {payload!r}')
    return js_cdn + '?' + query


def _mirror_roots_for_retries(preferred_host: str) -> list[str]:
    ph = preferred_host.lower()
    rest = [h for h in _PRIMARY_MIRRORS if h.lower() != ph]
    random.shuffle(rest)
    # Legacy после основных —
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
            # Bunker declares utf-8; ignore garbled surrogate rather than crashing.
            return int(code), raw.decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        try:
            body = exc.fp.read().decode('utf-8', 'replace') if exc.fp else ''
        except OSError:
            body = ''
        return int(exc.code), body


def _extract_meta(html: str) -> tuple[str, str | None]:
    fm = _DATA_FILE_ID_RE.search(html)
    if fm is None:
        _raise_dl(
            'Не удалось прочитать страницу Bunkr (нет id файла). '
            'Сайт изменился или требует Cloudflare-браузер; '
            'попробуйте другое зеркало (другой TLD bunkr+) или проверьте YTDLP_PROXY.'
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


def resolve_if_bunkr(url: str, log: logging.Logger) -> BunkrResolved | None:
    """Если это страница одного файла на bunker — вернуть прямую ссылку, иначе None."""
    from worker.core.exceptions import MediaDownloaderError  # noqa: PLC0415

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
            file_id, page_title = _extract_meta(html)
        except MediaDownloaderError as err:
            last_err = f'{root_host}: {err}'
            continue

        player_vars = _extract_player_vars(html)
        if player_vars is None:
            last_err = (
                f'{root_host}: нет jsCDN/signUrl на странице (Bunkr изменил разметку?)'
            )
            continue

        js_cdn, sign_url = player_vars
        log.info(
            'Bunkr file id=%s on %s (%s), signing CDN URL…',
            file_id,
            root_host,
            page_title[:_LOG_PREVIEW_TITLE_CHARS] + '…'
            if page_title and len(page_title) > _LOG_PREVIEW_TITLE_CHARS
            else page_title,
        )
        try:
            direct_url = _sign_cdn_url(opener, js_cdn, sign_url, page_url)
        except MediaDownloaderError as err:
            last_err = f'{root_host}: {err}'
            continue

        hdrs = {
            'Referer': page_url,
            'Origin': f'https://{root_host}',
            'User-Agent': _USER_AGENT,
        }

        log.info(
            'Bunkr resolved (mirror=%s): %s…',
            root_host,
            direct_url.split('?', maxsplit=1)[0][:100],
        )
        return BunkrResolved(
            direct_url=direct_url,
            http_headers=hdrs,
            page_title=page_title or None,
        )

    _raise_dl(
        'Не удалось загрузить страницу ни с одного зеркала Bunkr. '
        f'Последние попытки: {last_err or "нет"}. Облачная защита / блокировки: '
        'попробуйте другой bunker-домен или прокси (YTDLP_PROXY).'
    )
