"""
Cobalt API fallback for when yt-dlp fails with 403 errors.
Cobalt (cobalt.tools) is a modern download service that handles YouTube restrictions well.
"""

import requests
from typing import Dict, Any, Optional


class CobaltDownloader:
    """Fallback downloader using Cobalt API"""
    
    def __init__(self, api_url: str = "https://api.cobalt.tools"):
        self.api_url = api_url
        
    def get_download_url(self, video_url: str, quality: str = "max") -> Optional[Dict[str, Any]]:
        """
        Get direct download URL from Cobalt API
        
        Args:
            video_url: YouTube video URL
            quality: Video quality (max, 2160, 1440, 1080, 720, 480, 360)
            
        Returns:
            Dict with download info or None if failed
        """
        try:
            response = requests.post(
                f"{self.api_url}/",
                json={
                    "url": video_url,
                    "vQuality": quality,
                    "filenamePattern": "basic",
                    "isAudioOnly": False,
                    "disableMetadata": False,
                },
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=30
            )
            
            if response.status_code != 200:
                return None
                
            data = response.json()
            
            # Cobalt returns different response types
            if data.get("status") == "error":
                return None
                
            if data.get("status") == "redirect":
                # Direct download URL
                return {
                    "url": data.get("url"),
                    "filename": data.get("filename", "video.mp4"),
                }
                
            if data.get("status") == "picker":
                # Multiple formats available
                picker = data.get("picker", [])
                if picker:
                    # Return first (usually best quality)
                    return {
                        "url": picker[0].get("url"),
                        "filename": data.get("filename", "video.mp4"),
                    }
                    
            return None
            
        except Exception as e:
            print(f"Cobalt API error: {e}")
            return None
    
    def get_audio_url(self, video_url: str) -> Optional[Dict[str, Any]]:
        """Get audio-only download URL"""
        try:
            response = requests.post(
                f"{self.api_url}/",
                json={
                    "url": video_url,
                    "isAudioOnly": True,
                    "filenamePattern": "basic",
                    "disableMetadata": False,
                },
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=30
            )
            
            if response.status_code != 200:
                return None
                
            data = response.json()
            
            if data.get("status") == "redirect":
                return {
                    "url": data.get("url"),
                    "filename": data.get("filename", "audio.mp3"),
                }
                
            return None
            
        except Exception as e:
            print(f"Cobalt API error: {e}")
            return None

