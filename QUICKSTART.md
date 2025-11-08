# Quick Start Guide - Fetch

## Installation

1. **Install dependencies:**
```bash
cd /Users/erick/cursor/fetch
pip install -r requirements.txt
```

2. **Verify installation:**
```bash
python -c "from app import analyzer; print('✓ App loads successfully')"
yt-dlp --version
```

## Running the Application

### Option 1: Development Mode (Simple)
```bash
PORT=8000 python app.py
```

Then open in your browser: http://localhost:8000

### Option 2: Production Mode (Recommended)
```bash
gunicorn -w 1 --worker-class gevent -b 0.0.0.0:8000 app:app
```

Then open in your browser: http://localhost:8000

## Using the Application

1. **Paste a YouTube URL** into the input field
2. **Click "Analyze"** - wait a few seconds
3. **Select your preferred format:**
   - **Complete Files** (video + audio) - Best for most users
   - **Video Only** - If you want to add custom audio
   - **Audio Only** - For music/podcasts
4. **Click "Download Selected Format"**
5. **Wait for download** - progress bar shows status
6. **Save the file** when complete

## Troubleshooting

### Port 5000 in Use
```bash
# Use a different port
PORT=8000 python app.py
```

### "Format not available" Error
1. Click "Analyze" again to refresh formats
2. Try a different format (prefer complete files)
3. Update yt-dlp: `pip install --upgrade yt-dlp`

### 403 Forbidden Error
- The Cobalt API fallback will appear automatically
- Click one of the fallback buttons
- Or update yt-dlp: `pip install --upgrade yt-dlp`

### No Formats Showing
- Video might be private, age-restricted, or deleted
- Try a different video
- Check the browser console for errors

## Tips

- **Best quality:** Select the highest resolution in "Complete Files"
- **Smaller files:** Select 720p or 480p formats
- **Audio only:** Great for music videos - saves space
- **Video only:** Useful if you want to add custom audio later

## Health Check

Verify the app is working:
```bash
curl http://localhost:8000/health
```

Should return:
```json
{
  "active_downloads": 0,
  "disk_space": true,
  "downloads_dir": true,
  "status": "healthy",
  "ytdlp": true
}
```

## File Retention

- Downloaded files are kept for **24 hours**
- Files are automatically deleted after 24 hours
- Download files immediately to keep them

## Supported Sites

Currently supports:
- youtube.com
- youtu.be
- m.youtube.com

## Need Help?

Check the DEBUG_REPORT.md file for detailed information about recent fixes and common issues.

