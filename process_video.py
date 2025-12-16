import os
import sys
import requests
import subprocess
import json

# Configuration from environment variables
TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN')
VIDEO_URL = os.environ.get('VIDEO_URL')
SPEED = float(os.environ.get('SPEED', '1.5'))
CHAT_ID = os.environ.get('CHAT_ID')
MESSAGE_ID = os.environ.get('MESSAGE_ID')

def send_telegram_message(text):
    """Send message to Telegram."""
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    data = {
        'chat_id': CHAT_ID,
        'text': text,
        'parse_mode': 'Markdown'
    }
    try:
        response = requests.post(url, data=data)
        return response.json()
    except Exception as e:
        print(f"Error sending message: {e}")
        return None

def send_telegram_video(video_path, caption):
    """Send video to Telegram."""
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendVideo"
    
    with open(video_path, 'rb') as video_file:
        files = {'video': video_file}
        data = {
            'chat_id': CHAT_ID,
            'caption': caption,
            'supports_streaming': True,
            'parse_mode': 'Markdown'
        }
        try:
            response = requests.post(url, files=files, data=data)
            return response.json()
        except Exception as e:
            print(f"Error sending video: {e}")
            return None

def download_file(url, filename):
    """Download file from URL with progress."""
    print(f"Downloading from {url}")
    response = requests.get(url, stream=True)
    total_size = int(response.headers.get('content-length', 0))
    
    with open(filename, 'wb') as f:
        downloaded = 0
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total_size > 0:
                    percent = (downloaded / total_size) * 100
                    if int(percent) % 10 == 0:  # Log every 10%
                        print(f"Downloaded: {percent:.1f}%")
    
    print(f"Downloaded to {filename}")
    return True

def process_video():
    """Main processing function."""
    try:
        # Send start message
        send_telegram_message(f"⚙️ *Processing started on GitHub*\nSpeed: {SPEED}x")
        
        # Download video
        input_file = "input.mp4"
        if not download_file(VIDEO_URL, input_file):
            send_telegram_message("❌ Failed to download video")
            return False
        
        # Create audio filter for speed
        def create_audio_filter(speed):
            if speed > 2.0:
                atempo_filters = []
                remaining = speed
                while remaining > 2.0:
                    atempo_filters.append("atempo=2.0")
                    remaining /= 2.0
                atempo_filters.append(f"atempo={remaining:.2f}")
                return ",".join(atempo_filters)
            elif speed < 0.5:
                atempo_filters = []
                remaining = speed
                while remaining < 0.5:
                    atempo_filters.append("atempo=0.5")
                    remaining *= 2.0
                atempo_filters.append(f"atempo={remaining:.2f}")
                return ",".join(atempo_filters)
            else:
                return f"atempo={speed}"
        
        audio_filter = create_audio_filter(SPEED)
        video_filter = f"setpts={1/SPEED:.5f}*PTS"
        
        # Process with FFmpeg
        output_file = "output.mp4"
        cmd = [
            'ffmpeg', '-i', input_file,
            '-filter_complex', f'[0:v]{video_filter}[v];[0:a]{audio_filter}[a]',
            '-map', '[v]', '-map', '[a]',
            '-c:v', 'libx264', '-preset', 'medium',
            '-crf', '23', '-c:a', 'aac',
            '-b:a', '192k', '-movflags', '+faststart',
            '-y', output_file
        ]
        
        print(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            error_msg = result.stderr[:200] if result.stderr else "Unknown error"
            send_telegram_message(f"❌ Processing failed:\n```{error_msg}```")
            return False
        
        # Get file size
        import os
        file_size = os.path.getsize(output_file)
        file_size_mb = file_size / (1024 * 1024)
        
        # Send video
        send_telegram_message(f"✅ *Processing complete!*\n📤 Uploading {file_size_mb:.1f}MB...")
        
        response = send_telegram_video(output_file, f"✅ **Speed: {SPEED}x**")
        
        if response and response.get('ok'):
            send_telegram_message(f"🎉 *Done!* Speed: {SPEED}x")
        else:
            send_telegram_message("❌ Failed to upload video")
        
        # Cleanup
        if os.path.exists(input_file):
            os.remove(input_file)
        if os.path.exists(output_file):
            os.remove(output_file)
        
        return True
        
    except Exception as e:
        send_telegram_message(f"❌ *Error:* {str(e)[:200]}")
        return False

if __name__ == "__main__":
    process_video()
