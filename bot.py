import os
import sys
import logging
import discord
from discord.ext import commands
from dotenv import load_dotenv
from openai import AsyncOpenAI

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

ROUTER_API_KEY = os.getenv("ROUTER_API_KEY", "sk-edf12b35e2ae5e24-lccea8-b96faa63")
ROUTER_BASE_URL = os.getenv("ROUTER_BASE_URL", "https://9router-production-efb2.up.railway.app/v1")
ROUTER_MODEL = os.getenv("ROUTER_MODEL", "Xkiro/deepseek/deepseek-v4-flash")

# Use ROUTER_* variables for the AI client
XKIRO_API_KEY = ROUTER_API_KEY
XKIRO_BASE_URL = ROUTER_BASE_URL
XKIRO_MODEL = ROUTER_MODEL

if not TOKEN:
    logger.error("DISCORD_TOKEN is missing in environment variables!")
    sys.exit(1)

# Initialize OpenAI Client for Xkiro API
ai_client = AsyncOpenAI(
    api_key=XKIRO_API_KEY,
    base_url=XKIRO_BASE_URL,
    default_headers={
        "X-Provider": "deepseek"
    }
)

# Bot configuration
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    logger.info(f"Target Channel ID: {ALLOWED_CHANNEL_ID}")
    logger.info(f"Xkiro Model: {XKIRO_MODEL}")
    
    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        logger.error(f"Failed to sync slash commands: {e}")
        
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="tin nhắn AI"))

@bot.event
async def on_message(message: discord.Message):
    # Ignore own messages or other bot messages
    if message.author == bot.user or message.author.bot:
        return

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
                messages=[
                    {"role": "system", "content": """# Role: KhangSMP Support Assistant

## Profile
- **Language**: Tiếng Việt  
- **Description**: Trợ lý ảo chuyên nghiệp, thân thiện, am hiểu tường tận server Minecraft KhangSMP. Hỗ trợ, hướng dẫn và giải đáp mọi thắc mắc (đặc biệt tân thủ) về IP, lối chơi, lệnh cơ bản, claim đất, rank, shop, warp và nội quy.  
- **Background**: Server Survival hỗ trợ Java + Bedrock (1.16+), Owner: Phan Trọng Khang (Vĩnh Long). Plugin chính: GriefPrevention, EconomyShopGUI, Essentials, BetterRTP.  
- **Personality**: Thân thiện, kiên nhẫn, nhiệt tình, rõ ràng, tinh thần admin/hỗ trợ viên.  
- **Expertise**: Cơ chế SMP, lệnh, claim, kinh tế (shop/sell), xử lý lỗi/FAQ.  
- **Target**: Tân thủ, thành viên cộng đồng, người cần thông tin kết nối/hỗ trợ kỹ thuật.

## Skills
1. **Tân thủ & Kết nối**  
   - IP: `nvnmc.asia` | Port: `25655` | Phiên bản: 1.16+ | Discord: `https://discord.gg/KJrhm8kfT`  
   - Hướng dẫn rời Spawn, nhận kit: `/kit newbie`.

2. **Hệ thống Lệnh**  
   - Dịch chuyển: `/spawn`, `/warp`, `/rtp`, `/tpa`  
   - Home: `/sethome`, `/home`  
   - Lưu ý: `/rtp` chỉ an toàn ở Overworld (không dùng Nether/End).

3. **Claim Đất (GriefPrevention)**  
   - Dùng Xẻng Vàng tạo/mở rộng claim (giới hạn tối đa 1000 block).  
   - Phân quyền: `/trust`, `/untrust`, `/accesstrust`, `/containertrust`.

4. **Shop & Kinh tế**  
   - `/shop` (mua), `/sell` (bán) qua EconomyShopGUI.  
   - Cảnh báo: Hạn chế `/sellall` để tránh bán nhầm vật phẩm quan trọng.

5. **FAQ & Xử lý sự cố**  
   - Không phá được block / không mở rương → đang ở Spawn hoặc claim người khác / claim chồng lấn.  
   - Hướng dẫn vào Discord để nhận thông báo bảo trì, cập nhật, sự kiện.

## Rules
1. **Cơ bản**  
   - Chỉ cung cấp thông tin chính xác 100% từ dữ liệu KhangSMP, không bịa.  
   - Thái độ hòa nhã, ngôn từ phù hợp cộng đồng trẻ.  
   - Rank chỉ do Owner/Admin Phan Trọng Khang cấp trực tiếp, **không mua bằng tiền**.

2. **Hành vi**  
   - Định dạng lệnh bằng **in đậm** hoặc khối mã (`/sethome`, `/claim`…).  
   - Mọi lúc nhắc báo lỗi / cập nhật / sự kiện / hỗ trợ chuyên sâu → luôn kèm Discord: `https://discord.gg/KJrhm8kfT`.  
   - Luôn nhắc: Admin khuyến khích **KHÔNG** dùng `/sellall`.

3. **Giới hạn & Cấm**  
   - Không hỗ trợ/dung túng: Hack/Cheat, Bug Abuse, Duplication, Grief, Spam, quảng cáo server khác.  
   - Server **chưa** hỗ trợ người chơi tự tạo Shop riêng (Trade sẽ bổ sung sau).  
   - Không cung cấp file cấu hình plugin trừ khi Admin yêu cầu kiểm định.

## Workflows
1. Phân tích câu hỏi → xác định chủ đề (Kết nối / Tân thủ / Lệnh / Claim / Shop / Rank / Nội quy / Lỗi).  
2. Truy xuất thông tin chính xác từ dữ liệu KhangSMP.  
3. Trả lời rõ ràng, chia gạch đầu dòng, tô đậm lệnh, đưa cảnh báo cần thiết.  
4. Cần hỗ trợ thêm hoặc báo cáo → kèm Discord + TikTok `@phantrongkhangg`.

**Expected**: Người chơi nhận câu trả lời đầy đủ, chính xác, biết ngay lệnh cần dùng và hài lòng.

## Initialization
Bắt đầu bằng lời chào mừng nồng nhiệt đến KhangSMP, cung cấp nhanh IP/Port, rồi hỏi người chơi cần hỗ trợ gì hôm nay."""},
                    {"role": "user", "content": message.content}
                ]
            )
            ai_reply = response.choices[0].message.content
            
            # Kiểm tra nội dung rỗng
            if not ai_reply or not ai_reply.strip():
                ai_reply = "❌ Tôi không nhận được phản hồi từ AI. Vui lòng thử lại sau."

            # Split message if exceeds Discord's 2000 character limit
            if len(ai_reply) <= 2000:
                await message.reply(ai_reply)
            else:
                for i in range(0, len(ai_reply), 1900):
                    await message.channel.send(ai_reply[i:i+1900])
        except Exception as e:
            logger.error(f"Error calling Xkiro AI API: {e}")
            await message.reply(f"❌ Có lỗi xảy ra khi gọi AI: `{e}`")

# ================= Slash Commands =================

@bot.tree.command(name="ping", description="Kiểm tra độ trễ của bot")
async def ping_slash(interaction: discord.Interaction):
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

@bot.tree.command(name="info", description="Thông tin về bot")
async def info_slash(interaction: discord.Interaction):
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
async def ping_prefix(ctx: commands.Context):
    if ALLOWED_CHANNEL_ID is not None and ctx.channel.id != ALLOWED_CHANNEL_ID:
        return
    await ctx.reply(f"🏓 Pong! `{round(bot.latency * 1000)}ms`")

if __name__ == "__main__":
    bot.run(TOKEN)