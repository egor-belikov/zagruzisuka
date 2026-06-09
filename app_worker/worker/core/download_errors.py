"""User-facing download error messages for Telegram notifications."""

from __future__ import annotations

import re

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def clean_ytdlp_error(message: str) -> str:
    text = _ANSI_RE.sub('', message).strip()
    for prefix in ('ERROR: ', 'WARNING: '):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    return text


def format_download_failure(
    *,
    reason: str,
    url: str,
    resolved_url: str | None = None,
) -> str:
    """Build a concise, user-visible explanation (no «check logs» placeholders)."""
    lines = [clean_ytdlp_error(reason) or 'Неизвестная ошибка скачивания.']
    if resolved_url and resolved_url != url:
        lines.append(f'Разрешённый URL: {resolved_url}')
    return '\n'.join(lines)
