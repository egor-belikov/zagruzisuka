"""Queue / backlog metrics and /queue dashboard text."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from html import escape

from sqlalchemy import func, select

from yt_shared.db.session import AsyncSessionLocal
from yt_shared.enums import TaskStatus
from yt_shared.models.task import Task
from yt_shared.rabbit import get_rabbitmq
from yt_shared.rabbit.rabbit_config import INPUT_QUEUE
from yt_shared.repositories.task import TaskRepository

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


@dataclass(frozen=True, slots=True)
class QueueDashboard:
    html: str
    backlog_total: int
    listed_tasks: int


def _status_ru(status: TaskStatus) -> str:
    if status == TaskStatus.PENDING:
        return 'ожидает воркер'
    if status == TaskStatus.PROCESSING:
        return 'в работе'
    return status.value


def _short_id(task_id) -> str:
    s = str(task_id)
    return s[:8]


async def build_queue_dashboard(
    *,
    viewer_user_id: int | None,
    is_admin: bool,
    max_tasks: int = 20,
    snapshot_max: int = 900,
) -> QueueDashboard:
    backlog_total = await download_workflow_backlog_count()
    async with AsyncSessionLocal() as session:
        repo = TaskRepository(session)
        tasks = await repo.list_active_tasks_for_queue(
            viewer_user_id=viewer_user_id,
            include_all_users=is_admin,
            limit=max_tasks,
        )

    lines: list[str] = [
        '📥 <b>Очередь загрузок</b>',
        f'Всего на сервере (RabbitMQ + активные в БД): <code>{backlog_total}</code>',
    ]
    if is_admin:
        lines.append('<i>Показаны все активные задачи.</i>')
    else:
        lines.append('<i>Показаны только ваши задачи; общая цифра выше — по всему серверу.</i>')

    if not tasks:
        lines.append('\n<i>Нет активных задач в этом списке.</i>')
        return QueueDashboard(html='\n'.join(lines), backlog_total=backlog_total, listed_tasks=0)

    lines.append('')
    for t in tasks:
        url_disp = escape((t.url or '')[:96])
        snap = (t.progress_snapshot or '').strip()
        if len(snap) > snapshot_max:
            snap = snap[: snapshot_max - 1] + '…'
        if snap:
            snap_block = f'<pre>{escape(snap)}</pre>'
        else:
            snap_block = '<i>пока без детального статуса</i>'
        st = t.status if isinstance(t.status, TaskStatus) else TaskStatus(str(t.status))
        who = ''
        if is_admin and t.from_user_id is not None:
            who = f' · user <code>{t.from_user_id}</code>'
        elif is_admin and t.from_user_id is None:
            who = ' · <code>API</code>'
        block = (
            f'<b>{_short_id(t.id)}</b> · {_status_ru(st)}{who}\n'
            f'<code>{url_disp}</code>\n'
            f'{snap_block}'
        )
        lines.append(block)

    lines.append(
        f'\n<i>Обновление: каждые ~2.5 с (до ~2 мин), или вызовите /queue снова.</i>'
    )
    html = '\n'.join(lines)
    if len(html) > 4000:
        html = html[:3990] + '\n…'
    return QueueDashboard(
        html=html, backlog_total=backlog_total, listed_tasks=len(tasks)
    )
