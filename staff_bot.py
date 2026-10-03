# -*- coding: utf-8 -*-
import os
import sys
import json
import re
import time
import logging
import asyncio
import datetime
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
        logger.error("[StaffBot] NVNMC_KEY not configured!")
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
        resp = urllib.request.urlopen(req, timeout=7)
        return resp.status in (200, 204)
    except Exception as e:
        logger.error("[StaffBot] Command failed: " + str(e))
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
        resp = urllib.request.urlopen(req, timeout=7)
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
        logger.error("[StaffBot] Fetch resources failed: " + str(e))
        return {}

def read_endlock_config() -> dict:
    if not NVNMC_KEY:
        return {}
    url = NVNMC_BASE_URL + "/files/contents?file=%2Fplugins%2FEndLock%2Fconfig.yml"
    headers = {
        "Authorization": "Bearer " + NVNMC_KEY,
        "Accept": "text/plain",
        "User-Agent": "NVNMC-Bot/1.0"
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=7)
        content = resp.read().decode("utf-8")
        is_locked = True
        reason = "Maintenance"
        for line in content.splitlines():
            line_s = line.strip()
            if line_s.startswith("locked:"):
                val = line_s.split(":", 1)[1].strip().lower()
                is_locked = (val == "true")
            elif line_s.startswith("lock-reason:"):
                reason = line_s.split(":", 1)[1].strip()
        return {"locked": is_locked, "reason": reason}
    except Exception as e:
        logger.error("[StaffBot] Read EndLock config failed: " + str(e))
        return {}

def get_online_mc_players() -> list:
    """Chi lay danh sach nguoi choi DANG DANG NHAP TRUC TIEP TRONG SERVER MINECRAFT."""
    players = []
    # 1. Thu bang Server List Ping (mcstatus)
    try:
        server = JavaServer.lookup(MC_SERVER_HOST + ":" + str(MC_SERVER_PORT))
        status = server.status()
        if status.players.online == 0:
            return []
        if status.players.sample:
            for p in status.players.sample:
                if p.name and p.name not in players:
                    players.append(p.name)
            if players:
                return players
    except Exception as e:
        logger.warning("[StaffBot] mcstatus ping player sample error: " + str(e))

    # 2. Neu online > 0 nhung sample rong (an boi proxy/paper), lay bang lenh /list qua console log
    if NVNMC_KEY:
        try:
            send_nvnmc_cmd("list")
            time.sleep(0.4)
            url = NVNMC_BASE_URL + "/files/contents?file=%2Flogs%2Flatest.log"
            headers = {"Authorization": "Bearer " + NVNMC_KEY, "User-Agent": "NVNMC-Bot/1.0"}
            req = urllib.request.Request(url, headers=headers)
            content = urllib.request.urlopen(req, timeout=5).read().decode(errors="ignore")
            for line in reversed(content.splitlines()[-25:]):
                m = re.search(r"There are (\d+) of a max of (\d+) players online:(.*)", line)
                if m:
                    count = int(m.group(1))
                    names_str = m.group(3).strip()
                    if count == 0 or not names_str:
                        return []
                    for n in names_str.split(","):
                        n_clean = n.strip()
                        if n_clean and n_clean not in players:
                            players.append(n_clean)
                    return players
        except Exception as e:
            logger.error("[StaffBot] Fallback /list log parse error: " + str(e))

    return players

def get_mc_status_data() -> dict:
    try:
        server = JavaServer.lookup(MC_SERVER_HOST + ":" + str(MC_SERVER_PORT))
        status = server.status()
        sample = [p.name for p in status.players.sample] if status.players.sample else []
        return {
            "online": True,
            "latency_ms": round(status.latency),
            "players_online": status.players.online,
            "players_max": status.players.max,
            "player_list": sample,
            "version": status.version.name
        }
    except Exception as e:
        logger.warning("[StaffBot] MC ping failed: " + str(e))
        return {
            "online": False,
            "latency_ms": 0,
            "players_online": 0,
            "players_max": 0,
            "player_list": [],
            "version": "Unknown"
        }

class GamemodeView(discord.ui.View):
    def __init__(self, author, online_players):
        super().__init__(timeout=120)
        self.author = author
        self.selected_player = online_players[0]
        self.selected_mode = "survival"
        self.mode_label = "Sinh Tồn (Survival)"

        # Dropdown 1: CHI CHUA NGUOI CHOI DANG ONLINE TRONG SERVER MINECRAFT
        opts = [
            discord.SelectOption(
                label=p,
                description="Đang online trong server: " + p,
                emoji="🟢",
                default=(i == 0)
            ) for i, p in enumerate(online_players[:25])
        ]
        self.player_select = discord.ui.Select(
            placeholder="🟢 Bước 1: Chọn người chơi đang online...",
            min_values=1,
            max_values=1,
            options=opts,
            row=0
        )
        self.player_select.callback = self.player_select_callback
        self.add_item(self.player_select)

        # Dropdown 2: Che do choi
        mode_opts = [
            discord.SelectOption(label="Sinh Tồn (Survival)", value="survival", description="Chế độ sinh tồn mặc định", emoji="⚔️", default=True),
            discord.SelectOption(label="Sáng Tạo (Creative)", value="creative", description="Bay lượn và lấy vật phẩm vô hạn", emoji="🎨"),
            discord.SelectOption(label="Khán Giả (Spectator)", value="spectator", description="Tàng hình, bay xuyên tường", emoji="👁️"),
            discord.SelectOption(label="Phiêu Lưu (Adventure)", value="adventure", description="Không thể tự ý phá block", emoji="🗺️")
        ]
        self.mode_select = discord.ui.Select(
            placeholder="⚙️ Bước 2: Chọn chế độ muốn đổi...",
            min_values=1,
            max_values=1,
            options=mode_opts,
            row=1
        )
        self.mode_select.callback = self.mode_select_callback
        self.add_item(self.mode_select)

    async def player_select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("❌ Bạn không có quyền thao tác trên menu này!", ephemeral=True)
            return
        self.selected_player = self.player_select.values[0]
        for opt in self.player_select.options:
            opt.default = (opt.value == self.selected_player)
        await interaction.response.edit_message(view=self)

    async def mode_select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("❌ Bạn không có quyền thao tác trên menu này!", ephemeral=True)
            return
        self.selected_mode = self.mode_select.values[0]
        for opt in self.mode_select.options:
            if opt.value == self.selected_mode:
                opt.default = True
                self.mode_label = opt.label
            else:
                opt.default = False
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Áp Dụng Ngay", style=discord.ButtonStyle.success, emoji="⚡", row=2)
    async def apply_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("❌ Bạn không có quyền thực thi thao tác này!", ephemeral=True)
            return
        await interaction.response.defer()
        cmd = "gamemode " + self.selected_mode + " " + self.selected_player
        ok = await asyncio.to_thread(send_nvnmc_cmd, cmd)

        if ok:
            embed = discord.Embed(
                title="✨ ĐỔI CHẾ ĐỘ CHƠI THÀNH CÔNG",
                description=(
                    "───────────────────────────\n" +
                    "👤 **Người chơi:** `" + self.selected_player + "` (Online)\n" +
                    "🎮 **Chế độ mới:** **" + self.mode_label + "**\n" +
                    "💻 **Lệnh console:** `" + cmd + "`\n" +
                    "👑 **Thực hiện bởi:** " + interaction.user.mention + "\n" +
                    "───────────────────────────"
                ),
                color=0x2ecc71,
                timestamp=datetime.datetime.now()
            )
            embed.set_thumbnail(url="https://mc-heads.net/avatar/" + self.selected_player + "/100.png")
            embed.set_footer(text="KhangSMP Core Management System", icon_url=interaction.user.display_avatar.url)
            await interaction.edit_original_response(embed=embed, view=None)
        else:
            embed = discord.Embed(
                title="❌ THAO TÁC THẤT BẠI",
                description="Không thể gửi lệnh `" + cmd + "` tới máy chủ NVNMC. Vui lòng kiểm tra lại console.",
                color=0xe74c3c
            )
            await interaction.edit_original_response(embed=embed, view=None)
        self.stop()

    @discord.ui.button(label="Hủy Bỏ", style=discord.ButtonStyle.secondary, emoji="❌", row=2)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("❌ Bạn không có quyền hủy menu này!", ephemeral=True)
            return
        await interaction.response.edit_message(content="❌ Đã hủy thao tác đổi chế độ chơi.", embed=None, view=None)
        self.stop()

class EndLockView(discord.ui.View):
    def __init__(self, author):
        super().__init__(timeout=120)
        self.author = author

    @discord.ui.button(label="Khóa Cổng The End", style=discord.ButtonStyle.danger, emoji="🔒")
    async def lock_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("❌ Bạn không có quyền thao tác trên menu này!", ephemeral=True)
            return
        await interaction.response.defer()
        ok = await asyncio.to_thread(send_nvnmc_cmd, "endlock lock")
        if ok:
            await asyncio.sleep(0.5)
            cfg = await asyncio.to_thread(read_endlock_config)
            embed = make_endlock_embed(True, cfg.get("reason", "Maintenance"), interaction.user)
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.followup.send("❌ Gửi lệnh khóa thất bại!", ephemeral=True)

    @discord.ui.button(label="Mở Cổng The End", style=discord.ButtonStyle.success, emoji="🔓")
    async def unlock_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("❌ Bạn không có quyền thao tác trên menu này!", ephemeral=True)
            return
        await interaction.response.defer()
        ok = await asyncio.to_thread(send_nvnmc_cmd, "endlock unlock")
        if ok:
            await asyncio.sleep(0.5)
            cfg = await asyncio.to_thread(read_endlock_config)
            embed = make_endlock_embed(False, cfg.get("reason", "Mở tự do"), interaction.user)
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.followup.send("❌ Gửi lệnh mở thất bại!", ephemeral=True)

    @discord.ui.button(label="Làm Mới", style=discord.ButtonStyle.primary, emoji="🔄")
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("❌ Bạn không có quyền thao tác trên menu này!", ephemeral=True)
            return
        await interaction.response.defer()
        cfg = await asyncio.to_thread(read_endlock_config)
        embed = make_endlock_embed(cfg.get("locked", True), cfg.get("reason", "Maintenance"), interaction.user)
        await interaction.edit_original_response(embed=embed, view=self)

class SvStatusView(discord.ui.View):
    def __init__(self, author):
        super().__init__(timeout=120)
        self.author = author

    @discord.ui.button(label="Làm Mới Trạng Thái", style=discord.ButtonStyle.primary, emoji="🔄")
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("❌ Bạn không có quyền thao tác trên menu này!", ephemeral=True)
            return
        await interaction.response.defer()
        res = await asyncio.to_thread(get_nvnmc_resources)
        mc = await asyncio.to_thread(get_mc_status_data)
        embed = make_sv_status_embed(res, mc, interaction.user)
        await interaction.edit_original_response(embed=embed, view=self)

def make_endlock_embed(is_locked: bool, reason: str, user) -> discord.Embed:
    status_text = "🔒 ĐANG BỊ KHÓA CHẶT" if is_locked else "🔓 ĐÃ MỞ CỬA TỰ DO"
    color = 0xe74c3c if is_locked else 0x2ecc71
    desc = (
        "───────────────────────────\n" +
        "🌌 **Trạng thái:** **" + status_text + "**\n" +
        "📋 **Lý do kích hoạt:** `" + reason + "`\n" +
        "🛡️ **Cơ chế:** Chặn cổng The End, chặn lệnh `/tp`, mod và bug dịch chuyển.\n" +
        "👤 **Cập nhật bởi:** " + user.mention + "\n" +
        "───────────────────────────"
    )
    embed = discord.Embed(
        title="🌌 ĐIỀU KHIỂN CỔNG THẾ GIỚI THE END (ENDLOCK)",
        description=desc,
        color=color,
        timestamp=datetime.datetime.now()
    )
    embed.set_footer(text="KhangSMP Core Management System", icon_url=user.display_avatar.url)
    return embed

def make_sv_status_embed(res: dict, mc: dict, user) -> discord.Embed:
    state = res.get("state", "unknown")
    state_str = "🟢 Đang hoạt động (ONLINE)" if state == "running" else "🔴 " + str(state.upper())
    embed = discord.Embed(
        title="📊 BẢNG ĐIỀU KHIỂN HIỆU NĂNG MÁY CHỦ KHANGSMP",
        color=0x5865F2 if state == "running" else 0xe74c3c,
        timestamp=datetime.datetime.now()
    )
    embed.description = "───────────────────────────\nCập nhật thời gian thực từ panel NVNMC & Minecraft Core\n───────────────────────────"
    embed.add_field(name="⚙️ Trạng Thái", value="**" + state_str + "**", inline=True)
    embed.add_field(name="📶 Ping Server", value="`" + str(mc.get("latency_ms", 0)) + "ms`", inline=True)
    embed.add_field(name="👥 Người Chơi", value="**" + str(mc.get("players_online", 0)) + " / " + str(mc.get("players_max", 50)) + "**", inline=True)

    cpu_val = res.get("cpu", 0.0)
    ram_val = res.get("ram_mb", 0.0)
    disk_val = res.get("disk_mb", 0.0)
    uptime_val = res.get("uptime_h", 0.0)

    embed.add_field(name="💻 CPU Sử Dụng", value="`" + ("%.1f" % cpu_val) + "%`", inline=True)
    embed.add_field(name="💾 RAM Chiếm Dụng", value="`" + ("%.1f" % ram_val) + " MB`", inline=True)
    embed.add_field(name="💽 Bộ Nhớ Ổ Đĩa", value="`" + ("%.1f" % disk_val) + " MB`", inline=True)
    embed.add_field(name="⏱️ Thời Gian Chạy", value="`" + ("%.1f" % uptime_val) + " giờ`", inline=True)
    embed.add_field(name="🌐 Địa Chỉ Kết Nối", value="`" + MC_SERVER_HOST + ":" + str(MC_SERVER_PORT) + "`", inline=True)
    embed.add_field(name="📌 Phiên Bản", value="`Paper 1.21.x`", inline=True)

    embed.set_footer(text="Yêu cầu bởi " + user.name + " • KhangSMP Core", icon_url=user.display_avatar.url)
    return embed

@staff_bot.event
async def on_ready():
    logger.info("[StaffBot] Logged in as " + str(staff_bot.user) + " (ID: " + str(staff_bot.user.id) + ")")
    try:
        synced = await staff_bot.tree.sync()
        logger.info("[StaffBot] Synced " + str(len(synced)) + " slash commands.")
    except Exception as e:
        logger.error("[StaffBot] Error syncing slash commands: " + str(e))
    await staff_bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="KhangSMP Admin Core 🛡️"))

@staff_bot.tree.command(name="ping", description="🏓 Kiểm tra độ trễ phản hồi của bot")
async def slash_ping(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    latency = round(staff_bot.latency * 1000)
    embed = discord.Embed(
        title="🏓 PONG! ĐỘ TRỄ KẾT NỐI",
        description="Độ trễ Gateway Discord: **`" + str(latency) + "ms`**\nTrạng thái: 🟢 Hoạt động mượt mà",
        color=0x2ecc71,
        timestamp=datetime.datetime.now()
    )
    embed.set_footer(text="KhangSMP Admin Core", icon_url=interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed)

@staff_bot.tree.command(name="sv_status", description="📊 Xem thông số hiệu năng và người chơi máy chủ Minecraft")
async def slash_sv_status(interaction: discord.Interaction):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message("❌ Bạn không có quyền quản trị!", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    res = await asyncio.to_thread(get_nvnmc_resources)
    mc = await asyncio.to_thread(get_mc_status_data)
    embed = make_sv_status_embed(res, mc, interaction.user)
    view = SvStatusView(interaction.user)
    await interaction.followup.send(embed=embed, view=view)

@staff_bot.tree.command(name="list_players", description="👥 Liệt kê chi tiết người chơi đang online trên server")
async def slash_list_players(interaction: discord.Interaction):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message("❌ Bạn không có quyền quản trị!", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    mc = await asyncio.to_thread(get_mc_status_data)
    online_count = mc.get("players_online", 0)
    players = mc.get("player_list", [])
    embed = discord.Embed(
        title="👥 DANH SÁCH NGƯỜI CHƠI ĐANG ONLINE",
        color=0x3498db,
        timestamp=datetime.datetime.now()
    )
    embed.description = "───────────────────────────\nHiện đang có **" + str(online_count) + " / " + str(mc.get("players_max", 50)) + "** người chơi trực tuyến.\n───────────────────────────"
    if players:
        embed.add_field(name="🎮 Danh Sách:", value="\n".join(["• `" + p + "`" for p in players]), inline=False)
    else:
        embed.add_field(name="🎮 Danh Sách:", value="*Hiện tại không có ai online.*", inline=False)
    embed.set_footer(text="KhangSMP Core", icon_url=interaction.user.display_avatar.url)
    await interaction.followup.send(embed=embed)

@staff_bot.tree.command(name="gamemode", description="🎮 Menu chọn người chơi đang online và đổi chế độ chơi")
async def slash_gamemode(interaction: discord.Interaction):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message("❌ Bạn không có quyền quản trị!", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    online_players = await asyncio.to_thread(get_online_mc_players)
    if not online_players:
        embed = discord.Embed(
            title="⚠️ KHÔNG CÓ NGƯỜI CHƠI ONLINE TRÊN SERVER MINECRAFT",
            description=(
                "───────────────────────────\n" +
                "Hiện tại **không có ai đang ở trong server Minecraft** để hiển thị danh sách lựa chọn!\n\n" +
                "• Người chơi cần phải đăng nhập vào game trước.\n" +
                "• Hoặc nếu anh muốn gõ đổi chế độ ngay lập tức, dùng lệnh nhanh:\n" +
                "  👉 `gm <mode> <tên_player>`\n" +
                "  *(Ví dụ: `gm creative PE_KhangKYT` hoặc `gm survival phb.duong`)*\n" +
                "───────────────────────────"
            ),
            color=0xe67e22,
            timestamp=datetime.datetime.now()
        )
        embed.set_footer(text="KhangSMP Admin Core", icon_url=interaction.user.display_avatar.url)
        await interaction.followup.send(embed=embed)
        return

    embed = discord.Embed(
        title="🎮 BẢNG ĐIỀU KHIỂN CHẾ ĐỘ CHƠI (GAMEMODE)",
        description=(
            "───────────────────────────\n" +
            "Đã quét thấy **" + str(len(online_players)) + "** người chơi đang online trong game:\n" +
            "• **Bước 1:** Chọn người chơi đang online ở Menu 1.\n" +
            "• **Bước 2:** Chọn chế độ chơi (Survival, Creative, Spectator, Adventure).\n" +
            "• **Bước 3:** Nhấn nút **⚡ Áp Dụng Ngay**!\n" +
            "───────────────────────────"
        ),
        color=0x9b59b6,
        timestamp=datetime.datetime.now()
    )
    embed.set_footer(text="KhangSMP Admin Core", icon_url=interaction.user.display_avatar.url)
    view = GamemodeView(interaction.user, online_players)
    await interaction.followup.send(embed=embed, view=view)

@staff_bot.tree.command(name="endlock", description="🌌 Khóa, Mở hoặc Xem trạng thái cổng The End")
async def slash_endlock(interaction: discord.Interaction):
    if not is_authorized(interaction.user.id):
        await interaction.response.send_message("❌ Bạn không có quyền quản trị!", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    cfg = await asyncio.to_thread(read_endlock_config)
    is_locked = cfg.get("locked", True)
    reason = cfg.get("reason", "Bảo trì thế giới")
    embed = make_endlock_embed(is_locked, reason, interaction.user)
    view = EndLockView(interaction.user)
    await interaction.followup.send(embed=embed, view=view)

@staff_bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.author == staff_bot.user:
        return
    if not is_authorized(message.author.id):
        return
    content = message.content.strip().lower()

    if content in ("ping", "!ping"):
        async with message.channel.typing():
            latency = round(staff_bot.latency * 1000)
            embed = discord.Embed(
                title="🏓 PONG!",
                description="Độ trễ bot: **`" + str(latency) + "ms`**",
                color=0x2ecc71
            )
            await message.reply(embed=embed)
        return

    if content in ("sv status", "!sv status", "!status"):
        async with message.channel.typing():
            res = await asyncio.to_thread(get_nvnmc_resources)
            mc = await asyncio.to_thread(get_mc_status_data)
            embed = make_sv_status_embed(res, mc, message.author)
            view = SvStatusView(message.author)
            await message.reply(embed=embed, view=view)
        return

    if content in ("list player", "!list player", "list", "!list", "!players"):
        async with message.channel.typing():
            mc = await asyncio.to_thread(get_mc_status_data)
            online = mc.get("players_online", 0)
            pl = mc.get("player_list", [])
            embed = discord.Embed(
                title="👥 NGƯỜI CHƠI TRỰC TUYẾN (" + str(online) + "/" + str(mc.get("players_max", 50)) + ")",
                color=0x3498db
            )
            embed.description = "\n".join(["• `" + p + "`" for p in pl]) if pl else "*Hiện tại không có ai online.*"
            await message.reply(embed=embed)
        return

    if content in ("gamemode", "!gamemode", "gm", "!gm"):
        async with message.channel.typing():
            online_players = await asyncio.to_thread(get_online_mc_players)
            if not online_players:
                embed = discord.Embed(
                    title="⚠️ KHÔNG CÓ NGƯỜI CHƠI ONLINE TRÊN SERVER MINECRAFT",
                    description=(
                        "───────────────────────────\n" +
                        "Hiện tại **không có ai đang ở trong server Minecraft** để hiển thị danh sách lựa chọn!\n\n" +
                        "• Người chơi cần phải đăng nhập vào game trước.\n" +
                        "• Hoặc nếu anh muốn gõ đổi chế độ ngay lập tức, dùng lệnh nhanh:\n" +
                        "  👉 `gm <mode> <tên_player>`\n" +
                        "  *(Ví dụ: `gm creative PE_KhangKYT` hoặc `gm survival phb.duong`)*\n" +
                        "───────────────────────────"
                    ),
                    color=0xe67e22,
                    timestamp=datetime.datetime.now()
                )
                embed.set_footer(text="KhangSMP Admin Core", icon_url=message.author.display_avatar.url)
                await message.reply(embed=embed)
                return

            embed = discord.Embed(
                title="🎮 BẢNG ĐIỀU KHIỂN CHẾ ĐỘ CHƠI (GAMEMODE)",
                description=(
                    "───────────────────────────\n" +
                    "Đã quét thấy **" + str(len(online_players)) + "** người chơi đang online trong game:\n" +
                    "• **Bước 1:** Chọn người chơi đang online ở Menu 1.\n" +
                    "• **Bước 2:** Chọn chế độ chơi (Survival, Creative, Spectator, Adventure).\n" +
                    "• **Bước 3:** Nhấn nút **⚡ Áp Dụng Ngay**!\n" +
                    "───────────────────────────"
                ),
                color=0x9b59b6,
                timestamp=datetime.datetime.now()
            )
            embed.set_footer(text="KhangSMP Admin Core", icon_url=message.author.display_avatar.url)
            view = GamemodeView(message.author, online_players)
            await message.reply(embed=embed, view=view)
        return

    if content.startswith("gamemode ") or content.startswith("!gamemode ") or content.startswith("gm ") or content.startswith("!gm "):
        p = message.content.strip().split()
        if len(p) >= 3:
            async with message.channel.typing():
                mode, pl = p[1], p[2]
                cmd = "gamemode " + mode + " " + pl
                ok = await asyncio.to_thread(send_nvnmc_cmd, cmd)
                if ok:
                    embed = discord.Embed(
                        title="✨ ĐỔI CHẾ ĐỘ THÀNH CÔNG",
                        description="Đã chuyển chế độ của **`" + pl + "`** sang **`" + mode + "`**!\nLệnh console: `" + cmd + "`",
                        color=0x2ecc71
                    )
                    embed.set_thumbnail(url="https://mc-heads.net/avatar/" + pl + "/100.png")
                    await message.reply(embed=embed)
                else:
                    await message.reply("❌ Không thể thực thi lệnh `" + cmd + "`!")
        return

    if content in ("endlock", "!endlock", "end", "!end"):
        async with message.channel.typing():
            cfg = await asyncio.to_thread(read_endlock_config)
            is_locked = cfg.get("locked", True)
            reason = cfg.get("reason", "Bảo trì thế giới")
            embed = make_endlock_embed(is_locked, reason, message.author)
            view = EndLockView(message.author)
            await message.reply(embed=embed, view=view)
        return

    if content.startswith("endlock ") or content.startswith("!endlock ") or content.startswith("end ") or content.startswith("!end "):
        p = message.content.strip().split()
        if len(p) >= 2:
            async with message.channel.typing():
                act = p[1]
                if act in ("lock", "unlock"):
                    cmd = "endlock " + act
                    ok = await asyncio.to_thread(send_nvnmc_cmd, cmd)
                    if ok:
                        await asyncio.sleep(0.5)
                        cfg = await asyncio.to_thread(read_endlock_config)
                        embed = make_endlock_embed((act == "lock"), cfg.get("reason", "Bảo trì"), message.author)
                        view = EndLockView(message.author)
                        await message.reply(embed=embed, view=view)
                    else:
                        await message.reply("❌ Không thể gửi lệnh `" + cmd + "` tới máy chủ!")
                else:
                    cfg = await asyncio.to_thread(read_endlock_config)
                    embed = make_endlock_embed(cfg.get("locked", True), cfg.get("reason", "Bảo trì"), message.author)
                    view = EndLockView(message.author)
                    await message.reply(embed=embed, view=view)
        return

    await staff_bot.process_commands(message)
