import asyncio
import logging
import shutil
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from yt_shared.enums import DownMediaType, TaskStatus
from yt_shared.models import Task
from yt_shared.rabbit.publisher import RmqPublisher
from yt_shared.repositories.task import TaskRepository
from yt_shared.schemas.media import BaseMedia, DownMedia, InbMediaPayload, Video
from yt_shared.schemas.progress import DownloadProgressPayload
from yt_shared.utils.common import gen_random_str
from yt_shared.utils.file import remove_dir
from yt_shared.utils.tasks.tasks import create_task

from worker.core.config import settings
from worker.core.downloader import MediaDownloader
from worker.core.exceptions import DownloadVideoServiceError
from worker.core.tasks.encode import EncodeToH264Task
from worker.core.tasks.ffprobe_context import GetFfprobeContextTask
from worker.core.tasks.thumbnail import MakeThumbnailTask
from ytdl_opts.per_host._base import AbstractHostConfig
from ytdl_opts.per_host._registry import HostConfRegistry


class MediaService:
    _DB_PROGRESS_MIN_INTERVAL: float = 0.5

    def __init__(
        self,
        media_payload: InbMediaPayload,
        downloader: MediaDownloader,
        task_repository: TaskRepository,
    ) -> None:
        self._log = logging.getLogger(self.__class__.__name__)
        self._downloader = downloader
        self._repository = task_repository
        self._media_payload = media_payload

        self._task: Task | None = None
        self._phase_labels: list[str] = []
        self._current_phase_label: str | None = None
        self._ytdlp_tail: str = ''
        self._ytdlp_session_summary: str | None = None
        self._download_t0: float | None = None
        self._last_db_progress_write: float = 0.0
        self._publish_user_progress: bool = True

    async def process(self) -> tuple[DownMedia | None, Task | None]:
        self._task = await self._repository.get_or_create_task(self._media_payload)
        if self._task.status != TaskStatus.PENDING.value:
            return None, None
        return (await self._process(), self._task)

    async def _process(self) -> DownMedia:
        host_conf = self._get_host_conf()
        await self._repository.save_as_processing(self._task)
        await self._phase('⏳ Подключение к источнику, подготовка к скачиванию…')
        media = await self._start_download(host_conf=host_conf)
        await self._phase('✅ Файл от yt-dlp получен, дальше обработка на сервере…')
        self._publish_user_progress = False
        try:
            await self._post_process_media(media=media, host_conf=host_conf)
        except Exception:
            self._log.exception('Failed to post-process media %s', media)
            self._err_file_cleanup(media)
            raise
        return media

    def _get_host_conf(self) -> AbstractHostConfig:
        url = self._task.url
        host_to_cls_map = HostConfRegistry.get_host_to_cls_map()
        host_cls = host_to_cls_map.get(urlsplit(url).netloc, host_to_cls_map[None])
        return host_cls(url=url)

    @staticmethod
    def _format_session_duration(seconds: float) -> str:
        """Human-readable total duration (Russian)."""
        if seconds < 0 or seconds != seconds:
            return '—'
        rounded = max(0.0, round(seconds * 5) / 5)
        if rounded < 60:
            return f'{rounded:.1f} с'
        total = int(round(rounded))
        m, sec = divmod(total, 60)
        if m < 60:
            return f'{m} м {sec} с' if sec else f'{m} м'
        h, m = divmod(m, 60)
        parts: list[str] = [f'{h} ч']
        if m:
            parts.append(f'{m} м')
        if sec:
            parts.append(f'{sec} с')
        return ' '.join(parts)

    @staticmethod
    def _ytdlp_stream_role(d: dict) -> str:
        """Rough label for which DASH/stream fragment yt-dlp is writing (RU)."""
        fn = str(d.get('filename') or d.get('tmpfilename') or '').lower()
        if not fn:
            return 'данные'
        base = fn.rsplit('/', maxsplit=1)[-1]
        if '.f' in base:
            if any(
                base.endswith(ext)
                for ext in ('.m4a', '.opus', '.aac', '.mp3', '.oga', '.webm')
            ):
                if base.endswith('.webm') and 'dash' in fn:
                    return 'видеодорожка'
                return 'аудиодорожка'
            if any(base.endswith(ext) for ext in ('.mp4', '.mkv', '.mov', '.webm')):
                return 'видеодорожка'
        return 'поток'

    @staticmethod
    def _format_ytdlp_progress_line(d: dict) -> str | None:
        st = d.get('status')
        if st == 'finished':
            return 'Файл собран, слияние / запись на диск…'
        if st == 'extracting':
            return 'Получение метаданных…'
        if st in ('postprocessing', 'processing'):
            return 'Постобработка (ffmpeg)…'
        if st != 'downloading':
            return None
        role = MediaService._ytdlp_stream_role(d)
        parts: list[str] = []
        if p := d.get('_percent_str'):
            parts.append(p.strip())
        if s := d.get('_speed_str'):
            parts.append(f'⚡ {s.strip()}')
        if e := d.get('_eta_str'):
            parts.append(f'ETA {e.strip()}')
        tail = ' · '.join(parts) if parts else 'идёт приём данных…'
        return f'Скачивание ({role}): {tail}'

    def _bump_ytdlp_line(self, new_line: str) -> None:
        """Keep a single live yt-dlp line (no history per percent tick)."""
        if new_line == self._ytdlp_tail:
            return
        self._ytdlp_tail = new_line

    def _finalize_ytdlp_section(self) -> None:
        self._ytdlp_tail = ''
        if self._download_t0 is not None:
            elapsed = time.monotonic() - self._download_t0
            self._ytdlp_session_summary = (
                f'Скачивание (yt-dlp), всего: {self._format_session_duration(elapsed)}'
            )
            self._download_t0 = None

    def _compose_progress_body(self) -> str:
        lines = list(self._phase_labels)
        if self._current_phase_label:
            lines.append(self._current_phase_label)
        body = '\n'.join(lines)
        y_parts: list[str] = []
        if self._ytdlp_session_summary:
            y_parts.append(self._ytdlp_session_summary)
        if self._ytdlp_tail:
            y_parts.append(self._ytdlp_tail)
        if y_parts:
            yblock = '\n'.join(y_parts)
            body = (
                f'{body}\n── yt-dlp ──\n{yblock}' if body else f'── yt-dlp ──\n{yblock}'
            )
        return body

    async def _send_progress_line(self, line: str) -> None:
        if self._media_payload.ack_message_id is None:
            return
        if self._media_payload.from_chat_id is None:
            return
        if self._publish_user_progress:
            publisher = RmqPublisher()
            payload = DownloadProgressPayload(
                task_id=self._task.id,
                from_chat_id=self._media_payload.from_chat_id,
                ack_message_id=self._media_payload.ack_message_id,
                url=self._media_payload.url[:512],
                line=line[:4000],
            )
            await publisher.send_download_progress(payload)
        now = time.monotonic()
        if now - self._last_db_progress_write >= self._DB_PROGRESS_MIN_INTERVAL:
            self._last_db_progress_write = now
            try:
                await self._repository.update_task_progress_snapshot(self._task.id, line)
            except Exception:
                self._log.debug('progress_snapshot DB update failed', exc_info=True)

    async def _snapshot_progress_to_user(self) -> None:
        """Push full timeline + latest yt-dlp line to Telegram (HTML <pre> on bot side)."""
        await self._send_progress_line(self._compose_progress_body())

    async def _phase(self, step: str) -> None:
        if self._current_phase_label is not None:
            self._phase_labels.append(self._current_phase_label)
            if 'Файл от yt-dlp получен' in self._current_phase_label:
                self._ytdlp_session_summary = None
        self._current_phase_label = step
        self._log.info('Progress phase: %s', step)
        await self._snapshot_progress_to_user()

    async def _finalize_current_phase(self) -> None:
        if self._current_phase_label is None:
            return
        self._phase_labels.append(self._current_phase_label)
        self._current_phase_label = None
        await self._snapshot_progress_to_user()

    async def _start_download(self, host_conf: AbstractHostConfig) -> DownMedia:
        loop = asyncio.get_running_loop()
        last_ts = [0.0]

        def progress_hook(d: dict) -> None:
            try:
                line = MediaService._format_ytdlp_progress_line(d)
                if not line:
                    return
                self._bump_ytdlp_line(line)
                now = time.monotonic()
                if now - last_ts[0] < 0.9:
                    return
                last_ts[0] = now
                fut = asyncio.run_coroutine_threadsafe(
                    self._snapshot_progress_to_user(),
                    loop,
                )

                def _done(f: asyncio.Future) -> None:
                    try:
                        exc = f.exception()
                    except asyncio.CancelledError:
                        return
                    if exc:
                        self._log.debug('progress publish failed: %s', exc)

                fut.add_done_callback(_done)
            except Exception:
                return

        self._download_t0 = time.monotonic()
        try:
            media = await loop.run_in_executor(
                None,
                lambda: self._downloader.download(
                    host_conf=host_conf,
                    media_payload=self._media_payload,
                    progress_hook=progress_hook,
                ),
            )
        except Exception as err:
            self._log.exception(
                'Failed to download media. Context: %s', self._media_payload
            )
            await self._handle_download_exception(err)
            raise DownloadVideoServiceError(message=str(err), task=self._task) from None
        self._finalize_ytdlp_section()
        return media

    async def _post_process_media(
        self, media: DownMedia, host_conf: AbstractHostConfig
    ) -> None:
        def post_process_audio() -> Coroutine[Any, Any, None]:
            return self._post_process_audio(media=media, host_conf=host_conf)

        def post_process_video() -> Coroutine[Any, Any, None]:
            return self._post_process_video(media=media, host_conf=host_conf)

        match media.media_type:
            case DownMediaType.AUDIO:
                await post_process_audio()
            case DownMediaType.VIDEO:
                await post_process_video()
            case DownMediaType.AUDIO_VIDEO:
                await asyncio.gather(*(post_process_audio(), post_process_video()))

        await self._finalize_current_phase()
        await self._repository.save_as_done(self._task)

    async def _post_process_video(
        self, media: DownMedia, host_conf: AbstractHostConfig
    ) -> None:
        """Post-process downloaded media files, e.g. make thumbnail and copy to storage."""
        video = media.video
        await self._phase('📋 Проверка метаданных (длительность, разрешение)…')
        # yt-dlp's 'info-meta' may not contain all needed video metadata.
        if not all([video.duration, video.height, video.width]):
            # TODO: Move to higher level and re-raise as DownloadVideoServiceError with task,
            # TODO: or create new exception type.
            try:
                await self._set_probe_ctx(video)
            except RuntimeError as err:
                raise DownloadVideoServiceError(
                    message=str(err), task=self._task
                ) from None

        video_ar = video.aspect_ratio
        thumb_ar = video.thumb_aspect_ratio
        if not video.thumb_path or all([video_ar, thumb_ar, video_ar != thumb_ar]):
            await self._phase('🖼 Делаю превью (кадр из видео)…')
            thumb_path = Path(media.root_path) / Path(video.thumb_name)
            await MakeThumbnailTask(
                thumb_path,
                video.current_filepath,
                duration=video.duration,
                video_ctx=video,
            ).run()

        if self._media_payload.save_to_storage:
            await self._phase('📁 Копирование в постоянное хранилище…')
            await self._copy_file_to_storage(video)

        if host_conf.ENCODE_VIDEO:
            await self._phase('🎞 Конвертация в H.264 для Telegram (короткий проход)…')
            await EncodeToH264Task(
                media=media, cmd_tpl=host_conf.FFMPEG_VIDEO_OPTS
            ).run()
        else:
            await self._phase('🎞 Перекодирование не требуется (уже совместимый поток)…')

        await self._phase('💾 Сохранение в базу и финализация…')
        file = await self._repository.save_file(self._task, media.video, media.meta)
        video.orm_file_id = file.id

    async def _post_process_audio(
        self,
        media: DownMedia,
        host_conf: AbstractHostConfig,  # noqa: ARG002
    ) -> None:
        coro_tasks = [self._repository.save_file(self._task, media.audio, media.meta)]
        if self._media_payload.save_to_storage:
            coro_tasks.append(self._create_copy_file_task(media.audio))
        results = await asyncio.gather(*coro_tasks)
        file = results[0]
        media.audio.orm_file_id = file.id

    async def _set_probe_ctx(self, video: Video) -> None:
        probe_ctx = await GetFfprobeContextTask(video.current_filepath).run()
        if not probe_ctx:
            return

        video_streams = [s for s in probe_ctx['streams'] if s['codec_type'] == 'video']
        video.duration = float(probe_ctx['format']['duration'])
        if not video_streams:
            self._log.warning(
                'Video file does not contain video stream. Might be only audio. '
                'Ffprobe context: %s',
                probe_ctx,
            )
            return

        video.width = video_streams[0]['width']
        video.height = video_streams[0]['height']

    def _create_copy_file_task(self, file: BaseMedia) -> asyncio.Task:
        task_name = f'Copy {file.file_type} file to storage task'
        return create_task(
            self._copy_file_to_storage(file),
            task_name=task_name,
            logger=self._log,
            exception_message='Task "%s" raised an exception',
            exception_message_args=(task_name,),
        )

    def _create_thumb_task(
        self, file_path: Path, thumb_path: Path, duration: float, video_ctx: Video
    ) -> asyncio.Task:
        return create_task(
            MakeThumbnailTask(
                thumb_path, file_path, duration=duration, video_ctx=video_ctx
            ).run(),
            task_name=MakeThumbnailTask.__class__.__name__,
            logger=self._log,
            exception_message='Task "%s" raised an exception',
            exception_message_args=(MakeThumbnailTask.__class__.__name__,),
        )

    async def _copy_file_to_storage(self, file: BaseMedia) -> None:
        dst = settings.STORAGE_PATH / file.current_filename
        if dst.is_file():
            self._log.warning('Destination file in storage already exists: %s', dst)
            dst = (
                dst.parent
                / f'{dst.stem}-{int(time.time())}-{gen_random_str()}{dst.suffix}'
            )
            self._log.warning('Adding current timestamp to filename: %s', dst)

        self._log.info('Copying "%s" to storage "%s"', file.current_filepath, dst)
        await asyncio.to_thread(shutil.copy2, file.current_filepath, dst)
        file.mark_as_saved_to_storage(storage_path=dst)

    def _err_file_cleanup(self, video: DownMedia) -> None:
        """Cleanup any downloaded/created data if post-processing failed."""
        self._log.info('Performing error cleanup: removing %s', video.root_path)
        remove_dir(video.root_path)

    async def _handle_download_exception(self, err: Exception) -> None:
        await self._repository.save_as_failed(task=self._task, error_message=str(err))
