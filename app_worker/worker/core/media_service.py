import asyncio
import logging
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from yt_shared.enums import DownMediaType, TaskStatus
from yt_shared.models import Task
from yt_shared.rabbit.publisher import RmqPublisher
from yt_shared.repositories.task import TaskRepository
from yt_shared.schemas.media import DownMedia, InbMediaPayload, Video
from yt_shared.schemas.progress import DownloadProgressPayload
from yt_shared.utils.common import gen_random_str
from yt_shared.utils.file import remove_dir
from yt_shared.utils.tasks.tasks import create_task

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
        self._phase_started: float = 0.0
        self._ytdlp_tail: str = ''
        self._ytdlp_session_summary: str | None = None
        self._ytdlp_segment_lines: list[str] = []
        self._ytdlp_segment_kind: str | None = None
        self._ytdlp_segment_started: float = 0.0
        self._download_t0: float | None = None
        self._last_db_progress_write: float = 0.0
        self._publish_user_progress: bool = True
        self._final_progress_transcript: str = ''

    async def process(self) -> tuple[DownMedia | None, Task | None, str]:
        self._task = await self._repository.get_or_create_task(self._media_payload)
        if self._task.status != TaskStatus.PENDING.value:
            return None, None, ''
        media = await self._process()
        return media, self._task, self._final_progress_transcript

    async def _process(self) -> DownMedia:
        host_conf = self._get_host_conf()
        await self._repository.save_as_processing(self._task)
        await self._phase('⏳ Подключение к источнику, подготовка к скачиванию…')
        media = await self._start_download(host_conf=host_conf)
        await self._phase('✅ Файл от yt-dlp получен, дальше обработка на сервере…')
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
    def _format_phase_duration(seconds: float) -> str:
        rounded = round(seconds * 5) / 5
        return f'{rounded:.1f} с'

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

    @staticmethod
    def _normalize_ytdlp_kind(role_label: str) -> str | None:
        if 'видео' in role_label:
            return 'video'
        if 'аудио' in role_label:
            return 'audio'
        return None

    def _sync_close_ytdlp_segment(self) -> None:
        if self._ytdlp_segment_kind is None or self._ytdlp_segment_started <= 0:
            self._ytdlp_segment_kind = None
            self._ytdlp_segment_started = 0.0
            return
        elapsed = time.monotonic() - self._ytdlp_segment_started
        label = (
            'видеодорожки'
            if self._ytdlp_segment_kind == 'video'
            else 'аудиодорожки'
        )
        self._ytdlp_segment_lines.append(
            f'Скачивание {label}: {self._format_phase_duration(elapsed)}'
        )
        self._ytdlp_segment_kind = None
        self._ytdlp_segment_started = 0.0

    def _sync_ensure_ytdlp_segment(self, kind: str) -> bool:
        """Start or switch DASH segment timer; returns True if segment boundary crossed."""
        if kind == self._ytdlp_segment_kind:
            return False
        self._sync_close_ytdlp_segment()
        self._ytdlp_segment_kind = kind
        self._ytdlp_segment_started = time.monotonic()
        return True

    def _finalize_ytdlp_section(self) -> None:
        self._sync_close_ytdlp_segment()
        self._ytdlp_tail = ''
        if not self._ytdlp_segment_lines and self._download_t0 is not None:
            elapsed = time.monotonic() - self._download_t0
            self._ytdlp_session_summary = (
                f'Скачивание (yt-dlp), всего: {self._format_session_duration(elapsed)}'
            )
        elif self._ytdlp_segment_lines:
            self._ytdlp_session_summary = None
        self._download_t0 = None

    def _compose_progress_body(self) -> str:
        """Console-like order: pre-yt-dlp phases, yt-dlp (video/audio), then post phases."""
        marker = 'Файл от yt-dlp получен'
        split_idx = None
        for i, lab in enumerate(self._phase_labels):
            if marker in lab:
                split_idx = i
                break
        if split_idx is None:
            pre = list(self._phase_labels)
            post: list[str] = []
        else:
            pre = self._phase_labels[:split_idx]
            post = self._phase_labels[split_idx:]

        y_parts: list[str] = []
        y_parts.extend(self._ytdlp_segment_lines)
        if self._ytdlp_session_summary:
            y_parts.append(self._ytdlp_session_summary)
        if self._ytdlp_tail:
            y_parts.append(self._ytdlp_tail)
        yblock = '\n'.join(y_parts)

        bottom: list[str] = list(post)
        if self._current_phase_label:
            bottom.append(self._current_phase_label)

        sections: list[str] = []
        if s := '\n'.join(pre).strip():
            sections.append(s)
        if s := yblock.strip():
            sections.append(s)
        if s := '\n'.join(bottom).strip():
            sections.append(s)
        return '\n'.join(sections)

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
                pipeline_log_message_id=self._media_payload.pipeline_log_message_id,
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
        now = time.monotonic()
        if self._current_phase_label is not None:
            dur = now - self._phase_started
            self._phase_labels.append(
                f'{self._current_phase_label} ({self._format_phase_duration(dur)})'
            )
            if 'Файл от yt-dlp получен' in self._current_phase_label:
                self._ytdlp_session_summary = None
        self._current_phase_label = step
        self._phase_started = now
        self._log.info('Progress phase: %s', step)
        await self._snapshot_progress_to_user()

    async def _finalize_current_phase(self) -> None:
        if self._current_phase_label is None:
            return
        now = time.monotonic()
        dur = now - self._phase_started
        self._phase_labels.append(
            f'{self._current_phase_label} ({self._format_phase_duration(dur)})'
        )
        self._current_phase_label = None
        await self._snapshot_progress_to_user()

    async def _start_download(self, host_conf: AbstractHostConfig) -> DownMedia:
        await self._finalize_current_phase()
        loop = asyncio.get_running_loop()
        last_ts = [0.0]

        def _schedule_snapshot(*, force: bool) -> None:
            now = time.monotonic()
            if not force and now - last_ts[0] < 0.9:
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

        def progress_hook(d: dict) -> None:
            try:
                st = d.get('status')
                if st in ('finished', 'postprocessing', 'processing'):
                    self._sync_close_ytdlp_segment()
                    line = MediaService._format_ytdlp_progress_line(d)
                    if line:
                        self._bump_ytdlp_line(line)
                    _schedule_snapshot(force=True)
                    return
                if st == 'downloading':
                    role_label = MediaService._ytdlp_stream_role(d)
                    kind = MediaService._normalize_ytdlp_kind(role_label)
                    if kind:
                        boundary = self._sync_ensure_ytdlp_segment(kind)
                    else:
                        boundary = False
                    line = MediaService._format_ytdlp_progress_line(d)
                    if not line:
                        return
                    self._bump_ytdlp_line(line)
                    _schedule_snapshot(force=boundary)
                    return
                line = MediaService._format_ytdlp_progress_line(d)
                if not line:
                    return
                self._bump_ytdlp_line(line)
                _schedule_snapshot(force=False)
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
        await self._snapshot_progress_to_user()
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
        self._final_progress_transcript = self._compose_progress_body()
        await self._repository.save_as_done(self._task)
        self._publish_user_progress = False

    async def _post_process_video(
        self, media: DownMedia, host_conf: AbstractHostConfig
    ) -> None:
        """Post-process downloaded media files, e.g. make thumbnail."""
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
        file = await self._repository.save_file(self._task, media.audio, media.meta)
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

    def _err_file_cleanup(self, video: DownMedia) -> None:
        """Cleanup any downloaded/created data if post-processing failed."""
        self._log.info('Performing error cleanup: removing %s', video.root_path)
        remove_dir(video.root_path)

    async def _handle_download_exception(self, err: Exception) -> None:
        await self._repository.save_as_failed(task=self._task, error_message=str(err))
