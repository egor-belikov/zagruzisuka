"""Progress updates from worker to bot (Telegram message edits)."""

import uuid
from typing import Annotated

from pydantic import StringConstraints

from yt_shared.schemas.base import StrictBaseConfigModel


class DownloadProgressPayload(StrictBaseConfigModel):
    """Payload published while yt-dlp downloads (throttled on worker)."""

    task_id: uuid.UUID
    from_chat_id: int
    ack_message_id: int
    url: Annotated[str, StringConstraints(max_length=512)]
    line: Annotated[str, StringConstraints(max_length=1024)]
