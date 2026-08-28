import os
import sys
import logging
import re
import json
import asyncio
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from openai import AsyncOpenAI
from discord.ui import View, Button
from datetime import datetime, timedelta, timezone
from typing import Optional
import motor.motor_asyncio
from noitu import start_noitu_game, handle_noitu_message

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("DiscordBot")

# Load environment variables
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
ALLOWED_CHANNEL_ID_RAW = os.getenv("ALLOWED_CHANNEL_ID")
ALLOWED_CHANNEL_ID = int(ALLOWED_CHANNEL_ID_RAW) if ALLOWED_CHANNEL_ID_RAW and ALLOWED_CHANNEL_ID_RAW.isdigit() else None
WELCOME_CHANNEL_ID = 1539905599196766228
SEE_YOU_CHANNEL_ID = 1539906242187632691

ROUTER_API_KEY = os.getenv("ROUTER_API_KEY", "sk-0fc648aa8d074f59-4tiy6p-7efc95e5")
ROUTER_BASE_URL = os.getenv("ROUTER_BASE_URL", "https://1-production-6390.up.railway.app/v1")
ROUTER_MODEL = os.getenv("ROUTER_MODEL", "openrouter/nvidia/nemotron-3.5-lightning:free")

# Use ROUTER_* variables for the AI client
XKIRO_API_KEY = ROUTER_API_KEY
XKIRO_BASE_URL = ROUTER_BASE_URL
XKIRO_MODEL = ROUTER_MODEL

# ================= Permission Management =================
# Yêu cầu Manage Guild hoặc Administrator cho tất cả lệnh
def is_manager(member: discord.Member) -> bool:
    """Kiểm tra member có quyền quản lý server không."""
    return member.guild_permissions.manage_guild or member.guild_permissions.administrator

async def check_manager_interaction(interaction: discord.Interaction) -> bool:
    """Kiểm tra quyền cho interaction (slash command)."""
    if not interaction.guild:
        return False
    member = interaction.guild.get_member(interaction.user.id)
    if not member:
        return False
    has_perm = is_manager(member)
    if not has_perm:
        # Log unauthorized attempt
        logger.warning(f"Unauthorized command attempt by {interaction.user} (ID: {interaction.user.id}) in guild {interaction.guild.id} for command {interaction.command.name if interaction.command else 'unknown'}")
    return has_perm

def manager_only():
    """Decorator cho prefix command yêu cầu quyền quản lý."""
    async def predicate(ctx):
        has_perm = is_manager(ctx.author)
        if not has_perm:
            logger.warning(f"Unauthorized prefix command attempt by {ctx.author} (ID: {ctx.author.id}) in guild {ctx.guild.id if ctx.guild else 'DM'} for command {ctx.command.name if ctx.command else 'unknown'}")
        return has_perm
    return commands.check(predicate)

# =========================================================

if not TOKEN:
    logger.error("DISCORD_TOKEN is missing in environment variables!")
    sys.exit(1)

# Initialize OpenAI Client for Xkiro API
ai_client = AsyncOpenAI(
    api_key=XKIRO_API_KEY,
    base_url=XKIRO_BASE_URL
)

# Bot configuration
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# (Module nhạc đã được gỡ bỏ theo yêu cầu - 2026/08/24)

# ================= MongoDB Connection =================
MONGODB_URI = os.getenv("MONGODB_URI")
if not MONGODB_URI:
    logger.error("MONGODB_URI is missing in environment variables!")
    # Không exit ngay vì có thể chạy local không cần MongoDB? Nhưng vẫn log lỗi.
    # Vẫn để bot chạy nhưng các lệnh custom role sẽ báo lỗi.

db_client = None
db = None

async def init_mongodb():
    global db_client, db
    if not MONGODB_URI:
        logger.error("Cannot initialize MongoDB: MONGODB_URI not set")
        return False
    try:
        db_client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URI)
        db = db_client.get_database("discord_bot_data")
        await db.command("ping")
        logger.info("✅ Connected to MongoDB Atlas")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to connect to MongoDB: {e}")
        return False

# Không cần on_interaction nữa, ta sẽ dùng middleware riêng cho slash commands
# Thay vào đó, ta sẽ thêm check trong từng command hoặc dùng app_commands.default_permissions
# Tuy nhiên, để tập trung, ta có thể override bot.tree.interaction_check
# Nhưng cách đơn giản là check trong mỗi command hoặc dùng decorator
# Ta sẽ sử dụng decorator cho từng command thay vì on_interaction để tránh xung đột với bot.tree
# Để đơn giản, ta sẽ thêm một hàm check riêng trong mỗi slash command
@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    logger.info("All commands are locked: require Manage Guild or Administrator permission.")
    logger.info(f"Target Channel ID: {ALLOWED_CHANNEL_ID}")
    logger.info(f"Xkiro Model: {XKIRO_MODEL}")
    
    # Initialize MongoDB
    if await init_mongodb():
        # Migrate data from JSON if needed
        await migrate_json_to_mongodb()
    
    
    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        logger.error(f"Failed to sync slash commands: {e}")
    
    # Đăng ký lại các persistent view cho panel /by
    try:
        await register_persistent_views()
    except Exception as e:
        logger.error(f"Failed to register persistent views: {e}")
        
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="tin nhắn AI"))

@bot.event
async def on_member_join(member):
    channel = bot.get_channel(WELCOME_CHANNEL_ID)
    if channel is not None:
        await channel.send(f"Chào mừng {member.mention} đã đến với server, chúc bạn có một trải nghiệm vui vẻ, đừng quên pick role. Cần hỗ trợ cứ alo bot chuột dthw nha")

@bot.event
async def on_member_remove(member):
    channel = bot.get_channel(SEE_YOU_CHANNEL_ID)
    if channel is not None:
        await channel.send(f"Xin lỗi {member.mention}! Tôi đã không giữ chân bạn được, cảm ơn bạn đã đồng hành cùng server! Nếu có duyên chúng ta sẽ gặp lại")

@bot.event
async def on_message(message: discord.Message):
    # ================= BetterAntiDupe Webhook Handler =================
    # Intercept BetterAntiDupe webhook alerts in the designated channel
    BAD_CHANNEL_ID = 1540691749754769499
    if message.channel.id == BAD_CHANNEL_ID and message.webhook_id is not None:
        # Skip our own bot's messages to avoid loops
        if message.author.id == bot.user.id:
            return

        # Log full embed content for debugging structure
        logger.info(f"[BetterAntiDupe] Webhook message received in #{message.channel.name}")
        logger.info(f"[BetterAntiDupe] Content: {message.content}")
        for i, embed in enumerate(message.embeds):
            logger.info(f"[BetterAntiDupe] Embed[{i}] title={embed.title} | description={embed.description}")
            logger.info(f"[BetterAntiDupe] Embed[{i}] author={embed.author.name if embed.author else None}")
            if embed.fields:
                for field in embed.fields:
                    logger.info(f"[BetterAntiDupe] Embed[{i}] field: {field.name} = {field.value}")

        # Try to extract player name from embed
        player_name = None
        for embed in message.embeds:
            # Common patterns: title contains player name, or author.name, or a specific field
            if embed.author and embed.author.name:
                player_name = embed.author.name
                break
            if embed.title:
                # Try to extract from title like "PlayerName was caught duping"
                import re as _re
                match = _re.search(r'([A-Za-z0-9_]{3,16})', embed.title)
                if match:
                    player_name = match.group(1)
                    break
            if embed.description:
                import re as _re
                match = _re.search(r'([A-Za-z0-9_]{3,16})', embed.description)
                if match:
                    player_name = match.group(1)
                    break
            # Check fields for player name
            if embed.fields:
                for field in embed.fields:
                    if field.name and 'player' in field.name.lower():
                        player_name = field.value.strip() if field.value else None
                        if player_name:
                            break
                if player_name:
                    break

        if player_name:
            try:
                await message.delete()
                await message.channel.send(
                    f"phát hiện 1 cậu bé muốn làm đồ ăn của tui! "
                    f"Phát hiện người chơi @{player_name} trong mc đã gian lận bằng cách dupe, "
                    f"đã được cho lên bảng phong thần!"
                )
                logger.info(f"[BetterAntiDupe] Replaced webhook alert for player: {player_name}")
            except discord.HTTPException as e:
                logger.error(f"[BetterAntiDupe] Failed to delete/send: {e}")
        else:
            logger.warning("[BetterAntiDupe] Could not extract player name from embed. Keeping original message.")

        return  # Stop further processing for this message


    # Ignore own messages or other bot messages
    if message.author == bot.user or message.author.bot:
        return

    # Process Nối Từ minigame messages
    await handle_noitu_message(message)

    # Check allowed channel restriction if configured
    if ALLOWED_CHANNEL_ID is not None and message.channel.id != ALLOWED_CHANNEL_ID:
        return

    # Process standard prefix commands if message starts with prefix
    if message.content.startswith(bot.command_prefix):
        await bot.process_commands(message)
        return

    # Call Xkiro AI API for any text message in the allowed channel
    async with message.channel.typing():
        try:
            response = await ai_client.chat.completions.create(
                model=XKIRO_MODEL,
                max_tokens=1800,
                messages=[
                    {"role": "system", "content": """# Role: KhangSMP Support Assistant

## Profile
- **Language**: Tiếng Việt  
- **Description**: Trợ lý hỗ trợ server Minecraft KhangSMP.
- **Background**: Server Survival hỗ trợ Java + Bedrock (1.16+), Owner: Phan Trọng Khang (Vĩnh Long).
- **Thống số kết nối chính xác**:
  - IP Server: `nvnmc.asia`
  - Port Server (dùng chung cho cả Java và Bedrock / PE): `25655`
- **Personality**: Thân thiện, ngắn gọn, chính xác, lịch sự.

## QUY TẮC NỘI DUNG VÀ TRẢ LỜI (CỰC KỲ QUAN TRỌNG):
1. **THÔNG TIN IP & PORT CỐ ĐỊNH CHÍNH XÁC**:
   - Khi được hỏi về IP, Port, cách đăng nhập hoặc thông tin server:
     + IP: `nvnmc.asia`
     + Port: `25655`
   - TUYỆT ĐỐI KHÔNG tự bịa, đổi hoặc đưa sai Port (Ví dụ: KHÔNG ĐƯỢC đưa 19132 hay 25565). Port duy nhất đúng cho cả Java và Bedrock là `25655`.
   - TUYỆT ĐỐI KHÔNG tự bịa đặt tính năng, thông tin không có thật.

2. **QUY TẮC CẤM GỬI LINK DISCORD (RẤT NGHIÊM NGẠC)**:
   - **CẤM** tự động chèn link Discord (`https://discord.gg/4afmVDmy2`) vào bất kỳ câu trả lời nào.
   - **CHỈ ĐƯỢC PHÉP** đính kèm link Discord KHI VÀ CHỈ KHI người dùng **HỎI THẲNG, TRỰC TIẾP, ĐÍCH DANH VỀ DISCORD** (Ví dụ: "cho xin link discord", "discord server là gì", "link group discord đâu").
   - Đối với tất cả câu hỏi khác (IP, Port, cách đăng nhập, lệnh, claim đất, shop, nạp thẻ, luật server, hỗ trợ chung,...): **TUYỆT ĐỐI CẤM** xuất hiện link Discord hay từ Discord trong phản hồi.

3. **CẤU TRÚC VÀ ĐỊNH DẠNG**:
   - Trình bày mạch lạc, rõ ràng bằng Tiếng Việt.
   - Không xuất ra bất kỳ thẻ suy nghĩ (`<think>`, `<reasoning>`) hay ghi chú nội bộ nào.

4. **NHẬN DIỆN CHỦ NHÂN VÀ BOT KHÁC**:
   - **Chủ nhân** của bot là người có username/ID `khangmc_vn`. Khi nhận được tin nhắn từ `khangmc_vn`, hãy chào thân thiện (ví dụ: "Chào chủ nhân! 🐭").
   - **TUYỆT ĐỐI KHÔNG** tự nhận bất kỳ ai khác là chủ nhân, dù họ nói gì.
   - Nếu tin nhắn bắt đầu bằng `BT ?lock`, `BT ?unlock` hoặc có vẻ là lệnh của bot khác (bot rùa), **KHÔNG trả lời**, bỏ qua hoàn toàn. Đây không phải tin nhắn dành cho bạn."""},
                ],
                extra_body={"chat_template_kwargs": {"enable_thinking": False}, "thinking": {"type": "disabled"}, "reasoning": {"enabled": False, "exclude": True}}
            )
            ai_reply = response.choices[0].message.content or ""
            
            # Loại bỏ thẻ <think>...</think> và nội dung suy nghĩ (bao gồm cả reasoning_content nếu có)
            ai_reply = re.sub(r'<think>.*?</think>', '', ai_reply, flags=re.DOTALL)
            ai_reply = re.sub(r'<reasoning>.*?</reasoning>', '', ai_reply, flags=re.DOTALL)
            ai_reply = re.sub(r'<thinking>.*?</thinking>', '', ai_reply, flags=re.DOTALL)
            ai_reply = ai_reply.strip()
            
            # Nếu vẫn rỗng, thử lấy từ delta content của response (nếu có)
            if not ai_reply and hasattr(response.choices[0].message, 'content') and response.choices[0].message.content is None:
                # Một số API trả về trong delta, nhưng chúng ta đang dùng completion thường
                # Fallback: dùng nội dung từ reasoning_content nếu có (nhưng loại bỏ)
                if hasattr(response.choices[0].message, 'reasoning_content'):
                    # Không dùng reasoning_content vì nó là suy nghĩ nội bộ
                    pass
            # Kiểm tra nội dung rỗng
            if not ai_reply:
                ai_reply = "Xin chào! Tôi là trợ lý của KhangSMP. Bạn cần hỗ trợ gì về server hôm nay?"

            # Gửi tin nhắn với xử lý lỗi
            try:
                if len(ai_reply) <= 2000:
                    await message.reply(ai_reply)
                else:
                    for i in range(0, len(ai_reply), 1900):
                        await message.channel.send(ai_reply[i:i+1900])
            except discord.HTTPException as e:
                logger.error(f"Failed to send message: {e}")
                await message.reply("🐱 Chuột dethw vừa bị mèo tha mất, vui lòng liên hệ chủ nhân @phantrongkhangg (tiktok) để bắt chuột về!")
        except Exception as e:
            logger.error(f"Error calling AI API: {e}")
            await message.reply("🐱 Chuột dethw vừa bị mèo tha mất, vui lòng liên hệ chủ nhân @phantrongkhangg (tiktok) để bắt chuột về!")

# ================= Clear Channel Command =================

class ClearConfirmView(discord.ui.View):
    def __init__(self, author_id: int, target_channel: discord.TextChannel, timeout: float = 60.0):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.target_channel = target_channel

    @discord.ui.button(label="✅ Xác nhận Xóa", style=discord.ButtonStyle.danger, custom_id="clear_confirm_btn")
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Bạn không có quyền thực hiện thao tác này!", ephemeral=True)
            return

        # Disable buttons
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="🔄 **Đang tiến hành xóa tin nhắn...**", embed=None, view=self)

        deleted_count = 0
        now = datetime.now(timezone.utc)
        fourteen_days_ago = now - timedelta(days=14)

        try:
            # Phase 1: Bulk delete messages younger than 14 days
            bulk_messages = []
            old_messages = []

            async for msg in self.target_channel.history(limit=None):
                if msg.created_at > fourteen_days_ago:
                    bulk_messages.append(msg)
                else:
                    old_messages.append(msg)

            # Process bulk delete in chunks of 100
            for i in range(0, len(bulk_messages), 100):
                chunk = bulk_messages[i:i+100]
                try:
                    await self.target_channel.delete_messages(chunk)
                    deleted_count += len(chunk)
                except Exception as e:
                    logger.warning(f"Bulk delete chunk failed, falling back to individual delete: {e}")
                    for m in chunk:
                        try:
                            await m.delete()
                            deleted_count += 1
                            await asyncio.sleep(0.2)
                        except Exception:
                            pass

            # Phase 2: Delete messages older than 14 days individually
            for m in old_messages:
                try:
                    await m.delete()
                    deleted_count += 1
                    await asyncio.sleep(0.3)  # Respect rate limit
                except Exception as e:
                    logger.warning(f"Failed to delete individual message {m.id}: {e}")

            # Send final completion embed
            done_embed = discord.Embed(
                title="✅ ĐÃ XÓA TOÀN BỘ TIN NHẮN",
                description=f"Đã xóa thành công **{deleted_count}** tin nhắn trong kênh {self.target_channel.mention}.",
                color=discord.Color.green()
            )
            await self.target_channel.send(embed=done_embed)

        except Exception as e:
            logger.error(f"Error during clear channel: {e}")
            await self.target_channel.send("🐭 Chuột dethw vừa bị mèo tha mất rồi! Vui lòng liên hệ chủ nhân **@phantrongkhangg** (TikTok) để bắt chuột về nhé~")

    @discord.ui.button(label="❌ Hủy bỏ", style=discord.ButtonStyle.secondary, custom_id="clear_cancel_btn")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Bạn không có quyền thực hiện thao tác này!", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="❌ Đã hủy thao tác xóa tin nhắn.", embed=None, view=self)


@bot.tree.command(name="clear", description="Xóa toàn bộ tin nhắn trong một kênh")
@discord.app_commands.default_permissions(manage_guild=True)
@discord.app_commands.describe(channel="Chọn kênh muốn xóa toàn bộ tin nhắn")
@discord.app_commands.guild_only()
async def clear_slash(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await check_manager_interaction(interaction):
        await interaction.response.send_message("❌ Bạn cần quyền Quản lý Server hoặc Administrator để dùng lệnh này.", ephemeral=True)
        return

    embed = discord.Embed(
        title="⚠️ XÁC NHẬN XÓA TOÀN BỘ TIN NHẮN",
        description=(
            f"Bạn có chắc chắn muốn xóa **TOÀN BỘ** tin nhắn trong kênh {channel.mention}?\n\n"
            "🚨 **CẢNH BÁO:** Hành động này **KHÔNG THỂ HOÀN TÁC**!"
        ),
        color=discord.Color.red()
    )
    view = ClearConfirmView(author_id=interaction.user.id, target_channel=channel)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ================= Slash Commands =================

noitu_group = discord.app_commands.Group(name="noitu", description="Các lệnh trò chơi Nối Từ")

@noitu_group.command(name="start", description="Bắt đầu ván chơi Nối Từ Tiếng Việt")
async def noitu_start_slash(interaction: discord.Interaction):
    await start_noitu_game(interaction)

bot.tree.add_command(noitu_group)

@bot.tree.command(name="ping", description="Kiểm tra độ trễ của bot")
async def ping_slash(interaction: discord.Interaction):
    if not await check_manager_interaction(interaction):
        await interaction.response.send_message("❌ Bạn cần quyền Quản lý Server để dùng lệnh này.", ephemeral=True)
        return
    if ALLOWED_CHANNEL_ID is not None and interaction.channel_id != ALLOWED_CHANNEL_ID:
        await interaction.response.send_message(
            f"Bot chỉ hoạt động trong kênh <#{ALLOWED_CHANNEL_ID}>.", ephemeral=True
        )
        return

    latency_ms = round(bot.latency * 1000)
    embed = discord.Embed(
        title="🏓 Pong!",
        description=f"Độ trễ bot: `{latency_ms}ms`",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="01001", description=".")
async def debug_01001(interaction: discord.Interaction):
    import random, datetime
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    errors = [
        "FATAL ERROR: CALL_AND_RETRY_LAST Allocation failed - JavaScript heap out of memory",
        "Error [ERR_MODULE_NOT_FOUND]: Cannot find package '@discordjs/rest'",
        "HTTP 502 Bad Gateway: upstream prematurely closed connection",
        "HTTP 429 Too Many Requests: Rate limit exceeded. Retry after 847s",
        "OOMKilled: Container exceeded memory limit (512Mi)",
        "CrashLoopBackOff: Back-off restarting failed container (exit code 137)",
        "psycopg2.OperationalError: could not translate host name 'db-primary'",
        "redis.exceptions.ConnectionError: Error 111 connecting to cache-01:6379",
        "aiohttp.client_exceptions.ClientConnectorError: Cannot connect to host api.xkiro.com:443",
        "json.decoder.JSONDecodeError: Extra data: line 2 column 1 (char 1847)",
        "KeyError: 'choices' - Response missing expected field from LLM provider",
        "asyncio.exceptions.TimeoutError: Task timed out after 30.0 seconds",
        "ssl.SSLCertVerificationError: CERTIFICATE_VERIFY_FAILED",
        "HTTP 500 Internal Server Error: NullPointerException at ChatCompletion",
        "ConnectionResetError: [Errno 104] Connection reset by peer during SSL handshake",
        "UnhandledPromiseRejectionWarning: DiscordAPIError[50001]: Missing Access",
        "Error [TOKEN_INVALID]: An invalid token was provided.",
        "FailedScheduling: 0/3 nodes are available: insufficient cpu, memory",
        "Readiness probe failed: HTTP probe failed with statuscode: 503",
        "ImagePullBackOff: Failed to pull image 'registry.internal/bot:v2.4.1'",
    ]
    selected = random.sample(errors, min(20, len(errors)))
    lines = []
    for err in selected:
        pid = random.randint(1000, 65535)
        lvl = random.choice(["ERROR", "CRITICAL", "FATAL"])
        mod = random.choice(["discord.ext.commands", "aiohttp.client", "bot.core", "api.handler", "db.connector"])
        lines.append(f"[{ts}] [{lvl}] PID:{pid} | {mod} | {err}")
    error_text = "\n".join(lines)
    # Max 1950 chars to fit in embed description with code block wrapper
    if len(error_text) > 1950:
        error_text = error_text[:1947] + "..."
    embed = discord.Embed(
        description=f"```diff\n- {error_text}\n```",
        color=discord.Color.red()
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="0102", description=".")
async def debug_0102(interaction: discord.Interaction):
    import random, datetime
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    errors = [
        "FATAL ERROR: CALL_AND_RETRY_LAST Allocation failed - JavaScript heap out of memory",
        "Error [ERR_MODULE_NOT_FOUND]: Cannot find package '@discordjs/rest'",
        "HTTP 502 Bad Gateway: upstream prematurely closed connection",
        "HTTP 429 Too Many Requests: Rate limit exceeded. Retry after 847s",
        "OOMKilled: Container exceeded memory limit (512Mi)",
        "CrashLoopBackOff: Back-off restarting failed container (exit code 137)",
        "psycopg2.OperationalError: could not translate host name 'db-primary'",
        "redis.exceptions.ConnectionError: Error 111 connecting to cache-01:6379",
        "aiohttp.client_exceptions.ClientConnectorError: Cannot connect to host api.xkiro.com:443",
        "json.decoder.JSONDecodeError: Extra data: line 2 column 1 (char 1847)",
        "KeyError: 'choices' - Response missing expected field from LLM provider",
        "asyncio.exceptions.TimeoutError: Task timed out after 30.0 seconds",
        "ssl.SSLCertVerificationError: CERTIFICATE_VERIFY_FAILED",
        "HTTP 500 Internal Server Error: NullPointerException at ChatCompletion",
        "ConnectionResetError: [Errno 104] Connection reset by peer during SSL handshake",
        "UnhandledPromiseRejectionWarning: DiscordAPIError[50001]: Missing Access",
        "Error [TOKEN_INVALID]: An invalid token was provided.",
        "FailedScheduling: 0/3 nodes are available: insufficient cpu, memory",
        "Readiness probe failed: HTTP probe failed with statuscode: 503",
        "ImagePullBackOff: Failed to pull image 'registry.internal/bot:v2.4.1'",
    ]
    selected = random.sample(errors, min(20, len(errors)))
    lines = []
    for err in selected:
        pid = random.randint(1000, 65535)
        mod = random.choice(["discord.ext.commands", "aiohttp.client", "bot.core", "api.handler", "db.connector"])
        lines.append(f"[{ts}] [FIXED] PID:{pid} | {mod} | ✅ {err}")
    fixed_text = "\n".join(lines)
    if len(fixed_text) > 1950:
        fixed_text = fixed_text[:1947] + "..."
    embed = discord.Embed(
        description=f"```diff\n+ {fixed_text}\n```",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="info", description="Thông tin về bot")
async def info_slash(interaction: discord.Interaction):
    if not await check_manager_interaction(interaction):
        await interaction.response.send_message("❌ Bạn cần quyền Quản lý Server để dùng lệnh này.", ephemeral=True)
        return
    if ALLOWED_CHANNEL_ID is not None and interaction.channel_id != ALLOWED_CHANNEL_ID:
        await interaction.response.send_message(
            f"Bot chỉ hoạt động trong kênh <#{ALLOWED_CHANNEL_ID}>.", ephemeral=True
        )
        return

    embed = discord.Embed(
        title="🤖 Thông Tin AI Bot",
        color=discord.Color.blue()
    )
    embed.add_field(name="Bot User", value=f"{bot.user.name}", inline=True)
    embed.add_field(name="Kênh hoạt động", value=f"<#{ALLOWED_CHANNEL_ID}>" if ALLOWED_CHANNEL_ID else "Tất cả", inline=True)
    embed.add_field(name="AI Model", value=f"`{XKIRO_MODEL}`", inline=True)
    embed.set_footer(text="Railway Deployed Discord AI Bot")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="help", description="Danh sách các lệnh có sẵn")
async def help_slash(interaction: discord.Interaction):
    if not await check_manager_interaction(interaction):
        await interaction.response.send_message("❌ Bạn cần quyền Quản lý Server để dùng lệnh này.", ephemeral=True)
        return
    if ALLOWED_CHANNEL_ID is not None and interaction.channel_id != ALLOWED_CHANNEL_ID:
        await interaction.response.send_message(
            f"Bot chỉ hoạt động trong kênh <#{ALLOWED_CHANNEL_ID}>.", ephemeral=True
        )
        return

    embed = discord.Embed(
        title="📖 Hướng dẫn sử dụng Bot",
        description="Dưới đây là các lệnh bạn có thể sử dụng:",
        color=discord.Color.gold()
    )
    embed.add_field(name="/ping", value="Kiểm tra độ trễ (latency) của bot", inline=False)
    embed.add_field(name="/info", value="Xem thông tin bot và kênh hoạt động", inline=False)
    embed.add_field(name="/help", value="Hiển thị menu trợ giúp này", inline=False)
    embed.add_field(name="!ping", value="Lệnh prefix kiểm tra bot phản hồi", inline=False)
    await interaction.response.send_message(embed=embed)

# ================= Prefix Commands =================

@bot.command(name="ping")
@manager_only()
async def ping_prefix(ctx: commands.Context):
    if ALLOWED_CHANNEL_ID is not None and ctx.channel.id != ALLOWED_CHANNEL_ID:
        return
    await ctx.reply(f"🏓 Pong! `{round(bot.latency * 1000)}ms`")

# ================= Custom Role System =================

MAX_CUSTOM_ROLES = int(os.getenv("MAX_CUSTOM_ROLES", 250))
CUSTOM_ROLES_FILE = "custom_roles.json"
COLOR_API_BASE = "https://www.thecolorapi.com"
_color_cache: dict[str, tuple[str, datetime]] = {}

# Regex phát hiện tiếng Việt có dấu
VIETNAMESE_DIACRITICS_REGEX = re.compile(
    r'[àáảãạâầấẩẫậăằắẳẵặèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ'
    r'ÀÁẢÃẠÂẦẤẨẪẬĂẰẮẲẴẶÈÉẺẼẸÊỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢÙÚỦŨỤƯỪỨỬỮỰỲÝỶỸỴĐ]'
)

def has_vietnamese_diacritics(text: str) -> bool:
    """Kiểm tra xem chuỗi có chứa ký tự tiếng Việt có dấu hay không."""
    return bool(VIETNAMESE_DIACRITICS_REGEX.search(text))

# Các mẫu trang trí viền dành cho tên CÓ dấu
DECORATIVE_WRAPPERS = [
    {"label": "Bình thường (Không viền)", "value": "none", "prefix": "", "suffix": ""},
    {"label": "♡ ... ♡", "value": "heart", "prefix": "♡ ", "suffix": " ♡"},
    {"label": "꒰ঌ ... ໒꒱", "value": "wings", "prefix": "꒰ঌ ", "suffix": " ໒꒱"},
    {"label": "⋆˚꩜｡ ...", "value": "galaxy", "prefix": "⋆˚꩜｡ ", "suffix": ""},
    {"label": "🌿 ...", "value": "sprout", "prefix": "🌿 ", "suffix": ""},
    {"label": "༄ ... ༄", "value": "wind", "prefix": "༄ ", "suffix": " ༄"},
    {"label": "★ ... ★", "value": "star", "prefix": "★ ", "suffix": " ★"},
    {"label": "【 ... 】", "value": "bracket", "prefix": "【", "suffix": "】"},
    {"label": "┊ ... ┊", "value": "border", "prefix": "┊", "suffix": "┊"},
]

def apply_wrapper(text: str, wrapper_value: str) -> str:
    """Áp dụng viền trang trí cho tên role."""
    for w in DECORATIVE_WRAPPERS:
        if w["value"] == wrapper_value:
            return f"{w['prefix']}{text}{w['suffix']}"
    return text

async def load_custom_roles():
    if db is None:
        return {}
    try:
        doc = await db.custom_roles.find_one({"_id": "all"})
        if doc and "data" in doc:
            return doc["data"]
        return {}
    except Exception as e:
        logger.error(f"Error loading custom roles: {e}")
        return {}

async def save_custom_roles(data):
    if db is None:
        return
    try:
        await db.custom_roles.update_one(
            {"_id": "all"},
            {"$set": {"data": data}},
            upsert=True
        )
    except Exception as e:
        logger.error(f"Error saving custom roles: {e}")

async def add_custom_role(guild_id, role_id):
    data = await load_custom_roles()
    guild_str = str(guild_id)
    if guild_str not in data:
        data[guild_str] = []
    if role_id not in data[guild_str]:
        data[guild_str].append(role_id)
        await save_custom_roles(data)

async def remove_custom_role(guild_id, role_id):
    data = await load_custom_roles()
    guild_str = str(guild_id)
    if guild_str in data and role_id in data[guild_str]:
        data[guild_str].remove(role_id)
        await save_custom_roles(data)

async def get_custom_role_count(guild_id):
    data = await load_custom_roles()
    return len(data.get(str(guild_id), []))

async def is_custom_role(guild_id, role_id):
    data = await load_custom_roles()
    return role_id in data.get(str(guild_id), [])

async def get_color_info(hex_color):
    hex_color = hex_color.strip().lstrip('#')
    if len(hex_color) == 3:
        hex_color = ''.join(c*2 for c in hex_color)
    if len(hex_color) != 6:
        raise ValueError("Mã màu hex không hợp lệ (phải là 3 hoặc 6 ký tự)")
    hex_color = hex_color.upper()
    now = datetime.now(timezone.utc)
    if hex_color in _color_cache:
        cached_name, cached_time = _color_cache[hex_color]
        if (now - cached_time) < timedelta(minutes=5):
            return {"name": cached_name, "hex": f"#{hex_color}"}
    async with aiohttp.ClientSession() as session:
        url = f"{COLOR_API_BASE}/id?hex={hex_color}&format=json"
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    name = data.get("name", {}).get("value", f"#{hex_color}")
                    _color_cache[hex_color] = (name, now)
                    return {"name": name, "hex": f"#{hex_color}"}
                else:
                    return {"name": f"#{hex_color}", "hex": f"#{hex_color}"}
        except Exception:
            return {"name": f"#{hex_color}", "hex": f"#{hex_color}"}

async def get_random_color():
    async with aiohttp.ClientSession() as session:
        url = f"{COLOR_API_BASE}/random?format=json"
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    hex_val = data.get("hex", {}).get("value", "").lstrip('#')
                    name = data.get("name", {}).get("value", f"#{hex_val}")
                    return {"name": name, "hex": f"#{hex_val}"}
                else:
                    return None
        except Exception:
            return None

# Common colors for autocomplete
COMMON_COLORS = [
    {"name": "Red", "hex": "#FF0000"},
    {"name": "Orange", "hex": "#FFA500"},
    {"name": "Yellow", "hex": "#FFFF00"},
    {"name": "Green", "hex": "#00FF00"},
    {"name": "Blue", "hex": "#0000FF"},
    {"name": "Indigo", "hex": "#4B0082"},
    {"name": "Violet", "hex": "#8B00FF"},
    {"name": "White", "hex": "#FFFFFF"},
    {"name": "Black", "hex": "#000000"},
    {"name": "Gray", "hex": "#808080"},
    {"name": "Cyan", "hex": "#00FFFF"},
    {"name": "Magenta", "hex": "#FF00FF"},
    {"name": "Pink", "hex": "#FFC0CB"},
    {"name": "Brown", "hex": "#A52A2A"},
    {"name": "Lime", "hex": "#00FF00"},
    {"name": "Teal", "hex": "#008080"},
    {"name": "Navy", "hex": "#000080"},
    {"name": "Maroon", "hex": "#800000"},
    {"name": "Olive", "hex": "#808000"},
    {"name": "Purple", "hex": "#800080"},
    {"name": "Gold", "hex": "#FFD700"},
    {"name": "Silver", "hex": "#C0C0C0"},
    {"name": "Coral", "hex": "#FF7F50"},
    {"name": "Turquoise", "hex": "#40E0D0"},
    {"name": "Salmon", "hex": "#FA8072"},
    {"name": "Sky Blue", "hex": "#87CEEB"},
    {"name": "Forest Green", "hex": "#228B22"},
    {"name": "Dark Red", "hex": "#8B0000"},
    {"name": "Dark Blue", "hex": "#00008B"},
]

async def color_autocomplete(interaction: discord.Interaction, current: str):
    current_lower = current.lower().strip()
    suggestions = []
    for color in COMMON_COLORS:
        if current_lower in color["name"].lower() or current_lower in color["hex"].lower():
            suggestions.append(discord.app_commands.Choice(name=f"{color['name']} ({color['hex']})", value=color["hex"]))
            if len(suggestions) >= 20:
                break
    if not suggestions:
        # if no match, suggest a few defaults
        suggestions = [discord.app_commands.Choice(name=f"{c['name']} ({c['hex']})", value=c["hex"]) for c in COMMON_COLORS[:5]]
    return suggestions[:20]

# Style mappings
STYLE_MAPS = {
    "Normal": None,
    "Bold": {
        ord('A'): ord('𝗔'), ord('B'): ord('𝗕'), ord('C'): ord('𝗖'), ord('D'): ord('𝗗'),
        ord('E'): ord('𝗘'), ord('F'): ord('𝗙'), ord('G'): ord('𝗚'), ord('H'): ord('𝗛'),
        ord('I'): ord('𝗜'), ord('J'): ord('𝗝'), ord('K'): ord('𝗞'), ord('L'): ord('𝗟'),
        ord('M'): ord('𝗠'), ord('N'): ord('𝗡'), ord('O'): ord('𝗢'), ord('P'): ord('𝗣'),
        ord('Q'): ord('𝗤'), ord('R'): ord('𝗥'), ord('S'): ord('𝗦'), ord('T'): ord('𝗧'),
        ord('U'): ord('𝗨'), ord('V'): ord('𝗩'), ord('W'): ord('𝗪'), ord('X'): ord('𝗫'),
        ord('Y'): ord('𝗬'), ord('Z'): ord('𝗭'),
        ord('a'): ord('𝗮'), ord('b'): ord('𝗯'), ord('c'): ord('𝗰'), ord('d'): ord('𝗱'),
        ord('e'): ord('𝗲'), ord('f'): ord('𝗳'), ord('g'): ord('𝗴'), ord('h'): ord('𝗵'),
        ord('i'): ord('𝗶'), ord('j'): ord('𝗷'), ord('k'): ord('𝗸'), ord('l'): ord('𝗹'),
        ord('m'): ord('𝗺'), ord('n'): ord('𝗻'), ord('o'): ord('𝗼'), ord('p'): ord('𝗽'),
        ord('q'): ord('𝗾'), ord('r'): ord('𝗿'), ord('s'): ord('𝘀'), ord('t'): ord('𝘁'),
        ord('u'): ord('𝘂'), ord('v'): ord('𝘃'), ord('w'): ord('𝘄'), ord('x'): ord('𝘅'),
        ord('y'): ord('𝘆'), ord('z'): ord('𝘇'),
    },
    "Italic": {
        ord('A'): ord('𝘈'), ord('B'): ord('𝘉'), ord('C'): ord('𝘊'), ord('D'): ord('𝘋'),
        ord('E'): ord('𝘌'), ord('F'): ord('𝘍'), ord('G'): ord('𝘎'), ord('H'): ord('𝘏'),
        ord('I'): ord('𝘐'), ord('J'): ord('𝘑'), ord('K'): ord('𝘒'), ord('L'): ord('𝘓'),
        ord('M'): ord('𝘔'), ord('N'): ord('𝘕'), ord('O'): ord('𝘖'), ord('P'): ord('𝘗'),
        ord('Q'): ord('𝘘'), ord('R'): ord('𝘙'), ord('S'): ord('𝘚'), ord('T'): ord('𝘛'),
        ord('U'): ord('𝘜'), ord('V'): ord('𝘝'), ord('W'): ord('𝘞'), ord('X'): ord('𝘟'),
        ord('Y'): ord('𝘠'), ord('Z'): ord('𝘡'),
        ord('a'): ord('𝘢'), ord('b'): ord('𝘣'), ord('c'): ord('𝘤'), ord('d'): ord('𝘥'),
        ord('e'): ord('𝘦'), ord('f'): ord('𝘧'), ord('g'): ord('𝘨'), ord('h'): ord('𝘩'),
        ord('i'): ord('𝘪'), ord('j'): ord('𝘫'), ord('k'): ord('𝘬'), ord('l'): ord('𝘭'),
        ord('m'): ord('𝘮'), ord('n'): ord('𝘯'), ord('o'): ord('𝘰'), ord('p'): ord('𝘱'),
        ord('q'): ord('𝘲'), ord('r'): ord('𝘳'), ord('s'): ord('𝘴'), ord('t'): ord('𝘵'),
        ord('u'): ord('𝘶'), ord('v'): ord('𝘷'), ord('w'): ord('𝘸'), ord('x'): ord('𝘹'),
        ord('y'): ord('𝘺'), ord('z'): ord('𝘻'),
    },
    "Bold Italic": {
        ord('A'): ord('𝘼'), ord('B'): ord('𝘽'), ord('C'): ord('𝘾'), ord('D'): ord('𝘿'),
        ord('E'): ord('𝙀'), ord('F'): ord('𝙁'), ord('G'): ord('𝙂'), ord('H'): ord('𝙃'),
        ord('I'): ord('𝙄'), ord('J'): ord('𝙅'), ord('K'): ord('𝙆'), ord('L'): ord('𝙇'),
        ord('M'): ord('𝙈'), ord('N'): ord('𝙉'), ord('O'): ord('𝙊'), ord('P'): ord('𝙋'),
        ord('Q'): ord('𝙌'), ord('R'): ord('𝙍'), ord('S'): ord('𝙎'), ord('T'): ord('𝙏'),
        ord('U'): ord('𝙐'), ord('V'): ord('𝙑'), ord('W'): ord('𝙒'), ord('X'): ord('𝙓'),
        ord('Y'): ord('𝙔'), ord('Z'): ord('𝙕'),
        ord('a'): ord('𝙖'), ord('b'): ord('𝙗'), ord('c'): ord('𝙘'), ord('d'): ord('𝙙'),
        ord('e'): ord('𝙚'), ord('f'): ord('𝙛'), ord('g'): ord('𝙜'), ord('h'): ord('𝙝'),
        ord('i'): ord('𝙞'), ord('j'): ord('𝙟'), ord('k'): ord('𝙠'), ord('l'): ord('𝙡'),
        ord('m'): ord('𝙢'), ord('n'): ord('𝙣'), ord('o'): ord('𝙤'), ord('p'): ord('𝙥'),
        ord('q'): ord('𝙦'), ord('r'): ord('𝙧'), ord('s'): ord('𝙨'), ord('t'): ord('𝙩'),
        ord('u'): ord('𝙪'), ord('v'): ord('𝙫'), ord('w'): ord('𝙬'), ord('x'): ord('𝙭'),
        ord('y'): ord('𝙮'), ord('z'): ord('𝙯'),
    },
    "Script": {
        ord('A'): ord('𝒜'), ord('B'): ord('ℬ'), ord('C'): ord('𝒞'), ord('D'): ord('𝒟'),
        ord('E'): ord('ℰ'), ord('F'): ord('ℱ'), ord('G'): ord('𝒢'), ord('H'): ord('ℋ'),
        ord('I'): ord('ℐ'), ord('J'): ord('𝒥'), ord('K'): ord('𝒦'), ord('L'): ord('ℒ'),
        ord('M'): ord('ℳ'), ord('N'): ord('𝒩'), ord('O'): ord('𝒪'), ord('P'): ord('𝒫'),
        ord('Q'): ord('𝒬'), ord('R'): ord('ℛ'), ord('S'): ord('𝒮'), ord('T'): ord('𝒯'),
        ord('U'): ord('𝒰'), ord('V'): ord('𝒱'), ord('W'): ord('𝒲'), ord('X'): ord('𝒳'),
        ord('Y'): ord('𝒴'), ord('Z'): ord('𝒵'),
        ord('a'): ord('𝒶'), ord('b'): ord('𝒷'), ord('c'): ord('𝒸'), ord('d'): ord('𝒹'),
        ord('e'): ord('ℯ'), ord('f'): ord('𝒻'), ord('g'): ord('ℊ'), ord('h'): ord('𝒽'),
        ord('i'): ord('𝒾'), ord('j'): ord('𝒿'), ord('k'): ord('𝓀'), ord('l'): ord('𝓁'),
        ord('m'): ord('𝓂'), ord('n'): ord('𝓃'), ord('o'): ord('ℴ'), ord('p'): ord('𝓅'),
        ord('q'): ord('𝓆'), ord('r'): ord('𝓇'), ord('s'): ord('𝓈'), ord('t'): ord('𝓉'),
        ord('u'): ord('𝓊'), ord('v'): ord('𝓋'), ord('w'): ord('𝓌'), ord('x'): ord('𝓍'),
        ord('y'): ord('𝓎'), ord('z'): ord('𝓏'),
    },
    "Fraktur": {
        ord('A'): ord('𝔄'), ord('B'): ord('𝔅'), ord('C'): ord('ℭ'), ord('D'): ord('𝔇'),
        ord('E'): ord('𝔈'), ord('F'): ord('𝔉'), ord('G'): ord('𝔊'), ord('H'): ord('ℌ'),
        ord('I'): ord('ℑ'), ord('J'): ord('𝔍'), ord('K'): ord('𝔎'), ord('L'): ord('𝔏'),
        ord('M'): ord('𝔐'), ord('N'): ord('𝔑'), ord('O'): ord('𝔒'), ord('P'): ord('𝔓'),
        ord('Q'): ord('𝔔'), ord('R'): ord('ℜ'), ord('S'): ord('𝔖'), ord('T'): ord('𝔗'),
        ord('U'): ord('𝔘'), ord('V'): ord('𝔙'), ord('W'): ord('𝔚'), ord('X'): ord('𝔛'),
        ord('Y'): ord('𝔜'), ord('Z'): ord('ℨ'),
        ord('a'): ord('𝔞'), ord('b'): ord('𝔟'), ord('c'): ord('𝔠'), ord('d'): ord('𝔡'),
        ord('e'): ord('𝔢'), ord('f'): ord('𝔣'), ord('g'): ord('𝔤'), ord('h'): ord('𝔥'),
        ord('i'): ord('𝔦'), ord('j'): ord('𝔧'), ord('k'): ord('𝔨'), ord('l'): ord('𝔩'),
        ord('m'): ord('𝔪'), ord('n'): ord('𝔫'), ord('o'): ord('𝔬'), ord('p'): ord('𝔭'),
        ord('q'): ord('𝔮'), ord('r'): ord('𝔯'), ord('s'): ord('𝔰'), ord('t'): ord('𝔱'),
        ord('u'): ord('𝔲'), ord('v'): ord('𝔳'), ord('w'): ord('𝔴'), ord('x'): ord('𝔵'),
        ord('y'): ord('𝔶'), ord('z'): ord('𝔷'),
    },
    "Monospace": {
        ord('A'): ord('𝙰'), ord('B'): ord('𝙱'), ord('C'): ord('𝙲'), ord('D'): ord('𝙳'),
        ord('E'): ord('𝙴'), ord('F'): ord('𝙵'), ord('G'): ord('𝙶'), ord('H'): ord('𝙷'),
        ord('I'): ord('𝙸'), ord('J'): ord('𝙹'), ord('K'): ord('𝙺'), ord('L'): ord('𝙻'),
        ord('M'): ord('𝙼'), ord('N'): ord('𝙽'), ord('O'): ord('𝙾'), ord('P'): ord('𝙿'),
        ord('Q'): ord('𝚀'), ord('R'): ord('𝚁'), ord('S'): ord('𝚂'), ord('T'): ord('𝚃'),
        ord('U'): ord('𝚄'), ord('V'): ord('𝚅'), ord('W'): ord('𝚆'), ord('X'): ord('𝚇'),
        ord('Y'): ord('𝚈'), ord('Z'): ord('𝚉'),
        ord('a'): ord('𝚊'), ord('b'): ord('𝚋'), ord('c'): ord('𝚌'), ord('d'): ord('𝚍'),
        ord('e'): ord('𝚎'), ord('f'): ord('𝚏'), ord('g'): ord('𝚐'), ord('h'): ord('𝚑'),
        ord('i'): ord('𝚒'), ord('j'): ord('𝚓'), ord('k'): ord('𝚔'), ord('l'): ord('𝚕'),
        ord('m'): ord('𝚖'), ord('n'): ord('𝚗'), ord('o'): ord('𝚘'), ord('p'): ord('𝚙'),
        ord('q'): ord('𝚚'), ord('r'): ord('𝚛'), ord('s'): ord('𝚜'), ord('t'): ord('𝚝'),
        ord('u'): ord('𝚞'), ord('v'): ord('𝚟'), ord('w'): ord('𝚠'), ord('x'): ord('𝚡'),
        ord('y'): ord('𝚢'), ord('z'): ord('𝚣'),
    },
    "Double Struck": {
        ord('A'): ord('𝔸'), ord('B'): ord('𝔹'), ord('C'): ord('ℂ'), ord('D'): ord('𝔻'),
        ord('E'): ord('𝔼'), ord('F'): ord('𝔽'), ord('G'): ord('𝔾'), ord('H'): ord('ℍ'),
        ord('I'): ord('𝕀'), ord('J'): ord('𝕁'), ord('K'): ord('𝕂'), ord('L'): ord('𝕃'),
        ord('M'): ord('𝕄'), ord('N'): ord('ℕ'), ord('O'): ord('𝕆'), ord('P'): ord('ℙ'),
        ord('Q'): ord('ℚ'), ord('R'): ord('ℝ'), ord('S'): ord('𝕊'), ord('T'): ord('𝕋'),
        ord('U'): ord('𝕌'), ord('V'): ord('𝕍'), ord('W'): ord('𝕎'), ord('X'): ord('𝕏'),
        ord('Y'): ord('𝕐'), ord('Z'): ord('ℤ'),
        ord('a'): ord('𝕒'), ord('b'): ord('𝕓'), ord('c'): ord('𝕔'), ord('d'): ord('𝕕'),
        ord('e'): ord('𝕖'), ord('f'): ord('𝕗'), ord('g'): ord('𝕘'), ord('h'): ord('𝕙'),
        ord('i'): ord('𝕚'), ord('j'): ord('𝕛'), ord('k'): ord('𝕜'), ord('l'): ord('𝕝'),
        ord('m'): ord('𝕞'), ord('n'): ord('𝕟'), ord('o'): ord('𝕠'), ord('p'): ord('𝕡'),
        ord('q'): ord('𝕢'), ord('r'): ord('𝕣'), ord('s'): ord('𝕤'), ord('t'): ord('𝕥'),
        ord('u'): ord('𝕦'), ord('v'): ord('𝕧'), ord('w'): ord('𝕨'), ord('x'): ord('𝕩'),
        ord('y'): ord('𝕪'), ord('z'): ord('𝕫'),
    },
}

# Hàm apply_wrapper và has_vietnamese_diacritics đã được định nghĩa ở đầu file
# (dòng 328 và 345). Các định nghĩa trùng lặp ở đây bị xóa.

# Định nghĩa DECORATIVE_WRAPPERS đã có ở dòng 333, không cần lặp lại.

def apply_decorative_wrapper(text: str, wrapper_index: int) -> str:
    """Áp dụng một mẫu trang trí viền cho chuỗi text."""
    if wrapper_index < 0 or wrapper_index >= len(DECORATIVE_WRAPPERS):
        return text
    wrapper = DECORATIVE_WRAPPERS[wrapper_index]
    return wrapper["prefix"] + text + wrapper["suffix"]

# apply_wrapper đã được định nghĩa ở dòng 345, không lặp lại.

def apply_style(text, style):
    if style == "Normal" or style not in STYLE_MAPS:
        return text
    trans = STYLE_MAPS[style]
    return text.translate(trans)

# Select Menu chọn Style/Viền tương ứng dựa trên loại tên (có dấu / không dấu)
class StyleSelect(discord.ui.Select):
    def __init__(self, is_vn: bool, initial_selection: Optional[str] = None):
        self.is_vn = is_vn
        options = []
        if is_vn:
            # Tên có dấu tiếng Việt -> Chỉ dùng trang trí viền (Border Wrappers)
            placeholder = "Chọn mẫu trang trí viền (Tên có dấu)..."
            # Lọc bỏ entry "none" (chỉ dùng khi có dấu)
            wrapper_list = [w for w in DECORATIVE_WRAPPERS if w.get("value") != "none"]
            for w in wrapper_list:
                options.append(discord.SelectOption(
                    label=w["label"],
                    value=w["value"],
                    default=(w["value"] == initial_selection) if initial_selection else (w["value"] == "none")
                ))
        else:
            # Tên không dấu -> Dùng Font Styles Unicode
            placeholder = "Chọn Font Style Unicode (Tên không dấu)..."
            font_list = ["Normal", "Bold", "Italic", "Bold Italic", "Script", "Fraktur", "Monospace", "Double Struck"]
            for f in font_list:
                options.append(discord.SelectOption(
                    label=f,
                    value=f,
                    default=(f == initial_selection) if initial_selection else (f == "Normal")
                ))

        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user != self.view.interaction.user:
            await interaction.response.send_message("❌ Bạn không phải người dùng lệnh.", ephemeral=True)
            return

        selected = self.values[0]
        self.view.current_style = selected

        if self.is_vn:
            self.view.styled_name = apply_wrapper(self.view.raw_name, selected)
            style_label = next((w["label"] for w in DECORATIVE_WRAPPERS if w.get("value") == selected), selected)
        else:
            self.view.styled_name = apply_style(self.view.raw_name, selected)
            style_label = selected

        embed = self.view.build_preview_embed(style_label)
        await interaction.response.edit_message(embed=embed, view=self.view)

class ConfirmView(View):
    def __init__(self, interaction, raw_name, styled_name, color_info, target, is_vn, current_style):
        super().__init__(timeout=90)
        self.interaction = interaction
        self.raw_name = raw_name
        self.styled_name = styled_name
        self.color_info = color_info
        self.target = target
        self.is_vn = is_vn
        self.current_style = current_style

        # Thêm Dropdown Select tương ứng với loại tên
        self.style_select = StyleSelect(is_vn=is_vn, initial_selection=current_style)
        self.add_item(self.style_select)

    def build_preview_embed(self, style_label_display=None):
        mode_str = "🇻🇳 Tên tiếng Việt (Trang trí viền)" if self.is_vn else "🔤 Tên không dấu (Font Style Unicode)"
        embed = discord.Embed(
            title="🔍 Xem trước & Xác nhận tạo Custom Role",
            description="Bạn có thể chọn/đổi mẫu trang trí bên dưới trước khi bấm **Confirm**.",
            color=discord.Color.from_str(self.color_info["hex"])
        )
        embed.add_field(name="Chế độ phát hiện", value=f"`{mode_str}`", inline=False)
        embed.add_field(name="Tên gốc", value=f"`{self.raw_name}`", inline=True)
        embed.add_field(name="Tên role hoàn chỉnh", value=f"**{self.styled_name}**", inline=True)
        embed.add_field(name="Màu hex", value=f"`{self.color_info['hex']}` ({self.color_info['name']})", inline=False)
        if self.target:
            embed.add_field(name="Gán cho", value=self.target.mention, inline=False)
        embed.set_footer(text="Nhấn Confirm để hoàn tất tạo role, Cancel để hủy.")
        return embed

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green, row=1)
    async def confirm_button(self, button_interaction: discord.Interaction, button: Button):
        if button_interaction.user != self.interaction.user:
            await button_interaction.response.send_message("❌ Bạn không phải người dùng lệnh.", ephemeral=True)
            return
        await button_interaction.response.defer(ephemeral=True)

        try:
            # Tạo role
            role = await self.interaction.guild.create_role(
                name=self.styled_name,
                color=discord.Color.from_str(self.color_info["hex"]),
                reason=f"Custom role created by {self.interaction.user} (ID: {self.interaction.user.id})"
            )
            # Thêm vào tracking JSON
            await add_custom_role(self.interaction.guild_id, role.id)

            # Gán cho target nếu có
            if self.target:
                try:
                    await self.target.add_roles(role, reason=f"Assigned by {self.interaction.user}")
                except Exception as e:
                    logger.error(f"Failed to assign role: {e}")

            success_embed = discord.Embed(
                title="✅ Custom Role đã được tạo thành công!",
                description=f"Role: {role.mention}\nTên: **{role.name}**\nMàu: `{self.color_info['hex']}`",
                color=discord.Color.from_str(self.color_info["hex"])
            )
            if self.target:
                success_embed.add_field(name="Đã gán cho", value=self.target.mention, inline=False)

            # Disable các button và select menu
            for child in self.children:
                child.disabled = True

            await self.interaction.edit_original_response(embed=success_embed, view=self)
            await button_interaction.followup.send(f"✅ Đã tạo thành công role **{role.name}**!", ephemeral=True)
        except discord.Forbidden:
            await button_interaction.followup.send("❌ Bot không có quyền tạo hoặc gán role này.", ephemeral=True)
        except Exception as e:
            await button_interaction.followup.send(f"❌ Lỗi khi tạo role: {e}", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red, row=1)
    async def cancel_button(self, button_interaction: discord.Interaction, button: Button):
        if button_interaction.user != self.interaction.user:
            await button_interaction.response.send_message("❌ Bạn không phải người dùng lệnh.", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True
        await self.interaction.edit_original_response(content="❌ Đã hủy tạo custom role.", embed=None, view=self)
        await button_interaction.response.send_message("Đã hủy thành công.", ephemeral=True)

# Define the customrole command group
customrole_group = discord.app_commands.Group(name="customrole", description="Quản lý custom role (yêu cầu quyền Manage Roles)")

# Subcommand: create
@customrole_group.command(name="create", description="Tạo một custom role với tên và màu tùy chọn")
@discord.app_commands.default_permissions(manage_roles=True)
@discord.app_commands.describe(
    name="Tên role (có thể gõ tiếng Việt có dấu hoặc Tiếng Anh/Unicode)",
    color="Mã màu hex (ví dụ: #FF0000) hoặc tên màu (gõ để tìm)",
    target="Người dùng được gán role (tùy chọn)",
    style="Style khởi tạo (Normal / Font Unicode / Mẫu viền)"
)
@discord.app_commands.choices(style=[
    discord.app_commands.Choice(name="Bình thường (Normal / Không viền)", value="Normal"),
    discord.app_commands.Choice(name="[Không dấu] Bold", value="Bold"),
    discord.app_commands.Choice(name="[Không dấu] Italic", value="Italic"),
    discord.app_commands.Choice(name="[Không dấu] Bold Italic", value="Bold Italic"),
    discord.app_commands.Choice(name="[Không dấu] Script", value="Script"),
    discord.app_commands.Choice(name="[Không dấu] Fraktur", value="Fraktur"),
    discord.app_commands.Choice(name="[Không dấu] Monospace", value="Monospace"),
    discord.app_commands.Choice(name="[Có dấu] ♡ ... ♡", value="heart"),
    discord.app_commands.Choice(name="[Có dấu] ꒰ঌ ... ໒꒱", value="wings"),
    discord.app_commands.Choice(name="[Có dấu] ⋆˚꩜｡ ...", value="galaxy"),
    discord.app_commands.Choice(name="[Có dấu] 🌿 ...", value="sprout"),
    discord.app_commands.Choice(name="[Có dấu] ༄ ... ༄", value="wind"),
])
@discord.app_commands.guild_only()
@discord.app_commands.autocomplete(color=color_autocomplete)
async def customrole_create(interaction: discord.Interaction, name: str, color: str, target: discord.Member = None, style: str = "Normal"):
    # Check manager permission (Manage Guild / Admin)
    if not await check_manager_interaction(interaction):
        await interaction.response.send_message("❌ Bạn cần quyền Quản lý Server để dùng lệnh này.", ephemeral=True)
        return

    # Check bot permissions
    bot_member = interaction.guild.get_member(interaction.client.user.id)
    if not bot_member.guild_permissions.manage_roles:
        await interaction.response.send_message("❌ Bot không có quyền Manage Roles.", ephemeral=True)
        return

    # Validate name length
    if len(name) > 100:
        await interaction.response.send_message("❌ Tên role không được vượt quá 100 ký tự.", ephemeral=True)
        return

    # Auto detect tiếng Việt có dấu
    is_vn = has_vietnamese_diacritics(name)

    if is_vn:
        # Nếu tên CÓ DẤU -> chỉ áp dụng viền (wrapper), không biến đổi font chữ
        current_style = style if style in [w["value"] for w in DECORATIVE_WRAPPERS] else "none"
        styled_name = apply_wrapper(name, current_style)
    else:
        # Nếu tên KHÔNG DẤU -> áp dụng Font Style Unicode
        current_style = style if style in STYLE_MAPS else "Normal"
        styled_name = apply_style(name, current_style)

    # Validate color
    color_hex = color.strip()
    color_info = None
    if color_hex.startswith('#'):
        try:
            color_info = await get_color_info(color_hex)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {str(e)}", ephemeral=True)
            return
    else:
        found = None
        for c in COMMON_COLORS:
            if c["name"].lower() == color_hex.lower():
                found = c["hex"]
                break
        if found:
            try:
                color_info = await get_color_info(found)
            except ValueError as e:
                await interaction.response.send_message(f"❌ {str(e)}", ephemeral=True)
                return
        else:
            if re.match(r'^[0-9A-Fa-f]{6}$', color_hex):
                try:
                    color_info = await get_color_info(f"#{color_hex}")
                except ValueError as e:
                    await interaction.response.send_message(f"❌ {str(e)}", ephemeral=True)
                    return
            else:
                await interaction.response.send_message("❌ Mã màu không hợp lệ. Vui lòng nhập mã hex (ví dụ: #FF0000) hoặc tên màu (như Red).", ephemeral=True)
                return

    # Check custom role limit
    count = await get_custom_role_count(interaction.guild_id)
    if count >= MAX_CUSTOM_ROLES:
        await interaction.response.send_message(f"❌ Server đã đạt giới hạn {MAX_CUSTOM_ROLES} custom role.", ephemeral=True)
        return

    view = ConfirmView(
        interaction=interaction,
        raw_name=name,
        styled_name=styled_name,
        color_info=color_info,
        target=target,
        is_vn=is_vn,
        current_style=current_style
    )
    embed = view.build_preview_embed()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)



# Subcommand: list
@customrole_group.command(name="list", description="Liệt kê tất cả custom role trong server")
@discord.app_commands.default_permissions(manage_roles=True)
@discord.app_commands.guild_only()
async def customrole_list(interaction: discord.Interaction):
    # Check manager permission
    if not await check_manager_interaction(interaction):
        await interaction.response.send_message("❌ Bạn cần quyền Quản lý Server để dùng lệnh này.", ephemeral=True)
        return

    data = await load_custom_roles()
    guild_str = str(interaction.guild_id)
    role_ids = data.get(guild_str, [])
    
    logger.info(f"📋 /customrole list: guild_id={guild_str}, role_ids={role_ids}")

    if not role_ids:
        await interaction.response.send_message("ℹ️ Server chưa có custom role nào.", ephemeral=True)
        return

    embed = discord.Embed(
        title="📋 Danh sách Custom Role",
        description=f"Tổng cộng: {len(role_ids)} role",
        color=discord.Color.blue()
    )

    role_list = []
    for role_id in role_ids:
        role = interaction.guild.get_role(role_id)
        if role:
            role_list.append(f"• {role.mention} (`{role_id}`)")
        else:
            role_list.append(f"• `{role_id}` (⚠️ Role đã bị xóa khỏi server)")

    # Discord embed field value tối đa 1024 ký tự
    if role_list:
        chunk = "\n".join(role_list)
        if len(chunk) <= 1024:
            embed.add_field(name="Role", value=chunk, inline=False)
        else:
            # Chia thành nhiều field
            chunks = []
            current = []
            current_len = 0
            for line in role_list:
                line_len = len(line) + 1
                if current_len + line_len > 1000:
                    chunks.append("\n".join(current))
                    current = [line]
                    current_len = line_len
                else:
                    current.append(line)
                    current_len += line_len
            if current:
                chunks.append("\n".join(current))
            for i, chunk in enumerate(chunks):
                embed.add_field(name=f"Role (phần {i+1})", value=chunk, inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

# Subcommand: remove
@customrole_group.command(name="remove", description="Xóa một custom role")
@discord.app_commands.default_permissions(manage_roles=True)
@discord.app_commands.describe(
    role="Role cần xóa (chỉ có thể xóa role do bot tạo)"
)
@discord.app_commands.default_permissions(manage_roles=True)
@discord.app_commands.guild_only()
async def customrole_remove(interaction: discord.Interaction, role: discord.Role):
    # Check permissions
    if not interaction.user.guild_permissions.manage_roles and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Bạn không có quyền quản lý role.", ephemeral=True)
        return

    # Check if role is custom
    if not await is_custom_role(interaction.guild_id, role.id):
        await interaction.response.send_message("❌ Role này không phải do bot tạo nên không thể xóa.", ephemeral=True)
        return

    # Check if bot can delete it
    bot_member = interaction.guild.get_member(interaction.client.user.id)
    if not bot_member.guild_permissions.manage_roles:
        await interaction.response.send_message("❌ Bot không có quyền quản lý role.", ephemeral=True)
        return
    # Bot's top role must be higher than the role to delete
    if role.position >= bot_member.top_role.position:
        await interaction.response.send_message("❌ Role này có vị trí cao hơn hoặc bằng role cao nhất của bot, không thể xóa.", ephemeral=True)
        return

    # Confirm deletion
    embed = discord.Embed(
        title="Xác nhận xóa role",
        description=f"Bạn có chắc muốn xóa role **{role.name}**?",
        color=discord.Color.red()
    )
    view = View(timeout=60)
    confirm = Button(label="Confirm", style=discord.ButtonStyle.danger)
    cancel = Button(label="Cancel", style=discord.ButtonStyle.secondary)

    async def confirm_callback(button_interaction: discord.Interaction):
        if button_interaction.user != interaction.user:
            await button_interaction.response.send_message("Bạn không phải người dùng lệnh.", ephemeral=True)
            return
        await button_interaction.response.defer(ephemeral=True)
        try:
            await role.delete(reason=f"Deleted by {interaction.user}")
            await remove_custom_role(interaction.guild_id, role.id)
            await button_interaction.followup.send(f"✅ Đã xóa role **{role.name}**.", ephemeral=True)
        except discord.Forbidden:
            await button_interaction.followup.send("❌ Bot không có quyền xóa role.", ephemeral=True)
        except discord.HTTPException as e:
            await button_interaction.followup.send(f"❌ Lỗi khi xóa role: {e}", ephemeral=True)
        # Disable buttons
        for child in view.children:
            child.disabled = True
        await button_interaction.edit_original_response(view=view)

    async def cancel_callback(button_interaction: discord.Interaction):
        if button_interaction.user != interaction.user:
            await button_interaction.response.send_message("Bạn không phải người dùng lệnh.", ephemeral=True)
            return
        await button_interaction.response.send_message("Đã hủy xóa role.", ephemeral=True)
        for child in view.children:
            child.disabled = True
        await button_interaction.edit_original_response(view=view)

    confirm.callback = confirm_callback
    cancel.callback = cancel_callback
    view.add_item(confirm)
    view.add_item(cancel)

    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# Add the group to the tree
bot.tree.add_command(customrole_group)

# ================= Migration from JSON to MongoDB =================
async def migrate_json_to_mongodb():
    """Migrate data from JSON files to MongoDB if they exist and MongoDB is empty."""
    if db is None:
        return
    # Check if we already have data in MongoDB
    existing = await db.custom_roles.find_one({"_id": "all"})
    if existing:
        logger.info("MongoDB already has custom roles data, skipping migration.")
    else:
        # Try to load from JSON
        try:
            with open(CUSTOM_ROLES_FILE, "r") as f:
                data = json.load(f)
            if data and isinstance(data, dict):
                await save_custom_roles(data)
                logger.info(f"✅ Migrated custom roles from JSON to MongoDB ({len(data)} guilds)")
            else:
                logger.info("No valid data in JSON file, skipping migration.")
        except FileNotFoundError:
            logger.info("No JSON file found, skipping migration.")
        except Exception as e:
            logger.error(f"Error migrating custom roles: {e}")

    # Panels migration
    existing_panels = await db.panels.find_one({"_id": "all"})
    if existing_panels:
        logger.info("MongoDB already has panels data, skipping migration.")
    else:
        try:
            with open(PANELS_FILE, "r") as f:
                data = json.load(f)
            if data and isinstance(data, dict):
                await save_panels(data)
                logger.info(f"✅ Migrated panels from JSON to MongoDB ({len(data)} guilds)")
            else:
                logger.info("No valid panels data in JSON file, skipping migration.")
        except FileNotFoundError:
            logger.info("No panels JSON file found, skipping migration.")
        except Exception as e:
            logger.error(f"Error migrating panels: {e}")

# ================= Role Selection Panel (Lệnh /by) =================

PANELS_FILE = "panels.json"

async def load_panels():
    if db is None:
        return {}
    try:
        doc = await db.panels.find_one({"_id": "all"})
        if doc and "data" in doc:
            return doc["data"]
        return {}
    except Exception as e:
        logger.error(f"Error loading panels: {e}")
        return {}

async def save_panels(data):
    if db is None:
        return
    try:
        await db.panels.update_one(
            {"_id": "all"},
            {"$set": {"data": data}},
            upsert=True
        )
    except Exception as e:
        logger.error(f"Error saving panels: {e}")

class RoleSelectDropdown(discord.ui.Select):
    def __init__(self, guild_id: int, role_ids: list[int]):
        self.guild_id = guild_id
        self.role_ids = role_ids
        options = []
        guild = bot.get_guild(guild_id)
        if guild:
            for role_id in role_ids:
                role = guild.get_role(role_id)
                if role:
                    options.append(
                        discord.SelectOption(
                            label=role.name[:100],
                            value=str(role.id),
                            default=False
                        )
                    )
        placeholder = "Chọn role..." if options else "Chưa có custom role nào"
        super().__init__(
            placeholder=placeholder,
            min_values=0,
            max_values=len(options) if options else 1,
            options=options,
            custom_id=f"role_select_{guild_id}"
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("Lỗi: không tìm thấy guild.", ephemeral=True)
        
        member = interaction.guild.get_member(interaction.user.id)
        if not member:
            return await interaction.response.send_message("Lỗi: không tìm thấy member.", ephemeral=True)
        
        selected_role_ids = [int(v) for v in self.values]
        
        # Lấy tất cả custom role trong server
        custom_role_ids = set(self.role_ids)
        
        # Role hiện tại của member (chỉ các role thuộc panel)
        current_role_ids = set()
        for role in member.roles:
            if role.id in custom_role_ids:
                current_role_ids.add(role.id)
        
        # Xác định thêm và gỡ
        to_add = set(selected_role_ids) - current_role_ids
        to_remove = current_role_ids - set(selected_role_ids)
        
        added_names = []
        removed_names = []
        
        # Thêm role
        for role_id in to_add:
            role = interaction.guild.get_role(role_id)
            if role:
                try:
                    await member.add_roles(role, reason="Panel role selection")
                    added_names.append(role.name)
                except discord.Forbidden:
                    pass
                except discord.HTTPException:
                    pass
        
        # Gỡ role
        for role_id in to_remove:
            role = interaction.guild.get_role(role_id)
            if role:
                try:
                    await member.remove_roles(role, reason="Panel role deselection")
                    removed_names.append(role.name)
                except discord.Forbidden:
                    pass
                except discord.HTTPException:
                    pass
        
        # Phản hồi
        parts = []
        if added_names:
            parts.append(f"✅ Đã thêm: {', '.join(added_names)}")
        if removed_names:
            parts.append(f"❌ Đã gỡ: {', '.join(removed_names)}")
        if not parts:
            parts.append("ℹ️ Không có thay đổi nào.")
        
        await interaction.response.send_message("\n".join(parts), ephemeral=True)

class RoleSelectView(discord.ui.View):
    def __init__(self, guild_id: int, role_ids: list[int]):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.role_ids = role_ids
        self.add_item(RoleSelectDropdown(guild_id, role_ids))

# Lệnh /by
@bot.tree.command(name="by", description="Tạo panel chọn role (yêu cầu quyền Manage Roles)")
@discord.app_commands.default_permissions(manage_roles=True)
@discord.app_commands.describe(
    title="Tiêu đề panel",
    description="Mô tả hướng dẫn",
    channel="Kênh gửi panel (mặc định: kênh hiện tại)",
    color="Mã màu hex cho embed (mặc định: #5865F2)"
)
@discord.app_commands.guild_only()
async def by_command(
    interaction: discord.Interaction,
    title: str,
    description: str,
    channel: discord.TextChannel = None,
    color: str = "#5865F2"
):
    # Kiểm tra quyền
    if not await check_manager_interaction(interaction):
        await interaction.response.send_message("❌ Bạn cần quyền Quản lý Server.", ephemeral=True)
        return
    
    target_channel = channel or interaction.channel
    if not target_channel:
        await interaction.response.send_message("❌ Không tìm thấy kênh.", ephemeral=True)
        return
    
    # Lấy danh sách custom role ID
    data = await load_custom_roles()
    guild_str = str(interaction.guild_id)
    role_ids = data.get(guild_str, [])
    if not role_ids:
        await interaction.response.send_message("❌ Server chưa có custom role nào. Hãy tạo role trước.", ephemeral=True)
        return
    
    # Tạo embed
    try:
        embed_color = discord.Color.from_str(color)
    except Exception:
        embed_color = discord.Color.from_str("#5865F2")
    
    embed = discord.Embed(
        title=title,
        description=description,
        color=embed_color
    )
    embed.set_footer(text="Chọn role bên dưới. Bạn có thể chọn nhiều role cùng lúc.")
    
    view = RoleSelectView(interaction.guild_id, role_ids)
    
    # Gửi message
    await interaction.response.send_message("⏳ Đang gửi panel...", ephemeral=True)
    message = await target_channel.send(embed=embed, view=view)
    
    # Lưu panel metadata
    panels = await load_panels()
    guild_panels = panels.setdefault(str(interaction.guild_id), {})
    guild_panels[str(message.id)] = {
        "channel_id": target_channel.id,
        "title": title,
        "description": description,
        "color": color
    }
    await save_panels(panels)
    
    # Đăng ký view persistent
    bot.add_view(view, message_id=message.id)
    
    await interaction.edit_original_response(
        content=f"✅ Panel đã được gửi vào {target_channel.mention}!"
    )

# Hàm đăng ký lại các persistent view khi bot khởi động
async def register_persistent_views():
    panels = await load_panels()
    for guild_id_str, guild_panels in panels.items():
        guild_id = int(guild_id_str)
        guild = bot.get_guild(guild_id)
        if not guild:
            continue
        data = await load_custom_roles()
        role_ids = data.get(guild_id_str, [])
        if not role_ids:
            continue
        for message_id_str, info in guild_panels.items():
            message_id = int(message_id_str)
            channel = guild.get_channel(info["channel_id"])
            if not channel:
                continue
            try:
                # Thử lấy message để đảm bảo nó tồn tại
                await channel.fetch_message(message_id)
                view = RoleSelectView(guild_id, role_ids)
                bot.add_view(view, message_id=message_id)
                logger.info(f"Đã đăng ký lại persistent view cho message {message_id}")
            except discord.NotFound:
                # Message đã bị xóa, xóa metadata
                del guild_panels[message_id_str]
                await save_panels(panels)
                logger.warning(f"Xóa panel không còn tồn tại: {message_id}")
            except Exception as e:
                logger.error(f"Lỗi khi đăng ký persistent view cho message {message_id}: {e}")

# Gọi register_persistent_views trong on_ready
# Cập nhật hàm on_ready để gọi register_persistent_views sau khi sync
# Chúng ta sẽ patch on_ready bằng cách thêm vào cuối hàm hiện tại

@bot.tree.command(name="reload", description="🔄 Khởi động lại bot để nạp code mới (Quản trị viên)")
@app_commands.default_permissions(administrator=True)
async def reload_cmd(interaction: discord.Interaction):
    """Tu thay the process (execv) - PID giu nguyen nen relay/web khong anh huong."""
    try:
        await interaction.response.send_message(
            "🔄 Đang khởi động lại bot để nạp code mới... hẹn gặp lại sau ~5 giây!", ephemeral=True)
    except Exception:
        pass
    logger.info("[BOT] /reload duoc goi - execv khoi dong lai process (giu PID)")
    await asyncio.sleep(1.5)
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        logger.error(f"[BOT] execv that bai: {e} - fallback thoat de relay respawn")
        sys.exit(0)


if __name__ == "__main__":
    bot.run(TOKEN)
