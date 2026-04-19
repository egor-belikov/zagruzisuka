import logging

from pyrogram.enums import ParseMode
from pyrogram.types import Message
from yt_shared.emoji import SUCCESS_EMOJI

from bot.bot.client import VideoBotClient
from bot.core.queue_status import download_workflow_backlog_count
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

    @staticmethod
    async def on_queue(client: VideoBotClient, message: Message) -> None:  # noqa: ARG004
        n = await download_workflow_backlog_count()
        await message.reply(
            (
                '📥 <b>Очередь загрузок</b>\n'
                f'Активных задач (в RabbitMQ + в работе у воркера): <code>{n}</code>\n\n'
                '<i>Прогресс по вашей ссылке обновляется под ответом «URL sent…».</i>'
            ),
            parse_mode=ParseMode.HTML,
            reply_to_message_id=message.id,
        )

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
        await self._url_service.process_urls(url_objects)
        if url_objects:
            try:
                n = await download_workflow_backlog_count()
                base = self._format_acknowledge_text(len(url_objects))
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
