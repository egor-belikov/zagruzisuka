"""Resolve video.sports.ru pages to a direct HLS playlist URL.

Sports.ru Arena serves VOD via GraphQL: content.access token + createView → video.playlist.
yt-dlp's generic extractor fails on relative /embed/… og:video URLs.
"""

from __future__ import annotations

import json
import logging  # noqa: TC003
import os
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import NoReturn
from urllib.parse import urljoin, urlsplit

_GRAPHQL_URL = 'https://video.sports.ru/site/graphql'
_ORIGIN = 'https://video.sports.ru'

_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
)

_SPORTS_RU_VIDEO_HOSTS: frozenset[str] = frozenset({'video.sports.ru'})
_VIDEO_PATH_RE = re.compile(
    r'^/(?:video|embed)/(?P<id>[A-Za-z0-9_-]+)(?:/|$|\?)',
    re.IGNORECASE,
)

_CONTENT_QUERY = """query Content($contentId: ID!) {
  content(id: $contentId) {
    id
    title
    type
    getIpUrl
    access(source: SITE) {
      token
      error
    }
  }
}"""

_CREATE_VIEW_MUTATION = """mutation CreateView($token: String!, $clientIp: String, $utm: UTMInput) {
  createView(token: $token, utm: $utm) {
    video {
      id
      playlist(clientIp: $clientIp)
      duration
    }
  }
}"""


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


def is_sports_ru_video_page_url(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https'):
        return False
    host = (parsed.netloc or '').lower().removeprefix('www.')
    if host not in _SPORTS_RU_VIDEO_HOSTS:
        return False
    path = parsed.path or ''
    if not path.startswith('/'):
        path = '/' + path
    return _VIDEO_PATH_RE.match(path) is not None


def _extract_content_id(url: str) -> str | None:
    parsed = urlsplit(url)
    path = parsed.path or ''
    if not path.startswith('/'):
        path = '/' + path
    m = _VIDEO_PATH_RE.match(path)
    return m.group('id') if m else None


def _page_referer(content_id: str) -> str:
    return f'{_ORIGIN}/video/{content_id}'


@dataclass(frozen=True)
class SportsRuResolved:
    direct_url: str
    http_headers: dict[str, str]
    page_title: str | None = None


def _urllib_opener() -> urllib.request.OpenerDirector:
    handlers: list = []
    proxy = _first_env_proxy()
    if proxy:
        handlers.append(
            urllib.request.ProxyHandler(
                {'http': proxy, 'https': proxy, 'socks5': proxy, 'socks': proxy}
            )
        )
    handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    return urllib.request.build_opener(*handlers)


def _graphql(
    opener: urllib.request.OpenerDirector,
    *,
    operation_name: str,
    query: str,
    variables: dict,
    referer: str,
) -> dict:
    body = json.dumps(
        {
            'operationName': operation_name,
            'query': query,
            'variables': variables,
        }
    ).encode()
    req = urllib.request.Request(
        _GRAPHQL_URL,
        data=body,
        headers={
            'User-Agent': _USER_AGENT,
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Origin': _ORIGIN,
            'Referer': referer,
        },
        method='POST',
    )
    try:
        with opener.open(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode('utf-8', 'replace'))
    except urllib.error.HTTPError as exc:
        detail = ''
        try:
            if exc.fp:
                detail = exc.fp.read().decode('utf-8', 'replace')
        except OSError:
            pass
        _raise_dl(
            f'Sports.ru GraphQL недоступен (HTTP {exc.code}). Ответ: {detail[:300]}',
            cause=exc,
        )
    except urllib.error.URLError as exc:
        _raise_dl(f'Sports.ru GraphQL: ошибка сети: {exc.reason}', cause=exc)

    if not isinstance(payload, dict):
        _raise_dl(f'Sports.ru GraphQL: неожиданный ответ: {payload!r}')

    if payload.get('errors'):
        err_text = json.dumps(payload['errors'], ensure_ascii=False)[:400]
        _raise_dl(f'Sports.ru GraphQL: {err_text}')

    data = payload.get('data')
    if not isinstance(data, dict):
        _raise_dl('Sports.ru GraphQL: пустой data в ответе.')
    return data


def _fetch_client_ip(
    opener: urllib.request.OpenerDirector,
    get_ip_url: str,
    referer: str,
) -> str:
    req = urllib.request.Request(
        get_ip_url,
        headers={
            'User-Agent': _USER_AGENT,
            'Accept': '*/*',
            'Origin': _ORIGIN,
            'Referer': referer,
        },
        method='GET',
    )
    try:
        with opener.open(req, timeout=60) as resp:
            client_ip = resp.read().decode('utf-8', 'replace').strip()
    except urllib.error.HTTPError as exc:
        _raise_dl(
            f'Sports.ru getIpUrl недоступен (HTTP {exc.code}).',
            cause=exc,
        )
    except urllib.error.URLError as exc:
        _raise_dl(f'Sports.ru getIpUrl: ошибка сети: {exc.reason}', cause=exc)

    if not client_ip:
        _raise_dl('Sports.ru getIpUrl вернул пустой clientIp.')
    return client_ip


def resolve_if_sports_ru(url: str, log: logging.Logger) -> SportsRuResolved | None:
    """If this is a video.sports.ru page — return HLS playlist URL, else None."""
    if not is_sports_ru_video_page_url(url):
        return None

    content_id = _extract_content_id(url)
    if not content_id:
        _raise_dl('Не удалось извлечь id видео из ссылки Sports.ru.')

    referer = _page_referer(content_id)
    opener = _urllib_opener()
    log.info('Resolving Sports.ru content %s via GraphQL…', content_id)

    content_data = _graphql(
        opener,
        operation_name='Content',
        query=_CONTENT_QUERY,
        variables={'contentId': content_id},
        referer=referer,
    ).get('content')

    if not isinstance(content_data, dict):
        _raise_dl('Sports.ru: видео не найдено или недоступно.')

    access = content_data.get('access') or {}
    access_error = access.get('error')
    if access_error:
        _raise_dl(f'Sports.ru: доступ к видео запрещён ({access_error}).')

    token = access.get('token')
    if not isinstance(token, str) or not token.strip():
        _raise_dl('Sports.ru: не получен access token для видео.')

    get_ip_url = content_data.get('getIpUrl')
    if not isinstance(get_ip_url, str) or not get_ip_url.startswith(('http://', 'https://')):
        _raise_dl('Sports.ru: не получен getIpUrl для clientIp.')

    client_ip = _fetch_client_ip(opener, get_ip_url, referer)
    view_data = _graphql(
        opener,
        operation_name='CreateView',
        query=_CREATE_VIEW_MUTATION,
        variables={
            'token': token,
            'clientIp': client_ip,
            'utm': None,
        },
        referer=referer,
    ).get('createView')

    if not isinstance(view_data, dict):
        _raise_dl('Sports.ru: createView не вернул данные просмотра.')

    video = view_data.get('video') or {}
    playlist = video.get('playlist')
    if not isinstance(playlist, str) or not playlist.strip():
        _raise_dl('Sports.ru: не получен HLS playlist для видео.')

    direct_url = urljoin(_ORIGIN + '/', playlist.lstrip('/'))

    title = content_data.get('title')
    if isinstance(title, str):
        title = title.strip() or None
    else:
        title = None

    log.info(
        'Sports.ru resolved %s → %s…',
        content_id,
        direct_url.rsplit('/', maxsplit=1)[-1][:80],
    )
    return SportsRuResolved(
        direct_url=direct_url,
        http_headers={
            'Referer': referer,
            'Origin': _ORIGIN,
            'User-Agent': _USER_AGENT,
        },
        page_title=title,
    )
