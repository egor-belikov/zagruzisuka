"""RabbitMQ input queue depth (ready messages)."""

from yt_shared.rabbit import get_rabbitmq
from yt_shared.rabbit.rabbit_config import INPUT_QUEUE


async def input_queue_ready_count() -> int:
    """Return number of messages waiting in the download queue."""
    rmq = get_rabbitmq()
    queue = await rmq.channel.declare_queue(INPUT_QUEUE, passive=True)
    return int(queue.declaration_result.message_count)
