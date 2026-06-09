import asyncio
import html
from typing import Any, ClassVar

from pyrogram.enums import ParseMode
from yt_shared.enums import RabbitPayloadType
from yt_shared.schemas.error import ErrorDownloadGeneralPayload, ErrorDownloadPayload

from bot.core.config import settings
from bot.core.handlers.abstract import AbstractDownloadHandler
from bot.core.utils import split_telegram_message
from bot.version import __version__

_GENERIC_ERROR_MESSAGES: frozenset[str] = frozenset(
    {
        'Download error',
        'General worker error',
        'Upload error',
    }
)


class ErrorDownloadHandler(AbstractDownloadHandler):
    _body: ErrorDownloadPayload | ErrorDownloadGeneralPayload
    _ERR_MSG_TPL = (
        '🛑 <b>{header}</b>\n\n'
        'ℹ <b>Task ID:</b> <code>{task_id}</code>\n'  # noqa: RUF001
        '💬 <b>Причина:</b>\n<pre>{reason}</pre>\n'
        '📹 <b>Video URL:</b> <code>{url}</code>\n'
        '🌊 <b>Source:</b> <code>{source}</code>\n'
        '🔧 <b>Тип:</b> <code>{exception_type}</code>\n'
        '⬇️ <b>yt-dlp version:</b> <code>{yt_dlp_version}</code>\n'
        '🤖 <b>yt-dlp-bot version:</b> <code>{yt_dlp_bot_version}</code>\n'
        '🏷️ <b>Tag:</b> #error'
    )

    _ERR_MSG_HEADER_MAP: ClassVar[dict[RabbitPayloadType, str]] = {
        RabbitPayloadType.DOWNLOAD_ERROR: 'Ошибка скачивания',
        RabbitPayloadType.GENERAL_ERROR: 'Внутренняя ошибка',
    }

    async def handle(self) -> None:
        await self._delete_pipeline_log_message()
        self._send_error_text()

    async def _delete_pipeline_log_message(self) -> None:
        mid = self._body.context.pipeline_log_message_id
        chat = self._body.from_chat_id
        if chat and mid:
            try:
                await self._bot.delete_messages(chat_id=chat, message_ids=mid)
            except Exception:
                self._log.debug('Pipeline log message delete failed', exc_info=True)

    def _send_error_text(self) -> None:
        for user in self._get_receiving_users():
            kwargs: dict[str, Any] = {
                'chat_id': user.id,
                'text': self._format_error_message(),
                'parse_mode': ParseMode.HTML,
            }
            if self._body.message_id:
                kwargs['reply_to_message_id'] = self._body.message_id
            asyncio.create_task(self._bot.send_message(**kwargs))  # noqa:RUF006

    @staticmethod
    def _last_traceback_line(traceback_text: str) -> str:
        lines = [line.strip() for line in traceback_text.splitlines() if line.strip()]
        return lines[-1] if lines else ''

    def _resolve_reason(self) -> str:
        message = (self._body.message or '').strip()
        exception_msg = (self._body.exception_msg or '').strip()

        if self._body.type == RabbitPayloadType.GENERAL_ERROR:
            if exception_msg:
                return exception_msg
            return message or 'Неизвестная ошибка'

        if message and message not in _GENERIC_ERROR_MESSAGES:
            return message
        if exception_msg and exception_msg not in _GENERIC_ERROR_MESSAGES:
            return exception_msg
        if exception_msg:
            last_line = self._last_traceback_line(exception_msg)
            if last_line:
                return last_line
        return message or 'Неизвестная ошибка'

    def _format_error_message(self) -> str:
        reason = html.escape(self._resolve_reason())
        pre_formatted_message = self._ERR_MSG_TPL.format(
            header=self._ERR_MSG_HEADER_MAP[self._body.type],
            url=html.escape(self._body.url),
            source=html.escape(self._body.context.source.value),
            task_id=self._body.task_id,
            exception_type=html.escape(self._body.exception_type),
            yt_dlp_version=self._body.yt_dlp_version,
            yt_dlp_bot_version=__version__,
        )

        placeholder = '{reason}'
        msg_len = len(pre_formatted_message) - len(placeholder)
        reason_len = len(reason)
        self._log.debug('Length of reason %s', reason_len)
        self._log.debug('Length of pre_formatted_message %s', msg_len)
        if msg_len + reason_len > settings.TG_MAX_MSG_SIZE:
            reason = next(
                split_telegram_message(
                    text=reason,
                    chunk_size=settings.TG_MAX_MSG_SIZE - msg_len,
                    return_first=True,
                    negate=True,
                )
            )
        message = pre_formatted_message.format(reason=reason)
        self._log.debug('Length of formatted_message %s', len(message))
        return message
