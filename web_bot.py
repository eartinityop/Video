import os
import sys
import asyncio
import logging
import aiohttp
import json
from pathlib import Path
from datetime import datetime
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from aiohttp import web

# ============================================
# CONFIGURATION & SETUP
# ============================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Load environment variables
API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')
SESSION_STRING = os.getenv('SESSION_STRING')
GH_TOKEN = os.getenv('GH_TOKEN')           # GitHub Personal Access Token
GH_REPO = os.getenv('GH_REPO')             # username/repo
BOT_TOKEN = os.getenv('BOT_TOKEN')         # Telegram Bot Token for file URLs
PORT = int(os.getenv('PORT', 10000))

# Validate critical environment variables
MISSING_VARS = []
if not API_ID: MISSING_VARS.append('API_ID')
if not API_HASH: MISSING_VARS.append('API_HASH')
if not SESSION_STRING: MISSING_VARS.append('SESSION_STRING')
if not BOT_TOKEN: MISSING_VARS.append('BOT_TOKEN')

if MISSING_VARS:
    logger.error(f"❌ Missing required environment variables: {', '.join(MISSING_VARS)}")
    logger.error("Please set these in Render dashboard → Environment")
    sys.exit(1)

if not GH_TOKEN or not GH_REPO:
    logger.warning("⚠️  GitHub Actions not fully configured (GH_TOKEN or GH_REPO missing)")
    logger.warning("Bot will work but cannot trigger GitHub Actions")

# ============================================
# CONSTANTS & GLOBALS
# ============================================

MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB (GitHub Actions limit)

# Speed options with inline buttons
SPEED_OPTIONS = [
    [Button.inline("0.5x", b"speed_0.5"), Button.inline("0.75x", b"speed_0.75")],
    [Button.inline("1.25x", b"speed_1.25"), Button.inline("1.5x", b"speed_1.5")],
    [Button.inline("2.0x", b"speed_2.0"), Button.inline("3.0x", b"speed_3.0")],
    [Button.inline("❌ Cancel", b"cancel")]
]

# Store user sessions: user_id -> session_data
user_sessions = {}

# ============================================
# GITHUB ACTIONS CLIENT
# ============================================

class GitHubActionsClient:
    def __init__(self, token, repo):
        self.token = token
        self.repo = repo
        self.base_url = f"https://api.github.com/repos/{repo}"
        self.headers = {
            'Authorization': f'token {token}',
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': 'Telegram-Video-Bot/1.0'
        }
    
    async def get_direct_video_url(self, file_id):
        """Get direct download URL for Telegram file with detailed error handling."""
        try:
            # Step 1: Get file info from Telegram
            file_info_url = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile"
            params = {'file_id': file_id}
            
            logger.info(f"🔍 Getting file info for file_id: {file_id[:20]}...")
            
            async with aiohttp.ClientSession() as session:
                async with session.get(file_info_url, params=params) as response:
                    response_text = await response.text()
                    logger.debug(f"Telegram API Response: {response.status} - {response_text}")
                    
                    if response.status != 200:
                        logger.error(f"❌ Telegram API HTTP Error: {response.status}")
                        return None
                    
                    data = json.loads(response_text)
                    
                    if not data.get('ok'):
                        error_desc = data.get('description', 'Unknown error')
                        logger.error(f"❌ Telegram API Error: {error_desc}")
                        
                        # Provide user-friendly error messages
                        if "file is too big" in error_desc:
                            logger.error("File exceeds Telegram's bot API limit (20MB for bots)")
                            return "FILE_TOO_BIG"
                        elif "invalid file id" in error_desc:
                            logger.error("File ID is invalid or expired")
                            return "INVALID_FILE_ID"
                        elif "wrong file id" in error_desc:
                            logger.error("Wrong file ID format")
                            return "WRONG_FILE_ID"
                        
                        return None
                    
                    # Success! Extract file path
                    file_path = data['result']['file_path']
                    direct_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
                    
                    logger.info(f"✅ Got direct URL: {direct_url[:80]}...")
                    return direct_url
                    
        except aiohttp.ClientError as e:
            logger.error(f"❌ Network error getting file URL: {str(e)}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"❌ Invalid JSON response from Telegram: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"❌ Unexpected error in get_direct_video_url: {str(e)}")
            return None
    
    async def trigger_video_workflow(self, video_url, speed, chat_id, message_id):
        """Trigger GitHub Actions workflow."""
        try:
            if not self.token or not self.repo:
                logger.error("Cannot trigger workflow: GitHub credentials missing")
                return False
            
            url = f"{self.base_url}/dispatches"
            
            payload = {
                'event_type': 'process_video',
                'client_payload': {
                    'video_url': video_url,
                    'speed': str(speed),
                    'chat_id': str(chat_id),
                    'message_id': str(message_id),
                    'timestamp': datetime.now().isoformat()
                }
            }
            
            logger.info(f"🚀 Triggering GitHub Actions for chat {chat_id}, speed {speed}x")
            logger.debug(f"Payload: {json.dumps(payload, indent=2)}")
            
            async with aiohttp.ClientSession(headers=self.headers) as session:
                async with session.post(url, json=payload) as response:
                    response_text = await response.text()
                    
                    if response.status == 204:
                        logger.info("✅ Successfully triggered GitHub Actions")
                        return True
                    else:
                        logger.error(f"❌ GitHub API error: {response.status} - {response_text}")
                        
                        # Parse error for better messages
                        try:
                            error_data = json.loads(response_text)
                            if 'message' in error_data:
                                logger.error(f"GitHub says: {error_data['message']}")
                        except:
                            pass
                        
                        return False
                        
        except Exception as e:
            logger.error(f"❌ Error triggering workflow: {str(e)}")
            return False

# ============================================
# TELEGRAM BOT MAIN CLASS
# ============================================

class TelegramGitHubBot:
    def __init__(self):
        # Initialize Telegram client
        self.client = TelegramClient(
            StringSession(SESSION_STRING),
            int(API_ID),
            API_HASH
        )
        
        # Initialize GitHub client if credentials exist
        self.github_client = None
        if GH_TOKEN and GH_REPO:
            self.github_client = GitHubActionsClient(GH_TOKEN, GH_REPO)
            logger.info(f"✅ GitHub Actions configured for: {GH_REPO}")
        else:
            logger.warning("⚠️  GitHub Actions not configured")
        
        self.me = None
        self.bot_username = None
    
    # ============================================
    # EVENT HANDLERS
    # ============================================
    
    async def setup_handlers(self):
        """Setup all Telegram event handlers."""
        
        @self.client.on(events.NewMessage(pattern='/start'))
        async def start_handler(event):
            """Handle /start command."""
            if not self.me:
                self.me = await self.client.get_me()
                self.bot_username = self.me.username
            
            github_status = "✅ Enabled" if self.github_client else "❌ Disabled"
            
            welcome = f"""
🎬 **Video Speed Bot** (GitHub Actions)

**Bot:** @{self.bot.username}
**GitHub Actions:** {github_status}
**Max File Size:** 2GB

**How it works:**
1. Send me any video file
2. Choose playback speed
3. I trigger GitHub Actions
4. GitHub processes the video
5. You receive the processed video here

**Commands:**
/start - Show this message
/help - Detailed instructions
/status - Check bot status
/settings - Bot settings

**Ready? Send me a video!** 🎥
            """
            await event.reply(welcome, parse_mode='Markdown')
        
        @self.client.on(events.NewMessage(pattern='/help'))
        async def help_handler(event):
            """Handle /help command."""
            help_text = """
**📖 Detailed Help**

**Supported Formats:**
• MP4, MOV, AVI, MKV, WebM, FLV
• Max size: 2GB (GitHub limit)

**Speed Options:**
• 0.5x - Slow motion (half speed)
• 0.75x - Slightly slow
• 1.25x - Slightly fast
• 1.5x - Fast (most popular)
• 2.0x - Double speed
• 3.0x - Triple speed

**Processing Details:**
• Videos are processed on GitHub's servers
• Processing time: 1-5 minutes depending on size
• Original audio is preserved
• Output format: MP4 with AAC audio

**Troubleshooting:**
• Large files (>500MB) may take longer
• Ensure stable internet connection
• If processing fails, try a smaller file

**Need more help?**
Contact the bot administrator.
            """
            await event.reply(help_text, parse_mode='Markdown')
        
        @self.client.on(events.NewMessage(pattern='/status'))
        async def status_handler(event):
            """Handle /status command."""
            import psutil
            
            # Get system info
            disk = psutil.disk_usage('/')
            memory = psutil.virtual_memory()
            
            status = f"""
**🤖 Bot Status Report**

**Basic Info:**
• Bot: @{self.bot_username or 'Loading...'}
• GitHub Actions: {'✅ Active' if self.github_client else '❌ Inactive'}
• Active Sessions: {len(user_sessions)}

**System Resources:**
• CPU: {psutil.cpu_percent()}%
• Memory: {memory.percent}% used
• Disk: {disk.free/(1024**3):.1f}GB free of {disk.total/(1024**3):.1f}GB

**Configuration:**
• GitHub Repo: {GH_REPO or 'Not set'}
• Max File Size: {MAX_FILE_SIZE/(1024**3):.1f}GB

**Bot is {'✅ ONLINE' if self.me else '⏳ STARTING'}**
            """
            await event.reply(status, parse_mode='Markdown')
        
        @self.client.on(events.NewMessage(pattern='/debug'))
        async def debug_handler(event):
            """Debug command (admin only)."""
            # You can add your user ID check here
            # if event.sender_id != YOUR_USER_ID: return
            
            debug_info = f"""
**🔧 Debug Information**

**Environment Variables:**
• API_ID: {'✅ Set' if API_ID else '❌ Missing'}
• API_HASH: {'✅ Set' if API_HASH else '❌ Missing'}
• SESSION_STRING: {'✅ Set' if SESSION_STRING else '❌ Missing'}
• BOT_TOKEN: {'✅ Set' if BOT_TOKEN else '❌ Missing'}
• GH_TOKEN: {'✅ Set' if GH_TOKEN else '❌ Missing'}
• GH_REPO: {'✅ Set' if GH_REPO else '❌ Missing'}

**Session Storage:**
• Active users: {len(user_sessions)}
• User IDs: {list(user_sessions.keys())}

**Bot State:**
• Logged in as: {self.me.username if self.me else 'Not logged in'}
• GitHub Client: {'Ready' if self.github_client else 'Not ready'}
            """
            await event.reply(debug_info, parse_mode='Markdown')
        
        @self.client.on(events.NewMessage(
            func=lambda e: e.video or (
                e.document and e.document.mime_type and 
                'video' in str(e.document.mime_type).lower()
            )
        ))
        async def video_handler(event):
            """Handle incoming video files."""
            user_id = event.sender_id
            
            try:
                # Get video information
                if event.video:
                    media = event.video
                    file_type = "video"
                else:
                    media = event.document
                    file_type = "document"
                
                file_name = getattr(media, 'file_name', 'video.mp4')
                file_size = media.size
                file_size_mb = file_size / (1024 * 1024)
                
                logger.info(f"📹 Received {file_type}: {file_name} ({file_size_mb:.1f}MB) from user {user_id}")
                
                # Check file size
                if file_size > MAX_FILE_SIZE:
                    await event.reply(
                        f"❌ **File too large!**\n"
                        f"Your file: {file_size_mb:.1f}MB\n"
                        f"Maximum allowed: {MAX_FILE_SIZE/(1024*1024):.1f}MB\n\n"
                        f"Please send a smaller video.",
                        parse_mode='Markdown'
                    )
                    return
                
                # Store session information
                user_sessions[user_id] = {
                    'media': media,
                    'file_id': media.id,
                    'file_name': file_name,
                    'file_size_mb': file_size_mb,
                    'chat_id': event.chat_id,
                    'message_id': event.message.id,
                    'timestamp': datetime.now(),
                    'file_type': file_type
                }
                
                # Send speed selection buttons
                await event.reply(
                    f"✅ **Video received successfully!**\n"
                    f"📁 Name: `{file_name}`\n"
                    f"📊 Size: {file_size_mb:.1f}MB\n"
                    f"🔄 Type: {file_type}\n\n"
                    f"**Choose playback speed:**",
                    buttons=SPEED_OPTIONS,
                    parse_mode='Markdown'
                )
                
            except Exception as e:
                logger.error(f"Error in video_handler: {str(e)}")
                await event.reply(
                    f"❌ **Error processing video:**\n`{str(e)[:200]}`",
                    parse_mode='Markdown'
                )
        
        @self.client.on(events.CallbackQuery())
        async def callback_handler(event):
            """Handle inline button callbacks."""
            user_id = event.sender_id
            data = event.data.decode() if event.data else ""
            
            try:
                # Get the message that contains the buttons
                callback_message = await event.get_message()
                
                if data == "cancel":
                    await event.edit("❌ **Operation cancelled.**")
                    if user_id in user_sessions:
                        del user_sessions[user_id]
                    return
                
                elif data.startswith("speed_"):
                    speed = float(data.split("_")[1])
                    
                    # Check if user has a video session
                    if user_id not in user_sessions:
                        await event.edit("❌ **No video found!**\nPlease send a video first.")
                        return
                    
                    # Check GitHub configuration
                    if not self.github_client:
                        await event.edit(
                            "❌ **GitHub Actions not configured!**\n"
                            "The administrator needs to set GH_TOKEN and GH_REPO environment variables."
                        )
                        return
                    
                    session = user_sessions[user_id]
                    
                    # Update status
                    await event.edit(f"⏳ **Getting video URL...**\nSpeed: {speed}x")
                    
                    # Step 1: Get direct download URL from Telegram
                    video_url = await self.github_client.get_direct_video_url(session['file_id'])
                    
                    if not video_url:
                        await event.edit(
                            "❌ **Failed to get video URL!**\n"
                            "This usually means:\n"
                            "1. File is too large (>20MB for bot API)\n"
                            "2. File ID is invalid or expired\n"
                            "3. Bot token is incorrect\n\n"
                            "Try sending the video again."
                        )
                        if user_id in user_sessions:
                            del user_sessions[user_id]
                        return
                    
                    # Handle special error cases
                    if video_url == "FILE_TOO_BIG":
                        await event.edit(
                            "❌ **File too large for bot API!**\n"
                            "Telegram bot API has a 20MB limit for file downloads.\n"
                            "Please send a video smaller than 20MB."
                        )
                        if user_id in user_sessions:
                            del user_sessions[user_id]
                        return
                    
                    if video_url in ["INVALID_FILE_ID", "WRONG_FILE_ID"]:
                        await event.edit(
                            "❌ **File ID error!**\n"
                            "The file ID is invalid or has expired.\n"
                            "Please send the video again."
                        )
                        if user_id in user_sessions:
                            del user_sessions[user_id]
                        return
                    
                    # Step 2: Trigger GitHub Actions
                    await event.edit(f"🚀 **Triggering GitHub Actions...**\nSpeed: {speed}x")
                    
                    success = await self.github_client.trigger_video_workflow(
                        video_url=video_url,
                        speed=speed,
                        chat_id=session['chat_id'],
                        message_id=session['message_id']
                    )
                    
                    if success:
                        await event.edit(
                            f"✅ **GitHub Actions triggered successfully!**\n\n"
                            f"**Details:**\n"
                            f"• Speed: {speed}x\n"
                            f"• Size: {session['file_size_mb']:.1f}MB\n"
                            f"• File: `{session['file_name']}`\n\n"
                            f"⏳ **Processing has started on GitHub...**\n"
                            f"You'll receive the processed video here once it's done.\n"
                            f"Estimated time: 1-5 minutes depending on file size."
                        )
                        
                        # Log successful trigger
                        logger.info(f"✅ Workflow triggered for user {user_id}: {session['file_name']} at {speed}x")
                    else:
                        await event.edit(
                            "❌ **Failed to trigger GitHub Actions!**\n"
                            "This could be due to:\n"
                            "1. Invalid GitHub token\n"
                            "2. Incorrect repository name\n"
                            "3. GitHub API rate limit\n"
                            "4. Network issues\n\n"
                            "Please try again later or contact the administrator."
                        )
                    
                    # Cleanup session
                    if user_id in user_sessions:
                        del user_sessions[user_id]
                
            except Exception as e:
                logger.error(f"Error in callback_handler: {str(e)}")
                try:
                    await event.edit(f"❌ **Unexpected error:**\n`{str(e)[:200]}`")
                except:
                    pass
                
                # Cleanup on error
                if user_id in user_sessions:
                    del user_sessions[user_id]
    
    # ============================================
    # BOT LIFECYCLE
    # ============================================
    
    async def start(self):
        """Start the Telegram bot."""
        print("\n" + "="*60)
        print("🎬 TELEGRAM VIDEO BOT WITH GITHUB ACTIONS")
        print("="*60)
        
        try:
            # Connect to Telegram
            await self.client.start()
            self.me = await self.client.get_me()
            self.bot_username = self.me.username
            
            # Setup event handlers
            await self.setup_handlers()
            
            print(f"✅ Logged in as: @{self.bot_username} (ID: {self.me.id})")
            print(f"✅ GitHub Actions: {'ENABLED' if self.github_client else 'DISABLED'}")
            if self.github_client:
                print(f"✅ Repository: {GH_REPO}")
            print(f"✅ Bot is ready and listening for messages...")
            print("="*60)
            print("💡 Send /start to your bot to begin")
            print("💡 Send /status to check bot health")
            print("💡 Send /debug for technical details")
            print("="*60)
            
            # Keep the bot running
            await self.client.run_until_disconnected()
            
        except Exception as e:
            logger.error(f"Failed to start bot: {str(e)}")
            raise
    
    async def stop(self):
        """Stop the bot gracefully."""
        await self.client.disconnect()
        logger.info("Bot stopped gracefully")

# ============================================
# WEB SERVER FOR RENDER
# ============================================

async def handle_health(request):
    """Health check endpoint for Render."""
    return web.Response(text="✅ Telegram Video Bot is running!")

async def handle_root(request):
    """Root endpoint with basic information."""
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Telegram Video Speed Bot</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
                line-height: 1.6;
                color: #333;
                max-width: 800px;
                margin: 0 auto;
                padding: 20px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                min-height: 100vh;
            }
            .container {
                background: white;
                border-radius: 20px;
                padding: 40px;
                box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                margin-top: 40px;
            }
            h1 {
                color: #2d3748;
                text-align: center;
                font-size: 2.5em;
                margin-bottom: 10px;
            }
            .status {
                background: #48bb78;
                color: white;
                padding: 10px 20px;
                border-radius: 50px;
                display: inline-block;
                font-weight: bold;
                margin: 20px 0;
            }
            .feature-list {
                background: #f7fafc;
                padding: 20px;
                border-radius: 10px;
                margin: 20px 0;
            }
            .feature-item {
                display: flex;
                align-items: center;
                margin: 10px 0;
            }
            .feature-icon {
                font-size: 1.5em;
                margin-right: 10px;
            }
            .instructions {
                background: #e6fffa;
                padding: 20px;
                border-radius: 10px;
                margin: 20px 0;
            }
            .step {
                margin: 15px 0;
                padding-left: 20px;
                position: relative;
            }
            .step:before {
                content: "→";
                position: absolute;
                left: 0;
                color: #4299e1;
                font-weight: bold;
            }
            .footer {
                text-align: center;
                margin-top: 40px;
                color: #718096;
                font-size: 0.9em;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🎬 Telegram Video Speed Bot</h1>
            
            <div style="text-align: center;">
                <div class="status">✅ Bot is Online and Running</div>
            </div>
            
            <div class="feature-list">
                <h3>🌟 Features:</h3>
                <div class="feature-item">
                    <span class="feature-icon">⚡</span>
                    <span>Process videos with FFmpeg on GitHub Actions</span>
                </div>
                <div class="feature-item">
                    <span class="feature-icon">🔧</span>
                    <span>Change playback speed (0.5x to 3.0x)</span>
                </div>
                <div class="feature-item">
                    <span class="feature-icon">📊</span>
                    <span>Supports videos up to 2GB</span>
                </div>
                <div class="feature-item">
                    <span class="feature-icon">🔒</span>
                    <span>Automatic cleanup of temporary files</span>
                </div>
            </div>
            
            <div class="instructions">
                <h3>📱 How to Use:</h3>
                <div class="step">Find the bot on Telegram by searching for your account</div>
                <div class="step">Send /start to begin</div>
                <div class="step">Send any video file to the bot</div>
                <div class="step">Choose your desired playback speed</div>
                <div class="step">Wait for GitHub to process your video</div>
                <div class="step">Receive the processed video in Telegram!</div>
            </div>
            
            <div style="text-align: center; margin: 30px 0;">
                <a href="/health" style="
                    background: #4299e1;
                    color: white;
                    padding: 12px 30px;
                    text-decoration: none;
                    border-radius: 50px;
                    font-weight: bold;
                    display: inline-block;
                ">Health Check</a>
            </div>
            
            <div class="footer">
                <p>Powered by Telegram + GitHub Actions + Render</p>
                <p>This service processes videos in the cloud using GitHub's infrastructure</p>
            </div>
        </div>
    </body>
    </html>
    """
    return web.Response(text=html_content, content_type='text/html')

async def start_bot(app):
    """Start the Telegram bot in background."""
    bot = TelegramGitHubBot()
    app['bot'] = bot
    # Start bot in background task
    asyncio.create_task(bot.start())

async def cleanup_bot(app):
    """Cleanup bot on shutdown."""
    if 'bot' in app:
        await app['bot'].stop()

async def main():
    """Main function to start web server and bot."""
    print("🚀 Starting Telegram Video Bot...")
    print(f"🌐 Web server will run on port {PORT}")
    
    # Create web application
    app = web.Application()
    
    # Add routes
    app.router.add_get('/', handle_root)
    app.router.add_get('/health', handle_health)
    
    # Add startup and cleanup callbacks
    app.on_startup.append(start_bot)
    app.on_cleanup.append(cleanup_bot)
    
    # Start web server
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    
    await site.start()
    print(f"✅ Web server started on http://0.0.0.0:{PORT}")
    print("🤖 Telegram bot is starting in the background...")
    print("🛑 Press Ctrl+C to stop the server")
    
    # Keep running until interrupted
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        print("\n👋 Shutting down gracefully...")
    finally:
        await runner.cleanup()

# ============================================
# ENTRY POINT
# ============================================

if __name__ == '__main__':
    # Check for critical issues before starting
    print("🔍 Pre-flight check...")
    
    if not BOT_TOKEN:
        print("❌ CRITICAL: BOT_TOKEN environment variable is not set!")
        print("Please set it in Render dashboard → Environment")
        print("Get your bot token from @BotFather on Telegram")
        sys.exit(1)
    
    if not GH_TOKEN or not GH_REPO:
        print("⚠️  WARNING: GitHub Actions configuration is incomplete")
        print("The bot will work but cannot trigger video processing")
        print("Set GH_TOKEN and GH_REPO to enable GitHub Actions")
    
    print("✅ All checks passed. Starting application...")
    
    # Run the application
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Application stopped by user")
    except Exception as e:
        print(f"\n💥 Application crashed: {str(e)}")
        sys.exit(1)
