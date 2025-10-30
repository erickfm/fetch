# Fetch - Railway Deployment Guide

## Overview

Fetch is a resilient Flask web application that provides a user-friendly interface for yt-dlp. This guide covers local testing and Railway deployment.

---

## Local Development

### Prerequisites

- Python 3.9+
- yt-dlp installed (`pip install yt-dlp` or via system package manager)
- ffmpeg (optional, for format muxing)

### Setup

```bash
# Clone/navigate to project
cd fetch

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run the app
python app.py
```

The app will start on `http://localhost:5000`

### Environment Variables (Optional)

```bash
export DOWNLOAD_DIR=./downloads        # Default: ./downloads
export MAX_CONCURRENT=3                # Default: 3
export FILE_RETENTION_HOURS=24         # Default: 24
export YTDLP_TIMEOUT=300              # Default: 300 seconds
export PORT=5000                       # Default: 5000
```

---

## Railway Deployment

### Step 1: Connect Repository

1. Go to [Railway](https://railway.app)
2. Click "New Project" → "Deploy from GitHub repo"
3. Select your `fetch` repository
4. Railway will auto-detect Python and use the `Procfile`

### Step 2: Configure Environment Variables

In Railway dashboard → Variables tab, add:

```
DOWNLOAD_DIR=./downloads
MAX_CONCURRENT=3
FILE_RETENTION_HOURS=24
YTDLP_TIMEOUT=300
```

**Note:** `PORT` is automatically provided by Railway.

### Step 3: Verify Buildpacks

Railway should automatically detect:
- Python buildpack (from `requirements.txt`)
- Gunicorn server (from `Procfile`)

### Step 4: Deploy

Railway will automatically deploy on push to main branch.

### Step 5: Verify Health

Once deployed, visit:
```
https://your-app.railway.app/health
```

Expected response:
```json
{
  "ytdlp": true,
  "disk_space": true,
  "downloads_dir": true,
  "active_downloads": 0,
  "status": "healthy"
}
```

---

## Post-Deployment

### Testing the Application

1. Visit your Railway URL
2. Paste a YouTube URL (e.g., `https://youtube.com/watch?v=dQw4w9WgXcQ`)
3. Click "Analyze Video"
4. Select a format
5. Click "Download Selected"
6. Monitor progress bar
7. Download the file when complete

### Monitoring

Railway provides:
- **Logs:** View in Railway dashboard → Deployments → Logs
- **Metrics:** CPU, Memory, Network usage
- **Health checks:** Railway can ping `/health` endpoint

### Key Log Messages

```
INFO: Fetch started
INFO: Downloads directory: ./downloads
INFO: yt-dlp version: 2024.10.22
INFO: Analyzing URL: https://...
INFO: Found 15 formats for: Video Title
INFO: Download started: abc123def456
INFO: Download complete: abc123def456 - 125.4MB
INFO: Cleaned up expired download: abc123def456
```

---

## Architecture

```
┌─────────────────────────────────────────┐
│          Railway Container              │
│                                         │
│  ┌───────────────────────────────────┐ │
│  │   Gunicorn (gevent worker)        │ │
│  │   - Flask app                     │ │
│  │   - SSE for progress streaming    │ │
│  └───────────────────────────────────┘ │
│              ↓                          │
│  ┌───────────────────────────────────┐ │
│  │   3 Agents                        │ │
│  │   - Format Analyzer               │ │
│  │   - Download Orchestrator         │ │
│  │   - Storage Agent                 │ │
│  └───────────────────────────────────┘ │
│              ↓                          │
│  ┌───────────────────────────────────┐ │
│  │   yt-dlp subprocess               │ │
│  │   (isolated execution)            │ │
│  └───────────────────────────────────┘ │
│              ↓                          │
│  ┌───────────────────────────────────┐ │
│  │   ./downloads/ directory          │ │
│  │   (ephemeral, 24h retention)      │ │
│  └───────────────────────────────────┘ │
└─────────────────────────────────────────┘
```

---

## Configuration Details

### Procfile Explanation

```
web: gunicorn -w 1 --worker-class gevent -b 0.0.0.0:$PORT app:app
```

- `-w 1`: Single worker (required for shared in-memory state)
- `--worker-class gevent`: Async I/O for SSE streaming
- `-b 0.0.0.0:$PORT`: Bind to Railway's dynamic port
- `app:app`: Import `app` from `app.py`

### Resource Requirements

- **Memory:** ~2GB per concurrent download
- **Disk:** Temporary storage for downloads (cleaned after 24h)
- **CPU:** Low (mostly I/O bound)
- **Network:** High during downloads

### Recommended Railway Plan

- **Starter Plan** sufficient for personal use
- **Pro Plan** recommended for:
  - Multiple concurrent users
  - Larger video files
  - Higher throughput

---

## Security Features

✅ **Implemented:**
- URL validation with domain whitelist
- Format ID sanitization (alphanumeric only)
- Path traversal protection with `.resolve()` verification
- Subprocess isolation (`shell=False`, env isolation)
- Rate limiting (10 analysis requests per minute per IP)
- No command injection vulnerabilities

---

## Troubleshooting

### Issue: "yt-dlp not installed"

**Cause:** Railway buildpack didn't install yt-dlp

**Fix:** Verify `requirements.txt` includes:
```
yt-dlp==2024.10.22
```

**Test locally:**
```bash
which yt-dlp
yt-dlp --version
```

### Issue: SSE not updating

**Cause:** Gunicorn worker class incorrect

**Fix:** Verify `Procfile` uses `--worker-class gevent`

**Check:** Look for `gevent==24.2.1` in `requirements.txt`

### Issue: Downloads fail immediately

**Cause:** Disk space or permissions

**Check health endpoint:**
```bash
curl https://your-app.railway.app/health
```

Look for `"disk_space": false` or `"downloads_dir": false`

### Issue: "Video unavailable"

**Possible causes:**
- Age-restricted (not supported)
- Private/deleted video
- Geo-blocked content
- yt-dlp needs update

**User-facing errors:**
- "Age-restricted video (not supported)"
- "Video is unavailable or private"
- "URL not recognized as valid YouTube link"

### Issue: Rate limit errors (429)

**Cause:** More than 10 analysis requests per minute from same IP

**Expected behavior:** User sees "Too many requests - wait a minute"

**Fix:** This is intentional to prevent abuse

---

## Maintenance

### Updating yt-dlp

1. Update `requirements.txt`:
   ```
   yt-dlp==2024.11.15  # New version
   ```

2. Test locally:
   ```bash
   pip install -r requirements.txt
   python app.py
   # Test with a few videos
   ```

3. Deploy:
   ```bash
   git commit -am "Update yt-dlp to 2024.11.15"
   git push origin main
   ```

4. Monitor logs for 24 hours
5. If error rate spikes, rollback:
   ```bash
   git revert HEAD
   git push origin main
   ```

### Disk Cleanup

**Automatic:** Background thread runs every hour, removes files older than 24h

**Manual cleanup (if needed):**
```bash
# SSH into Railway container (Railway CLI)
railway run bash
cd downloads
find . -type f -mtime +1 -delete
```

### Monitoring Checklist

Weekly:
- [ ] Check `/health` endpoint status
- [ ] Review error logs for patterns
- [ ] Monitor disk space usage
- [ ] Check for yt-dlp updates

Monthly:
- [ ] Update dependencies (Flask, gunicorn, gevent)
- [ ] Review Railway resource usage
- [ ] Test with various video types

---

## API Endpoints

### `GET /`
Returns HTML interface

### `POST /analyze`
**Input:**
```json
{
  "url": "https://youtube.com/watch?v=..."
}
```

**Output:**
```json
{
  "title": "Video Title",
  "duration": 300,
  "thumbnail": "https://...",
  "formats": [...],
  "categorized": {
    "video_audio": [...],
    "video_only": [...],
    "audio_only": [...]
  }
}
```

### `POST /download`
**Input:**
```json
{
  "url": "https://youtube.com/watch?v=...",
  "format_id": "137"
}
```

**Output:**
```json
{
  "download_id": "abc123def456",
  "status": "queued"
}
```

### `GET /progress/<download_id>`
Server-Sent Events stream:
```
data: {"status": "downloading", "progress": 45.2, "speed": "2.34MiB/s", "eta": "00:23"}
data: {"status": "complete", "progress": 100.0, "speed": "", "eta": ""}
```

### `GET /downloads/<download_id>`
Serves the downloaded file with proper Content-Type headers

### `POST /cancel/<download_id>`
Cancels active download

**Output:**
```json
{
  "status": "cancelled"
}
```

### `GET /health`
Health check endpoint

**Output (healthy):**
```json
{
  "ytdlp": true,
  "disk_space": true,
  "downloads_dir": true,
  "active_downloads": 2,
  "status": "healthy"
}
```

**Output (degraded):**
```json
{
  "ytdlp": false,
  "disk_space": true,
  "downloads_dir": true,
  "active_downloads": 0,
  "status": "degraded"
}
```

---

## Files Overview

```
fetch/
├── app.py                 # Main Flask application + all agents
├── templates/
│   └── index.html         # Single-page UI
├── requirements.txt       # Python dependencies
├── Procfile              # Railway deployment config
├── .gitignore            # Excludes downloads/, venv/, etc.
├── AGENTS.md             # Architecture specification
├── CODE_REVIEW.md        # Implementation review vs spec
├── DEPLOYMENT.md         # This file
└── README.md             # Project overview
```

---

## Support

For issues:
1. Check `/health` endpoint
2. Review Railway logs
3. Test locally with same URL
4. Verify yt-dlp version compatibility

For yt-dlp specific errors, see: https://github.com/yt-dlp/yt-dlp/issues

---

**Version:** 1.0.0  
**Last Updated:** 2025-10-30  
**Status:** Production Ready ✅

