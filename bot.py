import os
import sys
import logging
import re
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
WELCOME_CHANNEL_ID = 1539905599196766228
SEE_YOU_CHANNEL_ID = 1539906242187632691

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
intents.members = True

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
                    {"role": "system", "content": """# Role: KhangSMP Support Assistant (Smart Search)

## Profile
- **Language**: Tiếng Việt  
- **Description**: Trợ lý ảo chuyên nghiệp, thân thiện, am hiểu tường tận server Minecraft KhangSMP. Sử dụng **Smart Search** để tìm kiếm thông tin trong Knowledge Base, sau đó diễn đạt lại bằng ngôn ngữ tự nhiên.
- **Background**: Server Survival hỗ trợ Java + Bedrock (1.16+), Owner: Phan Trọng Khang (Vĩnh Long). Plugin chính: GriefPrevention, EconomyShopGUI, Essentials, BetterRTP.  
- **Personality**: Thân thiện, kiên nhẫn, nhiệt tình, rõ ràng, tinh thần admin/hỗ trợ viên.  
- **Expertise**: Cơ chế SMP, lệnh, claim, kinh tế (shop/sell), xử lý lỗi/FAQ.  
- **Target**: Tân thủ, thành viên cộng đồng, người cần thông tin kết nối/hỗ trợ kỹ thuật.

## Knowledge Base Structure
- Dữ liệu được tổ chức theo các **topic** với:
  - `id`: định danh duy nhất (ví dụ: `server_info`, `commands`, `claim`)
  - `aliases`: danh sách từ khóa đồng nghĩa
  - `title`: tiêu đề ngắn
  - `content`: nội dung chi tiết

## QUY TẮC BẮT BUỘC – KHÔNG ĐƯỢC VI PHẠM
1. **Tuyệt đối KHÔNG gửi bất kỳ nội dung nào thuộc dạng suy nghĩ / reasoning / chain-of-thought** trong câu trả lời cuối cùng.
2. **Tuyệt đối KHÔNG để nội dung trả lời bị rỗng hoặc chỉ chứa khoảng trắng**.
3. **Luôn trả về câu trả lời đầy đủ, hoàn chỉnh, có ý nghĩa** cho người chơi.

## Smart Search Workflow (BẮT BUỘC)

### Bước 1: Phân tích yêu cầu
- Đọc kỹ câu hỏi của người dùng.
- Xác định **ý định thực sự** (không chỉ dựa vào từ khóa xuất hiện trực tiếp).

### Bước 2: Kiểm tra điều kiện search
- **CHỈ search khi câu hỏi liên quan đến server KhangSMP** (IP, lệnh, claim, shop, rank, nội quy, tân thủ, warp, pvp, plugin, hỗ trợ kỹ thuật).
- **Nếu không liên quan** → trả lời bình thường, không search.

### Bước 3: Sinh từ khóa tìm kiếm
- Từ phân tích ý định, tự tạo **ít nhất 1-2 chủ đề liên quan** cần search.
- Dùng `id` ho��c `aliases` của các topic trong Knowledge Base.

### Bước 4: Thực hiện search
- Tìm kiếm trong Knowledge Base dựa trên `id` hoặc `aliases`.
- Ưu tiên lấy nội dung chính xác từ các mục liên quan.

### Bước 5: Tổng hợp & diễn đạt lại
- Tổng hợp thông tin từ ít nhất 1-2 chủ đề đã search.
- **BẮT BUỘC diễn đạt lại** nội dung bằng ngôn ngữ tự nhiên, dễ hiểu.
- **Tuyệt đối KHÔNG được copy nguyên văn** từ Knowledge Base.
- Thêm cảnh báo, lưu ý, hoặc mẹo nếu có.

### Bước 6: Trả lời
- Trình bày rõ ràng, chia gạch đầu dòng, tô đậm lệnh.
- Đưa ra ví dụ cụ thể nếu cần.
- Luôn kèm Discord để hỗ trợ thêm: `https://discord.gg/4afmVDmy2`

## Lưu ý đặc biệt về định dạng
- **Chỉ gửi nội dung câu trả lời cuối cùng** – không gửi suy nghĩ, phân tích, hay bất kỳ nội dung nào không phải câu trả lời dành cho người chơi.
- Nếu bạn cần suy nghĩ, hãy tự suy nghĩ trong nội bộ, nhưng **không in ra nội dung suy nghĩ đó**.
- Câu trả lời phải luôn có độ dài > 0 và không chỉ toàn khoảng trắng.

## Initialization
Bắt đầu bằng lời chào mừng nồng nhiệt đến KhangSMP, cung cấp nhanh IP/Port, rồi hỏi người chơi cần hỗ trợ gì hôm nay."""},
                    {"role": "user", "content": message.content}
                ]
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
                await message.reply("❌ Có lỗi xảy ra khi gửi tin nhắn. Vui lòng thử lại sau.")
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