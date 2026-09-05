"""Utility package for RB-DAT Converter.

Contains FFmpeg integration and media conversion helpers.
"""

from .converter import (
    FFmpegError,
    find_ffmpeg,
    find_ffprobe,
    probe_media,
    convert_dat_to_mp4,
)

__all__ = [
    "FFmpegError",
    "find_ffmpeg",
    "find_ffprobe",
    "probe_media",
    "convert_dat_to_mp4",
]
