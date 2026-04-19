"""Queue / backlog metrics for the download bot."""

import logging

from sqlalchemy import func, select

from yt_shared.db.session import AsyncSessionLocal
from yt_shared.enums import TaskStatus
from yt_shared.models.task import Task
from yt_shared.rabbit import get_rabbitmq
from yt_shared.rabbit.rabbit_config import INPUT_QUEUE

_log = logging.getLogger(__name__)


async def input_queue_ready_count() -> int:
    """Messages waiting in RabbitMQ (not yet taken by a consumer)."""
    rmq = get_rabbitmq()
    queue = await rmq.channel.declare_queue(INPUT_QUEUE, passive=True)
    return int(queue.declaration_result.message_count)


async def active_db_task_count() -> int:
    """Tasks in Postgres that are not finished (queued or running on worker)."""
    async with AsyncSessionLocal() as session:
        stmt = select(func.count()).select_from(Task).where(
            Task.status.in_((TaskStatus.PENDING, TaskStatus.PROCESSING))
        )
        return int((await session.execute(stmt)).scalar_one())


async def download_workflow_backlog_count() -> int:
    """Total visible backlog: broker queue + active DB rows.

    RabbitMQ 'ready' excludes in-flight unacked messages; DB counts PENDING/PROCESSING
    so tasks already taken by the worker still show up until done/failed.
    """
    n_rabbit = 0
    n_db = 0
    try:
        n_rabbit = await input_queue_ready_count()
    except Exception:
        _log.exception('Failed to read RabbitMQ queue depth')
    try:
        n_db = await active_db_task_count()
    except Exception:
        _log.exception('Failed to read DB task backlog')
    return n_rabbit + n_db
