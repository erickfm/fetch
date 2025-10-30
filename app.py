import json
import os
import re
import sys
import time
import glob
import secrets
import logging
import urllib.parse
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Optional, Dict, Any, List

from flask import (
    Flask,
    Response,
    jsonify,
    request,
    render_template,
    send_file,
    stream_with_context,
    abort,
)


# ----------------------------
# Configuration & Constants
# ----------------------------

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "./downloads")
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "3"))
FILE_RETENTION_HOURS = int(os.getenv("FILE_RETENTION_HOURS", "24"))
YTDLP_TIMEOUT = int(os.getenv("YTDLP_TIMEOUT", "300"))
PORT = int(os.getenv("PORT", "5000"))

SUPPORTED_SITES = ["youtube.com", "youtu.be", "m.youtube.com", "www.youtube.com"]
MAX_FILE_SIZE = 5 * 1024**3  # 5GB
PROGRESS_UPDATE_INTERVAL = 0.5  # seconds


# ----------------------------
# Flask App & Logger
# ----------------------------

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("fetch")


# ----------------------------
# Utilities & Validation
# ----------------------------


def is_valid_youtube_url(url: str) -> bool:
    if not url or len(url) > 500:
        return False
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    if parsed.netloc not in SUPPORTED_SITES:
        return False
    if parsed.scheme not in ["http", "https"]:
        return False
    return True


def sanitize_format_id(format_id: str) -> str:
    if not re.match(r"^[a-zA-Z0-9+\-]+$", format_id or ""):
        raise ValueError("Invalid format ID")
    return format_id


def ensure_download_dir(path: str) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_ytdlp_binary() -> str:
    return "yt-dlp"


def get_ytdlp_version() -> Optional[str]:
    try:
        result = subprocess.run([get_ytdlp_binary(), "--version"], capture_output=True, text=True, timeout=5, check=True)
        return result.stdout.strip()
    except Exception:
        return None


# ----------------------------
# Format Analyzer Agent
# ----------------------------


class YtDlpError(Exception):
    pass


class FormatAnalyzer:
    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    def get_formats(self, url: str, timeout: Optional[int] = None) -> Dict[str, Any]:
        effective_timeout = timeout or self.timeout
        try:
            info = self._run_yt_dlp_info(url, effective_timeout)
            parsed = self._parse_formats(info)
            parsed["formats"] = self._add_quality_labels(parsed["formats"])
            parsed["categorized"] = self.categorize_formats(parsed["formats"])  # convenience for UI
            return parsed
        except subprocess.TimeoutExpired:
            raise TimeoutError("yt-dlp took too long to respond")
        except subprocess.CalledProcessError as e:
            error_msg = e.stderr if isinstance(e.stderr, str) else (e.stderr.decode() if e.stderr else str(e))
            if "Sign in to confirm your age" in error_msg:
                raise YtDlpError("Age-restricted video (not supported)")
            if "Video unavailable" in error_msg:
                raise YtDlpError("Video is unavailable or private")
            if "Unsupported URL" in error_msg:
                raise YtDlpError("URL not recognized as valid YouTube link")
            raise YtDlpError(f"yt-dlp error: {error_msg}")
        except json.JSONDecodeError:
            raise YtDlpError("yt-dlp output format unreadable (try updating)")

    def _run_yt_dlp_info(self, url: str, timeout: int) -> Dict[str, Any]:
        result = subprocess.run(
            [
                get_ytdlp_binary(),
                "--dump-json",
                "--no-playlist",
                "--no-warnings",
                "--skip-download",
                url,
            ],
            capture_output=True,
            timeout=timeout,
            check=True,
            text=True,
            shell=False,
            env={**os.environ, "HOME": "/tmp"},
        )
        return json.loads(result.stdout)

    def _parse_formats(self, info: Dict[str, Any]) -> Dict[str, Any]:
        formats: List[Dict[str, Any]] = []
        for f in info.get("formats", []):
            if f.get("format_note") == "storyboard":
                continue
            formats.append(
                {
                    "format_id": f.get("format_id", ""),
                    "ext": f.get("ext", "unknown"),
                    "resolution": f.get("resolution") or ("audio only" if (f.get("vcodec") == "none") else None) or "",
                    "filesize": f.get("filesize") or f.get("filesize_approx"),
                    "vcodec": f.get("vcodec", "none"),
                    "acodec": f.get("acodec", "none"),
                    "fps": f.get("fps"),
                    "vbr": f.get("vbr"),
                    "abr": f.get("abr"),
                }
            )
        return {
            "title": info.get("title", "Unknown Title"),
            "duration": info.get("duration", 0),
            "thumbnail": info.get("thumbnail"),
            "formats": formats,
        }

    def _add_quality_labels(self, formats: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        for f in formats:
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            resolution = f.get("resolution") or ""
            fps = f.get("fps")
            abr = f.get("abr")
            if vcodec != "none" and acodec != "none":
                f["quality_label"] = f"{resolution} (complete)"
            elif vcodec != "none":
                f["quality_label"] = f"{resolution}{f' @ {fps}fps' if fps else ''} (video only)"
            else:
                bitrate = f"{abr}kbps" if abr else "unknown"
                f["quality_label"] = f"{bitrate} (audio only)"
        return formats

    def categorize_formats(self, formats: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        categorized = {"video_audio": [], "video_only": [], "audio_only": []}
        for f in formats:
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            if vcodec != "none" and acodec != "none":
                categorized["video_audio"].append(f)
            elif vcodec != "none":
                categorized["video_only"].append(f)
            else:
                categorized["audio_only"].append(f)
        return categorized


# ----------------------------
# Download Orchestrator Agent
# ----------------------------


@dataclass
class DownloadState:
    id: str
    url: str
    format_id: str
    status: str  # queued, downloading, complete, failed, cancelled
    output_path: str
    progress: float = 0.0
    speed: str = ""
    eta: str = ""
    error: Optional[str] = None
    process: Optional[subprocess.Popen] = None
    created_at: float = field(default_factory=lambda: time.time())
    completed_at: Optional[float] = None
    final_path: Optional[str] = None
    cancel_requested: bool = False


class DownloadOrchestrator:
    def __init__(self, download_dir: str, max_workers: int = 3):
        self.download_dir = ensure_download_dir(download_dir)
        self.active_downloads: Dict[str, DownloadState] = {}
        self.executor = ThreadPoolExecutor(max_workers=max_workers)

    def start_download(self, url: str, format_id: str) -> str:
        download_id = secrets.token_urlsafe(16)
        output_path = str(self.download_dir / f"{download_id}.%(ext)s")
        state = DownloadState(
            id=download_id,
            url=url,
            format_id=format_id,
            status="queued",
            output_path=output_path,
        )
        self.active_downloads[download_id] = state
        future = self.executor.submit(self._download_worker, state)
        future.add_done_callback(lambda f: self._handle_completion(download_id, f))
        return download_id

    def _download_worker(self, state: DownloadState) -> None:
        state.status = "downloading"
        try:
            cmd = [
                get_ytdlp_binary(),
                "-f",
                state.format_id,
                "--newline",
                "--no-playlist",
                "-o",
                state.output_path,
                state.url,
            ]
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                shell=False,
                env={**os.environ, "HOME": "/tmp"},
            )
            state.process = process

            if not process.stdout:
                raise RuntimeError("Failed to read yt-dlp output")

            for line in process.stdout:
                if state.cancel_requested:
                    try:
                        process.kill()
                    finally:
                        state.status = "cancelled"
                        return
                self._parse_progress_line(line, state)

            process.wait(timeout=YTDLP_TIMEOUT)

            if process.returncode == 0:
                state.status = "complete"
                state.progress = 100.0
                state.completed_at = time.time()
                state.final_path = self._find_downloaded_file(state)
                if state.final_path and os.path.exists(state.final_path):
                    size_mb = os.path.getsize(state.final_path) / (1024**2)
                    log.info(f"Download complete: {state.id} - {size_mb:.1f}MB")
            else:
                raise subprocess.CalledProcessError(process.returncode, cmd)
        except OSError as e:
            state.status = "failed"
            if e.errno == 28:  # ENOSPC - No space left on device
                state.error = "Storage full - contact admin"
                log.error(f"Download {state.id} failed: Disk full")
            else:
                state.error = str(e)
                log.error(f"Download {state.id} failed: {e}")
            self._cleanup_partial_files(state)
        except Exception as e:
            state.status = "failed"
            state.error = str(e)
            log.error(f"Download {state.id} failed: {e}")
            self._cleanup_partial_files(state)

    def _parse_progress_line(self, line: str, state: DownloadState) -> None:
        if "[download]" not in line:
            return
        if "%" in line:
            try:
                percent_str = line.split("%", 1)[0].split()[-1]
                state.progress = float(percent_str)
            except Exception:
                pass
        if " at " in line and "/s" in line:
            try:
                after_at = line.split(" at ", 1)[1]
                speed = after_at.split(" ETA", 1)[0].strip()
                state.speed = speed
            except Exception:
                pass
        if "ETA" in line:
            try:
                eta = line.split("ETA", 1)[1].strip()
                state.eta = eta
            except Exception:
                pass

    def _find_downloaded_file(self, state: DownloadState) -> Optional[str]:
        pattern = state.output_path.replace(".%(ext)s", ".*")
        matches = glob.glob(pattern)
        return matches[0] if matches else None

    def _handle_completion(self, download_id: str, future: Future) -> None:
        try:
            future.result()
        except Exception as e:
            state = self.active_downloads.get(download_id)
            if state:
                state.status = "failed"
                state.error = str(e)
                self._cleanup_partial_files(state)
            log.error(f"Download {download_id} failed: {e}")

    def _cleanup_partial_files(self, state: DownloadState) -> None:
        pattern = state.output_path.replace(".%(ext)s", ".*")
        for filepath in glob.glob(pattern):
            try:
                os.remove(filepath)
            except OSError:
                pass

    def get_progress(self, download_id: str) -> Dict[str, Any]:
        state = self.active_downloads.get(download_id)
        if not state:
            return {"error": "Download not found"}
        return {
            "status": state.status,
            "progress": state.progress,
            "speed": state.speed,
            "eta": state.eta,
            "error": state.error,
        }

    def cancel_download(self, download_id: str) -> None:
        state = self.active_downloads.get(download_id)
        if state and state.status == "downloading":
            state.cancel_requested = True

    def cleanup_expired_downloads(self) -> None:
        cutoff = time.time() - (FILE_RETENTION_HOURS * 3600)
        to_delete = []
        for download_id, state in list(self.active_downloads.items()):
            if state.completed_at and state.completed_at < cutoff:
                if state.final_path and os.path.exists(state.final_path):
                    try:
                        os.remove(state.final_path)
                    except OSError:
                        pass
                to_delete.append(download_id)
        for download_id in to_delete:
            self.active_downloads.pop(download_id, None)
            log.info(f"Cleaned up expired download: {download_id}")


# ----------------------------
# Storage Agent
# ----------------------------


class StorageAgent:
    def __init__(self, download_dir: str = "./downloads"):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(exist_ok=True, parents=True)

    def get_file_path(self, download_id: str) -> Path:
        if not re.match(r"^[a-zA-Z0-9_\-]{16,32}$", download_id or ""):
            raise ValueError("Invalid download ID")
        pattern = f"{download_id}.*"
        matches = list(self.download_dir.glob(pattern))
        if not matches:
            raise FileNotFoundError(f"Download {download_id} not found")
        
        # Verify path is within download directory (defense in depth)
        filepath = matches[0].resolve()
        if not str(filepath).startswith(str(self.download_dir.resolve())):
            raise ValueError("Path traversal attempt detected")
        
        return filepath

    def get_file_info(self, filepath: Path) -> Dict[str, Any]:
        stat = filepath.stat()
        return {
            "filename": filepath.name,
            "size": stat.st_size,
            "created": stat.st_ctime,
            "mimetype": self._guess_mimetype(filepath),
        }

    def _guess_mimetype(self, filepath: Path) -> str:
        ext = filepath.suffix.lower()
        mimetypes = {
            ".mp4": "video/mp4",
            ".webm": "video/webm",
            ".mkv": "video/x-matroska",
            ".mp3": "audio/mpeg",
            ".m4a": "audio/mp4",
            ".opus": "audio/opus",
        }
        return mimetypes.get(ext, "application/octet-stream")

    def serve_file(self, download_id: str) -> Response:
        filepath = self.get_file_path(download_id)
        info = self.get_file_info(filepath)
        return send_file(
            filepath,
            mimetype=info["mimetype"],
            as_attachment=True,
            download_name=info["filename"],
        )


# ----------------------------
# App State
# ----------------------------


downloads_path = ensure_download_dir(DOWNLOAD_DIR)
orchestrator = DownloadOrchestrator(download_dir=str(downloads_path), max_workers=MAX_CONCURRENT)
storage = StorageAgent(download_dir=str(downloads_path))
analyzer = FormatAnalyzer(timeout=30)

# Simple in-memory rate limiting for /analyze
MAX_ANALYSIS_PER_MINUTE = 10
analysis_attempts: Dict[str, List[float]] = {}


@app.before_request
def rate_limit() -> None:
    if request.endpoint == "analyze_url":
        ip = request.remote_addr or "unknown"
        now = time.time()
        timestamps = [t for t in analysis_attempts.get(ip, []) if now - t < 60]
        if len(timestamps) >= MAX_ANALYSIS_PER_MINUTE:
            abort(429, description="Too many requests - wait a minute")
        timestamps.append(now)
        analysis_attempts[ip] = timestamps


# ----------------------------
# HTTP Routes
# ----------------------------


@app.route("/")
def index() -> Response:
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze_url() -> Response:
    body = request.get_json(silent=True) or {}
    url = body.get("url", "").strip()
    if not is_valid_youtube_url(url):
        return jsonify({"error": "Invalid YouTube URL"}), 400
    try:
        log.info(f"Analyzing URL: {url}")
        data = analyzer.get_formats(url)
        log.info(f"Found {len(data.get('formats', []))} formats for: {data.get('title', 'Unknown')}")
        return jsonify(data)
    except TimeoutError:
        log.warning(f"Analysis timeout for: {url}")
        return jsonify({"error": "Request timed out"}), 504
    except YtDlpError as e:
        return jsonify({"error": f"Could not fetch video info: {e}"}), 502
    except Exception as e:
        log.exception("Unexpected error during analysis")
        return jsonify({"error": f"Unexpected error: {e}"}), 500


@app.route("/download", methods=["POST"])
def start_download() -> Response:
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    format_id = (body.get("format_id") or "").strip()
    if not is_valid_youtube_url(url):
        return jsonify({"error": "Invalid YouTube URL"}), 400
    try:
        format_id = sanitize_format_id(format_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        download_id = orchestrator.start_download(url, format_id)
        log.info(f"Download started: {download_id}")
        return jsonify({"download_id": download_id, "status": "queued"})
    except Exception as e:
        log.exception("Failed to start download")
        return jsonify({"error": f"Download failed to start: {e}"}), 500


@app.route("/progress/<download_id>")
def stream_progress(download_id: str) -> Response:
    def event_stream() -> Any:
        last_emit = 0.0
        while True:
            state = orchestrator.get_progress(download_id)
            payload = json.dumps(state)
            yield f"data: {payload}\n\n"

            status = state.get("status")
            if status in {"complete", "failed", "cancelled"} or state.get("error"):
                break

            now = time.time()
            if now - last_emit > 15:
                yield ": keep-alive\n\n"
                last_emit = now

            time.sleep(PROGRESS_UPDATE_INTERVAL)

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return Response(stream_with_context(event_stream()), headers=headers)


@app.route("/downloads/<download_id>")
def serve_file(download_id: str) -> Response:
    try:
        return storage.serve_file(download_id)
    except FileNotFoundError:
        return jsonify({"error": "File not found"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        log.exception("Failed to serve file")
        return jsonify({"error": f"Unexpected error: {e}"}), 500


@app.route("/cancel/<download_id>", methods=["POST"])
def cancel_download(download_id: str) -> Response:
    orchestrator.cancel_download(download_id)
    return jsonify({"status": "cancelled"})


@app.route("/health")
def health_check() -> Response:
    def check_ytdlp_available() -> bool:
        return get_ytdlp_version() is not None

    def check_disk_space() -> bool:
        try:
            stat = os.statvfs(str(downloads_path))
            free_bytes = stat.f_bavail * stat.f_frsize
            return free_bytes > 5 * 1024**3
        except Exception:
            return False

    checks = {
        "ytdlp": check_ytdlp_available(),
        "disk_space": check_disk_space(),
        "downloads_dir": downloads_path.exists(),
        "active_downloads": len(orchestrator.active_downloads),
    }
    if checks["ytdlp"] and checks["disk_space"] and checks["downloads_dir"]:
        return jsonify({**checks, "status": "healthy"}), 200
    return jsonify({**checks, "status": "degraded"}), 503


# ----------------------------
# Startup & Periodic Cleanup
# ----------------------------


def periodic_cleanup() -> None:
    while True:
        time.sleep(3600)
        try:
            orchestrator.cleanup_expired_downloads()
        except Exception as e:
            log.warning(f"Cleanup error: {e}")


def startup_checks() -> None:
    log.info("Fetch started")
    log.info(f"Downloads directory: {DOWNLOAD_DIR}")
    
    ensure_download_dir(DOWNLOAD_DIR)
    
    try:
        version = get_ytdlp_version()
        log.info(f"yt-dlp version: {version}")
    except Exception:
        log.error("ERROR: yt-dlp not installed or not accessible")
        # Do not sys.exit in hosted environments; allow /health to reflect degraded state


def start_background_tasks() -> None:
    import threading

    cleanup_thread = threading.Thread(target=periodic_cleanup, daemon=True)
    cleanup_thread.start()


# ----------------------------
# Main
# ----------------------------


startup_checks()
start_background_tasks()

if __name__ == "__main__":
    log.info("Fetch started")
    log.info(f"Downloads directory: {DOWNLOAD_DIR}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)


