"""FFmpeg integration module for RB-DAT Converter.

Responsibilities:
  * Locate FFmpeg and ffprobe binaries (PATH or env vars).
  * Probe media files to detect format and duration.
  * Convert .dat video files to real .mp4 files using subprocess.
  * Report real progress from FFmpeg's machine-readable progress output.
  * Never use shell=True. Never pass user input into a shell.

Security:
  * All subprocess calls use argument lists (no shell).
  * File paths are validated by the caller (app.py) before reaching here.
  * No arbitrary FFmpeg arguments are accepted from users.
"""

import os
import re
import json
import shutil
import subprocess
import threading


class FFmpegError(Exception):
    """Raised when FFmpeg/ffprobe cannot complete the requested operation."""
    pass


# --------------------------------------------------------------------------
# Binary discovery
# --------------------------------------------------------------------------

def _which(name):
    """Locate an executable.

    Resolution order:
      1. Environment variable <NAME>_PATH (e.g. FFMPEG_PATH).
      2. shutil.which() (searches PATH).
    """
    env_var = f"{name.upper()}_PATH"
    env_val = os.environ.get(env_var)
    if env_val and os.path.isfile(env_val) and os.access(env_val, os.X_OK):
        return env_val
    return shutil.which(name)


def find_ffmpeg():
    """Return the path to the ffmpeg binary, or None if not found."""
    return _which("ffmpeg")


def find_ffprobe():
    """Return the path to the ffprobe binary, or None if not found."""
    return _which("ffprobe")


# --------------------------------------------------------------------------
# Media probing
# --------------------------------------------------------------------------

def probe_media(input_path):
    """Inspect a media file with ffprobe.

    Returns a dict with at least:
        duration: float|None   (seconds, or None if unknown)
        format:   str          (short format name, e.g. "mpeg")
        format_long_name: str
        bit_rate: str|None

    Raises FFmpegError if ffprobe is unavailable or fails.
    """
    ffprobe = find_ffprobe()
    if not ffprobe:
        raise FFmpegError(
            "ffprobe is not installed or not on PATH. "
            "The server cannot inspect media files without it."
        )

    if not os.path.isfile(input_path):
        raise FFmpegError("Input file does not exist.")

    cmd = [
        ffprobe,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        input_path,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise FFmpegError(
            "Media inspection timed out. The file may be very large or corrupted."
        )
    except Exception as exc:
        raise FFmpegError(f"Failed to run ffprobe: {exc}")

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if not stderr:
            stderr = "ffprobe could not read the file."
        raise FFmpegError(f"ffprobe failed: {stderr}")

    try:
        info = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        raise FFmpegError("ffprobe returned unparseable output.")

    fmt = info.get("format", {}) or {}

    duration = None
    raw_duration = fmt.get("duration")
    if raw_duration is not None:
        try:
            duration = float(raw_duration)
            if duration <= 0:
                duration = None
        except (TypeError, ValueError):
            duration = None

    return {
        "duration": duration,
        "format": fmt.get("format_name", "unknown"),
        "format_long_name": fmt.get("format_long_name", "unknown"),
        "bit_rate": fmt.get("bit_rate"),
    }


# --------------------------------------------------------------------------
# Conversion
# --------------------------------------------------------------------------

def convert_dat_to_mp4(input_path, output_path, progress_callback=None):
    """Convert a .dat file to a real .mp4 file.

    Strategy:
      1. Probe the file to learn its duration (for progress reporting).
      2. Attempt stream copy (``-c copy``) into an MP4 container. This is
         fast and lossless when the source codecs are MP4-compatible.
      3. If stream copy fails, fall back to re-encoding with libx264 + AAC.
         This handles MPEG-1 / MPEG-2 / VCD / SVCD sources whose codecs are
         not allowed inside an MP4 container.

    The output is always a genuine MP4 container (``-movflags +faststart``
    moves the moov atom to the front for progressive web playback).

    Args:
        input_path:  Absolute path to the source .dat file.
        output_path: Absolute path to the destination .mp4 file.
        progress_callback: Optional callable(percent:int, message:str).

    Returns:
        dict with ``method`` = "stream_copy" | "reencode" and ``success`` = True.

    Raises:
        FFmpegError on any failure.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise FFmpegError(
            "FFmpeg is not installed or not on PATH. "
            "Conversion is not possible without it."
        )

    if not os.path.isfile(input_path):
        raise FFmpegError("Input file does not exist.")

    # Probe for duration (best-effort; not fatal if it fails).
    duration = None
    try:
        probe = probe_media(input_path)
        duration = probe.get("duration")
    except FFmpegError:
        pass

    # Attempt 1: stream copy (fast, lossless).
    try:
        _run_ffmpeg(
            ffmpeg, input_path, output_path,
            duration=duration,
            progress_callback=progress_callback,
            reencode=False,
        )
        if os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
            return {"method": "stream_copy", "success": True}
        raise FFmpegError("Stream copy produced an empty or missing output file.")
    except FFmpegError as copy_error:
        # Clean any partial output before retrying.
        _safe_remove(output_path)

        # Attempt 2: re-encode.
        try:
            _run_ffmpeg(
                ffmpeg, input_path, output_path,
                duration=duration,
                progress_callback=progress_callback,
                reencode=True,
            )
        except FFmpegError:
            # Re-encoding genuinely failed — surface the real error.
            _safe_remove(output_path)
            raise

        if os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
            return {"method": "reencode", "success": True}

        _safe_remove(output_path)
        raise FFmpegError(
            "Conversion failed: the output file was not created. "
            f"Stream-copy error was: {copy_error}"
        )


def _run_ffmpeg(ffmpeg, input_path, output_path, duration, progress_callback, reencode):
    """Run a single FFmpeg pass.

    Uses ``-progress pipe:1`` to receive machine-readable progress on stdout,
    and reads stderr on a background thread to avoid pipe-buffer deadlock.
    """
    cmd = [ffmpeg, "-y", "-i", input_path]

    if reencode:
        cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"])
        cmd.extend(["-c:a", "aac", "-b:a", "128k"])
    else:
        cmd.extend(["-c", "copy"])

    cmd.extend(["-movflags", "+faststart"])
    cmd.extend(["-progress", "pipe:1", "-nostats"])
    cmd.append(output_path)

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        raise FFmpegError("FFmpeg executable could not be started.")
    except Exception as exc:
        raise FFmpegError(f"Failed to start FFmpeg: {exc}")

    stderr_lines = []

    def _drain_stderr():
        try:
            while True:
                line = process.stderr.readline()
                if not line:
                    break
                stderr_lines.append(line)
        except Exception:
            pass

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    last_progress = -1
    try:
        while True:
            line = process.stdout.readline()
            if not line:
                break

            line = line.strip()
            if not line:
                continue

            # Prefer out_time_us (microseconds, present in modern FFmpeg).
            if line.startswith("out_time_us="):
                percent = _percent_from_us(line, duration)
                if percent is not None and percent > last_progress:
                    last_progress = percent
                    if progress_callback:
                        progress_callback(percent, "Converting...")
            # Fallback: out_time_ms. Historically this was microseconds due to
            # an FFmpeg bug; treat it the same as out_time_us for safety.
            elif line.startswith("out_time_ms=") and last_progress < 0:
                percent = _percent_from_us(line, duration)
                if percent is not None and percent > last_progress:
                    last_progress = percent
                    if progress_callback:
                        progress_callback(percent, "Converting...")
            # Fallback: human-readable out_time (HH:MM:SS.us).
            elif line.startswith("out_time=") and duration and last_progress < 0:
                time_s = _parse_time_string(line.split("=", 1)[1].strip())
                if time_s is not None:
                    percent = min(99, max(0, int((time_s / duration) * 100)))
                    if percent > last_progress:
                        last_progress = percent
                        if progress_callback:
                            progress_callback(percent, "Converting...")
            elif line.startswith("progress="):
                if line.split("=", 1)[1].strip() == "end":
                    if progress_callback:
                        progress_callback(100, "Finalizing...")
    finally:
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            raise FFmpegError("FFmpeg process did not terminate in time.")
        stderr_thread.join(timeout=5)

    if process.returncode != 0:
        stderr_text = "".join(stderr_lines)
        raise FFmpegError(_parse_ffmpeg_error(stderr_text))


def _percent_from_us(line, duration):
    """Compute an integer percent (0-99) from an out_time_us=/out_time_ms= line."""
    if not duration or duration <= 0:
        return None
    try:
        time_us = int(line.split("=", 1)[1].strip())
    except (ValueError, IndexError):
        return None
    time_s = time_us / 1_000_000.0
    return min(99, max(0, int((time_s / duration) * 100)))


def _parse_time_string(s):
    """Parse 'HH:MM:SS.microseconds' into seconds (float)."""
    parts = s.split(":")
    if len(parts) != 3:
        return None
    try:
        h = int(parts[0])
        m = int(parts[1])
        sec = float(parts[2])
        return h * 3600 + m * 60 + sec
    except (ValueError, IndexError):
        return None


def _parse_ffmpeg_error(stderr_text):
    """Turn FFmpeg's verbose stderr into a short, user-friendly message."""
    if not stderr_text:
        return "FFmpeg conversion failed with no specific error message."

    lines = [ln.strip() for ln in stderr_text.splitlines() if ln.strip()]

    for line in reversed(lines):
        low = line.lower()
        if "invalid data found" in low or "not contain any stream" in low:
            return (
                "FFmpeg could not decode this DAT file. "
                "The file may be corrupted or use an unsupported format."
            )
        if "no such file" in low:
            return "Input file not found during conversion."
        if "permission denied" in low:
            return "Permission denied when accessing the file."
        if "cannot allocate" in low or "no space left" in low or "not enough space" in low:
            return "Not enough disk space to complete the conversion."
        if "operation timed out" in low or "timed out" in low:
            return "Conversion timed out."
        if "error while opening encoder" in low or "encoder" in low and "failed" in low:
            return "Failed to initialize the video encoder."
        if "decoder" in low and ("failed" in low or "error" in low):
            return (
                "FFmpeg could not decode this DAT file. "
                "The file may be corrupted or use an unsupported format."
            )
        if "could not find tag" in low or "codec not currently supported" in low:
            return "The source codec is not supported for MP4 output."

    # Return the last non-empty line as a best-effort message, but strip
    # any internal server paths that might leak.
    last = lines[-1] if lines else "Conversion failed."
    return _scrub_paths(last)


def _scrub_paths(text):
    """Remove absolute paths from an error string before showing it to users."""
    return re.sub(r"/[^\s'\"]+", "[path]", text)


def _safe_remove(path):
    """Remove a file if it exists, ignoring errors."""
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass
