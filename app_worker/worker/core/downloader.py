import glob
import logging
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.utils import DownloadError
from yt_shared.enums import DownMediaType
from yt_shared.schemas.media import Audio, DownMedia, InbMediaPayload, Video
from yt_shared.utils.common import format_bytes, gen_random_str
from yt_shared.utils.file import file_size, list_files_human, remove_dir

from worker.core.config import settings
from worker.core.exceptions import MediaDownloaderError
from ytdl_opts.per_host._base import AbstractHostConfig

try:
    from ytdl_opts.user import FINAL_AUDIO_FORMAT, FINAL_THUMBNAIL_FORMAT
except ImportError:
    from ytdl_opts.default import FINAL_AUDIO_FORMAT, FINAL_THUMBNAIL_FORMAT

_DEFAULT_MAX_FILESIZE = 10 * 1024 * 1024 * 1024
_STREAMFF_HOSTS = {
    'streamff.com',
    'www.streamff.com',
    'streamff.link',
    'www.streamff.link',
}
_STREAMFF_PATH_RE = re.compile(r'^/v/(?P<share_id>[A-Za-z0-9_-]+)(?:/)?$')
_STREAMFF_CDN_MEDIA_TPL = 'https://cdn.streamff.one/{share_id}.mp4'


def _first_env_proxy() -> str | None:
    for key in (
        'YTDLP_PROXY',
        'ALL_PROXY',
        'HTTPS_PROXY',
        'https_proxy',
        'HTTP_PROXY',
        'http_proxy',
    ):
        raw = (os.environ.get(key) or '').strip()
        if raw:
            return raw
    return None


def _merge_global_ytdl_opts(opts: dict) -> dict:
    out = dict(opts)
    if out.get('proxy') in (None, ''):
        p = _first_env_proxy()
        if p:
            out['proxy'] = p
    if out.get('max_filesize') in (None, 0):
        raw = (os.environ.get('YTDLP_MAX_FILESIZE_BYTES') or '').strip().lower()
        if raw in ('0', 'none', 'unlimited'):
            pass
        elif raw:
            try:
                out['max_filesize'] = int(raw, 10)
            except ValueError:
                out['max_filesize'] = _DEFAULT_MAX_FILESIZE
        else:
            out['max_filesize'] = _DEFAULT_MAX_FILESIZE
    return out


def _resolve_streamff_direct_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.netloc.lower() not in _STREAMFF_HOSTS:
        return url

    match = _STREAMFF_PATH_RE.match(parsed.path or '')
    if match is None:
        return url

    return _STREAMFF_CDN_MEDIA_TPL.format(share_id=match.group('share_id'))


class MediaDownloader:
    _PLAYLIST_TYPE = 'playlist'
    _DESTINATION_TMP_DIR_NAME_LEN = 4
    _KEEP_VIDEO_OPTION = '--keep-video'

    _EXT_TO_NAME: ClassVar[dict[str, str]] = {
        FINAL_AUDIO_FORMAT: 'audio',
        FINAL_THUMBNAIL_FORMAT: 'thumbnail',
    }

    def __init__(self) -> None:
        self._log = logging.getLogger(self.__class__.__name__)
        self._tmp_downloaded_dest_dir = (
            settings.TMP_DOWNLOAD_ROOT_PATH / settings.TMP_DOWNLOADED_DIR
        )

    def download(
        self,
        host_conf: AbstractHostConfig,
        media_payload: InbMediaPayload,
        progress_hook: Callable[[dict], None] | None = None,
    ) -> DownMedia:
        try:
            return self._download(
                host_conf=host_conf,
                media_payload=media_payload,
                progress_hook=progress_hook,
            )
        except Exception:
            self._log.error('Failed to download %s', host_conf.url)
            raise

    def _download(
        self,
        host_conf: AbstractHostConfig,
        media_payload: InbMediaPayload,
        progress_hook: Callable[[dict], None] | None = None,
    ) -> DownMedia:
        media_type = media_payload.download_media_type
        url = host_conf.url
        resolved_url = url
        try:
            resolved_url = _resolve_streamff_direct_url(url)
        except Exception:
            self._log.warning('Failed to resolve streamff direct URL for %s', url)
        if resolved_url != url:
            self._log.info('Resolved %s to direct URL %s', url, resolved_url)
        self._log.info('Downloading %s, media_type %s', url, media_type)
        tmp_down_path = settings.TMP_DOWNLOAD_ROOT_PATH / settings.TMP_DOWNLOAD_DIR
        with TemporaryDirectory(prefix='tmp_media_dir-', dir=tmp_down_path) as tmp_dir:
            curr_tmp_dir = tmp_down_path / tmp_dir

            ytdl_opts_model = host_conf.build_config(
                media_type=media_type, curr_tmp_dir=curr_tmp_dir
            )

            opts = _merge_global_ytdl_opts(dict(ytdl_opts_model.ytdl_opts))
            hooks = list(opts.get('progress_hooks') or [])
            if progress_hook:
                hooks.append(progress_hook)
            opts['progress_hooks'] = hooks

            with yt_dlp.YoutubeDL(opts) as ytdl:
                self._log.info('Downloading "%s" to "%s"', resolved_url, curr_tmp_dir)
                self._log.info('Downloading with options: %s', opts)

                try:
                    meta: dict | None = ytdl.extract_info(resolved_url, download=True)
                except DownloadError as err:
                    self._log.error(
                        'yt-dlp DownloadError for %s (resolved=%s): %s',
                        url,
                        resolved_url,
                        err,
                    )
                    raise MediaDownloaderError(str(err)) from err
                if not meta:
                    err_msg = 'Error during media download. Check logs.'
                    self._log.error('%s. Meta: %s', err_msg, meta)
                    raise MediaDownloaderError(err_msg)

                current_files = list(curr_tmp_dir.iterdir())
                if not current_files:
                    err_msg = 'Nothing downloaded. Is URL valid?'
                    self._log.error(err_msg)
                    raise MediaDownloaderError(err_msg)

                meta_sanitized = ytdl.sanitize_info(meta)

            self._log.info('Finished downloading %s', url)
            self._log.debug('Downloaded "%s" meta: %s', url, meta_sanitized)
            self._log.info(
                'Content of "%s": %s', curr_tmp_dir, list_files_human(curr_tmp_dir)
            )

            destination_dir = self._tmp_downloaded_dest_dir / gen_random_str(
                length=self._DESTINATION_TMP_DIR_NAME_LEN
            )
            destination_dir.mkdir()

            audio, video = self._create_media_dtos(
                media_type=media_type,
                meta=meta,
                curr_tmp_dir=curr_tmp_dir,
                destination_dir=destination_dir,
                custom_video_filename=media_payload.custom_filename,
            )
            self._log.info(
                'Removing temporary download directory "%s" with leftover files %s',
                curr_tmp_dir,
                list_files_human(curr_tmp_dir),
            )

        return DownMedia(
            media_type=media_type,
            audio=audio,
            video=video,
            meta=meta_sanitized,
            root_path=destination_dir,
        )

    def _create_media_dtos(
        self,
        media_type: DownMediaType,
        meta: dict,
        curr_tmp_dir: str,
        destination_dir: str,
        custom_video_filename: str | None = None,
    ) -> tuple[Audio | None, Video | None]:
        def get_audio() -> Audio:
            return create_dto(self._create_audio_dto)

        def get_video() -> Video:
            return create_dto(self._create_video_dto)

        def create_dto(
            func: Callable[[dict, str, str, str | None], Audio | Video],
        ) -> Audio | Video:
            try:
                return func(meta, curr_tmp_dir, destination_dir, custom_video_filename)
            except Exception:
                remove_dir(destination_dir)
                raise

        match media_type:
            case DownMediaType.AUDIO:
                return get_audio(), None
            case DownMediaType.VIDEO:
                return None, get_video()
            case DownMediaType.AUDIO_VIDEO:
                return get_audio(), get_video()
            case _:
                raise RuntimeError(f'Unknown media type "{media_type}"')

    def _create_video_dto(
        self,
        meta: dict,
        curr_tmp_dir: Path,
        destination_dir: Path,
        custom_video_filename: str | None = None,
    ) -> Video:
        video_filename = self._get_video_filename(meta)
        video_filepath = curr_tmp_dir / video_filename

        if custom_video_filename:
            dest_path = destination_dir / custom_video_filename
        else:
            dest_path = destination_dir / video_filename

        self._log.info('Moving "%s" to "%s"', video_filepath, dest_path)
        shutil.move(video_filepath, dest_path)

        thumb_path: Path | None = None
        thumb_name = self._find_downloaded_file(
            root_path=curr_tmp_dir, extension=FINAL_THUMBNAIL_FORMAT
        )
        if thumb_name:
            _thumb_path = curr_tmp_dir / thumb_name
            shutil.move(_thumb_path, destination_dir)
            thumb_path = destination_dir / thumb_name

        duration, width, height = self._get_video_context(meta)
        return Video(
            title=meta['title'],
            original_filename=video_filename,
            custom_filename=custom_video_filename,
            duration=duration,
            width=width,
            height=height,
            directory_path=destination_dir,
            file_size=file_size(dest_path),
            thumb_path=thumb_path,
            thumb_name=thumb_name,
        )

    def _create_audio_dto(
        self,
        meta: dict,
        curr_tmp_dir: Path,
        destination_dir: Path,
        custom_video_filename: str | None = None,  # noqa: ARG002 # TODO: Make for audio.
    ) -> Audio:
        audio_filename = self._find_downloaded_file(
            root_path=curr_tmp_dir, extension=FINAL_AUDIO_FORMAT
        )
        audio_filepath = curr_tmp_dir / audio_filename
        self._log.info('Moving "%s" to "%s"', audio_filepath, destination_dir)
        shutil.move(audio_filepath, destination_dir)
        return Audio(
            title=meta['title'],
            original_filename=audio_filename,
            duration=None,
            directory_path=destination_dir,
            file_size=file_size(destination_dir / audio_filename),
        )

    def _find_downloaded_file(self, root_path: Path, extension: str) -> str | None:
        """Try to find downloaded audio or thumbnail file."""
        verbose_name = self._EXT_TO_NAME[extension]
        for file_name in glob.glob(f'*.{extension}', root_dir=root_path):  # noqa: PTH207
            self._log.info(
                'Found downloaded %s: "%s" [%s]',
                verbose_name,
                file_name,
                format_bytes(file_size(root_path / file_name)),
            )
            return file_name
        self._log.info('Downloaded %s not found in "%s"', verbose_name, root_path)
        return None

    def _get_video_context(
        self, meta: dict
    ) -> tuple[float | None, int | float | None, int | float | None]:
        if meta['_type'] == self._PLAYLIST_TYPE:
            if not len(meta['entries']):
                raise ValueError(
                    'Item said to be downloaded but no entries to process.'
                )
            entry: dict = meta['entries'][0]
            requested_video = self._get_requested_video(entry['requested_downloads'])
            return (
                self._to_float(entry.get('duration')),
                requested_video.get('width'),
                requested_video.get('height'),
            )
        requested_video = self._get_requested_video(meta['requested_downloads'])
        return (
            self._to_float(meta.get('duration')),
            requested_video.get('width'),
            requested_video.get('height'),
        )

    def _get_requested_video(self, requested_downloads: list[dict]) -> dict | None:
        for download_obj in requested_downloads:
            if download_obj.get('ext', '') != FINAL_AUDIO_FORMAT:
                # Attempt to handle yt-dlp glitch.
                download_obj['filepath'] = download_obj.get(
                    'filepath', download_obj.get('filename', download_obj['_filename'])
                )
                return download_obj

        # When video was converted to audio but video kept.
        for download_obj in requested_downloads:
            if download_obj['ext'] != download_obj['_filename'].rsplit('.', 1)[-1]:
                download_obj_copy = download_obj.copy()
                self._log.info(
                    'Replacing video path in meta "%s" with "%s"',
                    download_obj_copy['filepath'],
                    download_obj_copy['_filename'],
                )
                download_obj_copy['filepath'] = download_obj_copy.get(
                    'filename', download_obj_copy['_filename']
                )
                return download_obj_copy
        return None

    @staticmethod
    def _to_float(duration: float | None) -> float | None:
        try:
            return float(duration)
        except TypeError:
            return duration

    def _get_video_filename(self, meta: dict) -> str:
        return self._get_video_filepath(meta).rsplit('/', maxsplit=1)[-1]

    def _get_video_filepath(self, meta: dict) -> str:
        if meta['_type'] == self._PLAYLIST_TYPE:
            requested_downloads: list[dict] = meta['entries'][0]['requested_downloads']
            requested_video = self._get_requested_video(requested_downloads)
        else:
            requested_downloads = meta['requested_downloads']
            requested_video = self._get_requested_video(requested_downloads)

        try:
            return requested_video['filepath']
        except (AttributeError, KeyError):
            err_msg = 'Video filepath not found'
            self._log.exception('%s, meta: %s', err_msg, meta)
            raise ValueError(err_msg) from None
