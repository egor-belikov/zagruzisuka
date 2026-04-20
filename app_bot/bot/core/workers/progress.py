import html

from pyrogram.enums import ParseMode
from pyrogram.errors import MessageIdInvalid, MessageNotModified
from yt_shared.rabbit.rabbit_config import PROGRESS_QUEUE
from yt_shared.schemas.progress import DownloadProgressPayload

from bot.core.workers.abstract import AbstractDownloadResultWorker
from bot.core.workers.enums import RabbitWorkerType


class ProgressDownloadResultWorker(AbstractDownloadResultWorker):
    TYPE = RabbitWorkerType.PROGRESS
    QUEUE_TYPE = PROGRESS_QUEUE
    SCHEMA_CLS = (DownloadProgressPayload,)

    async def _process_body(self, body: DownloadProgressPayload) -> bool:
        url = html.escape(body.url[:220])
        detail = (body.line or '').strip()
        log_block = ''
        if detail:
            safe = html.escape(detail[:3500])
            log_block = f'<b>Лог скачивания и обработки</b>\n<pre>{safe}</pre>'

        if body.pipeline_log_message_id:
            last_line = ''
            if detail:
                tail = detail.strip().split('\n')[-1].strip()
                if tail:
                    last_line = html.escape(tail[:500])
            ack_extra = (
                f'\n\n<code>{last_line}</code>'
                if last_line
                else '\n\n<i>Подробный журнал — в отдельном сообщении ниже.</i>'
            )
            ack_text = (
                f'⬇️ <b>Загрузка</b>\n<code>{url}</code>{ack_extra}'[:4090]
            )
            try:
                await self._bot.edit_message_text(
                    chat_id=body.from_chat_id,
                    message_id=body.ack_message_id,
                    text=ack_text,
                    parse_mode=ParseMode.HTML,
                )
            except MessageNotModified:
                pass
            except MessageIdInvalid:
                self._log.debug('Progress ack edit skipped (message removed)')
            except Exception:
                self._log.debug('Progress ack edit failed', exc_info=True)

            if log_block:
                try:
                    await self._bot.edit_message_text(
                        chat_id=body.from_chat_id,
                        message_id=body.pipeline_log_message_id,
                        text=log_block[:4090],
                        parse_mode=ParseMode.HTML,
                    )
                except MessageNotModified:
                    pass
                except MessageIdInvalid:
                    self._log.debug('Progress log edit skipped (message removed)')
                except Exception:
                    self._log.debug('Progress log edit failed', exc_info=True)
        else:
            if detail:
                block = f'<pre>{html.escape(detail[:3500])}</pre>'
            else:
                block = ''
            text = f'⬇️ <b>Загрузка</b>\n<code>{url}</code>\n{block}'
            try:
                await self._bot.edit_message_text(
                    chat_id=body.from_chat_id,
                    message_id=body.ack_message_id,
                    text=text[:4090],
                    parse_mode=ParseMode.HTML,
                )
            except MessageNotModified:
                pass
            except MessageIdInvalid:
                self._log.debug('Progress edit skipped (message removed)')
            except Exception:
                self._log.debug('Progress edit failed', exc_info=True)
        return True
