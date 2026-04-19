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
