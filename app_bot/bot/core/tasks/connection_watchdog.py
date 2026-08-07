import asyncio
import os
import time
from typing import TYPE_CHECKING

from yt_shared.utils.tasks.abstract import AbstractTask

if TYPE_CHECKING:
    from bot.bot.client import VideoBotClient


class ConnectionWatchdogTask(AbstractTask):
    """Restart the bot process if Telegram connection stays down too long."""

    _CHECK_INTERVAL_SECONDS: int = 90
    _DISCONNECT_EXIT_SECONDS: int = 180
    _PING_FAIL_EXIT_SECONDS: int = 300

    def __init__(self, bot: 'VideoBotClient') -> None:
        super().__init__()
        self._bot = bot
        self._unhealthy_since: float | None = None

    async def run(self) -> None:
        await asyncio.sleep(self._CHECK_INTERVAL_SECONDS)
        while True:
            await self._check_once()
            await asyncio.sleep(self._CHECK_INTERVAL_SECONDS)

    async def _check_once(self) -> None:
        now = time.monotonic()
        healthy = await self._is_healthy()
        if healthy:
            self._unhealthy_since = None
            return

        if self._unhealthy_since is None:
            self._unhealthy_since = now
            self._log.warning('Telegram connection unhealthy, starting watchdog timer')
            return

        elapsed = now - self._unhealthy_since
        if elapsed >= self._DISCONNECT_EXIT_SECONDS and not self._bot.is_connected:
            self._log.error(
                'Telegram disconnected for %ds, exiting for container restart',
                int(elapsed),
            )
            os._exit(1)

        if elapsed >= self._PING_FAIL_EXIT_SECONDS:
            self._log.error(
                'Telegram health check failed for %ds, exiting for container restart',
                int(elapsed),
            )
            os._exit(1)

    async def _is_healthy(self) -> bool:
        if not self._bot.is_connected:
            return False
        try:
            await asyncio.wait_for(self._bot.get_me(), timeout=30)
        except Exception:
            self._log.warning('Telegram ping failed', exc_info=True)
            return False
        return True
