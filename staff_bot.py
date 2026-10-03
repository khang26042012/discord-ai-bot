import os
import sys
import json
import logging
import asyncio
import urllib.request
import urllib.parse
import discord
from discord import app_commands
from discord.ext import commands
from mcstatus import JavaServer

logger = logging.getLogger("StaffBot")

NVNMC_KEY = os.getenv("NVNMC_API_KEY")
if not NVNMC_KEY:
    nvnmc_key_path = os.path.expanduser("~/.credentials/nvnmc.key")
    if os.path.exists(nvnmc_key_path):
        with open(nvnmc_key_path, "r", encoding="utf-8") as f:
            NVNMC_KEY = f.read().strip()

NVNMC_SERVER_ID = os.getenv("NVNMC_SERVER_ID", "7d69f9de")
NVNMC_BASE_URL = "https://panel.nvnmc.cloud/api/client/servers/" + NVNMC_SERVER_ID
MC_SERVER_HOST = os.getenv("MC_SERVER_HOST", "1.54.183.121")
MC_SERVER_PORT = int(os.getenv("MC_SERVER_PORT", "26181"))

ADMIN_IDS = {
    1263707768876044382, # khangmc_vn / phantrongkhangg
    1368172692045434981, # 26042012khang / Khangplay
    1539894170452238348, # nhi_chuot / Chip iu
    1286678485116387339, # phb.duong_98521 / bhd.duong
}
ADMIN_CHANNEL_ID = int(os.getenv("ADMIN_CHANNEL_ID", "1555918488386932797"))

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

staff_bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

def is_authorized(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def send_nvnmc_cmd(cmd: str) -> bool:
    if not NVNMC_KEY:
        logger.error("NVNMC_KEY not configured!")
        return False
    url = NVNMC_BASE_URL + "/command"
    headers = {
        "Authorization": "Bearer " + NVNMC_KEY,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "NVNMC-Bot/1.0"
    }
    data = json.dumps({"command": cmd}).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        return resp.status in (200, 204)
    except Exception as e:
        logger.error("Failed to send command: " + str(e))
        return False

def get_nvnmc_resources() -> dict:
    if not NVNMC_KEY:
        return {}
    url = NVNMC_BASE_URL + "/resources"
    headers = {
        "Authorization": "Bearer " + NVNMC_KEY,
        "Accept": "application/json",
        "User-Agent": "NVNMC-Bot/1.0"
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read().decode())
        attrs = data.get("attributes", {})
        res = attrs.get("resources", {})
        return {
            "state": attrs.get("current_state", "unknown"),
            "cpu": res.get("cpu_absolute", 0.0),
            "ram_mb": res.get("memory_bytes", 0) / (1024 * 1024),
            "disk_mb": res.get("disk_bytes", 0) / (1024 * 1024),
            "uptime_h": res.get("uptime", 0) / 3600
        }
    except Exception as e:
        logger.error("Failed to fetch resources: " + str(e))
        return {}

def get_mc_status_data() -> dict:
    try:
        server = JavaServer.lookup(MC_SERVER_HOST + ":" + str(MC_SERVER_PORT))
        status = server.status()
        players = []
        if status.players.sample:
            players = [p.name for p in status.players.sample]
        return {
            "online": True,
            "latency_ms": round(status.latency),
            "players_online": status.players.online,
            "players_max": status.players.max,
            "player_list": players,
            "version": status.version.name
        }
    except Exception as e:
        logger.warning("MC ping failed: " + str(e))
        return {
            "online": False,
            "latency_ms": 0,
            "players_online": 0,
            "players_max": 0,
            "player_list": [],
            "version": "Unknown"
        }

@staff_bot.event
async def on_ready():
    logger.info("StaffBot logged in as " + str(staff_bot.user) + " (ID: " + str(staff_bot.user.id) + ")")
    try:
        synced = await staff_bot.tree.sync()
        logger.info("StaffBot synced " + str(len(synced)) + " slash commands.")
    except Exception as e:
        logger.error("StaffBot error syncing slash commands: " + str(e))
    await staff_bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="KhangSMP Admin Core 🛡️"))

@staff_bot.tree.command(name="ping", description="Kiểm tra độ trễ của bot")
async def slash_ping(interaction: discord.Interaction):
    latency = round(staff_bot.latency * 1000)
    embed = discord.Embed(
        title="🏓 Pong!",
        description="Độ trễ bot: **" + str(latency) + "ms**",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed)

@staff_bot.tree.command(name="sv_status", description="Xem thông số hiệu năng máy chủ Minecraft")
async def slash_sv_status(interaction: discord.Interaction):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message("❌ Bạn không có quyền quản trị!", ephemeral=True)
        return
    await interaction.response.defer()
    res = await asyncio.to_thread(get_nvnmc_resources)
    mc = await asyncio.to_thread(get_mc_status_data)
    state_str = "🟢 Đang chạy (ONLINE)" if res.get("state") == "running" else "🔴 " + str(res.get("state", "OFFLINE"))
    embed = discord.Embed(
        title="📊 TRẠNG THÁI MÁY CHỦ KHANGSMP (SERVER PHỤ)",
        color=0x2ecc71 if res.get("state") == "running" else 0xe74c3c
    )
    embed.add_field(name="⚙️ Trạng thái Panel", value="**" + state_str + "**", inline=True)
    embed.add_field(name="📶 Ping Server", value="`" + str(mc.get("latency_ms", 0)) + "ms`", inline=True)
    embed.add_field(name="👥 Người chơi", value="**" + str(mc.get("players_online", 0)) + " / " + str(mc.get("players_max", 50)) + "**", inline=True)
    embed.add_field(name="💻 CPU Sử Dụng", value="`" + f"{res.get('cpu', 0.0):.1f}" + "%`", inline=True)
    embed.add_field(name="💾 RAM Sử Dụng", value="`" + f"{res.get('ram_mb', 0.0):.1f}" + " MB`", inline=True)
    embed.add_field(name="💽 Dung Lượng Ổ Đĩa", value="`" + f"{res.get('disk_mb', 0.0):.1f}" + " MB`", inline=True)
    embed.add_field(name="⏱️ Uptime", value="`" + f"{res.get('uptime_h', 0.0):.1f}" + " giờ`", inline=True)
    embed.add_field(name="🌐 Địa Chỉ Server", value="`" + MC_SERVER_HOST + ":" + str(MC_SERVER_PORT) + "`", inline=True)
    await interaction.followup.send(embed=embed)

@staff_bot.tree.command(name="list_players", description="Liệt kê danh sách người chơi online")
async def slash_list_players(interaction: discord.Interaction):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message("❌ Bạn không có quyền quản trị!", ephemeral=True)
        return
    await interaction.response.defer()
    mc = await asyncio.to_thread(get_mc_status_data)
    online_count = mc.get("players_online", 0)
    players = mc.get("player_list", [])
    embed = discord.Embed(
        title="👥 DANH SÁCH NGƯỜI CHƠI ĐANG ONLINE",
        color=0x3498db
    )
    embed.description = "Hiện đang có **" + str(online_count) + " / " + str(mc.get("players_max", 50)) + "** người chơi online."
    if players:
        embed.add_field(name="🎮 Danh Sách Tên:", value="\n".join(["• `" + p + "`" for p in players]), inline=False)
    else:
        embed.add_field(name="🎮 Danh Sách Tên:", value="*Hiện tại không có ai online.*", inline=False)
    await interaction.followup.send(embed=embed)

@staff_bot.tree.command(name="gamemode", description="Thay đổi chế độ chơi của một người chơi")
@app_commands.describe(player="Tên người chơi Minecraft", mode="Chế độ chơi")
@app_commands.choices(mode=[
    app_commands.Choice(name="Sinh Tồn (Survival)", value="survival"),
    app_commands.Choice(name="Sáng Tạo (Creative)", value="creative"),
    app_commands.Choice(name="Khán Giả (Spectator)", value="spectator"),
    app_commands.Choice(name="Phiêu Lưu (Adventure)", value="adventure")
])
async def slash_gamemode(interaction: discord.Interaction, player: str, mode: app_commands.Choice[str]):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message("❌ Bạn không có quyền quản trị!", ephemeral=True)
        return
    await interaction.response.defer()
    cmd = "gamemode " + mode.value + " " + player
    ok = await asyncio.to_thread(send_nvnmc_cmd, cmd)
    if ok:
        embed = discord.Embed(
            title="🎮 ĐỔI CHẾ ĐỘ CHƠI THÀNH CÔNG",
            description="Đã chuyển chế độ của **" + player + "** sang **" + mode.name + "**!\nLệnh thực thi: `" + cmd + "`",
            color=0x2ecc71
        )
    else:
        embed = discord.Embed(
            title="❌ THẤT BẠI",
            description="Không thể gửi lệnh đổi chế độ cho **" + player + "**. Vui lòng kiểm tra lại kết nối máy chủ.",
            color=0xe74c3c
        )
    await interaction.followup.send(embed=embed)

@staff_bot.tree.command(name="endlock", description="Khóa, Mở hoặc Xem trạng thái thế giới The End")
@app_commands.describe(action="Hành động với cổng The End")
@app_commands.choices(action=[
    app_commands.Choice(name="🔒 Khóa Cổng The End (lock)", value="lock"),
    app_commands.Choice(name="🔓 Mở Cổng The End (unlock)", value="unlock"),
    app_commands.Choice(name="ℹ️ Xem Trạng Thái (status)", value="status")
])
async def slash_endlock(interaction: discord.Interaction, action: app_commands.Choice[str]):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message("❌ Bạn không có quyền quản trị!", ephemeral=True)
        return
    await interaction.response.defer()
    cmd = "endlock " + action.value
    ok = await asyncio.to_thread(send_nvnmc_cmd, cmd)
    if ok:
        if action.value == "lock":
            embed = discord.Embed(
                title="🔒 ĐÃ KHÓA CỔNG THE END",
                description="Thế giới The End đã được khóa chặt! Người chơi không thể vào End.",
                color=0xe74c3c
            )
        elif action.value == "unlock":
            embed = discord.Embed(
                title="🔓 ĐÃ MỞ CỔNG THE END",
                description="Cổng The End đã chính thức mở cửa trở lại cho toàn bộ người chơi tham gia săn Rồng!",
                color=0x2ecc71
            )
        else:
            embed = discord.Embed(
                title="ℹ️ LỆNH ĐÃ GỬI TỚI MÁY CHỦ",
                description="Đã gửi lệnh kiểm tra `" + cmd + "` tới console máy chủ.",
                color=0x3498db
            )
    else:
        embed = discord.Embed(
            title="❌ THẤT BẠI",
            description="Không thể kết nối tới console máy chủ NVNMC để thực thi lệnh EndLock!",
            color=0xe74c3c
        )
    await interaction.followup.send(embed=embed)

@staff_bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.author == staff_bot.user:
        return
    if not is_authorized(message.author.id):
        return
    content = message.content.strip().lower()

    if content in ("ping", "!ping"):
        lat = round(staff_bot.latency * 1000)
        await message.reply("🏓 **Pong!** Độ trễ: `" + str(lat) + "ms`")
        return

    if content in ("sv status", "!sv status", "!status"):
        res = await asyncio.to_thread(get_nvnmc_resources)
        mc = await asyncio.to_thread(get_mc_status_data)
        state_str = "🟢 Đang chạy (ONLINE)" if res.get("state") == "running" else "🔴 " + str(res.get("state", "OFFLINE"))
        embed = discord.Embed(title="📊 TRẠNG THÁI MÁY CHỦ KHANGSMP", color=0x2ecc71)
        embed.add_field(name="⚙️ Trạng thái", value=state_str, inline=True)
        embed.add_field(name="📶 Ping", value=str(mc.get("latency_ms", 0)) + "ms", inline=True)
        embed.add_field(name="👥 Online", value=str(mc.get("players_online", 0)) + "/" + str(mc.get("players_max", 50)), inline=True)
        embed.add_field(name="💻 CPU", value=f"{res.get('cpu', 0.0):.1f}%", inline=True)
        embed.add_field(name="💾 RAM", value=f"{res.get('ram_mb', 0.0):.1f} MB", inline=True)
        embed.add_field(name="💽 Disk", value=f"{res.get('disk_mb', 0.0):.1f} MB", inline=True)
        embed.add_field(name="⏱️ Uptime", value=f"{res.get('uptime_h', 0.0):.1f} giờ", inline=True)
        await message.reply(embed=embed)
        return

    if content in ("list player", "!list player", "list", "!list", "!players"):
        mc = await asyncio.to_thread(get_mc_status_data)
        online = mc.get("players_online", 0)
        pl = mc.get("player_list", [])
        embed = discord.Embed(title="👥 Người chơi online (" + str(online) + "/" + str(mc.get("players_max", 50)) + ")", color=0x3498db)
        embed.description = "\n".join(["• `" + p + "`" for p in pl]) if pl else "*Hiện tại không có ai online.*"
        await message.reply(embed=embed)
        return

    if content.startswith("gamemode ") or content.startswith("!gamemode ") or content.startswith("gm ") or content.startswith("!gm "):
        p = message.content.strip().split()
        if len(p) >= 3:
            mode, pl = p[1], p[2]
            cmd = "gamemode " + mode + " " + pl
            ok = await asyncio.to_thread(send_nvnmc_cmd, cmd)
            if ok:
                await message.reply("✅ Đã chuyển chế độ của **" + pl + "** sang **" + mode + "** (`" + cmd + "`)!")
            else:
                await message.reply("❌ Thất bại gửi lệnh tới máy chủ!")
        else:
            await message.reply("Cú pháp: `gamemode <mode> <player>`")
        return

    if content.startswith("endlock ") or content.startswith("!endlock ") or content.startswith("end ") or content.startswith("!end "):
        p = message.content.strip().split()
        if len(p) >= 2:
            act = p[1]
            cmd = "endlock " + act
            ok = await asyncio.to_thread(send_nvnmc_cmd, cmd)
            if ok:
                if act == "lock":
                    await message.reply("🔒 **ĐÃ KHÓA CỔNG THE END!** Người chơi không thể vào End.")
                elif act == "unlock":
                    await message.reply("🔓 **ĐÃ MỞ CỔNG THE END!** Chúc anh em săn Rồng vui vẻ.")
                else:
                    await message.reply("ℹ️ Đã gửi lệnh `" + cmd + "` tới máy chủ!")
            else:
                await message.reply("❌ Thất bại gửi lệnh tới máy chủ!")
        else:
            await message.reply("Cú pháp: `endlock <lock|unlock|status>`")
        return

    await staff_bot.process_commands(message)
