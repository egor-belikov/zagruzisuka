from pathlib import Path
from typing import Final

from yt_shared.constants import VEED_HOSTS
from yt_shared.enums import DownMediaType

from ytdl_opts.per_host._base import AbstractHostConfig, BaseHostConfModel
from ytdl_opts.per_host._registry import HostConfRegistry

# VEED: page URL is resolved to CDN MP4 in worker.veed_resolver before yt-dlp download.
_VEED_VIDEO_YTDL_OPTS: Final[tuple[str, ...]] = (
    '--format',
    'best[ext=mp4]/best',
    '--write-thumbnail',
    '--convert-thumbnails',
    'jpg',
)


class VeedHostModel(BaseHostConfModel):
    pass


class VeedHost(AbstractHostConfig, metaclass=HostConfRegistry):
    ALLOW_NULL_HOSTNAMES = False
    HOSTNAMES = VEED_HOSTS
    ENCODE_AUDIO = False
    ENCODE_VIDEO = False
    DEFAULT_VIDEO_YTDL_OPTS: tuple[str, ...] = _VEED_VIDEO_YTDL_OPTS

    def build_config(
        self, media_type: DownMediaType, curr_tmp_dir: Path
    ) -> VeedHostModel:
        return VeedHostModel(
            hostnames=self.HOSTNAMES,
            encode_audio=self.ENCODE_AUDIO,
            encode_video=self.ENCODE_VIDEO,
            ffmpeg_audio_opts=self.FFMPEG_AUDIO_OPTS,
            ffmpeg_video_opts=self.FFMPEG_VIDEO_OPTS,
            ytdl_opts=self._build_ytdl_opts(media_type, curr_tmp_dir),
        )

    def _build_custom_ytdl_video_opts(self) -> tuple[str, ...]:
        return self.DEFAULT_VIDEO_FORMAT_SORT_OPT
