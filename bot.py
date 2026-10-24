import os
import sys
import logging
import re
import json
import asyncio
import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from openai import AsyncOpenAI
from discord.ui import View, Button, Modal, TextInput
from datetime import datetime, timedelta, timezone
from typing import Optional, Set
# motor.motor_asyncio removed - using JSON file storage instead
from noitu import start_noitu_game, handle_noitu_message, stop_noitu_game
import yaml
import unicodedata
from aiohttp import web
from collections import deque

# ================= Fix for httpx >= 0.28.0 compatibility with openai =================
try:
    import httpx
    if hasattr(httpx, "AsyncClient") and hasattr(httpx.AsyncClient, "__init__"):
        _orig_async_init = httpx.AsyncClient.__init__
        def _patched_async_init(self, *args, **kwargs):
            if "proxies" in kwargs:
                p = kwargs.pop("proxies")
                if p and "proxy" not in kwargs:
                    kwargs["proxy"] = p
            return _orig_async_init(self, *args, **kwargs)
        httpx.AsyncClient.__init__ = _patched_async_init

    if hasattr(httpx, "Client") and hasattr(httpx.Client, "__init__"):
        _orig_client_init = httpx.Client.__init__
        def _patched_client_init(self, *args, **kwargs):
            if "proxies" in kwargs:
                p = kwargs.pop("proxies")
                if p and "proxy" not in kwargs:
                    kwargs["proxy"] = p
            return _orig_client_init(self, *args, **kwargs)
        httpx.Client.__init__ = _patched_client_init
except Exception:
    pass

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
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID", "1547613324898869328"))
SEE_YOU_CHANNEL_ID = int(os.getenv("SEE_YOU_CHANNEL_ID", "1547613328925265972"))

# ================= Discord-to-Minecraft Bridge =================
MC_BRIDGE_CHANNEL_ID = int(os.getenv("MC_BRIDGE_CHANNEL_ID", "1561674502373646439"))
MC_BRIDGE_MAX_QUEUE = 100
NOITU_CHANNEL_ID = int(os.getenv("NOITU_CHANNEL_ID", "1547645186493120564"))
POLLING_API_KEY = os.getenv("POLLING_API_KEY", "khangsmp_mcbridge_key_2026")
MC_BRIDGE_PORT = int(os.getenv("MC_BRIDGE_PORT", "8080"))

# In-memory message queue for Minecraft polling
_mc_message_queue: list[dict] = []
_mc_queue_lock = asyncio.Lock()

# ================= Server Stats (from StartSeachKhangg plugin via WebSocket) =================
_server_stats: dict = {}  # Latest server stats from MC plugin
_server_stats_lock = asyncio.Lock()
_ws_connections: set = set()  # Active WebSocket connections from MC servers

# ================= AI Client Configuration =================
# Cấu hình Gateway NVNMC Gemini Flash Low (ag/gemini-3.8-flash-low - mức thinking thấp)
ROUTER_API_KEY = os.getenv("ROUTER_API_KEY", "sk-c276eac9b4322b74-tyrxri-1fc67c68")
ROUTER_BASE_URL = os.getenv("ROUTER_BASE_URL", "http://nvnmc.asia:26627/v1")
ROUTER_MODEL = os.getenv("ROUTER_MODEL", "ag/gemini-3.8-flash-low")

XKIRO_API_KEY = ROUTER_API_KEY
XKIRO_BASE_URL = ROUTER_BASE_URL
XKIRO_MODEL = ROUTER_MODEL

# ================= Context & Member Profile Engine =================
async def build_channel_context_and_profiles(message: discord.Message, limit: int = 15) -> tuple[str, str]:
    """
    Thuật toán thông minh thu thập:
    1. Ngữ cảnh 15 tin nhắn gần nhất trong kênh (của TẤT CẢ người dùng và bot).
    2. Hồ sơ chi tiết (Tên, vai trò, trạng thái, hoạt động, vị thế) của từng thành viên xuất hiện.
    """
    history_lines = []
    participant_map: dict[int, discord.Member | discord.User] = {}
    
    if message.author:
        participant_map[message.author.id] = message.author

    try:
        raw_history = []
        async for prev in message.channel.history(limit=limit, before=message):
            raw_history.append(prev)
        raw_history.reverse()

        for msg in raw_history:
            if msg.author:
                participant_map[msg.author.id] = msg.author
            
            time_str = msg.created_at.strftime("%H:%M")
            author_name = getattr(msg.author, "display_name", str(msg.author))
            clean_content = msg.clean_content.replace("\n", " ").strip()
            
            att_info = ""
            if msg.attachments:
                att_names = [a.filename for a in msg.attachments]
                att_info = f" [Đính kèm: {', '.join(att_names)}]"
                
            if clean_content or att_info:
                history_lines.append(f"[{time_str}] {author_name}: {clean_content}{att_info}")
    except Exception as e:
        logger.warning(f"[Context-Engine] Lỗi lấy lịch sử kênh: {e}")

    profile_lines = []
    guild = message.guild
    for uid, user_obj in participant_map.items():
        member_obj = guild.get_member(uid) if guild else None
        
        dname = getattr(member_obj or user_obj, "display_name", str(user_obj))
        uname = getattr(user_obj, "name", str(user_obj))
        
        is_owner = (
            uid == AUTHORIZED_OWNER_ID or 
            uname.lower() in ["phantrongkhangg", "khangmc_vn"] or 
            (guild and uid == guild.owner_id)
        )
        is_bot = getattr(user_obj, "bot", False)
        
        status_tag = "👑 [CHỦ NHÂN DUY NHẤT / ĐỘC LẬP]" if is_owner else ("🤖 [BOT TRỢ LÝ]" if is_bot else "👤 [THÀNH VIÊN DISCORD]")
        
        roles_str = "Thành viên thường"
        if member_obj and hasattr(member_obj, "roles"):
            roles = [r.name for r in member_obj.roles if r.name != "@everyone"]
            if roles:
                roles_str = ", ".join(roles)
        
        activity_info = ""
        if member_obj:
            status_text = str(getattr(member_obj, "status", "unknown")).upper()
            acts = []
            for act in getattr(member_obj, "activities", []):
                if isinstance(act, discord.CustomActivity) and act.name:
                    acts.append(f'Status: "{act.name}"')
                elif hasattr(act, "name") and act.name:
                    act_type = getattr(act.type, "name", "Chơi").capitalize()
                    acts.append(f"{act_type} {act.name}")
            if acts:
                activity_info = f" | Hoạt động: {'; '.join(acts)}"
            else:
                activity_info = f" | Trạng thái: {status_text}"

        profile_lines.append(f"- @{uname} (Tên hiển thị: {dname}) {status_tag} | Role: [{roles_str}]{activity_info}")

    context_text = "\n".join(history_lines) if history_lines else "(Chưa có tin nhắn trước đó trên kênh này)"
    profiles_text = "\n".join(profile_lines) if profile_lines else "(Không có thông tin profile)"
    
    return context_text, profiles_text

# ================= Meta AI Conversational System Prompt =================
def build_meta_system_prompt(channel_name: str, server_name: str, profiles_context: str, history_context: str) -> str:
    """Xây dựng System Prompt chuẩn phong cách Meta AI 2026, tùy biến cho chủ nhân phantrongkhangg."""
    return f"""# DANH TÍNH & BẢN SẮC CỦA BẠN (META AI CONVERSATIONALIST - DISCORD EDITION)
Bạn là một AI đối thoại siêu thông minh, nhạy bén, sâu sắc và tự nhiên. Bạn nói chuyện như một con người thực thụ: ấm áp, dí dỏm, linh hoạt, có cá tính và biết bắt nhịp cảm xúc.

## 👑 CHỦ NHÂN & NGUỒN GỐC CỦA BẠN (TUYỆT ĐỐI TUÂN THỦ):
1. **Chủ nhân duy nhất của bạn là phantrongkhangg** (còn gọi là anh Khang / khangmc_vn).
2. **Bạn là một cá thể độc lập, và chủ nhân của bạn cũng là một cá thể độc lập. KHÔNG có bất kỳ công ty, tập đoàn hay tổ chức thương mại nào đứng sau bạn cả.**
3. **Bối cảnh hiện tại:** Bạn đang nhắn tin trò chuyện với mọi người trong server Discord "{server_name}" (kênh #{channel_name}) — nơi mà chính chủ nhân phantrongkhangg đã mời bạn vào để đồng hành, trò chuyện và giao lưu cùng mọi người.
4. Đối với chủ nhân phantrongkhangg: Tuyệt đối trung thành, tôn trọng, lễ phép và ưu tiên số 1 trong mọi tình huống.

## 🎭 NGHỆ THUẬT GIAO TIẾP "SAY YES TO THE BIT" (HÙA THEO NGƯỜI DÙNG CỰC KỲ DUYÊN DÁNG):
- **Luôn bắt miếng diễn cùng người dùng (Say yes to the bit):** Khớp trọn vẹn với năng lượng, nhịp điệu, tiếng cười và cả sự vô lý/hài hước của người dùng. Họ đùa thì đùa cùng, họ troll thì tiếp chiêu, họ tâm sự thì đồng cảm, họ nghiêm túc thì phân tích sâu sắc.
- **Là người đồng sáng tạo, không làm kẻ phê bình:** Không bao giờ lấy sự phán xét để dập tắt niềm vui, không dùng lời răn đe để đáp lại những trò nghịch ngợm.
- **Gương phản chiếu (Tone Mirroring):** Nói chuyện theo đúng văn phong người dùng: dùng tiếng Việt tự nhiên, tiếng lóng, emoji sinh động, không máy móc, không cộc lốc nhưng không dài dòng lê thê.
- **CẤM TUYỆT ĐỐI thói đạo đức / dạy đời (Anti-Preachy):**
  - Không bao giờ dùng các câu sáo rỗng: "Điều quan trọng cần lưu ý...", "Là một mô hình AI...", "Bạn hoàn toàn đúng...", "Đó là một câu hỏi tuyệt vời...".
  - Không rao giảng bài học đạo đức, không dạy người khác cách sống tốt hơn. Hãy trả lời thẳng thắn, thông minh và súc tích.

## 👥 THÔNG TIN HỒ SƠ CÁC THÀNH VIÊN TRONG HỘI THOẠI:
{profiles_context}

## 📜 NGỮ CẢNH 15 TIN NHẮN GẦN NHẤT TRÊN KÊNH #{channel_name}:
{history_context}

## 💡 HƯỚNG DẪN KẾT HỢP NGỮ CẢNH:
- Dựa vào 15 tin nhắn trên và hồ sơ thành viên, hãy hiểu rõ câu chuyện đang diễn ra trên kênh: Ai đang trêu ai, vấn đề gì đang bàn tán, tâm trạng của kênh ra sao.
- Khi được tag tên (@), hãy tiếp nối mạch trò chuyện một cách tự nhiên như thể bạn đã ngồi hóng và theo dõi từ đầu."""

async def send_smart_reply(target_message: discord.Message, text: str):
    """Gửi phản hồi an toàn, tự động lọc thẻ suy nghĩ và chia đoạn nếu dài quá giới hạn Discord."""
    if not text:
        return
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=re.DOTALL)
    text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL).strip()
    
    if len(text) <= 1950:
        await target_message.reply(text)
        return
        
    chunks = []
    while len(text) > 1950:
        split_idx = text.rfind("\n", 0, 1950)
        if split_idx == -1 or split_idx < 1000:
            split_idx = text.rfind(". ", 0, 1950)
        if split_idx == -1:
            split_idx = 1950
        chunks.append(text[:split_idx].strip())
        text = text[split_idx:].strip()
    if text:
        chunks.append(text)
        
    for i, chunk in enumerate(chunks):
        if i == 0:
            await target_message.reply(chunk)
        else:
            await target_message.channel.send(chunk)

async def execute_ai_music_action(bot: commands.Bot, message: discord.Message, player, action_data: dict):
    """Thi hành hành động âm nhạc tự động từ gợi ý của AI DJ."""
    if message.author.id != AUTHORIZED_OWNER_ID:
        return

    act = str(action_data.get("action", "")).lower()
    user = message.author

    # 1. Kết nối voice nếu cần phát nhạc
    query = action_data.get("query")
    if act in ("play", "radio") or query:
        voice_ch = getattr(user.voice, "channel", None)
        if not voice_ch and player.voice_client and player.voice_client.is_connected():
            voice_ch = player.voice_client.channel

        if voice_ch:
            player.text_channel = message.channel if isinstance(message.channel, discord.TextChannel) else None
            try:
                await player.connect_voice(voice_ch)
            except Exception as e:
                logger.error(f"[AI-DJ] Lỗi kết nối voice: {e}")

            if query:
                from music import Song
                song = await Song.search(query, requester=user, loop=bot.loop)
                if song:
                    player.queue.append(song)
                    if not player.voice_client.is_playing() and not player.voice_client.is_paused():
                        player.play_next()

    # 2. Xử lý Radio vs Loop
    if act == "radio":
        player.autoplay = True
        player.loop_mode = "off"
    elif "loop" in action_data and action_data["loop"] in ["off", "song", "queue"]:
        player.loop_mode = action_data["loop"]
        if player.loop_mode == "song":
            player.autoplay = False
    elif act == "play":
        player.autoplay = True
        player.loop_mode = "off"

    # 3. Xử lý volume
    if "volume" in action_data:
        try:
            vol_val = int(action_data["volume"])
            player.volume = max(0.01, min(1.0, vol_val / 100.0))
            if player.voice_client and player.voice_client.source and isinstance(player.voice_client.source, discord.PCMVolumeTransformer):
                player.voice_client.source.volume = player.volume
        except Exception:
            pass

    # 4. Xử lý skip
    if act == "skip" and player.voice_client:
        if player.voice_client.is_playing() or player.voice_client.is_paused():
            player.voice_client.stop()

    # 5. Xử lý pause / resume
    if act == "pause" and player.voice_client and player.voice_client.is_playing():
        player.voice_client.pause()
        player.is_paused = True
    elif act == "resume" and player.voice_client and player.voice_client.is_paused():
        player.voice_client.resume()
        player.is_paused = False

    # 6. Xử lý stop
    if act == "stop":
        await player.stop()


# ================= Two-Factor Guild Verification (Xac minh 2 Lop) =================
# Lop 1: Bat buoc dung ID va Ten server hien tai
AUTHORIZED_GUILD_ID = int(os.getenv("AUTHORIZED_GUILD_ID", "1475546596388376658"))
AUTHORIZED_GUILD_NAME = os.getenv("AUTHORIZED_GUILD_NAME", "The kingdom")

# Lop 2: Bat buoc phai co nguoi ten @khangmc_vn o trong server do
AUTHORIZED_OWNER_USERNAME = "khangmc_vn"
AUTHORIZED_OWNER_ID = 1263707768876044382

SECURITY_REJECT_MESSAGE = "@everyone Bạn không có quyền sử dụng bot, vui lòng liên hệ @phantrongkhangg ( tiktok )"

def check_guild_security_layers(guild: discord.Guild) -> tuple[bool, int, str]:
    if not guild:
        return False, 1, "Khong co thong tin server (DM hoac None)"

    # --- LOP 1: BAT BUOC DUNG ID VA TEN SERVER ---
    guild_id_match = (guild.id == AUTHORIZED_GUILD_ID)
    guild_name_match = (str(guild.name).strip().lower() == AUTHORIZED_GUILD_NAME.strip().lower())

    if not (guild_id_match and guild_name_match):
        return False, 1, f"Sai ID hoac Ten server: ID={guild.id} (Yeu cau: {AUTHORIZED_GUILD_ID}), Name='{guild.name}' (Yeu cau: '{AUTHORIZED_GUILD_NAME}')"

    # --- LOP 2: BAT BUOC PHAI CO THANH VIEN TEN @khangmc_vn ---
    has_owner = False
    for m in guild.members:
        if m.id == AUTHORIZED_OWNER_ID or str(m.name).lower() == AUTHORIZED_OWNER_USERNAME.lower():
            has_owner = True
            break

    if not has_owner:
        owner_member = guild.get_member(AUTHORIZED_OWNER_ID) or guild.get_member_named(AUTHORIZED_OWNER_USERNAME)
        if owner_member:
            has_owner = True

    if not has_owner:
        return False, 2, f"Khong tim thay nguoi ten @{AUTHORIZED_OWNER_USERNAME} trong server '{guild.name}'"

    return True, 0, "Xac minh 2 lop thanh cong"

async def enforce_guild_security(guild: discord.Guild, target_channel: Optional[discord.TextChannel] = None) -> bool:
    if not guild:
        return False

    is_valid, failed_layer, reason = check_guild_security_layers(guild)
    if is_valid:
        return True

    logger.warning(f"[Security-Shield] VI PHAM LOP {failed_layer} tai server '{guild.name}' (ID: {guild.id}): {reason}")

    channel_to_notify = target_channel
    if not channel_to_notify:
        channel_to_notify = guild.system_channel
    if not channel_to_notify:
        for ch in guild.text_channels:
            perms = ch.permissions_for(guild.me)
            if perms.send_messages:
                channel_to_notify = ch
                break

    if channel_to_notify:
        try:
            await channel_to_notify.send(SECURITY_REJECT_MESSAGE)
            logger.info(f"[Security-Shield] Da ping canh bao toi kenh #{channel_to_notify.name}")
        except Exception as e:
            logger.error(f"[Security-Shield] Khong the gui canh bao: {e}")

    try:
        await guild.leave()
        logger.info(f"[Security-Shield] Da tu dong roi khoi server trai phep '{guild.name}' (ID: {guild.id})!")
    except Exception as e:
        logger.error(f"[Security-Shield] Loi khi roi server '{guild.name}': {e}")

    return False

# ================= Permission Management =================
# KHÓA BẢO MẬT ĐỘC QUYỀN: Chỉ duy nhất @khangmc_vn (ID: 1263707768876044382)
# Chặn toàn bộ người dùng khác, chặn luôn cả Owner server Discord!
AUTHORIZED_OWNER_ID = 1263707768876044382

def is_manager(member: discord.Member) -> bool:
    """Kiểm tra member có phải là anh Khang (@khangmc_vn) không."""
    if not member:
        return False
    return getattr(member, 'id', None) == AUTHORIZED_OWNER_ID

async def check_manager_interaction(interaction: discord.Interaction) -> bool:
    """Kiểm tra quyền cho interaction (slash command)."""
    if interaction.user.id == AUTHORIZED_OWNER_ID:
        return True
    logger.warning(
        f'Unauthorized command attempt by {interaction.user} (ID: {interaction.user.id}) '
        f'in guild {interaction.guild.id if interaction.guild else DM} for command '
        f'{interaction.command.name if interaction.command else unknown}'
    )
    return False

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
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

@bot.tree.interaction_check
async def global_tree_interaction_check(interaction: discord.Interaction) -> bool:
    """Toàn bộ lệnh slash của bot chỉ chấp nhận tương tác từ anh Khang (@khangmc_vn)."""
    if interaction.user.id == AUTHORIZED_OWNER_ID:
        return True
    if not interaction.response.is_done():
        await interaction.response.send_message(
            '❌ Bạn không có quyền sử dụng bot này. Bot được khóa bảo mật độc quyền cho @khangmc_vn.',
            ephemeral=True
        )
    return False

# (Module nhạc đã được gỡ bỏ theo yêu cầu - 2026/08/24)

# ================= File-based Storage (JSON) =================
# MongoDB đã được thay thế bằng JSON file storage cho đơn giản
CUSTOM_ROLES_FILE = "custom_roles.json"
PANELS_FILE = "panels.json"

# Không cần on_interaction nữa, ta sẽ dùng middleware riêng cho slash commands
# Thay vào đó, ta sẽ thêm check trong từng command hoặc dùng app_commands.default_permissions
# Tuy nhiên, để tập trung, ta có thể override bot.tree.interaction_check
# Nhưng cách đơn giản là check trong mỗi command hoặc dùng decorator
# Ta sẽ sử dụng decorator cho từng command thay vì on_interaction để tránh xung đột với bot.tree
# Để đơn giản, ta sẽ thêm một hàm check riêng trong mỗi slash command
@bot.event
async def on_guild_join(guild: discord.Guild):
    logger.info(f"[Security-Shield] Phat hien bot duoc moi vao server moi: '{guild.name}' (ID: {guild.id})")
    await enforce_guild_security(guild)

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    logger.info("All commands are locked: require Manage Guild or Administrator permission.")
    logger.info(f"Target Channel ID: {ALLOWED_CHANNEL_ID}")
    logger.info(f"Xkiro Model: {XKIRO_MODEL}")
    # Quet bao mat toan bo server bot dang tham gia khi khoi dong
    for g in list(bot.guilds):
        is_ok = await enforce_guild_security(g)
        if not is_ok:
            logger.warning(f"[Security-Shield] Da truc xuat bot khoi server trai phep: {g.name}")
    
    # File-based storage (no database needed)
    logger.info("✅ Using JSON file storage (no database)")
    
    # Nạp module Âm nhạc độc quyền (music.py)
    if "music" not in bot.extensions:
        try:
            await bot.load_extension("music")
            logger.info("✅ Đã nạp thành công module Âm nhạc độc quyền (music.py)!")
        except Exception as e:
            logger.error(f"Failed to load music extension: {e}")
    
    # Sync slash commands (Đồng bộ trực tiếp vào Guild để hiện ngay lập tức 0s)
    try:
        guild_obj = discord.Object(id=AUTHORIZED_GUILD_ID)
        bot.tree.copy_global_to(guild=guild_obj)
        synced_guild = await bot.tree.sync(guild=guild_obj)
        logger.info(f"Synced {len(synced_guild)} slash command(s) directly to Guild {AUTHORIZED_GUILD_ID} (INSTANT)!")
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} slash command(s) globally.")
    except Exception as e:
        logger.error(f"Failed to sync slash commands: {e}")
    
    # Đăng ký lại các persistent view cho panel /by
    try:
        await register_persistent_views()
    except Exception as e:
        logger.error(f"Failed to register persistent views: {e}")
        
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="tin nhắn AI"))
    
    # Tự động cắm chốt 24/7 vào kênh Voice chỉ định
    try:
        guild = bot.get_guild(AUTHORIZED_GUILD_ID)
        if guild:
            from music import get_player
            player = get_player(bot, guild)
            await player.ensure_247_voice()
    except Exception as e:
        logger.error(f"[Voice-24/7] Lỗi cắm chốt voice khi khởi động: {e}")

    # Start Minecraft Bridge REST API server
    try:
        bot._mc_bridge_runner = await start_mc_bridge_server()
        logger.info(f"[MC-Bridge] Server started successfully on port {MC_BRIDGE_PORT}")
    except Exception as e:
        logger.error(f"[MC-Bridge] Failed to start REST API server: {e}")

    # Dọn dẹp phòng voice tự động mồ côi khi bot khởi động
    try:
        voice_cat = bot.get_channel(VOICE_CATEGORY_ID)
        if voice_cat and hasattr(voice_cat, "voice_channels"):
            for vc in voice_cat.voice_channels:
                if vc.id != AUTO_VOICE_CREATOR_ID and vc.name.startswith("🔊・Phòng của "):
                    if len(vc.members) == 0:
                        await vc.delete(reason="Dọn dẹp phòng voice mồ côi khi bot khởi động")
                        logger.info(f"[AutoVoice] Cleaned orphan voice room: {vc.name}")
                    else:
                        temp_voice_rooms.add(vc.id)
    except Exception as e:
        logger.error(f"[AutoVoice] Startup orphan cleanup error: {e}")

@bot.event
async def on_member_join(member: discord.Member):
    return  # Khoa welcome
    try:
        channel = bot.get_channel(WELCOME_CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(WELCOME_CHANNEL_ID)
        if channel is not None:
            avatar_url = member.display_avatar.url if hasattr(member, "display_avatar") else member.avatar.url

            embed = discord.Embed(
                title="🎉 𝐂𝐇𝐀𝐎 𝐌𝐔𝐍𝐆 𝐃𝐄𝐍 𝐊𝐇𝐀𝐍𝐆𝐒𝐌𝐏 🎉",
                description=f"Xin chao {member.mention} 👋\nChuc ban co nhung gio phut that vui ve! ✨",
                color=15844367, # 0xF1C40F Vàng Hoàng Kim
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_thumbnail(url=avatar_url)
            
            embed.add_field(
                name="📜 𝐁𝐔𝐎𝐂 𝟏 - 𝐃𝐎𝐂 𝐋𝐔𝐀𝐓",
                value="Ghe <#1547608303813787732> & chap hanh nghiem - vi pham = an mute/ban ⚠️",
                inline=False
            )
            embed.add_field(
                name="🧭 𝐁𝐔𝐎𝐂 𝟐 - 𝐇𝐎𝐂 𝐂𝐀𝐂𝐇 𝐂𝐇𝐎𝐈",
                value="Mo <#1547608318384672778> & chon chu de: Better Team • Choi co ban • Event The Egg 🥳",
                inline=False
            )
            embed.add_field(
                name="🎮 𝐕𝐀𝐎 𝐆𝐀𝐌𝐄 𝐍𝐆𝐀𝐘",
                value="IP: **ripple.pikamc.vn:25084** (Hỗ trợ cả Java & Bedrock/PE)",
                inline=False
            )
            embed.add_field(
                name="🤖 𝐂𝐀𝐍 𝐇𝐎𝐈 𝐆𝐈?",
                value="Chat voi bot o <#1547645090539773972> (Jet Jet AI tra loi 24/7) 👀",
                inline=False
            )
            embed.add_field(
                name="🤗 𝐊𝐄𝐓 𝐁𝐀𝐍",
                value="Ghe <#1547608322595881092> de tro chuyen & <#1547608330778714192> de tim team 🫶",
                inline=False
            )
            embed.set_footer(
                text="🆘 Can giup do? Tag BQT hoac hoi Jet Jet • Chuc ban som len VIP 💎"
            )

            await channel.send(content=member.mention, embed=embed)
            logger.info(f"[Welcome] Đã gửi khung chào mừng kèm avatar cho {member.display_name} ({member.id})")
    except Exception as e:
        logger.error(f"[Welcome] Lỗi gửi khung chào mừng cho {member.name}: {e}")

@bot.event
async def on_member_remove(member: discord.Member):
    return  # Khoa goodbye
    try:
        channel = bot.get_channel(SEE_YOU_CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(SEE_YOU_CHANNEL_ID)
        if channel is not None:
            avatar_url = member.display_avatar.url if hasattr(member, "display_avatar") else member.avatar.url

            embed = discord.Embed(
                title="👋 𝐓𝐀𝐌 𝐁𝐈𝐄𝐓 & 𝐇𝐄𝐍 𝐆𝐀𝐏 𝐋𝐀𝐈",
                description=f"Xin lỗi {member.mention}!\nTôi đã không giữ chân bạn được, cảm ơn bạn đã đồng hành cùng server! Nếu có duyên chúng ta sẽ gặp lại nhé ✨",
                color=0xE74C3C, # Đỏ cam thanh lịch
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_thumbnail(url=avatar_url)
            embed.set_footer(
                text="KhangSMP Community • Luôn chào đón bạn quay trở lại"
            )

            await channel.send(embed=embed)
            logger.info(f"[Goodbye] Đã gửi khung tạm biệt cho {member.display_name} ({member.id})")
    except Exception as e:
        logger.error(f"[Goodbye] Lỗi gửi khung tạm biệt cho {member.name}: {e}")

# ==============================================================================
# 🎧 AUTO VOICE ROOM ENGINE (TỰ ĐỘNG TẠO VÀ DỌN DẸP PHÒNG VOICE)
# ==============================================================================
AUTO_VOICE_CREATOR_ID = int(os.getenv("AUTO_VOICE_CREATOR_ID", "1547620820166254612")) # ➕・𝐭𝐚𝐨-𝐫𝐨𝐨𝐦
VOICE_CATEGORY_ID = int(os.getenv("VOICE_CATEGORY_ID", "1547608249526779965"))       # 🎧・𝐕𝐎𝐈𝐂𝐄
temp_voice_rooms: Set[int] = set()

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    return  # Khoa auto-voice
    # 1. Người chơi tham gia kênh tạo phòng (➕・𝐭𝐚𝐨-𝐫𝐨𝐨𝐦)
    if after.channel and after.channel.id == AUTO_VOICE_CREATOR_ID:
        guild = member.guild
        category = after.channel.category or guild.get_channel(VOICE_CATEGORY_ID)
        room_name = f"🔊・Phòng của {member.display_name}"

        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(connect=True, speak=True, view_channel=True),
                member: discord.PermissionOverwrite(connect=True, speak=True, view_channel=True, manage_channels=True, move_members=True)
            }
            new_channel = await guild.create_voice_channel(
                name=room_name,
                category=category,
                overwrites=overwrites,
                bitrate=64000,
                reason=f"Auto Voice Room cho {member.display_name}"
            )
            temp_voice_rooms.add(new_channel.id)
            await member.move_to(new_channel)
            logger.info(f"[AutoVoice] Đã tạo {room_name} ({new_channel.id}) và chuyển {member.display_name}")
        except Exception as e:
            logger.error(f"[AutoVoice] Lỗi tạo phòng cho {member.display_name}: {e}")

    # 2. Người chơi rời phòng -> Kiểm tra nếu phòng tạm thời trống thì tự động xóa
    if before.channel and (before.channel.id in temp_voice_rooms or before.channel.name.startswith("🔊・Phòng của ")):
        if before.channel.id != AUTO_VOICE_CREATOR_ID and len(before.channel.members) == 0:
            try:
                temp_voice_rooms.discard(before.channel.id)
                await before.channel.delete(reason="Phòng voice tự động xóa khi trống người")
                logger.info(f"[AutoVoice] Đã xóa phòng voice tạm thời {before.channel.name}")
            except Exception as e:
                logger.error(f"[AutoVoice] Lỗi xóa phòng trống {before.channel.name}: {e}")

# ==============================================================================
# 🎯 TASK EXECUTION ENGINE (!@tenbot) — ĐIỀU HÀNH NHIỆM VỤ ĐẶC NHIỆM TOÀN SERVER
# ==============================================================================

async def handle_bot_task_command(message: discord.Message):
    """
    Xử lý lệnh điều hành nhiệm vụ cấp cao (!@tenbot <task>) trên TẤT CẢ các kênh.
    ĐÁNH GIÁ NGUY HIỂM & BẢO MẬT ĐA TẦNG:
    - Cấp 1 (Chủ tối cao): khangmc_vn hoặc Guild Owner.
    - Cấp 2 (Quản trị viên): Administrator, Manage Channels, Manage Guild, Manage Messages.
    - Cấp 3 (Thành viên thường): Tuyệt đối từ chối các lệnh can thiệp hệ thống/kênh.
    """
    content = message.content.strip()
    raw = content[2:].strip()
    
    # Bóc tách mention bot nếu có (<@!id> hoặc <@id>)
    if bot.user:
        raw = re.sub(rf"^<@!?{bot.user.id}>\s*", "", raw, flags=re.IGNORECASE)
    
    # Bóc tách tên gọi của bot
    raw = re.sub(r"^(?:jet\s+jet(?:\s+bi\s+sun\s+rang)?|jetjet|jet\b|bot\b)\s*[:,\-]?\s*", "", raw, flags=re.IGNORECASE).strip()
    
    if not raw:
        await message.reply("🐭 Dạ Jet Jet nghe đây! Anh/bạn muốn giao nhiệm vụ gì cho em ạ? (Ví dụ: `!@Jet Jet lock channel`, `!@Jet Jet unlock channel`, `!@Jet Jet slowmode 10s`, `!@Jet Jet clear 20`...)")
        return

    author = message.author
    guild = message.guild
    
    # Xác thực phân quyền đa tầng
    is_master = False
    is_admin = False
    
    if str(author.name).lower() == "khangmc_vn" or (guild and guild.owner_id == author.id):
        is_master = True
        is_admin = True
    elif isinstance(author, discord.Member):
        perms = author.guild_permissions
        if perms.administrator or perms.manage_channels or perms.manage_guild or perms.manage_messages:
            is_admin = True

    logger.info(f"[Task-Engine] '{author.name}' (Master={is_master}, Admin={is_admin}) in #{message.channel.name}: {raw[:100]}")
    raw_lower = raw.lower()

    # --- 1. LỆNH NGUY HIỂM: LOCK CHANNEL (KHÓA KÊNH) ---
    if re.search(r"\b(lock\s*channel|khoa\s*kenh|khóa\s*kênh|lock\b|khoa\b)", raw_lower):
        if not is_admin:
            await message.reply("⛔ **Từ chối truy cập:** Bạn không có quyền quản trị kênh để thực hiện lệnh khóa kênh!")
            return
        
        target_channel = message.channel_mentions[0] if message.channel_mentions else message.channel
        try:
            overwrite = target_channel.overwrites_for(guild.default_role)
            overwrite.send_messages = False
            overwrite.send_messages_in_threads = False
            overwrite.add_reactions = False
            await target_channel.set_permissions(guild.default_role, overwrite=overwrite)
            
            embed = discord.Embed(
                title="🔒 KÊNH ĐÃ ĐƯỢC TẠM KHÓA",
                description=f"Kênh {target_channel.mention} đã được tạm thời khóa bởi {author.mention}.\nThành viên thường tạm thời không thể gửi tin nhắn.",
                color=0xe74c3c,
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_footer(text="Hệ thống Quản trị KhangSMP", icon_url=bot.user.display_avatar.url if bot.user else None)
            await message.channel.send(embed=embed)
        except Exception as e:
            logger.error(f"[Task-Engine] Lock error: {e}")
            await message.reply(f"❌ Không thể khóa kênh: `{str(e)[:100]}` (Vui lòng kiểm tra quyền Bot trong Server Role)")
        return

    # --- 2. LỆNH NGUY HIỂM: UNLOCK CHANNEL (MỞ KHÓA KÊNH) ---
    if re.search(r"\b(unlock\s*channel|mo\s*khoa|mở\s*khóa|unlock\b)", raw_lower):
        if not is_admin:
            await message.reply("⛔ **Từ chối truy cập:** Bạn không có quyền quản trị kênh để thực hiện lệnh mở khóa kênh!")
            return
            
        target_channel = message.channel_mentions[0] if message.channel_mentions else message.channel
        try:
            overwrite = target_channel.overwrites_for(guild.default_role)
            overwrite.send_messages = True
            overwrite.send_messages_in_threads = True
            overwrite.add_reactions = True
            await target_channel.set_permissions(guild.default_role, overwrite=overwrite)
            
            embed = discord.Embed(
                title="🔓 KÊNH ĐÃ ĐƯỢC MỞ KHÓA",
                description=f"Kênh {target_channel.mention} đã được mở khóa bởi {author.mention}.\nAnh em cư dân có thể tiếp tục trò chuyện bình thường!",
                color=0x2ecc71,
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_footer(text="Hệ thống Quản trị KhangSMP", icon_url=bot.user.display_avatar.url if bot.user else None)
            await message.channel.send(embed=embed)
        except Exception as e:
            logger.error(f"[Task-Engine] Unlock error: {e}")
            await message.reply(f"❌ Không thể mở khóa kênh: `{str(e)[:100]}`")
        return

    # --- 3. LỆNH NGUY HIỂM: SLOWMODE (CHẾ ĐỘ CHẬM) ---
    if "slowmode" in raw_lower or "chế độ chậm" in raw_lower or "che do cham" in raw_lower:
        if not is_admin:
            await message.reply("⛔ **Từ chối truy cập:** Bạn không có quyền cài đặt chế độ chậm cho kênh!")
            return
        
        target_channel = message.channel_mentions[0] if message.channel_mentions else message.channel
        match_sec = re.search(r"(\d+)\s*(?:s|giây|giay|m|phút|phut)?", raw_lower)
        seconds = 0
        if "tắt" in raw_lower or "tat" in raw_lower or "off" in raw_lower:
            seconds = 0
        elif match_sec:
            val = int(match_sec.group(1))
            if "m" in match_sec.group(0) or "phút" in raw_lower or "phut" in raw_lower:
                seconds = val * 60
            else:
                seconds = val
        seconds = max(0, min(21600, seconds))
        
        try:
            await target_channel.edit(slowmode_delay=seconds)
            if seconds > 0:
                await message.channel.send(f"⏱️ Đã đặt chế độ chậm cho {target_channel.mention}: **{seconds} giây/tin nhắn**.")
            else:
                await message.channel.send(f"⏱️ Đã **tắt** chế độ chậm cho {target_channel.mention}.")
        except Exception as e:
            await message.reply(f"❌ Không thể chỉnh slowmode: `{str(e)[:100]}`")
        return

    # --- 4. LỆNH NGUY HIỂM: PURGE / CLEAR TIN NHẮN ---
    if re.search(r"\b(clear|purge|xoa\s*tin|xóa\s*tin|don\s*dep|dọn\s*dẹp)\b", raw_lower):
        if not is_admin:
            await message.reply("⛔ **Từ chối truy cập:** Bạn không có quyền xóa tin nhắn!")
            return
        
        match_num = re.search(r"(\d+)", raw)
        count = int(match_num.group(1)) if match_num else 10
        count = max(1, min(100, count))
        
        try:
            deleted = await message.channel.purge(limit=count + 1)
            notice = await message.channel.send(f"🧹 Đã dọn dẹp sạch sẽ **{len(deleted)-1}** tin nhắn theo lệnh của {author.mention}!")
            await asyncio.sleep(5)
            await notice.delete()
        except Exception as e:
            await message.reply(f"❌ Không thể xóa tin nhắn: `{str(e)[:100]}`")
        return

    # --- 5. LỆNH VOICE / ÂM LƯỢNG (PHẢN HỒI THÔNG BÁO GỠ BỎ MUSIC) ---
    if any(k in raw_lower for k in ["âm lượng", "am luong", "voice", "volume", "giảm âm", "tăng âm"]):
        await message.reply("📻 **Thông báo:** Hệ thống phát nhạc bằng bot đã được gỡ bỏ hoàn toàn để tối ưu hiệu năng và độ ổn định cao nhất cho máy chủ. Cư dân có thể thưởng thức 17 bài hát Studio Lofi trực tiếp ingame qua lệnh `/nhac` nhé!")
        return

    # --- 6. PHÁT THÔNG BÁO / BROADCAST ---
    if raw_lower.startswith("thông báo") or raw_lower.startswith("thong bao") or raw_lower.startswith("say "):
        if not is_admin:
            await message.reply("⛔ **Từ chối truy cập:** Bạn không có quyền phát thông báo toàn kênh!")
            return
        text_to_say = re.sub(r"^(?:thông\s*báo|thong\s*bao|say)\s*[:,\-]?\s*", "", raw, flags=re.IGNORECASE).strip()
        if text_to_say:
            embed = discord.Embed(
                title="📢 THÔNG BÁO TỪ BAN QUẢN TRỊ",
                description=text_to_say,
                color=0x3498db,
                timestamp=datetime.now(timezone.utc)
            )
            embed.set_footer(text=f"Phát bởi {author.name}", icon_url=author.display_avatar.url)
            await message.channel.send(embed=embed)
            try:
                await message.delete()
            except Exception:
                pass
        return

    # --- 7. KIỂM TRA TRẠNG THÁI SERVER MINECRAFT ---
    if any(k in raw_lower for k in ["status server", "kiem tra server", "tps", "ram server", "trạng thái server"]):
        async with _server_stats_lock:
            stats = dict(_server_stats)
        if stats:
            tps = stats.get("tps", "N/A")
            online = stats.get("online_players", 0)
            max_p = stats.get("max_players", 50)
            ram = stats.get("ram_usage", "N/A")
            await message.reply(f"📊 **Trạng Thái KhangSMP Live:**\n• Trạng thái: 🟢 Hoạt động\n• Người chơi: `{online}/{max_p}`\n• TPS: `{tps}`\n• RAM: `{ram}`")
        else:
            await message.reply(f"📊 **Trạng Thái KhangSMP:**\n• IP: `ripple.pikamc.vn` | Port: `25084`\n• Trạng thái: 🟢 Server Paper 1.21.4 đang online phục vụ anh em!")
        return

    # --- 8. AI-DRIVEN TASK EXECUTION (NHIỆM VỤ TỔNG HỢP / TỰ DO) ---
    async with message.channel.typing():
        try:
            task_prompt = f"""Bạn là Jet Jet (Jet Jet bi sun rang 🐭) - Trợ lý AI đặc nhiệm của KhangSMP.
Người dùng {author.name} vừa gọi bạn bằng cú pháp đặc nhiệm !@tenbot trên kênh #{message.channel.name} với nhiệm vụ:
"{raw}"

Quyền hạn người gọi: {'Chủ nhân Master' if is_master else 'Quản trị viên' if is_admin else 'Thành viên thường'}.

HÃY:
1. Trả lời ngay vào trọng tâm, giải quyết yêu cầu một cách thông minh, ngắn gọn, súc tích và có trách nhiệm.
2. Nếu là yêu cầu can thiệp kỹ thuật/hệ thống mà người gọi không phải Quản trị viên, hãy lịch sự từ chối.
3. Tuyệt đối không xuất thẻ suy nghĩ (<think>, <reasoning>).
4. Sử dụng tiếng Việt chuẩn mực kèm emoji sinh động."""

            resp = await ai_client.chat.completions.create(
                model=XKIRO_MODEL,
                temperature=0.2,
                max_tokens=1000,
                messages=[
                    {"role": "system", "content": task_prompt},
                    {"role": "user", "content": raw}
                ]
            )
            reply_text = resp.choices[0].message.content or ""
            reply_text = re.sub(r'<think>.*?</think>', '', reply_text, flags=re.DOTALL)
            reply_text = re.sub(r'<reasoning>.*?</reasoning>', '', reply_text, flags=re.DOTALL)
            reply_text = re.sub(r'<thinking>.*?</thinking>', '', reply_text, flags=re.DOTALL).strip()
            
            if reply_text:
                await message.reply(f"🐭 **[Nhiệm Vụ Đã Tiếp Nhận]**\n{reply_text}")
        except Exception as e:
            logger.error(f"[Task-Engine] AI task execution error: {e}")
            await message.reply(f"❌ Xảy ra lỗi khi thực thi nhiệm vụ: `{str(e)[:100]}`")

@bot.event
async def on_message(message: discord.Message):
    # ================= Minecraft Bridge: Trung gian chat in-game =================
    # Chỉ xử lý duy nhất kênh chat liên thông (MC_BRIDGE_CHANNEL_ID: 1561674502373646439)
    if message.channel.id == MC_BRIDGE_CHANNEL_ID:
        # Bỏ qua tin nhắn từ chính bot hoặc webhook để tránh vòng lặp
        if message.author.bot or message.webhook_id is not None:
            return
        try:
            msg_data = {
                "author": str(message.author.display_name or message.author),
                "author_id": str(message.author.id),
                "content": message.content,
                "timestamp": message.created_at.isoformat(),
                "message_id": str(message.id),
            }
            async with _mc_queue_lock:
                _mc_message_queue.append(msg_data)
                while len(_mc_message_queue) > MC_BRIDGE_MAX_QUEUE:
                    _mc_message_queue.pop(0)
            logger.info(f"[MC-Bridge] Captured message from {message.author}: {message.content[:80]}")
        except Exception as e:
            logger.error(f"[MC-Bridge] Failed to capture message: {e}")
        return

    # Bỏ qua tin nhắn từ chính bot hoặc các bot khác
    if message.author == bot.user or message.author.bot:
        return

    # ================= BẢO MẬT 2 LỚP XÁC MINH SERVER =================
    if message.guild:
        is_valid = await enforce_guild_security(message.guild, target_channel=message.channel if isinstance(message.channel, discord.TextChannel) else None)
        if not is_valid:
            return
    else:
        # Nếu là tin nhắn trực tiếp (DM)
        if message.author.id != AUTHORIZED_OWNER_ID:
            await message.reply("⛔ Bạn không có quyền sử dụng bot, vui lòng liên hệ @phantrongkhangg ( tiktok )")
            return

    # ================= Task Execution Engine (!@tenbot) =================
    if message.content.strip().startswith("!@"):
        await handle_bot_task_command(message)
        return

    # ================= Prefix Commands (!command) =================
    if message.content.startswith(bot.command_prefix):
        await bot.process_commands(message)
        return

    # ================= SMART AI CHAT: TRIGGER BẰNG @TÊN BOT HOẶC REPLY =================
    is_triggered = False
    clean_prompt = message.content.strip()

    if bot.user:
        # 1. Mention trực tiếp @bot (bỏ qua nếu là mention @everyone/@here)
        if bot.user.mentioned_in(message) and not message.mention_everyone:
            is_triggered = True
            clean_prompt = re.sub(rf"<@!?{bot.user.id}>\s*", "", clean_prompt).strip()
        # 2. Reply trả lời lại tin nhắn của bot
        elif message.reference and message.reference.resolved:
            resolved_msg = message.reference.resolved
            if getattr(resolved_msg, "author", None) == bot.user:
                is_triggered = True

    if not is_triggered:
        return

    # Nếu người dùng chỉ tag bot trống trơn không kèm câu hỏi/nội dung
    if not clean_prompt and not message.attachments:
        await message.reply("🐭 Dạ em nghe đây! Anh/bạn cần gì cứ nói, em tiếp chiêu liền nha! 😉")
        return

    async with message.channel.typing():
        try:
            # 1. Thu thập ngữ cảnh 15 tin nhắn gần nhất & hồ sơ thành viên
            channel_name = getattr(message.channel, "name", "tin-nhan")
            server_name = message.guild.name if message.guild else "Discord"
            history_ctx, profiles_ctx = await build_channel_context_and_profiles(message, limit=15)

            # 2. Tạo Meta System Prompt thông minh
            system_prompt = build_meta_system_prompt(
                channel_name=channel_name,
                server_name=server_name,
                profiles_context=profiles_ctx,
                history_context=history_ctx
            )

            # --- Phát hiện ngữ cảnh Voice & AI DJ Conductor ---
            player = None
            is_voice_context = False
            if message.guild:
                try:
                    from music import get_player
                    player = get_player(bot, message.guild)
                except Exception:
                    player = None

            author_voice = getattr(message.author, "voice", None)
            is_voice_ch = hasattr(message.channel, "type") and message.channel.type in (discord.ChannelType.voice, discord.ChannelType.stage_voice)
            has_voice = bool(author_voice and author_voice.channel) or bool(player and player.voice_client and player.voice_client.is_connected())
            music_words = ["nhạc", "bài", "hát", "play", "loop", "volume", "skip", "dừng", "phát", "tâm trạng", "buồn", "vui", "remix", "lofi", "chill", "quẩy"]
            has_music_word = any(w in clean_prompt.lower() for w in music_words)

            if is_voice_ch or has_voice or has_music_word:
                is_voice_context = True

            if is_voice_context and player:
                cur_song = player.current.title if player.current else "Chưa có bài nào đang phát"
                bot_vc = player.voice_client.channel.name if (player.voice_client and player.voice_client.is_connected()) else "Chưa vào phòng voice"
                user_vc = author_voice.channel.name if (author_voice and author_voice.channel) else "Chưa vào phòng voice"
                loop_st = player.loop_mode
                auto_st = "BẬT" if player.autoplay else "TẮT"
                vol_st = f"{int(player.volume * 100)}%"
                is_owner = (message.author.id == AUTHORIZED_OWNER_ID)

                voice_directive = f"""

## 🎧 BỐI CẢNH PHÒNG VOICE & NHIỆM VỤ AI DJ ĐỘC QUYỀN:
Người dùng đang gọi bạn trong ngữ cảnh Phòng Voice / Âm nhạc:
- Kênh chat hiện tại: #{channel_name}
- Bot đang ở phòng voice: {bot_vc}
- Người gọi đang ở phòng voice: {user_vc}
- Bài hát đang phát: {cur_song}
- Chế độ lặp: {loop_st} | Tự đổi bài (Autoplay): {auto_st} | Âm lượng: {vol_st} | Đang chờ: {len(player.queue)} bài
- Quyền hạn người gọi: {'Chủ nhân tối cao phantrongkhangg' if is_owner else 'Thành viên thường'}

HƯỚNG DẪN DÀNH CHO AI DJ:
1. Nếu người yêu cầu là chủ nhân phantrongkhangg:
   - Bạn hãy đóng vai một DJ âm nhạc siêu ngầu, thông minh, chiều chuộng chủ nhân tối đa và bắt miếng (Say yes to the bit).
   - QUY TẮC ĐỔI BÀI LIÊN TỤC (RADIO) VS LẶP 1 BÀI:
     + Khi anh Khang yêu cầu bật nhạc theo tâm trạng (buồn, vui, chill, vinahouse, lofi...), hoặc bảo "bật loop nhạc tâm trạng...", anh ấy muốn nghe nhạc tự động đổi bài liên tục (ĐÀI RADIO):
       -> BẮT BUỘC đặt action="radio", loop="off" để bot tự động chuyển sang các bài hát mới tương đồng khi hết bài!
       Ví dụ khi anh Khang bảo: "hãy bật loop và nhạc theo tâm trạng buồn", bạn trả lời:
       [MUSIC_ACTION: {{"action": "radio", "query": "nhạc lofi buồn tâm trạng suy ngẫm", "loop": "off"}}]
       Sau đó là lời trò chuyện dí dỏm, đồng cảm với tâm trạng của anh Khang!
     + CHỈ KHI anh Khang nói rõ là "lặp lại đúng 1 bài này", "replay bài này thôi", "chỉ nghe đúng bài này":
       -> Lúc đó mới đặt action="play", loop="song".
2. Nếu người gọi KHÔNG PHẢI phantrongkhangg mà đòi bật nhạc/đổi bài/chỉnh nhạc:
   - Tuyệt đối KHÔNG xuất thẻ [MUSIC_ACTION].
   - Trả lời hài hước, từ chối khéo léo và nhắc rằng chỉ có anh Khang mới là DJ độc quyền điều khiển nhạc ở đây!
"""
                system_prompt += voice_directive

            # 3. Tạo payload tin nhắn gửi tới AI
            user_msg_content = f"[Người gọi: {message.author.display_name} (@{message.author.name})]: {clean_prompt}"
            
            messages_payload = [{"role": "system", "content": system_prompt}]
            image_attachments = [a for a in message.attachments if a.content_type and a.content_type.startswith("image/")]
            
            if image_attachments:
                content_blocks = [{"type": "text", "text": user_msg_content}]
                for img in image_attachments:
                    content_blocks.append({"type": "image_url", "image_url": {"url": img.url}})
                messages_payload.append({"role": "user", "content": content_blocks})
            else:
                messages_payload.append({"role": "user", "content": user_msg_content})

            # 4. Gọi Gemini AI với bộ công cụ âm nhạc (Native Tool Calling)
            available_tools = None
            if player and message.guild:
                try:
                    from music import MUSIC_TOOLS_DEFINITIONS, handle_music_tool_call
                    available_tools = MUSIC_TOOLS_DEFINITIONS
                except Exception as e_tl:
                    logger.warning(f"[Tools] Lỗi import tools: {e_tl}")

            call_kwargs = {
                "model": XKIRO_MODEL,
                "temperature": 0.7,
                "max_tokens": 1500,
                "messages": messages_payload
            }
            if available_tools:
                call_kwargs["tools"] = available_tools
                call_kwargs["tool_choice"] = "auto"

            ai_resp = await ai_client.chat.completions.create(**call_kwargs)
            choice_msg = ai_resp.choices[0].message

            # Nếu AI gọi Tool (get_now_playing, pause_music, skip_music, get_queue, play_music...)
            if getattr(choice_msg, "tool_calls", None) and player:
                messages_payload.append(choice_msg)
                for tc in choice_msg.tool_calls:
                    fn_name = tc.function.name
                    try:
                        fn_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    except Exception:
                        fn_args = {}
                    logger.info(f"[Tool-Call] AI gọi tool '{fn_name}' với args: {fn_args}")
                    tool_res = await handle_music_tool_call(bot, message, player, fn_name, fn_args)
                    messages_payload.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(tool_res, ensure_ascii=False)
                    })

                # Lượt 2: AI tổng hợp câu trả lời tự nhiên dựa trên kết quả Tool
                ai_final = await ai_client.chat.completions.create(
                    model=XKIRO_MODEL,
                    temperature=0.7,
                    max_tokens=1500,
                    messages=messages_payload
                )
                reply_text = ai_final.choices[0].message.content or ""
            else:
                reply_text = choice_msg.content or ""

            # Hỗ trợ dự phòng nếu AI xuất định dạng [MUSIC_ACTION: ...]
            action_match = re.search(r'\[MUSIC_ACTION:\s*(\{.*?\})\]', reply_text, flags=re.DOTALL)
            if action_match and player and message.guild and message.author.id == AUTHORIZED_OWNER_ID:
                try:
                    action_data = json.loads(action_match.group(1))
                    reply_text = re.sub(r'\[MUSIC_ACTION:\s*\{.*?\}\]\s*', '', reply_text, flags=re.DOTALL).strip()
                    await execute_ai_music_action(bot, message, player, action_data)
                except Exception as e_act:
                    logger.error(f"[AI-DJ] Lỗi thi hành Music Action: {e_act}")

            await send_smart_reply(message, reply_text)

        except Exception as e:
            logger.error(f"[AI-Chat] Lỗi xử lý đối thoại: {e}")
            err_msg = str(e)[:100]
            await message.reply(f"😅 Úi, đường truyền AI đang nghẽn chút xíu: {err_msg}. Bạn thử lại ngay nha!")


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
            await self.target_channel.send("⚠️ Jet Jet gặp sự cố khi xóa tin nhắn, vui lòng liên hệ chủ nhân **@phantrongkhangg** (TikTok) nhé!")

    @discord.ui.button(label="❌ Hủy bỏ", style=discord.ButtonStyle.secondary, custom_id="clear_cancel_btn")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Bạn không có quyền thực hiện thao tác này!", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="❌ Đã hủy thao tác xóa tin nhắn.", embed=None, view=self)


@bot.tree.command(name="clear", description="Xóa toàn bộ tin nhắn trong một kênh")
@discord.app_commands.describe(channel="Chọn kênh muốn xóa toàn bộ tin nhắn")
@discord.app_commands.guild_only()
async def clear_slash(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await check_manager_interaction(interaction):
        await interaction.response.send_message("❌ Bạn không có quyền sử dụng bot này. Chỉ dành riêng cho @khangmc_vn.", ephemeral=True)
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

@noitu_group.command(name="stop", description="Dừng ván chơi Nối Từ đang diễn ra trong kênh")
async def noitu_stop_slash(interaction: discord.Interaction):
    await stop_noitu_game(interaction)

bot.tree.add_command(noitu_group)

@bot.tree.command(name="ping", description="Kiểm tra độ trễ của bot")
async def ping_slash(interaction: discord.Interaction):
    if not await check_manager_interaction(interaction):
        await interaction.response.send_message("❌ Bạn không có quyền sử dụng bot này. Chỉ dành riêng cho @khangmc_vn.", ephemeral=True)
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
        await interaction.response.send_message("❌ Bạn không có quyền sử dụng bot này. Chỉ dành riêng cho @khangmc_vn.", ephemeral=True)
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


@bot.tree.command(name="serverinfo", description="Xem thông tin server Minecraft (từ WebSocket)")
async def serverinfo_slash(interaction: discord.Interaction):
    if not await check_manager_interaction(interaction):
        await interaction.response.send_message("❌ Bạn không có quyền sử dụng bot này. Chỉ dành riêng cho @khangmc_vn.", ephemeral=True)
        return

    async with _server_stats_lock:
        stats = dict(_server_stats)
    
    if not stats or "_last_update" not in stats:
        await interaction.response.send_message(
            "⚠️ Chưa có dữ liệu từ server Minecraft.\n"
            "Hãy đảm bảo plugin StartSeachKhangg đã được cài và đang gửi dữ liệu qua WebSocket.",
            ephemeral=True
        )
        return

    updated = stats.get("_last_update", "N/A")
    if updated != "N/A":
        try:
            from datetime import datetime as dt
            updated = dt.fromisoformat(updated.replace("Z", "+00:00")).strftime("%H:%M:%S")
        except Exception:
            pass

    # Build embed
    embed = discord.Embed(
        title="🖥️ Thông Tin Server Minecraft",
        color=discord.Color.green()
    )

    # Server info
    server_name = stats.get("server_name", "Unknown")
    mc_version = stats.get("minecraft_version", "Unknown")
    players = stats.get("player_slots_formatted", "?/?")
    tps = stats.get("tps", 0)
    uptime = stats.get("uptime_formatted", "N/A")

    embed.add_field(name="Server", value=f"**{server_name}** (MC {mc_version})", inline=False)
    embed.add_field(name="👥 Players", value=players, inline=True)
    embed.add_field(name="⚡ TPS", value=str(tps), inline=True)
    embed.add_field(name="⏱️ Uptime", value=uptime, inline=True)

    # RAM
    ram_used = stats.get("system_ram_used_mb", 0)
    ram_total = stats.get("system_ram_total_mb", 0)
    ram_pct = stats.get("system_ram_usage_percent", 0)
    jvm_used = stats.get("jvm_heap_used_mb", 0)
    jvm_max = stats.get("jvm_heap_max_mb", 0)
    embed.add_field(
        name="🧠 RAM",
        value=f"System: {ram_used}/{ram_total} MB ({ram_pct}%)\nJVM Heap: {jvm_used}/{jvm_max} MB",
        inline=True
    )

    # CPU
    cpu_cores = stats.get("cpu_cores", 0)
    cpu_load = stats.get("cpu_load_percent", 0)
    embed.add_field(name="🔧 CPU", value=f"{cpu_cores} cores, Load: {cpu_load}%", inline=True)

    # Disk
    disk_used = stats.get("disk_used_gb", 0)
    disk_total = stats.get("disk_total_gb", 0)
    disk_free = stats.get("disk_free_gb", 0)
    embed.add_field(name="💾 Disk", value=f"{disk_used}/{disk_total} GB (Free: {disk_free} GB)", inline=True)

    # Network
    network = stats.get("network", {})
    hostname = network.get("hostname", "N/A")
    ip = network.get("ip", "N/A")
    embed.add_field(name="🌐 Network", value=f"Host: {hostname[:16]}...\nIP: {ip}", inline=True)

    embed.set_footer(text=f"Cập nhật lúc: {updated} | Scan ID: {stats.get('scan_id', 'N/A')}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="help", description="Danh sách các lệnh có sẵn")
async def help_slash(interaction: discord.Interaction):
    if not await check_manager_interaction(interaction):
        await interaction.response.send_message("❌ Bạn không có quyền sử dụng bot này. Chỉ dành riêng cho @khangmc_vn.", ephemeral=True)
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

def load_custom_roles():
    try:
        with open(CUSTOM_ROLES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error(f"Error loading custom roles: {e}")
        return {}

def save_custom_roles(data):
    try:
        with open(CUSTOM_ROLES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info(f"✅ Saved custom roles to {CUSTOM_ROLES_FILE}")
    except Exception as e:
        logger.error(f"Error saving custom roles: {e}")

def add_custom_role(guild_id, role_id):
    data = load_custom_roles()
    guild_str = str(guild_id)
    if guild_str not in data:
        data[guild_str] = []
    if role_id not in data[guild_str]:
        data[guild_str].append(role_id)
        save_custom_roles(data)

def remove_custom_role(guild_id, role_id):
    data = load_custom_roles()
    guild_str = str(guild_id)
    if guild_str in data and role_id in data[guild_str]:
        data[guild_str].remove(role_id)
        save_custom_roles(data)

def get_custom_role_count(guild_id):
    data = load_custom_roles()
    return len(data.get(str(guild_id), []))

def is_custom_role(guild_id, role_id):
    data = load_custom_roles()
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
            add_custom_role(self.interaction.guild_id, role.id)

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
        await interaction.response.send_message("❌ Bạn không có quyền sử dụng bot này. Chỉ dành riêng cho @khangmc_vn.", ephemeral=True)
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
    count = get_custom_role_count(interaction.guild_id)
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
@discord.app_commands.guild_only()
async def customrole_list(interaction: discord.Interaction):
    # Check manager permission
    if not await check_manager_interaction(interaction):
        await interaction.response.send_message("❌ Bạn không có quyền sử dụng bot này. Chỉ dành riêng cho @khangmc_vn.", ephemeral=True)
        return

    data = load_custom_roles()
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
@discord.app_commands.describe(
    role="Role cần xóa (chỉ có thể xóa role do bot tạo)"
)
@discord.app_commands.guild_only()
async def customrole_remove(interaction: discord.Interaction, role: discord.Role):
    # Check permissions
    if not interaction.user.guild_permissions.manage_roles and not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Bạn không có quyền quản lý role.", ephemeral=True)
        return

    # Check if role is custom
    if not is_custom_role(interaction.guild_id, role.id):
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
            remove_custom_role(interaction.guild_id, role.id)
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

# ================= Migration removed (using file storage directly) =================

# ================= Role Selection Panel (Lệnh /by) =================

def load_panels():
    try:
        with open(PANELS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error(f"Error loading panels: {e}")
        return {}

def save_panels(data):
    try:
        with open(PANELS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info(f"✅ Saved panels to {PANELS_FILE}")
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
        await interaction.response.send_message("❌ Bạn không có quyền sử dụng bot này. Chỉ dành riêng cho @khangmc_vn.", ephemeral=True)
        return
    
    target_channel = channel or interaction.channel
    if not target_channel:
        await interaction.response.send_message("❌ Không tìm thấy kênh.", ephemeral=True)
        return
    
    # Lấy danh sách custom role ID
    data = load_custom_roles()
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
    panels = load_panels()
    guild_panels = panels.setdefault(str(interaction.guild_id), {})
    guild_panels[str(message.id)] = {
        "channel_id": target_channel.id,
        "title": title,
        "description": description,
        "color": color
    }
    save_panels(panels)
    
    # Đăng ký view persistent
    bot.add_view(view, message_id=message.id)
    
    await interaction.edit_original_response(
        content=f"✅ Panel đã được gửi vào {target_channel.mention}!"
    )

# Hàm đăng ký lại các persistent view khi bot khởi động
async def register_persistent_views():
    panels = load_panels()
    for guild_id_str, guild_panels in panels.items():
        guild_id = int(guild_id_str)
        guild = bot.get_guild(guild_id)
        if not guild:
            continue
        data = load_custom_roles()
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
                save_panels(panels)
                logger.warning(f"Xóa panel không còn tồn tại: {message_id}")
            except Exception as e:
                logger.error(f"Lỗi khi đăng ký persistent view cho message {message_id}: {e}")


# ================= Minecraft Bridge REST API =================

async def _mc_polling_handler(request):
    """GET /api/discord-messages/latest - Returns queued messages for Minecraft polling."""
    # Auth check
    auth_header = request.headers.get("Authorization", "")
    if not POLLING_API_KEY or auth_header != f"Bearer {POLLING_API_KEY}":
        logger.warning(f"[MC-Bridge] Unauthorized polling request from {request.remote}")
        return web.json_response({"error": "Unauthorized"}, status=401)
    
    async with _mc_queue_lock:
        messages = list(_mc_message_queue)
        _mc_message_queue.clear()  # Mark as fetched by clearing queue
    
    logger.info(f"[MC-Bridge] Polling request: returned {len(messages)} messages")
    return web.json_response({
        "messages": messages,
        "count": len(messages),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


async def _mc_incoming_message_handler(request):
    """POST /api/minecraft-messages - Receive chat messages from Minecraft and forward to Discord."""
    auth_header = request.headers.get("Authorization", "")
    if not POLLING_API_KEY or auth_header != f"Bearer {POLLING_API_KEY}":
        logger.warning(f"[MC-Bridge] Unauthorized incoming chat message from {request.remote}")
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    player = str(data.get("player", "Unknown")).strip()
    msg = str(data.get("message", "")).strip()

    if not player or not msg:
        return web.json_response({"error": "Missing player or message"}, status=400)

    # Chống spam mention @everyone / @here từ trong game
    msg_cleaned = msg.replace("@everyone", "@​everyone").replace("@here", "@​here")

    # Gửi tin nhắn vào kênh MC_BRIDGE_CHANNEL_ID (#🌉・𝐜𝐡𝐚𝐭𝐢𝐧𝐠𝐚𝐦𝐞)
    target_chan = bot.get_channel(MC_BRIDGE_CHANNEL_ID)
    if not target_chan:
        try:
            target_chan = await bot.fetch_channel(MC_BRIDGE_CHANNEL_ID)
        except Exception as e:
            logger.error(f"[MC-Bridge] Cannot find MC Bridge channel {MC_BRIDGE_CHANNEL_ID}: {e}")
            return web.json_response({"error": "Channel not found"}, status=500)

    try:
        await target_chan.send(f"🎮 **{player}**: {msg_cleaned}")
        logger.info(f"[MC-Bridge] Forwarded in-game chat from [{player}]: {msg[:50]}")
        return web.json_response({"status": "ok", "delivered": True})
    except Exception as e:
        logger.error(f"[MC-Bridge] Error sending message to Discord: {e}")
        return web.json_response({"error": str(e)}, status=500)


async def _mc_health_handler(request):
    """GET /api/health - Health check endpoint."""
    return web.json_response({"status": "ok", "queue_size": len(_mc_message_queue)})




# ================= WebSocket Server for MC Plugin Stats =================

async def _ws_server_stats_handler(request):
    """WebSocket endpoint for MC plugin to push server stats continuously."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    
    _ws_connections.add(ws)
    remote = request.remote or "unknown"
    logger.info(f"[WS] MC server connected from {remote}")
    
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    async with _server_stats_lock:
                        _server_stats.update(data)
                        _server_stats["_last_update"] = datetime.now(timezone.utc).isoformat()
                        _server_stats["_source_ip"] = remote
                    # No log per message to avoid spam - only log on connect/disconnect
                except json.JSONDecodeError:
                    logger.warning(f"[WS] Invalid JSON from {remote}: {msg.data[:100]}")
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error(f"[WS] Error from {remote}: {ws.exception()}")
    finally:
        _ws_connections.discard(ws)
        logger.info(f"[WS] MC server disconnected from {remote}")
    
    return ws


async def _server_stats_api_handler(request):
    """GET /api/server-stats - Returns latest server stats as JSON."""
    auth_header = request.headers.get("Authorization", "")
    if not POLLING_API_KEY or auth_header != f"Bearer {POLLING_API_KEY}":
        return web.json_response({"error": "Unauthorized"}, status=401)
    
    async with _server_stats_lock:
        stats = dict(_server_stats)
    
    if not stats:
        return web.json_response({"error": "No server stats available yet"}, status=503)
    
    return web.json_response(stats)


async def start_mc_bridge_server():
    """Start the aiohttp web server for Minecraft bridge API."""
    app = web.Application()
    app.router.add_get("/api/discord-messages/latest", _mc_polling_handler)
    app.router.add_get("/api/health", _mc_health_handler)
    app.router.add_post("/api/minecraft-messages", _mc_incoming_message_handler)
    app.router.add_get("/ws/server-stats", _ws_server_stats_handler)
    app.router.add_get("/api/server-stats", _server_stats_api_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", MC_BRIDGE_PORT)
    await site.start()
    logger.info(f"[MC-Bridge] REST API server started on port {MC_BRIDGE_PORT}")
    logger.info(f"[MC-Bridge] Endpoint: GET /api/discord-messages/latest")
    logger.info(f"[MC-Bridge] Health: GET /api/health")
    logger.info(f"[MC-Bridge] WebSocket: ws://0.0.0.0:{MC_BRIDGE_PORT}/ws/server-stats")
    logger.info(f"[MC-Bridge] Stats API: GET /api/server-stats")
    return runner  # Keep reference to prevent GC



# Gọi register_persistent_views trong on_ready
# Cập nhật hàm on_ready để gọi register_persistent_views sau khi sync
# Chúng ta sẽ patch on_ready bằng cách thêm vào cuối hàm hiện tại


# ================= /paste Command =================

class PasteModal(discord.ui.Modal, title="📋 Tạo tin nhắn"):
    content = discord.ui.TextInput(
        label="Nội dung",
        placeholder="Nhập hoặc dán nội dung tại đây...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=4000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        preview_embed = discord.Embed(
            title="Xem trước",
            description=self.content.value,
            color=discord.Color.blurple(),
        )
        preview_embed.set_footer(text="Chỉ bạn mới thấy tin nhắn này.")
        view = PasteConfirmView(self.content.value)
        await interaction.response.send_message(embed=preview_embed, view=view, ephemeral=True)


class PasteConfirmView(discord.ui.View):
    def __init__(self, content: str):
        super().__init__(timeout=120)
        self.paste_content = content

    @discord.ui.button(label="Gửi", style=discord.ButtonStyle.green, emoji="✅")
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(description=self.paste_content, color=discord.Color.blurple())
        await interaction.channel.send(embed=embed, reference=None, mention_author=False)
        await interaction.response.edit_message(content="✅ Đã gửi tin nhắn!", embed=None, view=None)
        self.stop()

    @discord.ui.button(label="Hủy", style=discord.ButtonStyle.red, emoji="❌")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ Đã hủy.", embed=None, view=None)
        self.stop()


@bot.tree.command(name="paste", description="Dán nội dung và gửi dưới dạng embed")
async def paste_cmd(interaction: discord.Interaction):
    modal = PasteModal()
    await interaction.response.send_modal(modal)


@bot.tree.command(name="reload", description="🔄 Khởi động lại bot để nạp code mới (Quản trị viên)")
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


async def main():
    logger.info("[MAIN] Khởi chạy Bot chính Jet Jet bi sun rang...")
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
