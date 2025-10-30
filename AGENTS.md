# AGENTS.md

**Project:** Fetch  
**Purpose:** Resilient GUI wrapper for yt-dlp with format selection and download management  
**Architecture:** Single-page web application with background download orchestration

---

## System Context

Fetch is a Flask web application that provides a user-friendly interface for yt-dlp, focusing on reliability and simplicity. Users paste a YouTube URL, view all available formats, select what they want, and download. The system handles yt-dlp's brittleness through version pinning, error recovery, and graceful degradation.

**Key Constraints:**
- Must handle yt-dlp failures gracefully (updates, API changes, network issues)
- Must work with single URL at a time (no batch processing initially)
- Must show real-time download progress
- Must run on single process (simple deployment)
- Must persist across yt-dlp breaking changes

---

## Agent Architecture

The system consists of 3 primary agents:

```
┌────────────────────────────────────────────────────┐
│              HTTP REQUEST HANDLER                  │
│  Handles: URL input, format display, download UI   │
└──────────┬─────────────────────────────────────────┘
           │
           ├─────► [Format Analyzer Agent] ◄─────┐
           │       Parses available formats       │
           │                                      │
           │                                      │
           ├─────► [Download Orchestrator Agent] │
           │       Manages download lifecycle     │
           │                                      │
           └─────► [Storage Agent]                │
                   Tracks downloads & cleanup     │
                                                  │
                   ┌────────────────────┐        │
                   │  yt-dlp subprocess │◄───────┘
                   │  (isolated exec)   │
                   └────────────────────┘
```

---

## Agent Specifications

### 1. HTTP Request Handler Agent

**Type:** Synchronous, event-driven  
**Runtime:** Flask WSGI server (Gunicorn)  
**Lifecycle:** Per-request instantiation

**Responsibilities:**
- Serve single-page dashboard
- Accept YouTube URL input
- Display available formats
- Initiate downloads
- Stream progress updates (SSE)
- Serve completed files

**Interfaces:**

```python
@app.route('/')
def index() -> Response:
    """Render main interface"""
    
@app.route('/analyze', methods=['POST'])
def analyze_url() -> Response:
    """Parse URL and return available formats
    Input: {url: str}
    Output: {formats: [...], title: str, duration: int}
    """
    
@app.route('/download', methods=['POST'])
def start_download() -> Response:
    """Initiate download with selected format
    Input: {url: str, format_id: str}
    Output: {download_id: str, status: str}
    """
    
@app.route('/progress/<download_id>')
def stream_progress(download_id: str) -> Response:
    """SSE endpoint for real-time progress
    Output: data: {percent: float, speed: str, eta: str}
    """
    
@app.route('/downloads/<download_id>')
def serve_file(download_id: str) -> Response:
    """Serve completed download"""
    
@app.route('/cancel/<download_id>', methods=['POST'])
def cancel_download(download_id: str) -> Response:
    """Stop active download"""
```

**State Management:**
- In-memory download tracking (dict of active downloads)
- No user authentication (single-user tool)
- Automatic cleanup of old files (24h retention)

**Error Handling:**
```python
# Invalid URL
if not is_valid_youtube_url(url):
    return jsonify({'error': 'Invalid YouTube URL'}), 400

# yt-dlp failure
try:
    formats = analyzer.get_formats(url)
except YtDlpError as e:
    return jsonify({'error': f'Could not fetch video info: {e}'}), 500

# Download failure
if download fails:
    cleanup_partial_files()
    return jsonify({'error': 'Download failed', 'details': str(e)}), 500
```

---

### 2. Format Analyzer Agent

**Type:** Synchronous, stateless  
**Runtime:** Within Flask request context  
**Lifecycle:** Per analysis request

**Responsibilities:**
- Execute yt-dlp in info-only mode
- Parse JSON output into structured format data
- Handle yt-dlp version incompatibilities
- Provide fallback when parsing fails
- Categorize formats (video, audio, video+audio)

**Interfaces:**

```python
class FormatAnalyzer:
    def get_formats(url: str, timeout: int = 30) -> dict:
        """Extract all available formats
        Returns: {
            title: str,
            duration: int,  # seconds
            thumbnail: str,  # URL
            formats: [
                {
                    format_id: str,
                    ext: str,
                    resolution: str,  # "1920x1080" or "audio only"
                    filesize: int,  # bytes, may be None
                    vcodec: str,  # "h264" or "none"
                    acodec: str,  # "aac" or "none"
                    fps: int,
                    quality_label: str,  # "1080p" or "128kbps"
                }
            ]
        }
        Raises: YtDlpError, TimeoutError
        """
        
    def categorize_formats(formats: list) -> dict:
        """Group formats by type
        Returns: {
            'video_audio': [...],  # Complete files (mp4, webm)
            'video_only': [...],   # Video streams
            'audio_only': [...]    # Audio streams
        }
        """
```

**Implementation:**

```python
def get_formats(self, url: str) -> dict:
    """Resilient format extraction with fallbacks"""
    
    # Step 1: Try primary method
    try:
        result = self._run_yt_dlp_info(url)
        return self._parse_formats(result)
    except subprocess.TimeoutExpired:
        raise TimeoutError("yt-dlp took too long to respond")
    except subprocess.CalledProcessError as e:
        # yt-dlp returned error code
        error_msg = e.stderr.decode() if e.stderr else str(e)
        
        # Known recoverable errors
        if 'Sign in to confirm your age' in error_msg:
            raise YtDlpError("Age-restricted video (not supported)")
        elif 'Video unavailable' in error_msg:
            raise YtDlpError("Video is unavailable or private")
        elif 'Unsupported URL' in error_msg:
            raise YtDlpError("URL not recognized as valid YouTube link")
        else:
            raise YtDlpError(f"yt-dlp error: {error_msg}")
    except json.JSONDecodeError:
        # yt-dlp output format changed (version mismatch)
        raise YtDlpError("yt-dlp output format unreadable (try updating)")

def _run_yt_dlp_info(self, url: str) -> dict:
    """Execute yt-dlp in JSON mode"""
    result = subprocess.run(
        [
            'yt-dlp',
            '--dump-json',  # JSON output
            '--no-playlist',  # Single video only
            '--no-warnings',  # Cleaner output
            '--skip-download',  # Info only
            url
        ],
        capture_output=True,
        timeout=30,
        check=True,
        text=True
    )
    return json.loads(result.stdout)

def _parse_formats(self, info: dict) -> dict:
    """Extract relevant format data"""
    formats = []
    
    for f in info.get('formats', []):
        # Filter out storyboard/thumbnail formats
        if f.get('format_note') == 'storyboard':
            continue
            
        formats.append({
            'format_id': f['format_id'],
            'ext': f.get('ext', 'unknown'),
            'resolution': f.get('resolution', 'audio only'),
            'filesize': f.get('filesize') or f.get('filesize_approx'),
            'vcodec': f.get('vcodec', 'none'),
            'acodec': f.get('acodec', 'none'),
            'fps': f.get('fps'),
            'vbr': f.get('vbr'),  # Video bitrate
            'abr': f.get('abr'),  # Audio bitrate
        })
    
    return {
        'title': info.get('title', 'Unknown Title'),
        'duration': info.get('duration', 0),
        'thumbnail': info.get('thumbnail'),
        'formats': formats
    }
```

**Format Quality Labels:**

```python
def _add_quality_labels(self, formats: list) -> list:
    """Add human-readable quality labels"""
    for f in formats:
        if f['vcodec'] != 'none' and f['acodec'] != 'none':
            # Video + Audio
            f['quality_label'] = f"{f['resolution']} (complete)"
        elif f['vcodec'] != 'none':
            # Video only
            f['quality_label'] = f"{f['resolution']} @ {f['fps']}fps (video only)"
        else:
            # Audio only
            bitrate = f.get('abr', 'unknown')
            f['quality_label'] = f"{bitrate}kbps (audio only)"
    return formats
```

**Resilience Strategy:**

```python
# Version pinning in requirements.txt
yt-dlp==2024.10.22  # Lock to known-good version

# Periodic updates via CI
# Check for new version weekly, run test suite, auto-update if passes

# Fallback to older version
def _try_fallback_version(self, url: str):
    """If primary fails, try system yt-dlp"""
    try:
        result = subprocess.run(['yt-dlp-legacy', ...])
        # Parse with legacy parser
    except:
        raise YtDlpError("All yt-dlp versions failed")
```

---

### 3. Download Orchestrator Agent

**Type:** Asynchronous, stateful  
**Runtime:** Background thread pool  
**Lifecycle:** Per download request

**Responsibilities:**
- Execute yt-dlp download subprocess
- Parse progress output in real-time
- Track download state (queued, downloading, completed, failed)
- Handle cancellation requests
- Manage temporary and final file locations
- Cleanup on completion or failure

**State Machine:**

```
┌──────────┐
│  QUEUED  │
└────┬─────┘
     │
     ▼
┌──────────────┐
│ DOWNLOADING  │◄───┐
└────┬────┬────┘    │
     │    │         │
     │    └─────────┘ (resume after error)
     │
     ├──► CANCELLED ──► CLEANUP
     │
     ▼
┌──────────┐
│ COMPLETE │
└──────────┘
     │
     ▼ (after 24h)
┌──────────┐
│ EXPIRED  │──► CLEANUP
└──────────┘
```

**Implementation:**

```python
class DownloadOrchestrator:
    def __init__(self):
        self.active_downloads = {}  # {download_id: DownloadState}
        self.executor = ThreadPoolExecutor(max_workers=3)
        
    def start_download(self, url: str, format_id: str) -> str:
        """Initiate async download
        Returns: download_id
        """
        download_id = secrets.token_urlsafe(16)
        output_path = f"./downloads/{download_id}.%(ext)s"
        
        state = DownloadState(
            id=download_id,
            url=url,
            format_id=format_id,
            status='queued',
            output_path=output_path,
            progress=0.0,
            speed='',
            eta='',
            created_at=time.time()
        )
        
        self.active_downloads[download_id] = state
        
        # Submit to thread pool
        future = self.executor.submit(self._download_worker, state)
        future.add_done_callback(lambda f: self._handle_completion(download_id, f))
        
        return download_id
    
    def _download_worker(self, state: DownloadState):
        """Execute download in background thread"""
        state.status = 'downloading'
        
        try:
            process = subprocess.Popen(
                [
                    'yt-dlp',
                    '-f', state.format_id,
                    '--newline',  # Progress on separate lines
                    '--no-playlist',
                    '-o', state.output_path,
                    state.url
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1  # Line buffered
            )
            
            state.process = process
            
            # Parse progress output
            for line in process.stdout:
                if state.cancel_requested:
                    process.kill()
                    state.status = 'cancelled'
                    return
                
                self._parse_progress_line(line, state)
            
            process.wait()
            
            if process.returncode == 0:
                state.status = 'complete'
                state.progress = 100.0
                state.completed_at = time.time()
                # Resolve actual filename
                state.final_path = self._find_downloaded_file(state)
            else:
                raise subprocess.CalledProcessError(process.returncode, 'yt-dlp')
                
        except Exception as e:
            state.status = 'failed'
            state.error = str(e)
            self._cleanup_partial_files(state)
    
    def _parse_progress_line(self, line: str, state: DownloadState):
        """Extract progress from yt-dlp output
        
        Example lines:
        [download]  45.2% of 125.43MiB at 2.34MiB/s ETA 00:23
        [download] 100% of 125.43MiB in 00:54
        """
        if '[download]' not in line:
            return
        
        # Parse percentage
        if '%' in line:
            try:
                percent_str = line.split('%')[0].split()[-1]
                state.progress = float(percent_str)
            except (ValueError, IndexError):
                pass
        
        # Parse speed
        if 'at' in line and '/s' in line:
            try:
                speed = line.split('at')[1].split('ETA')[0].strip()
                state.speed = speed
            except IndexError:
                pass
        
        # Parse ETA
        if 'ETA' in line:
            try:
                eta = line.split('ETA')[1].strip()
                state.eta = eta
            except IndexError:
                pass
    
    def get_progress(self, download_id: str) -> dict:
        """Get current download state for SSE streaming"""
        state = self.active_downloads.get(download_id)
        if not state:
            return {'error': 'Download not found'}
        
        return {
            'status': state.status,
            'progress': state.progress,
            'speed': state.speed,
            'eta': state.eta,
            'error': state.error
        }
    
    def cancel_download(self, download_id: str):
        """Request cancellation of active download"""
        state = self.active_downloads.get(download_id)
        if state and state.status == 'downloading':
            state.cancel_requested = True
```

**Error Recovery:**

```python
def _handle_completion(self, download_id: str, future: Future):
    """Called when download thread finishes"""
    try:
        future.result()  # Raise exception if occurred
    except Exception as e:
        state = self.active_downloads[download_id]
        state.status = 'failed'
        state.error = str(e)
        
        # Cleanup
        self._cleanup_partial_files(state)
        
        # Log for debugging
        app.logger.error(f"Download {download_id} failed: {e}")

def _cleanup_partial_files(self, state: DownloadState):
    """Remove incomplete downloads"""
    pattern = state.output_path.replace('.%(ext)s', '.*')
    for filepath in glob.glob(pattern):
        try:
            os.remove(filepath)
        except OSError:
            pass  # Already cleaned up
```

**File Cleanup:**

```python
def cleanup_expired_downloads(self):
    """Remove downloads older than 24h (run periodically)"""
    cutoff = time.time() - 86400  # 24 hours
    
    for download_id, state in list(self.active_downloads.items()):
        if state.completed_at and state.completed_at < cutoff:
            # Remove file
            if state.final_path and os.path.exists(state.final_path):
                os.remove(state.final_path)
            
            # Remove from tracking
            del self.active_downloads[download_id]
            
            app.logger.info(f"Cleaned up expired download: {download_id}")
```

---

### 4. Storage Agent

**Type:** Synchronous file operations  
**Runtime:** Within Flask app context  
**Lifecycle:** Per file operation

**Responsibilities:**
- Manage downloads directory
- Track file metadata (size, created time)
- Serve files with proper headers
- Handle filename encoding
- Prevent directory traversal attacks

**Implementation:**

```python
class StorageAgent:
    def __init__(self, download_dir='./downloads'):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(exist_ok=True)
    
    def get_file_path(self, download_id: str) -> Path:
        """Get path to downloaded file
        Returns: Path object
        Raises: FileNotFoundError
        """
        # Security: Prevent directory traversal
        safe_id = secrets.compare_digest(download_id, download_id)
        if not safe_id or '/' in download_id or '..' in download_id:
            raise ValueError("Invalid download ID")
        
        # Find file with any extension
        pattern = f"{download_id}.*"
        matches = list(self.download_dir.glob(pattern))
        
        if not matches:
            raise FileNotFoundError(f"Download {download_id} not found")
        
        return matches[0]
    
    def get_file_info(self, filepath: Path) -> dict:
        """Get file metadata"""
        stat = filepath.stat()
        return {
            'filename': filepath.name,
            'size': stat.st_size,
            'created': stat.st_ctime,
            'mimetype': self._guess_mimetype(filepath)
        }
    
    def _guess_mimetype(self, filepath: Path) -> str:
        """Determine MIME type from extension"""
        ext = filepath.suffix.lower()
        mimetypes = {
            '.mp4': 'video/mp4',
            '.webm': 'video/webm',
            '.mkv': 'video/x-matroska',
            '.mp3': 'audio/mpeg',
            '.m4a': 'audio/mp4',
            '.opus': 'audio/opus',
        }
        return mimetypes.get(ext, 'application/octet-stream')
    
    def serve_file(self, download_id: str) -> Response:
        """Generate Flask response for file download"""
        filepath = self.get_file_path(download_id)
        info = self.get_file_info(filepath)
        
        return send_file(
            filepath,
            mimetype=info['mimetype'],
            as_attachment=True,
            download_name=info['filename']
        )
```

---

## Configuration Management

### Environment Variables

```bash
# Optional
DOWNLOAD_DIR=./downloads        # Where files are saved
MAX_CONCURRENT=3                # Max simultaneous downloads
FILE_RETENTION_HOURS=24         # Auto-cleanup after this long
YTDLP_TIMEOUT=300              # Max seconds for yt-dlp execution
PORT=5000                       # HTTP port
```

### Runtime Constants

```python
SUPPORTED_SITES = ['youtube.com', 'youtu.be', 'm.youtube.com']
MAX_FILE_SIZE = 5 * 1024**3  # 5GB
PROGRESS_UPDATE_INTERVAL = 0.5  # SSE update frequency (seconds)
```

---

## Data Flow Diagrams

### Format Analysis Flow

```
User submits URL
    ↓
[HTTP Handler] validates URL format
    ↓
[Format Analyzer] spawns yt-dlp subprocess
    ↓
yt-dlp fetches video info (--dump-json)
    ↓
[Format Analyzer] parses JSON output
    ├─ Success? → Return formatted list
    └─ Failure? → Parse error message → Return user-friendly error
    ↓
[HTTP Handler] renders format selection UI
```

### Download Flow

```
User selects format + clicks download
    ↓
[HTTP Handler] validates request
    ↓
[Download Orchestrator] creates DownloadState
    ├─ Generate unique download_id
    ├─ Determine output path
    └─ Submit to thread pool
    ↓
[Download Orchestrator] spawns yt-dlp subprocess
    ↓
Background thread reads stdout line-by-line
    ├─ Parse progress
    ├─ Update DownloadState
    └─ Check for cancellation
    ↓
User polls /progress/<id> via SSE
    ↓
[HTTP Handler] streams DownloadState changes
    ↓
On completion:
    ├─ Success → [Storage Agent] makes file available
    └─ Failure → [Download Orchestrator] cleans up
    ↓
User clicks download link
    ↓
[Storage Agent] serves file with correct headers
```

---

## Deployment Specification

### Requirements

**Python:** 3.9+  
**Dependencies:**
```
Flask==3.0.0
gunicorn==21.2.0
yt-dlp==2024.10.22  # Pinned version
```

**System Requirements:**
- ffmpeg (for muxing video+audio if needed)
- ~2GB RAM per concurrent download
- Disk space = concurrent_downloads * max_file_size

### Process Model

```
Single process, single worker:
- Main thread: Flask app (handles HTTP)
- Thread pool: Download workers (3 threads)

Why this architecture?
- Simple deployment (no queue infrastructure)
- Shared memory for progress tracking
- SSE requires long-lived connections (Gunicorn handles this)
- Railway/Heroku friendly
```

### Procfile

```
web: gunicorn -w 1 --worker-class gevent -b 0.0.0.0:$PORT app:app
```

**Config notes:**
- Workers: 1 (shared state for downloads)
- Worker class: gevent (async I/O for SSE)
- Timeout: 0 (SSE connections are long-lived)

### Startup Sequence

```python
if __name__ == '__main__':
    # 1. Create downloads directory
    os.makedirs('./downloads', exist_ok=True)
    
    # 2. Verify yt-dlp available
    try:
        subprocess.run(['yt-dlp', '--version'], check=True, capture_output=True)
    except:
        sys.exit("ERROR: yt-dlp not installed")
    
    # 3. Start periodic cleanup (background thread)
    cleanup_thread = Thread(target=periodic_cleanup, daemon=True)
    cleanup_thread.start()
    
    # 4. Start Flask
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)))

def periodic_cleanup():
    """Run every hour"""
    while True:
        time.sleep(3600)
        orchestrator.cleanup_expired_downloads()
```

---

## Resilience Strategies

### yt-dlp Version Management

**Problem:** yt-dlp updates frequently, sometimes breaking changes

**Solution:**

```python
# 1. Pin version in requirements.txt
yt-dlp==2024.10.22

# 2. Weekly CI job to test new versions
# .github/workflows/test-ytdlp-update.yml
- name: Test latest yt-dlp
  run: |
    pip install yt-dlp --upgrade
    pytest tests/test_analyzer.py
    # If passes, create PR to update version

# 3. Fallback to system package
def get_ytdlp_binary() -> str:
    """Prefer pinned version, fall back to system"""
    if os.path.exists('./venv/bin/yt-dlp'):
        return './venv/bin/yt-dlp'
    return 'yt-dlp'  # System package
```

### Network Failures

```python
# Automatic retry with exponential backoff
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    retry=retry_if_exception_type(NetworkError)
)
def _run_yt_dlp_info(self, url: str):
    # ... execution code
```

### YouTube API Changes

```python
# Graceful degradation
def get_formats(self, url: str) -> dict:
    try:
        return self._get_formats_detailed(url)
    except YtDlpError:
        # Fall back to simple mode
        return self._get_formats_simple(url)

def _get_formats_simple(self, url: str) -> dict:
    """Extract only basic formats when detailed fails"""
    # Use -F (list formats) instead of --dump-json
    result = subprocess.run(
        ['yt-dlp', '-F', url],
        capture_output=True,
        text=True,
        timeout=30
    )
    # Parse human-readable output
    formats = self._parse_format_table(result.stdout)
    return {'formats': formats, 'title': 'Unknown', 'duration': 0}
```

### Timeout Handling

```python
# All subprocess calls have timeouts
subprocess.run(..., timeout=30)  # Info fetch
subprocess.run(..., timeout=300) # Download

# User notification
if TimeoutError:
    return jsonify({
        'error': 'Request timed out',
        'suggestion': 'Video may be too large or unavailable'
    })
```

---

## Security Considerations

### Input Validation

```python
def is_valid_youtube_url(url: str) -> bool:
    """Strict URL validation to prevent command injection"""
    if not url or len(url) > 500:
        return False
    
    # Whitelist domains
    parsed = urllib.parse.urlparse(url)
    valid_domains = ['youtube.com', 'youtu.be', 'm.youtube.com', 'www.youtube.com']
    
    if parsed.netloc not in valid_domains:
        return False
    
    # Ensure proper scheme
    if parsed.scheme not in ['http', 'https']:
        return False
    
    return True

def sanitize_format_id(format_id: str) -> str:
    """Prevent command injection via format selection"""
    # Format IDs are always alphanumeric + plus/hyphen
    if not re.match(r'^[a-zA-Z0-9+\-]+$', format_id):
        raise ValueError("Invalid format ID")
    return format_id
```

### File Access Control

```python
def get_file_path(download_id: str) -> Path:
    """Prevent directory traversal attacks"""
    # Validate download_id format (no special characters)
    if not re.match(r'^[a-zA-Z0-9_-]{16,32}$', download_id):
        raise ValueError("Invalid download ID")
    
    # Resolve path and verify it's within downloads directory
    filepath = (self.download_dir / download_id).resolve()
    
    if not str(filepath).startswith(str(self.download_dir.resolve())):
        raise ValueError("Path traversal attempt detected")
    
    return filepath
```

### Resource Limits

```python
# Prevent abuse
MAX_CONCURRENT_DOWNLOADS = 3
MAX_FILE_SIZE = 5 * 1024**3  # 5GB
MAX_ANALYSIS_PER_MINUTE = 10

# Rate limiting (simple in-memory)
analysis_attempts = {}  # {ip: [timestamps]}

@app.before_request
def rate_limit():
    if request.endpoint == 'analyze_url':
        ip = request.remote_addr
        now = time.time()
        
        # Clean old attempts
        attempts = [t for t in analysis_attempts.get(ip, []) 
                   if now - t < 60]
        
        if len(attempts) >= MAX_ANALYSIS_PER_MINUTE:
            abort(429, "Too many requests - wait a minute")
        
        attempts.append(now)
        analysis_attempts[ip] = attempts
```

### Subprocess Isolation

```python
def _run_yt_dlp(self, args: list) -> subprocess.CompletedProcess:
    """Execute yt-dlp with security constraints"""
    # Never use shell=True (prevents injection)
    # Always use absolute paths or validated commands
    # Set resource limits
    
    return subprocess.run(
        ['yt-dlp'] + args,
        capture_output=True,
        text=True,
        timeout=self.timeout,
        check=False,  # Handle errors manually
        shell=False,  # CRITICAL: Prevent command injection
        env={
            **os.environ,
            'HOME': '/tmp',  # Isolate config files
        }
    )
```

---

## Monitoring & Logging

### Log Events

```python
# Startup
log.info("Fetch started")
log.info(f"yt-dlp version: {get_ytdlp_version()}")
log.info(f"Downloads directory: {DOWNLOAD_DIR}")

# Analysis
log.info(f"Analyzing URL: {url}")
log.info(f"Found {len(formats)} formats for: {title}")
log.warning(f"Analysis timeout for: {url}")

# Downloads
log.info(f"Download started: {download_id} - {title}")
log.info(f"Download progress: {download_id} - {progress}%")
log.info(f"Download complete: {download_id} - {filesize}MB")
log.error(f"Download failed: {download_id} - {error}")

# Cleanup
log.info(f"Cleaned up expired download: {download_id}")
log.info(f"Disk space freed: {size}MB")
```

### Metrics

```python
# System metrics
active_downloads_gauge
completed_downloads_total (counter)
failed_downloads_total (counter)
disk_space_used_bytes (gauge)

# Performance metrics
analysis_duration_seconds (histogram)
download_duration_seconds (histogram)
download_speed_mbps (histogram)

# Error rates
ytdlp_errors_total (counter by error_type)
timeout_errors_total (counter)
```

### Health Check

```python
@app.route('/health')
def health_check():
    """Verify system operational"""
    checks = {
        'ytdlp': check_ytdlp_available(),
        'disk_space': check_disk_space(),
        'downloads_dir': os.path.exists(DOWNLOAD_DIR),
        'active_downloads': len(orchestrator.active_downloads)
    }
    
    if all([checks['ytdlp'], checks['disk_space'], checks['downloads_dir']]):
        return jsonify({**checks, 'status': 'healthy'}), 200
    else:
        return jsonify({**checks, 'status': 'degraded'}), 503

def check_ytdlp_available() -> bool:
    try:
        subprocess.run(['yt-dlp', '--version'], 
                      capture_output=True, 
                      timeout=5, 
                      check=True)
        return True
    except:
        return False

def check_disk_space() -> bool:
    """Ensure at least 5GB free"""
    stat = os.statvfs(DOWNLOAD_DIR)
    free_bytes = stat.f_bavail * stat.f_frsize
    return free_bytes > 5 * 1024**3
```

---

## Testing Strategy

### Unit Tests

```python
def test_url_validation():
    assert is_valid_youtube_url('https://youtube.com/watch?v=abc123')
    assert not is_valid_youtube_url('https://example.com')

def test_progress_parsing():
    line = "[download]  45.2% of 125.43MiB at 2.34MiB/s ETA 00:23"
    state = DownloadState()
    parse_progress_line(line, state)
    assert state.progress == 45.2
    assert state.speed == "2.34MiB/s"
    assert state.eta == "00:23"

def test_format_categorization():
    formats = [
        {'vcodec': 'h264', 'acodec': 'aac'},  # Complete
        {'vcodec': 'vp9', 'acodec': 'none'},   # Video only
        {'vcodec': 'none', 'acodec': 'opus'},  # Audio only
    ]
    result = categorize_formats(formats)
    assert len(result['video_audio']) == 1
    assert len(result['video_only']) == 1
    assert len(result['audio_only']) == 1
```

### Integration Tests

```python
def test_end_to_end_download(client):
    # 1. Submit URL
    response = client.post('/analyze', json={
        'url': 'https://youtube.com/watch?v=test123'
    })
    assert response.status_code == 200
    data = response.json
    assert 'formats' in data
    
    # 2. Start download
    format_id = data['formats'][0]['format_id']
    response = client.post('/download', json={
        'url': 'https://youtube.com/watch?v=test123',
        'format_id': format_id
    })
    assert response.status_code == 200
    download_id = response.json['download_id']
    
    # 3. Poll progress
    time.sleep(1)
    response = client.get(f'/progress/{download_id}')
    assert response.status_code == 200
    
    # 4. Download file
    response = client.get(f'/downloads/{download_id}')
    assert response.status_code == 200
    assert response.headers['Content-Type'].startswith('video/')
```

### Mock Strategy

```python
@pytest.fixture
def mock_ytdlp(monkeypatch):
    """Mock yt-dlp subprocess calls"""
    def mock_run(*args, **kwargs):
        cmd = args[0]
        if '--dump-json' in cmd:
            # Return mock video info
            return MockResult(stdout=json.dumps({
                'title': 'Test Video',
                'duration': 123,
                'formats': [
                    {
                        'format_id': '137',
                        'ext': 'mp4',
                        'resolution': '1920x1080',
                        'filesize': 1024000,
                        'vcodec': 'h264',
                        'acodec': 'aac',
                        'fps': 30
                    }
                ]
            }))
        elif '-f' in cmd:
            # Mock download - create dummy file
            output_path = cmd[cmd.index('-o') + 1]
            with open(output_path.replace('.%(ext)s', '.mp4'), 'wb') as f:
                f.write(b'fake video data')
            return MockResult(returncode=0)
    
    monkeypatch.setattr(subprocess, 'run', mock_run)
```

---

## Error Handling Matrix

| Error Type | Detection | User Message | Recovery |
|------------|-----------|--------------|----------|
| Invalid URL | Regex validation | "Invalid YouTube URL" | Prompt for correction |
| Age-restricted | yt-dlp stderr | "Age-restricted (not supported)" | None |
| Private video | yt-dlp stderr | "Video unavailable or private" | None |
| Network timeout | subprocess.TimeoutExpired | "Request timed out - try again" | Retry button |
| yt-dlp version mismatch | JSON parse error | "Tool needs update - contact admin" | Notify maintainer |
| Download interrupted | Process exit code | "Download failed - try again" | Restart download |
| Disk space full | OSError | "Storage full - contact admin" | Clean old files |
| Format not available | Missing format_id | "Format no longer available" | Re-analyze video |

---

## Troubleshooting Guide

### Common Issues

**Issue: "yt-dlp not found"**
```bash
# Verify installation
which yt-dlp
yt-dlp --version

# Reinstall if missing
pip install yt-dlp
```

**Issue: Downloads stuck at 0%**
```python
# Check subprocess status
ps aux | grep yt-dlp

# Check logs for errors
tail -f app.log | grep ERROR

# Kill stuck process
pkill -f "yt-dlp.*<download_id>"
```

**Issue: "Video unavailable"**
```python
# Test manually
yt-dlp --dump-json <url>

# Common causes:
# - Age-restricted (no workaround)
# - Geo-blocked (use VPN/proxy)
# - Private/deleted
# - yt-dlp outdated
```

**Issue: SSE not updating**
```python
# Verify gevent installed
pip install gevent

# Check gunicorn config
gunicorn --worker-class gevent app:app

# Browser compatibility (IE doesn't support SSE)
# Use polling fallback
```

### Debug Mode

```python
# Enable verbose logging
app.config['DEBUG'] = True
logging.basicConfig(level=logging.DEBUG)

# Add debug endpoint
@app.route('/debug/<download_id>')
def debug_download(download_id):
    state = orchestrator.active_downloads.get(download_id)
    if not state:
        return jsonify({'error': 'Not found'}), 404
    
    return jsonify({
        'status': state.status,
        'progress': state.progress,
        'process_alive': state.process.poll() is None if state.process else False,
        'output_path': state.output_path,
        'error': state.error,
        'created_at': state.created_at,
        'elapsed': time.time() - state.created_at
    })
```

---

## User Interface Design

### Main Interface

```
┌────────────────────────────────────────────────┐
│  Fetch                                         │
├────────────────────────────────────────────────┤
│                                                │
│  YouTube URL:                                  │
│  ┌─────────────────────────────────────────┐  │
│  │ https://youtube.com/watch?v=...        │  │
│  └─────────────────────────────────────────┘  │
│                                                │
│  [ Analyze Video ]                             │
│                                                │
└────────────────────────────────────────────────┘
```

### Format Selection

```
┌────────────────────────────────────────────────┐
│  Video Title Here (12:34)                      │
├────────────────────────────────────────────────┤
│                                                │
│  📹 Complete Files (video + audio)             │
│  ┌─────────────────────────────────────────┐  │
│  │ ○ 1080p (mp4) - 245 MB                  │  │
│  │ ○ 720p (mp4) - 128 MB                   │  │
│  │ ○ 480p (webm) - 67 MB                   │  │
│  └─────────────────────────────────────────┘  │
│                                                │
│  🎬 Video Only                                 │
│  ┌─────────────────────────────────────────┐  │
│  │ ○ 1080p60 (webm) - 312 MB               │  │
│  │ ○ 720p60 (webm) - 156 MB                │  │
│  └─────────────────────────────────────────┘  │
│                                                │
│  🎵 Audio Only                                 │
│  ┌─────────────────────────────────────────┐  │
│  │ ○ 128kbps (m4a) - 18 MB                 │  │
│  │ ○ 48kbps (opus) - 7 MB                  │  │
│  └─────────────────────────────────────────┘  │
│                                                │
│  [ Download Selected ]                         │
│                                                │
└────────────────────────────────────────────────┘
```

### Download Progress

```
┌────────────────────────────────────────────────┐
│  Downloading: Video Title Here                 │
├────────────────────────────────────────────────┤
│                                                │
│  ████████████░░░░░░░░░░░░░░░░░░  45.2%         │
│                                                │
│  Speed: 2.34 MB/s    ETA: 00:23                │
│                                                │
│  [ Cancel ]                                    │
│                                                │
└────────────────────────────────────────────────┘
```

### Completion

```
┌────────────────────────────────────────────────┐
│  ✓ Download Complete                           │
├────────────────────────────────────────────────┤
│                                                │
│  Video Title Here                              │
│  1080p (mp4) - 245 MB                          │
│                                                │
│  [ Download File ]  [ New Download ]           │
│                                                │
└────────────────────────────────────────────────┘
```

---

## Scaling Considerations

### Current Limits

**Bottleneck:** Thread pool size (3 concurrent downloads)  
**Throughput:** ~3 videos per download duration (avg 5 min = ~36/hour)  
**Storage:** Limited by disk space (temporary files)

### Scaling Path

**Phase 1: Increase Concurrency (100 users)**
```python
# Increase thread pool
MAX_CONCURRENT_DOWNLOADS = 10

# Add download queue
from queue import Queue
download_queue = Queue(maxsize=50)

# Multiple worker threads
for i in range(10):
    Thread(target=queue_worker, daemon=True).start()
```

**Phase 2: Separate Storage (1000 users)**
```python
# Use S3/Cloudflare R2 for downloads
STORAGE_BACKEND = 'r2'

def save_download(filepath: str) -> str:
    """Upload to R2, return public URL"""
    s3.upload_file(filepath, BUCKET, key)
    return f"{CDN_URL}/{key}"

# Generate presigned URLs for downloads
def get_download_url(download_id: str) -> str:
    return s3.generate_presigned_url('get_object', 
                                     Params={'Bucket': BUCKET, 'Key': key},
                                     ExpiresIn=3600)
```

**Phase 3: Distributed Workers (10,000 users)**
```python
# Use Celery + Redis for queue
@celery.task
def download_video(url: str, format_id: str):
    # Same logic, but distributed
    pass

# Multiple worker machines
celery -A app.celery worker --concurrency=10
```

---

## Maintenance Procedures

### Updating yt-dlp

```bash
# 1. Test new version locally
pip install yt-dlp --upgrade
python test_analyzer.py

# 2. If tests pass, update requirements.txt
yt-dlp==2024.11.15  # New version

# 3. Deploy with rollback plan
git commit -m "Update yt-dlp to 2024.11.15"
git push

# 4. Monitor error rates for 24h
# If error rate spikes, rollback:
git revert HEAD
git push
```

### Disk Space Management

```bash
# Manual cleanup
find ./downloads -type f -mtime +1 -delete

# Monitor disk usage
df -h ./downloads

# Set up cron job (alternative to app-based cleanup)
0 * * * * find /app/downloads -type f -mtime +1 -delete
```

---

## References

**Tools:**
- yt-dlp: https://github.com/yt-dlp/yt-dlp
- Flask: https://flask.palletsprojects.com
- Server-Sent Events: https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events

**Deployment:**
- Railway: https://docs.railway.app
- Heroku: https://devcenter.heroku.com

---

**Last Updated:** 2025-10-30  
**Version:** 1.0.0  
**Status:** Production Ready