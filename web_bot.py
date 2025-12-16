import os
import sys
import asyncio
import logging
import json
import aiohttp
from pathlib import Path
from datetime import datetime
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from aiohttp import web
import requests

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
API_ID = int(os.getenv('API_ID', '123456'))
API_HASH = os.getenv('API_HASH', 'your_api_hash_here')
SESSION_STRING = os.getenv('SESSION_STRING', '')
GH_TOKEN = os.getenv('GH_TOKEN', '')           # GitHub Personal Access Token
GH_REPO = os.getenv('GH_REPO', '')             # username/repo
BOT_TOKEN = os.getenv('BOT_TOKEN', '')         # Your bot token for file URLs
PORT = int(os.getenv('PORT', 10000))

# Speed options
SPEED_OPTIONS = [
    [Button.inline("0.5x", b"speed_0.5"), Button.inline("0.75x", b"speed_0.75")],
    [Button.inline("1.25x", b"speed_1.25"), Button.inline("1.5x", b"speed_1.5")],
    [Button.inline("2.0x", b"speed_2.0"), Button.inline("3.0x", b"speed_3.0")],
    [Button.inline("❌ Cancel", b"cancel")]
]

# Store user sessions
user_sessions = {}

class GitHubActionsClient:
    def __init__(self, token, repo):
        self.token = token
        self.repo = repo
        self.base_url = f"https://api.github.com/repos/{repo}"
        self.headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json'
        }
    
    async def trigger_video_workflow(self, video_url, speed, chat_id, message_id):
        """Trigger GitHub Actions workflow."""
        try:
            url = f"{self.base_url}/dispatches"
            
            payload = {
                'event_type': 'process_video',
                'client_payload': {
                    'video_url': video_url,
                    'speed': speed,
                    'chat_id': chat_id,
                    'message_id': message_id,
                    'timestamp': datetime.now().isoformat()
                }
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, 
                    json=payload,
                    headers=self.headers
                ) as response:
                    if response.status == 204:
                        logger.info(f"GitHub Actions triggered for chat {chat_id}")
                        return True
                    else:
                        error_text = await response.text()
                        logger.error(f"GitHub API error: {response.status} - {error_text}")
                        return False
                        
        except Exception as e:
            logger.error(f"Trigger workflow error: {e}")
            return False
    
    def get_direct_video_url(self, file_id):
    """Get direct download URL for Telegram file."""
    # First get file path
    file_info_url = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}"
    print(f"[DEBUG] Calling Telegram API: {file_info_url}")  # Logs the URL being called
    
    response = requests.get(file_info_url)
    print(f"[DEBUG] API Response Status: {response.status_code}")  # Logs the HTTP status
    print(f"[DEBUG] API Response Body: {response.text}")           # Logs the full response
    
    if response.status_code == 200:
        data = response.json()
        if data.get('ok'):
            file_path = data['result']['file_path']
            final_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
            print(f"[DEBUG] Success! Direct URL: {final_url}")
            return final_url
        else:
            print(f"[DEBUG] Telegram API returned 'ok: false'. Description: {data.get('description')}")
    else:
        print(f"[DEBUG] HTTP request failed with status {response.status_code}")
    
    return None

class TelegramGitHubBot:
    def __init__(self):
        self.client = TelegramClient(
            StringSession(SESSION_STRING),
            API_ID,
            API_HASH
        )
        self.me = None
        self.github_client = None
        
        if GH_TOKEN and GH_REPO:
            self.github_client = GitHubActionsClient(GH_TOKEN, GH_REPO)
    
    async def setup_handlers(self):
        """Setup all event handlers."""
        
        @self.client.on(events.NewMessage(pattern='/start'))
        async def start_handler(event):
            """Handle /start command."""
            if not self.me:
                self.me = await self.client.get_me()
            
            status = "✅ GitHub Actions Enabled" if self.github_client else "⚠️ GitHub Actions Not Configured"
            
            welcome = f"""
🎬 **Video Speed Bot** (GitHub Actions)

**Status:** {status}
**How it works:**
1. Send me a video
2. Choose speed
3. I trigger GitHub Actions
4. GitHub processes video
5. You get result in seconds!

**Ready? Send me a video!**
            """
            await event.reply(welcome)
        
        @self.client.on(events.NewMessage(pattern='/help'))
        async def help_handler(event):
            """Handle /help command."""
            help_text = """
**Commands:**
/start - Start bot
/help - This message
/status - Check bot status

**Speed Options:**
• 0.5x - Half speed
• 0.75x - Slow
• 1.25x - Slightly fast
• 1.5x - Fast (recommended)
• 2.0x - Double speed
• 3.0x - Triple speed

**Processing:**
• Videos processed on GitHub Actions
• Much faster than free servers
• 2000 free minutes/month
            """
            await event.reply(help_text)
        
        @self.client.on(events.NewMessage(pattern='/status'))
        async def status_handler(event):
            """Handle /status command."""
            status = f"""
**Bot Status**
✅ Online and active

**GitHub Actions:** {'✅ Enabled' if self.github_client else '❌ Disabled'}
**Repository:** {GH_REPO or 'Not set'}
**Active sessions:** {len(user_sessions)}

**Ready to process videos!**
            """
            await event.reply(status)
        
        @self.client.on(events.NewMessage(
            func=lambda e: e.video or (
                e.document and e.document.mime_type and 
                'video' in str(e.document.mime_type).lower()
            )
        ))
        async def video_handler(event):
            """Handle incoming videos."""
            user_id = event.sender_id
            
            try:
                # Get video info
                if event.video:
                    media = event.video
                    file_name = "video.mp4"
                else:
                    media = event.document
                    file_name = media.file_name or "video.mp4"
                
                # Check size (GitHub Actions has 2GB artifact limit)
                if media.size > 2 * 1024 * 1024 * 1024:
                    await event.reply("❌ **File too large!** Max 2GB for GitHub Actions")
                    return
                
                # Store session
                user_sessions[user_id] = {
                    'media': media,
                    'file_id': media.id,
                    'file_name': file_name,
                    'chat_id': event.chat_id,
                    'message_id': event.message.id,
                    'timestamp': datetime.now()
                }
                
                # Send buttons
                file_size_mb = media.size / (1024*1024)
                await event.reply(
                    f"✅ **Video received!**\n"
                    f"Size: {file_size_mb:.1f}MB\n"
                    f"Choose speed (processed on GitHub):",
                    buttons=SPEED_OPTIONS
                )
                
            except Exception as e:
                logger.error(f"Video handler error: {str(e)}")
                await event.reply(f"❌ Error: {str(e)[:200]}")
        
        @self.client.on(events.CallbackQuery())
        async def callback_handler(event):
            """Handle button callbacks."""
            user_id = event.sender_id
            data = event.data.decode() if event.data else ""
            
            try:
                if data == "cancel":
                    await event.edit("❌ **Operation cancelled.**")
                    if user_id in user_sessions:
                        del user_sessions[user_id]
                    return
                
                elif data.startswith("speed_"):
                    speed = float(data.split("_")[1])
                    
                    if user_id not in user_sessions:
                        await event.edit("❌ No video found! Please send again.")
                        return
                    
                    if not self.github_client:
                        await event.edit("❌ GitHub Actions not configured!")
                        return
                    
                    session = user_sessions[user_id]
                    
                    # Update status
                    await event.edit(f"🚀 **Triggering GitHub Actions...**\nSpeed: {speed}x")
                    
                    # Get direct download URL
                    video_url = self.github_client.get_direct_video_url(session['file_id'])
                    
                    if not video_url:
                        await event.edit("❌ Failed to get video URL")
                        return
                    
                    # Trigger GitHub Actions
                    success = await self.github_client.trigger_video_workflow(
                        video_url=video_url,
                        speed=speed,
                        chat_id=session['chat_id'],
                        message_id=session['message_id']
                    )
                    
                    if success:
                        await event.edit(
                            f"✅ **GitHub Actions triggered!**\n"
                            f"Speed: {speed}x\n"
                            f"Processing will start shortly...\n"
                            f"You'll get the video here when done!"
                        )
                    else:
                        await event.edit("❌ Failed to trigger GitHub Actions")
                    
                    # Cleanup session
                    if user_id in user_sessions:
                        del user_sessions[user_id]
                
            except Exception as e:
                logger.error(f"Callback error: {str(e)}")
                try:
                    await event.edit(f"❌ Error: {str(e)[:200]}")
                except:
                    pass
                if user_id in user_sessions:
                    del user_sessions[user_id]
    
    async def start(self):
        """Start the bot."""
        print("\n" + "="*50)
        print("🎬 TELEGRAM + GITHUB ACTIONS BOT")
        print("="*50)
        
        # Connect with session string
        await self.client.start()
        self.me = await self.client.get_me()
        
        # Setup handlers
        await self.setup_handlers()
        
        print(f"✅ Logged in as: @{self.me.username}")
        if self.github_client:
            print(f"✅ GitHub Actions enabled for: {GH_REPO}")
        else:
            print("⚠️  GitHub Actions not configured")
        print("✅ Bot is ready!")
        print("💬 Send videos to trigger GitHub Actions")
        print("="*50)
        
        # Keep running
        await self.client.run_until_disconnected()
    
    async def stop(self):
        """Stop the bot."""
        await self.client.disconnect()

async def handle_health(request):
    """Health check endpoint."""
    return web.Response(text="✅ Bot is running!")

async def start_bot(app):
    """Start the Telegram bot in background."""
    bot = TelegramGitHubBot()
    app['bot'] = bot
    asyncio.create_task(bot.start())

async def cleanup_bot(app):
    """Cleanup bot on shutdown."""
    if 'bot' in app:
        await app['bot'].stop()

async def main():
    """Main function."""
    # Create web application
    app = web.Application()
    
    # Add routes
    app.router.add_get('/', handle_health)
    app.router.add_get('/health', handle_health)
    
    # Add startup and cleanup
    app.on_startup.append(start_bot)
    app.on_cleanup.append(cleanup_bot)
    
    # Start web server
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    
    print(f"🌐 Web server on port {PORT}")
    await site.start()
    
    print("✅ Bot is running!")
    print("🛑 Press Ctrl+C to stop")
    
    # Keep running
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        print("\n👋 Shutting down...")
    finally:
        await runner.cleanup()

if __name__ == '__main__':
    asyncio.run(main())
