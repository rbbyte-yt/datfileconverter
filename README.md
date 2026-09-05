# RB-DAT Converter

> Convert `.dat` video files to real `.mp4` files with FFmpeg. No fake progress, no fake downloads.

RB-DAT Converter is a small Flask web application that takes a `.dat` video file (commonly found on VCD / SVCD discs or exported by certain DVR software), inspects it with `ffprobe`, and converts it to a genuine MP4 container with `ffmpeg`. Every step is real: the file is actually uploaded, FFmpeg actually runs, and the downloaded `.mp4` is a real video file produced by FFmpeg — never a renamed copy of the input.

---

## Table of contents

1. [Features](#features)
2. [Technology](#technology)
3. [Folder structure](#folder-structure)
4. [How it works](#how-it-works)
5. [Local setup](#local-setup)
6. [FFmpeg installation](#ffmpeg-installation)
7. [GitHub setup](#github-setup)
8. [Render deployment](#render-deployment)
9. [Build & start commands](#build--start-commands)
10. [Environment variables](#environment-variables)
11. [The 2 GB limit — application vs. infrastructure](#the-2-gb-limit--application-vs-infrastructure)
12. [Render Free limitations](#render-free-limitations)
13. [Temporary storage behavior](#temporary-storage-behavior)
14. [Security](#security)
15. [Troubleshooting](#troubleshooting)
16. [Known limitations](#known-limitations)

---

## Features

- **Real FFmpeg conversion** — `.dat` files are decoded and re-muxed / re-encoded into a valid MP4 container with `+faststart` for web playback.
- **Stream-copy first** — if the source streams are MP4-compatible, FFmpeg copies them without re-encoding (fast and lossless). If that fails, a real `libx264` + `aac` re-encode fallback runs automatically.
- **Real progress** — conversion percentage is parsed from FFmpeg's machine-readable `-progress` output. If the source duration cannot be determined, an honest indeterminate indicator is shown instead of a fabricated percentage.
- **Large-file friendly** — uploads stream straight to disk, downloads stream straight to the client. Nothing loads a full 2 GB file into RAM.
- **Per-job isolation** — every upload gets a UUID and its own `uploads/<uuid>/` and `converted/<uuid>/` directories. Multiple users never overwrite each other.
- **Background conversion** — FFmpeg runs in a background thread so the web request returns immediately. Status is polled by the frontend.
- **Honest validation** — extension, size, and emptiness are checked on both client and server. The server never trusts the client.
- **Automatic cleanup** — a background sweeper deletes expired job files after a configurable TTL (default 1 hour). Manual cleanup is also available.
- **Health endpoint** — `GET /health` reports whether FFmpeg and ffprobe are actually available.
- **No database, no Redis, no Celery** — the dependency surface is intentionally minimal.

---

## Technology

| Layer        | Choice                                           |
|--------------|--------------------------------------------------|
| Frontend     | HTML5, CSS3, Vanilla JavaScript (no frameworks)  |
| Backend      | Python 3.11, Flask 3.x                           |
| Video        | FFmpeg + ffprobe (via subprocess, no `shell=True`) |
| Server       | Gunicorn                                         |
| Deployment   | Render (native Python runtime + `apt.txt`)       |
| Database     | None                                             |

---

## Folder structure

```
RB-DAT-Converter/
├── app.py                  # Flask application: routes, job system, background worker
├── requirements.txt        # Python dependencies (Flask, gunicorn)
├── render.yaml             # Render Blueprint (web service definition)
├── apt.txt                 # System packages for Render (ffmpeg)
├── .env.example            # Example environment variables (no secrets)
├── .gitignore              # Ignores venv, uploads, converted, caches, etc.
├── README.md               # This file
├── templates/
│   └── index.html          # Single-page UI
└── static/
    ├── css/
    │   └── style.css       # Modern responsive styling
    └── js/
        └── app.js          # Drag & drop, upload, polling, download, reset
└── utils/
    ├── __init__.py         # Package marker + re-exports
    └── converter.py        # FFmpeg discovery, probing, conversion, progress
```

`uploads/` and `converted/` are created automatically at startup and are git-ignored — they never contain committed content.

---

## How it works

```
User opens website
       │
       ▼
Selects / drags a .dat file
       │
       ▼
Frontend validates extension + size
       │
       ▼
POST /api/upload  ──►  Flask streams file to uploads/<uuid>/input.dat
       │
       ▼
POST /api/convert  ──►  Background thread starts FFmpeg
       │
       ▼
GET /api/status/<uuid>  (polled every 1.5 s)
       │
       ▼  status = completed
GET /api/download/<uuid>  ──►  send_file() streams the .mp4 to the browser
       │
       ▼
POST /api/cleanup/<uuid>  (or background sweeper after TTL)
```

**Conversion strategy (in `utils/converter.py`):**

1. `ffprobe` inspects the file to detect format and duration.
2. FFmpeg attempts `-c copy` (stream copy) into an MP4 container. This is fast and lossless when the source codecs are MP4-compatible.
3. If stream copy fails (common for MPEG-1 / MPEG-2 / VCD / SVCD sources, whose codecs are not allowed inside MP4), the fallback re-encodes with `libx264` + `aac`.
4. `+faststart` moves the MP4 `moov` atom to the front so the file can begin playing before download finishes.

A `.dat` file is **never** simply renamed to `.mp4`. That is not conversion and this project does not do it.

---

## Local setup

### Prerequisites

- **Python 3.10+** (3.11 recommended)
- **FFmpeg** installed and on your `PATH` (see below)
- **Git**

### Steps

```bash
# 1. Clone your repository
git clone https://github.com/<your-username>/RB-DAT-Converter.git
cd RB-DAT-Converter

# 2. Create a virtual environment
python -m venv venv

# 3. Activate it
#    Windows (PowerShell):
venv\Scripts\Activate.ps1
#    Windows (cmd):
venv\Scripts\activate.bat
#    macOS / Linux:
source venv/bin/activate

# 4. Install Python dependencies
pip install -r requirements.txt

# 5. (Optional) Copy the example env file
cp .env.example .env

# 6a. Run with Flask's dev server (local only)
python app.py
#    → http://127.0.0.1:5000

# 6b. Or run with Gunicorn (closer to production)
#     macOS / Linux:
gunicorn app:app --workers 1 --threads 4 --timeout 120
#     Windows (Gunicorn is not supported on Windows — use step 6a instead)
```

Open `http://127.0.0.1:5000` in your browser.

---

## FFmpeg installation

FFmpeg must be installed and available on `PATH` (or pointed to by `FFMPEG_PATH` / `FFPROBE_PATH` environment variables).

### Windows

1. Download a static build from a trusted source, e.g.:
   - https://www.gyan.dev/ffmpeg/builds/ (look for "release builds")
   - https://github.com/BtbN/FFmpeg-Builds/releases
2. Extract the archive (e.g. to `C:\ffmpeg`).
3. Add `C:\ffmpeg\bin` to your system `PATH`, **or** set environment variables:
   ```
   FFMPEG_PATH=C:\ffmpeg\bin\ffmpeg.exe
   FFPROBE_PATH=C:\ffmpeg\bin\ffprobe.exe
   ```
4. Verify:
   ```powershell
   ffmpeg -version
   ffprobe -version
   ```

### macOS

```bash
brew install ffmpeg
ffmpeg -version
```

### Linux (Debian / Ubuntu)

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg
ffmpeg -version
```

### Render

FFmpeg is installed automatically during the build via the `apt.txt` file. See [Render deployment](#render-deployment).

---

## GitHub setup

1. Create a new repository on GitHub named `RB-DAT-Converter` (or any name you prefer).
2. Push the project files:
   ```bash
   cd RB-DAT-Converter
   git init
   git add .
   git commit -m "Initial commit: RB-DAT Converter"
   git branch -M main
   git remote add origin https://github.com/<your-username>/RB-DAT-Converter.git
   git push -u origin main
   ```
3. Verify that `uploads/` and `converted/` are **not** committed (they are in `.gitignore`).

---

## Render deployment

### Method: native Python runtime + `apt.txt`

Render's native Python environment reads an `apt.txt` file in the repo root and installs those system packages with `apt-get` during the build. This is the simplest way to make `ffmpeg` available.

### Steps

1. Push the project to GitHub (see above).
2. Log in to [Render](https://render.com/).
3. Click **New +** → **Web Service**.
4. Connect your GitHub account and select the `RB-DAT-Converter` repository.
5. Fill in the service settings:
   - **Name:** `rb-dat-converter` (or any name)
   - **Runtime:** Python 3 (Render auto-detects from `requirements.txt`)
   - **Branch:** `main`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app --workers 1 --threads 4 --timeout 120 --graceful-timeout 30`
   - **Instance Type:** Free
6. Click **Create Web Service**.
7. Render will install Python dependencies, run `apt-get install ffmpeg` (from `apt.txt`), and start Gunicorn.
8. When deployment finishes, visit the URL Render assigns (e.g. `https://rb-dat-converter.onrender.com`).

### Alternative: use the Render Blueprint (`render.yaml`)

The included `render.yaml` is a Render Blueprint. You can also:

1. Push the repo to GitHub.
2. In Render, go to **Blueprints** → **New Blueprint Instance**.
3. Select the repository. Render reads `render.yaml` and creates the service for you.

### Verifying FFmpeg on Render

After deployment, visit:

```
https://<your-service>.onrender.com/health
```

You should see:

```json
{
  "status": "healthy",
  "ffmpeg_available": true,
  "ffmpeg_path": "/usr/bin/ffmpeg",
  "ffprobe_available": true,
  "ffprobe_path": "/usr/bin/ffprobe",
  "max_file_size": 2147483648
}
```

If `ffmpeg_available` is `false`, the `apt.txt` build step failed. Check Render's build logs.

---

## Build & start commands

| Purpose        | Command                                                              |
|----------------|----------------------------------------------------------------------|
| Build          | `pip install -r requirements.txt`                                    |
| Start (Render) | `gunicorn app:app --workers 1 --threads 4 --timeout 120 --graceful-timeout 30` |
| Start (local)  | `python app.py`                                                      |

### Why `--workers 1`?

Job state lives in an in-process dict. With multiple workers, a status request could hit a different worker than the one running the conversion, and the job would appear "not found". A single worker with four threads keeps the architecture simple (no Redis) while still handling concurrent requests. This is a deliberate trade-off documented in [Known limitations](#known-limitations).

---

## Environment variables

All optional. No secrets are required.

| Variable           | Default        | Description                                      |
|--------------------|----------------|--------------------------------------------------|
| `FFMPEG_PATH`      | (auto-detected)| Absolute path to the `ffmpeg` binary.            |
| `FFPROBE_PATH`     | (auto-detected)| Absolute path to the `ffprobe` binary.           |
| `MAX_FILE_SIZE`    | `2147483648`   | Max upload size in bytes (2 GiB = 2.00 GB).      |
| `JOB_TTL_SECONDS`  | `3600`         | Seconds before a job's files are auto-deleted.   |
| `PORT`             | `5000`         | Port for the Flask dev server (local only).      |
| `PYTHON_VERSION`   | `3.11.9`       | Python version on Render (set in `render.yaml`). |

---

## The 2 GB limit — application vs. infrastructure

These are two different things and the project treats them separately:

### Application-configured limit

The application accepts files up to **2.00 GB** (exactly `2,147,483,648` bytes). This is enforced in three independent places:

1. **Flask config** — `app.config["MAX_CONTENT_LENGTH"]` rejects oversized requests before the body is read.
2. **Pre-save check** — `request.content_length` is compared to the limit.
3. **Post-save check** — the actual size on disk is verified after the upload completes.

The frontend also validates the size before uploading, but the server never trusts the frontend.

### Infrastructure limit

Whether a **2 GB file can actually complete the full upload → convert → download cycle** depends on the hosting provider:

| Factor            | What it means                                                                                  |
|-------------------|-----------------------------------------------------------------------------------------------|
| Disk space        | The server needs enough temp disk for the upload + the output (up to ~4 GB peak for a 2 GB source). |
| RAM               | FFmpeg re-encoding can use significant RAM. Stream copy uses very little.                     |
| CPU               | Re-encoding a 2 GB file can take many minutes.                                                |
| Request timeout   | Render's load balancer and Gunicorn both impose timeouts. A slow upload of 2 GB may exceed them. |
| Service sleep     | Render Free services sleep after inactivity and may be killed during long jobs.               |

**The application is architecturally capable of 2 GB files. Render Free may not reliably complete them.** This is documented honestly and the limit is **not** silently reduced.

---

## Render Free limitations

This project is designed to deploy on Render Free, but you must understand the real constraints:

| Limitation                | Impact on this app                                                                                                  |
|---------------------------|---------------------------------------------------------------------------------------------------------------------|
| **512 MB RAM**            | Re-encoding large files with libx264 can exceed this. Stream copy uses far less. Very large re-encodes may OOM.     |
| **Ephemeral filesystem**  | `uploads/` and `converted/` live on ephemeral disk. They survive across requests but **not** across deploys / restarts. If Render restarts the service mid-conversion, the job is lost. |
| **Request timeout (~100 s)** | Uploads and downloads that take longer than the LB timeout will be terminated by Render, not by the app. A 2 GB upload over a slow connection can easily exceed this. |
| **Service sleep**         | Free services sleep after ~15 minutes of inactivity. A long conversion running when the service sleeps will be killed. |
| **Single worker**         | This app uses `--workers 1` (see above). Throughput is limited but correctness is preserved.                        |
| **No background workers** | Render Free does not support separate worker dynos. The conversion runs in a thread inside the web process. If the web process is killed, the conversion dies. |

### What this means in practice

- **Small `.dat` files (a few hundred MB or less):** work reliably.
- **Medium files (~500 MB – 1 GB):** usually work, but re-encoding may be slow.
- **Very large files (1–2 GB):** may fail due to timeouts, OOM, or service restarts. The application does not pretend otherwise.

For production-grade large-file conversion, use a paid Render instance, a VPS, or a dedicated video-processing service with persistent workers (Redis + RQ / Celery, or a separate worker process).

---

## Temporary storage behavior

- Each upload is saved to `uploads/<job-uuid>/input.dat`.
- Each conversion output is saved to `converted/<job-uuid>/<safe-name>.mp4`.
- The input `.dat` is deleted as soon as the conversion finishes (success or failure).
- The output `.mp4` is kept until:
  - The user clicks **Start Again** (triggers `POST /api/cleanup/<job_id>`), or
  - The background sweeper deletes it after `JOB_TTL_SECONDS` (default 1 hour).
- Cleanup only touches the specific job's directories. No recursive deletion of parent directories is ever performed.
- On Render, all of this lives on ephemeral disk and is wiped on every deploy / restart.

---

## Security

| Concern                    | Mitigation                                                                                              |
|----------------------------|---------------------------------------------------------------------------------------------------------|
| Path traversal             | Job directories are UUID-named. `os.path.realpath()` is compared against the expected parent directory. |
| Malicious filenames        | `werkzeug.utils.secure_filename` sanitizes every user-supplied filename.                               |
| Arbitrary command execution| FFmpeg is invoked with an **argument list** via `subprocess.Popen`. **No `shell=True`.** No user input is ever placed in a shell. |
| Arbitrary FFmpeg args      | The FFmpeg argument list is hard-coded. Users cannot inject codec names, filters, or output paths.      |
| Uploaded file execution    | The uploaded `.dat` is only ever passed to FFmpeg as input media. It is never executed, imported, or `eval`'d. |
| Oversized uploads          | Three independent size checks (Flask config, Content-Length header, post-save stat).                    |
| Wrong extension            | Server-side extension check independent of the frontend.                                                |
| Empty files                | Rejected after save with an explicit error.                                                             |
| Traceback leakage          | All error handlers return controlled JSON messages. Internal paths are scrubbed from FFmpeg errors.     |
| Cross-job interference     | Every job has its own UUID-scoped directory. The cleanup function validates paths before deleting.      |

No secret keys, API keys, or credentials are required or stored.

---

## Troubleshooting

### "FFmpeg is not installed or not on PATH"

- **Local:** install FFmpeg (see [FFmpeg installation](#ffmpeg-installation)) and verify with `ffmpeg -version`.
- **Render:** check the build logs for the `apt.txt` step. Verify with `GET /health`.

### Upload fails with "413 Request Entity Too Large"

- The file exceeds 2.00 GB. The application will not accept larger files.

### Conversion fails with "FFmpeg could not decode this DAT file"

- The `.dat` file is not a valid video, or uses a codec FFmpeg cannot decode.
- Try opening the file with a desktop media player (VLC) to confirm it is playable.
- Some `.dat` files are not video at all (e.g. Windows registry hives, email attachments). This tool only converts video `.dat` files.

### Conversion hangs at "Converting..." indefinitely

- The file may be very large and re-encoding is slow. Wait longer.
- If the source duration is unknown, an indeterminate indicator is shown — this is expected, not a hang.
- On Render Free, the service may have been killed. Refresh the page and try again.

### Download starts but does not complete

- Large files on Render Free may hit the request timeout. Try a smaller file, or use a paid instance.

### `GET /health` returns 503

- FFmpeg or ffprobe is missing. The service is running but cannot convert. Fix the FFmpeg installation first.

### Status polling returns "Job not found"

- The service was restarted (e.g. on Render) and in-memory job state was lost. Re-upload the file.
- The job expired (older than `JOB_TTL_SECONDS`). Re-upload.

---

## Known limitations

1. **Single-worker requirement.** Job state is in-process. Multiple Gunicorn workers would break status polling. This is a deliberate trade-off to avoid Redis/Celery.
2. **No persistence across restarts.** If the server restarts mid-conversion, the job is lost. On Render Free, this can happen during deploys or sleep.
3. **Render Free is not suitable for very large files.** The application supports 2 GB, but Render Free's RAM, timeout, and sleep constraints make large conversions unreliable. This is an infrastructure limit, not an application bug.
4. **No resume.** If an upload or download is interrupted, it must start over. Range requests are supported on downloads via `conditional=True`, but the conversion itself cannot resume.
5. **`out_time_ms` ambiguity.** Older FFmpeg versions reported `out_time_ms` in microseconds due to a long-standing bug. The parser treats it as microseconds and falls back to `out_time=` (human-readable) if needed. Progress may be unavailable for sources with unknown duration.
6. **No audio normalization.** If the source has no audio, the output will have no audio. FFmpeg handles this gracefully.
7. **Re-encode uses `veryfast` preset.** This favors speed over compression efficiency. For smaller files at the cost of longer conversion, edit the preset in `utils/converter.py`.
8. **Gunicorn not supported on Windows for local use.** Use `python app.py` on Windows. Gunicorn works on macOS, Linux, and Render.

---

## License

This project is provided as-is for your use. Modify and distribute freely.
