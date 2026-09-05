"""RB-DAT Converter — Flask application.

A real .dat → .mp4 converter backed by FFmpeg.

Architecture:
  * Flask serves the HTML page and a small JSON API.
  * Each upload gets a UUID job ID and its own subdirectory under uploads/.
  * Conversion runs in a background thread (no Redis/Celery) because the
    dependency surface must stay minimal.
  * Job state lives in an in-process dict guarded by a lock. With Gunicorn
    configured for a single worker (see render.yaml / README) this is
    consistent across requests.
  * Files are streamed to/from disk — never loaded fully into RAM.

API:
  GET  /                      HTML UI
  GET  /health                Service + FFmpeg health
  POST /api/upload            Upload .dat (multipart, streamed to disk)
  POST /api/convert           Start conversion for a job
  GET  /api/status/<job_id>   Poll job status / progress
  GET  /api/download/<job_id> Download the produced .mp4 (streamed)
  POST /api/cleanup/<job_id>  Delete a job's temporary files
"""

import os
import uuid
import threading
import time
import logging

from flask import (
    Flask, request, jsonify, render_template,
    send_file, abort,
)
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge

from utils.converter import (
    FFmpegError,
    find_ffmpeg,
    find_ffprobe,
    probe_media,
    convert_dat_to_mp4,
)

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
CONVERTED_DIR = os.path.join(BASE_DIR, "converted")

# 2.00 GB in bytes (exactly 2 * 1024^3).
MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE", 2 * 1024 * 1024 * 1024))

ALLOWED_EXTENSIONS = {"dat"}

# How long a job's files are kept before automatic cleanup.
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", 3600))  # 1 hour

# How often the background sweeper runs.
SWEEP_INTERVAL_SECONDS = 300  # 5 minutes

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("rb-dat-converter")

# --------------------------------------------------------------------------
# Flask app
# --------------------------------------------------------------------------

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
app.config["JSON_SORT_KEYS"] = False

# Ensure storage directories exist at import time.
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(CONVERTED_DIR, exist_ok=True)

# --------------------------------------------------------------------------
# In-process job store (single-worker Gunicorn only — see README)
# --------------------------------------------------------------------------

_jobs_lock = threading.Lock()
_jobs = {}  # job_id -> dict


def _allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _valid_job_id(job_id):
    try:
        uuid.UUID(str(job_id))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _get_job(job_id):
    with _jobs_lock:
        return _jobs.get(job_id)


def _update_job(job_id, **kwargs):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        job.update(kwargs)
        return dict(job)


def _cleanup_job_files(job_id):
    """Safely delete every file belonging to a single job.

    Only the job-specific subdirectories are touched. No recursive deletion
    is performed on parent directories.
    """
    if not _valid_job_id(job_id):
        return
    for root in (UPLOAD_DIR, CONVERTED_DIR):
        job_dir = os.path.join(root, job_id)
        if not os.path.isdir(job_dir):
            continue
        # Resolve and verify the path stays inside its parent.
        real_job_dir = os.path.realpath(job_dir)
        real_root = os.path.realpath(root)
        if not real_job_dir.startswith(real_root + os.sep):
            logger.warning("Refusing to clean path outside its root: %s", real_job_dir)
            continue
        try:
            for name in os.listdir(job_dir):
                full = os.path.join(job_dir, name)
                if os.path.isfile(full) or os.path.islink(full):
                    try:
                        os.remove(full)
                    except OSError as exc:
                        logger.warning("Could not delete %s: %s", full, exc)
            os.rmdir(job_dir)
        except OSError as exc:
            logger.warning("Could not remove job dir %s: %s", job_dir, exc)


# --------------------------------------------------------------------------
# Conversion worker (runs in a background thread)
# --------------------------------------------------------------------------

def _conversion_worker(job_id, input_path, output_path, original_name):
    """Run FFmpeg in a background thread and update job state."""
    try:
        _update_job(
            job_id,
            status="converting",
            progress=0,
            message="Inspecting media...",
        )

        # Probe (best-effort) for duration + format info.
        try:
            probe = probe_media(input_path)
            _update_job(
                job_id,
                media_format=probe.get("format"),
                media_duration=probe.get("duration"),
            )
        except FFmpegError as exc:
            # If probe fails outright, the file is likely unreadable.
            raise FFmpegError(
                "Could not read media information from this DAT file. "
                "It may be corrupted or not a valid video. "
                f"Detail: {exc}"
            )

        _update_job(job_id, message="Converting...")

        def _on_progress(percent, message=None):
            data = {"progress": int(percent)}
            if message:
                data["message"] = message
            _update_job(job_id, **data)

        result = convert_dat_to_mp4(
            input_path, output_path, progress_callback=_on_progress
        )

        if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
            raise FFmpegError("Conversion finished but no output file was produced.")

        _update_job(
            job_id,
            status="completed",
            progress=100,
            message="Conversion completed.",
            output_filename=os.path.basename(output_path),
            output_size=os.path.getsize(output_path),
            conversion_method=result.get("method"),
            completed_at=time.time(),
        )
        logger.info("Job %s completed (%s)", job_id, result.get("method"))

    except FFmpegError as exc:
        logger.error("Job %s failed: %s", job_id, exc)
        _update_job(
            job_id,
            status="failed",
            error=str(exc),
            message="Conversion failed.",
            failed_at=time.time(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error in job %s", job_id)
        _update_job(
            job_id,
            status="failed",
            error="An unexpected error occurred during conversion.",
            message="Conversion failed.",
            failed_at=time.time(),
        )
    finally:
        # Remove the original .dat — it is no longer needed once conversion
        # has succeeded or failed. The .mp4 (if any) remains for download.
        try:
            if os.path.isfile(input_path):
                os.remove(input_path)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Background sweeper: delete expired jobs
# --------------------------------------------------------------------------

def _sweeper_loop():
    while True:
        time.sleep(SWEEP_INTERVAL_SECONDS)
        now = time.time()
        expired_ids = []
        with _jobs_lock:
            for jid, job in _jobs.items():
                created = job.get("created_at") or 0
                if now - created > JOB_TTL_SECONDS:
                    expired_ids.append(jid)
        for jid in expired_ids:
            _cleanup_job_files(jid)
            with _jobs_lock:
                if jid in _jobs:
                    _jobs[jid]["status"] = "expired"
                    _jobs[jid]["message"] = "Job expired and files were cleaned up."
            logger.info("Expired job %s", jid)


_sweeper_thread = threading.Thread(target=_sweeper_loop, daemon=True)
_sweeper_thread.start()


# --------------------------------------------------------------------------
# Error handlers
# --------------------------------------------------------------------------

@app.errorhandler(413)
def _handle_too_large(_e):
    return jsonify({
        "success": False,
        "error": "File too large. Maximum size is 2.00 GB.",
    }), 413


@app.errorhandler(404)
def _handle_not_found(_e):
    return jsonify({"success": False, "error": "Resource not found."}), 404


@app.errorhandler(500)
def _handle_server_error(_e):
    return jsonify({
        "success": False,
        "error": "An internal server error occurred.",
    }), 500


# --------------------------------------------------------------------------
# Routes — UI
# --------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# --------------------------------------------------------------------------
# Routes — health
# --------------------------------------------------------------------------

@app.route("/health")
def health():
    ffmpeg = find_ffmpeg()
    ffprobe = find_ffprobe()
    ok = ffmpeg is not None and ffprobe is not None
    return jsonify({
        "status": "healthy" if ok else "degraded",
        "ffmpeg_available": ffmpeg is not None,
        "ffmpeg_path": ffmpeg,
        "ffprobe_available": ffprobe is not None,
        "ffprobe_path": ffprobe,
        "max_file_size": MAX_FILE_SIZE,
    }), (200 if ok else 503)


# --------------------------------------------------------------------------
# Routes — API
# --------------------------------------------------------------------------

@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file provided."}), 400

    file = request.files["file"]
    if not file or file.filename == "":
        return jsonify({"success": False, "error": "No file selected."}), 400

    original_name = secure_filename(file.filename)
    if not original_name:
        return jsonify({"success": False, "error": "Invalid filename."}), 400

    # Extension check (independent of the frontend).
    if not _allowed_file(original_name):
        return jsonify({
            "success": False,
            "error": "Only .dat files are accepted.",
        }), 400

    # Content-Length pre-check (the real size check happens after save too).
    content_length = request.content_length
    if content_length is not None and content_length > MAX_FILE_SIZE:
        return jsonify({
            "success": False,
            "error": "File too large. Maximum size is 2.00 GB.",
        }), 413

    job_id = str(uuid.uuid4())
    job_upload_dir = os.path.join(UPLOAD_DIR, job_id)
    os.makedirs(job_upload_dir, exist_ok=True)
    input_path = os.path.join(job_upload_dir, "input.dat")

    # Stream the upload straight to disk (Werkzeug handles this for us via
    # FileStorage.save(), which does NOT load the whole file into RAM).
    try:
        file.save(input_path)
    except RequestEntityTooLarge:
        _cleanup_job_files(job_id)
        return jsonify({
            "success": False,
            "error": "File too large. Maximum size is 2.00 GB.",
        }), 413
    except Exception as exc:
        logger.error("Upload save failed: %s", exc)
        _cleanup_job_files(job_id)
        return jsonify({
            "success": False,
            "error": "Failed to save the uploaded file.",
        }), 500

    # Post-save validation.
    try:
        actual_size = os.path.getsize(input_path)
    except OSError:
        _cleanup_job_files(job_id)
        return jsonify({
            "success": False,
            "error": "Could not read the saved file.",
        }), 500

    if actual_size == 0:
        _cleanup_job_files(job_id)
        return jsonify({"success": False, "error": "File is empty."}), 400

    if actual_size > MAX_FILE_SIZE:
        _cleanup_job_files(job_id)
        return jsonify({
            "success": False,
            "error": "File too large. Maximum size is 2.00 GB.",
        }), 413

    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "uploaded",
            "progress": 0,
            "message": "File uploaded. Ready to convert.",
            "original_name": original_name,
            "file_size": actual_size,
            "created_at": time.time(),
            "input_path": input_path,
        }

    logger.info("Upload accepted: job=%s name=%s size=%d", job_id, original_name, actual_size)

    return jsonify({
        "success": True,
        "job_id": job_id,
        "file_name": original_name,
        "file_size": actual_size,
    })


@app.route("/api/convert", methods=["POST"])
def api_convert():
    data = request.get_json(silent=True) or {}
    job_id = data.get("job_id")

    if not job_id or not _valid_job_id(job_id):
        return jsonify({"success": False, "error": "Valid job_id is required."}), 400

    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return jsonify({"success": False, "error": "Job not found."}), 404
        if job["status"] in ("converting", "completed"):
            return jsonify({
                "success": False,
                "error": f"Job is already {job['status']}.",
            }), 409
        if not os.path.isfile(job["input_path"]):
            return jsonify({
                "success": False,
                "error": "Input file no longer exists. Please upload again.",
            }), 410

        # Reserve the job so a duplicate request cannot start two threads.
        job["status"] = "queued"
        job["message"] = "Queued for conversion."

        input_path = job["input_path"]
        original_name = job["original_name"]

    # Output path.
    job_output_dir = os.path.join(CONVERTED_DIR, job_id)
    os.makedirs(job_output_dir, exist_ok=True)
    base_name = os.path.splitext(original_name)[0]
    safe_base = secure_filename(base_name) or "output"
    output_filename = f"{safe_base}.mp4"
    output_path = os.path.join(job_output_dir, output_filename)

    thread = threading.Thread(
        target=_conversion_worker,
        args=(job_id, input_path, output_path, original_name),
        daemon=True,
    )
    thread.start()

    logger.info("Conversion started: job=%s", job_id)
    return jsonify({"success": True, "job_id": job_id})


@app.route("/api/status/<job_id>")
def api_status(job_id):
    if not _valid_job_id(job_id):
        return jsonify({"success": False, "error": "Invalid job ID."}), 400

    job = _get_job(job_id)
    if not job:
        return jsonify({"success": False, "error": "Job not found."}), 404

    response = {
        "success": True,
        "job_id": job_id,
        "status": job.get("status"),
        "progress": job.get("progress", 0),
        "message": job.get("message"),
    }
    if job.get("error"):
        response["error"] = job["error"]
    if job.get("output_filename"):
        response["output_filename"] = job["output_filename"]
    if job.get("output_size") is not None:
        response["output_size"] = job["output_size"]
    if job.get("conversion_method"):
        response["conversion_method"] = job["conversion_method"]
    if job.get("media_format"):
        response["media_format"] = job["media_format"]
    return jsonify(response)


@app.route("/api/download/<job_id>")
def api_download(job_id):
    if not _valid_job_id(job_id):
        return jsonify({"success": False, "error": "Invalid job ID."}), 400

    job = _get_job(job_id)
    if not job:
        return jsonify({"success": False, "error": "Job not found."}), 404

    if job.get("status") != "completed":
        return jsonify({
            "success": False,
            "error": "Conversion has not completed.",
        }), 400

    output_filename = job.get("output_filename") or "output.mp4"
    output_path = os.path.join(CONVERTED_DIR, job_id, output_filename)

    # Path-traversal guard: resolve the real path on disk and require its
    # parent directory to be exactly this job's converted directory.
    real_path = os.path.realpath(output_path)
    expected_dir = os.path.realpath(os.path.join(CONVERTED_DIR, job_id))
    if os.path.dirname(real_path) != expected_dir:
        return jsonify({"success": False, "error": "Invalid path."}), 400

    if not os.path.isfile(real_path):
        return jsonify({
            "success": False,
            "error": "Output file not found. It may have been cleaned up.",
        }), 404

    if os.path.getsize(real_path) == 0:
        return jsonify({"success": False, "error": "Output file is empty."}), 404

    # Build a friendly download name from the original .dat filename.
    original = job.get("original_name") or "video.dat"
    base = os.path.splitext(original)[0]
    safe_base = secure_filename(base) or "output"
    download_name = f"{safe_base}.mp4"

    logger.info("Download: job=%s file=%s", job_id, download_name)
    return send_file(
        real_path,
        as_attachment=True,
        download_name=download_name,
        mimetype="video/mp4",
        conditional=True,
        max_age=0,
    )


@app.route("/api/cleanup/<job_id>", methods=["POST"])
def api_cleanup(job_id):
    if not _valid_job_id(job_id):
        return jsonify({"success": False, "error": "Invalid job ID."}), 400

    with _jobs_lock:
        exists = job_id in _jobs
    if not exists:
        return jsonify({"success": False, "error": "Job not found."}), 404

    _cleanup_job_files(job_id)

    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["status"] = "cleaned"
            _jobs[job_id]["message"] = "Temporary files cleaned up."
            _jobs[job_id].pop("input_path", None)

    logger.info("Cleanup: job=%s", job_id)
    return jsonify({"success": True, "job_id": job_id})


# --------------------------------------------------------------------------
# Entrypoint (local dev only; Render uses gunicorn)
# --------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # debug=False in all cases — never expose the Werkzeug debugger in a
    # converter that accepts file uploads.
    app.run(host="0.0.0.0", port=port, debug=False)
