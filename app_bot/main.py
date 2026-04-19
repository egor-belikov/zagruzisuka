#!/usr/bin/env python3
"""Bot Launcher Module."""

import asyncio
import os

import uvloop

from bot.bot.launcher import BotLauncher
from bot.core.log import setup_logging


async def main() -> None:
    if (os.environ.get("TELEGRAM_SOCKS5_PROXY") or "").strip():
        from bot.core.pyrogram_socks_asyncio_patch import apply_pyrogram_socks_asyncio_patch

        apply_pyrogram_socks_asyncio_patch()
    setup_logging()
    await BotLauncher().run()


if __name__ == "__main__":
    if not (os.environ.get("TELEGRAM_SOCKS5_PROXY") or "").strip():
        uvloop.install()
    asyncio.run(main())
