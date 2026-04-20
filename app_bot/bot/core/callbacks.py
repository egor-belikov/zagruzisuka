import asyncio
import logging

from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageIdInvalid, MessageNotModified
from pyrogram.types import Message
from yt_shared.emoji import SUCCESS_EMOJI
from yt_shared.schemas.url import URL
from yt_shared.utils.tasks.tasks import create_task as bg_create_task

from bot.bot.client import VideoBotClient
from bot.core.queue_status import build_queue_dashboard, download_workflow_backlog_count
from bot.core.service import UrlParser, UrlService
from bot.core.utils import bold, get_user_id


class TelegramCallback:
    _MSG_SEND_OK: str = (
        f'{SUCCESS_EMOJI} {bold("{count}URL{plural} sent for download")}'
    )
    _MSG_SEND_FAIL: str = f'🛑 {bold("Failed to send URL for download")}'

    def __init__(self) -> None:
        self._log = logging.getLogger(self.__class__.__name__)
        self._url_parser = UrlParser()
        self._url_service = UrlService()

    @staticmethod
    async def on_start(client: VideoBotClient, message: Message) -> None:  # noqa: ARG004
        await message.reply(
            bold('Send video URL to start processing'),
            parse_mode=ParseMode.HTML,
            reply_to_message_id=message.id,
        )

    async def on_queue(self, client: VideoBotClient, message: Message) -> None:
        viewer_id = get_user_id(message)
        is_admin = viewer_id in client.admin_users
        dash = await build_queue_dashboard(viewer_user_id=viewer_id, is_admin=is_admin)
        reply = await message.reply(
            dash.html,
            parse_mode=ParseMode.HTML,
            reply_to_message_id=message.id,
        )
        bg_create_task(
            self._poll_queue_dashboard(
                client=client,
                chat_id=message.chat.id,
                message_id=reply.id,
                viewer_user_id=viewer_id,
                is_admin=is_admin,
            ),
            task_name='queue_dashboard_refresh',
            logger=self._log,
            exception_message='Task "%s" raised an exception',
            exception_message_args=('queue_dashboard_refresh',),
        )

    async def _poll_queue_dashboard(
        self,
        client: VideoBotClient,
        *,
        chat_id: int,
        message_id: int,
        viewer_user_id: int,
        is_admin: bool,
    ) -> None:
        """Periodically refresh the /queue message until globally idle or cap."""
        empty_runs = 0
        for _ in range(48):
            await asyncio.sleep(2.5)
            try:
                dash = await build_queue_dashboard(
                    viewer_user_id=viewer_user_id, is_admin=is_admin
                )
                await client.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=dash.html,
                    parse_mode=ParseMode.HTML,
                )
                if dash.backlog_total == 0 and dash.listed_tasks == 0:
                    empty_runs += 1
                else:
                    empty_runs = 0
                if empty_runs >= 2:
                    break
            except MessageNotModified:
                dash = await build_queue_dashboard(
                    viewer_user_id=viewer_user_id, is_admin=is_admin
                )
                if dash.backlog_total == 0 and dash.listed_tasks == 0:
                    empty_runs += 1
                else:
                    empty_runs = 0
                if empty_runs >= 2:
                    break
            except MessageIdInvalid:
                break
            except FloodWait as e:
                await asyncio.sleep(float(e.value) + 0.5)
            except Exception:
                self._log.debug('queue poll edit failed', exc_info=True)

    async def on_message(self, client: VideoBotClient, message: Message) -> None:
        """Receive video URL and send to the download worker."""
        self._log.debug('Received Telegram Message: %s', message)
        text = message.text
        if not text:
            self._log.debug('Forwarded message, skipping')
            return

        urls = text.splitlines()
        user = client.get_user_config(get_user_id(message))
        if user.use_url_regex_match:
            urls = self._url_parser.filter_urls(
                urls=urls, regexes=client.conf.telegram.url_validation_regexes
            )
            if not urls:
                self._log.debug('No urls to download, skipping message')
                return

        ack_message = await self._send_acknowledge_message(
            message=message, url_count=len(urls)
        )
        context = {'message': message, 'user': user, 'ack_message': ack_message}
        url_objects = self._url_parser.parse_urls(urls=urls, context=context)
        enriched_urls: list[URL] = []
        for u in url_objects:
            log_msg = await message.reply(
                text=(
                    '<b>Лог скачивания и обработки</b>\n'
                    '<pre>⏳ Журнал обновляется по этапам…</pre>'
                ),
                parse_mode=ParseMode.HTML,
                reply_to_message_id=message.id,
            )
            enriched_urls.append(
                u.model_copy(update={'pipeline_log_message_id': log_msg.id})
            )
        await self._url_service.process_urls(enriched_urls)
        if enriched_urls:
            try:
                n = await download_workflow_backlog_count()
                base = self._format_acknowledge_text(len(enriched_urls))
                await ack_message.edit_text(
                    text=f'{base}\n\n📊 <b>Очередь</b>: сейчас ~{n} задач(и) впереди (включая эту)',
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                self._log.exception('Failed to update queue hint on ack message')

    async def _send_acknowledge_message(
        self, message: Message, url_count: int
    ) -> Message:
        return await message.reply(
            text=self._format_acknowledge_text(url_count),
            parse_mode=ParseMode.HTML,
            reply_to_message_id=message.id,
        )

    def _format_acknowledge_text(self, url_count: int) -> str:
        is_multiple = url_count > 1
        return self._MSG_SEND_OK.format(
            count=f'{url_count} ' if is_multiple else '',
            plural='s' if is_multiple else '',
        )
