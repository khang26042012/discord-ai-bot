"""
==============================================
 MUSIC MODULE - Chuot dethw bot
==============================================
Tinh nang:
  - /play <ten bai | URL>  : tim kiem thong minh (menu chon Top 5) hoac phat truc tiep tu URL
  - /pause /resume /skip /queue /nowplaying /volume /loop (off|track|queue)
  - /radio <kenh>          : radio stream 24/7 khong quang cao, hop phap 100%
  - Playlist luu MongoDB: /taoplaylist /themvao /phatplaylist /xoaplaylist /danhsachplaylist
  - TU DONG NGAT KET NOI:
      + Moi nguoi rut khoi room voice -> sau 60 giay bot tu stop + roi di (tiet kiem RAM)
      + Nhac phat xong ma khong co gi trong queue -> sau 5 phut tu roi di
Phan quyen:
  - Member thuong dung duoc TAT CA lenh nghe nhac.
  - Lenh NGUY HIEM (/stop, /disconnect) chi danh cho Manage Messages / Manage Guild / Admin.
Nguon nhac: YouTube/SoundCloud qua yt-dlp + radio SomaFM/Nightride (khong QC).
"""

import os
import time
import base64
import tempfile
import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Dict, Deque, List, Any

import discord
from discord.ext import commands
import discord.app_commands as app_commands

logger = logging.getLogger("DiscordBot")

try:
    import yt_dlp
except ImportError:
    yt_dlp = None
    logger.warning("yt-dlp chua duoc cai - tinh nang nhac se khong hoat dong!")

# ================= CONFIG =================

YTDL_OPTS_BASE: Dict[str, Any] = {
    "format": "bestaudio/best",
    "noplaylist": True,          # URL playlist -> chi lay 1 video
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "socket_timeout": 15,
    "retries": 3,
    "source_address": "0.0.0.0",
}
# Chong YouTube chan "confirm you're not a bot":
# thu lan luot cac bo player_client den khi nao thanh cong
YT_CLIENT_FALLBACKS = [
    ["tv", "ios"],                    # thuong khong can PO token
    ["tv_simply"],                    # client moi it bi chan
    ["tv_embedded", "android_vr"],    # phuong an 2
    ["android", "web"],               # cuoi cung
]
_cookie_file = os.getenv("YT_COOKIES_FILE")
if _cookie_file and os.path.exists(_cookie_file):
    YTDL_OPTS_BASE["cookiefile"] = _cookie_file

# Chi ro duong dan bgutil provider cho plugin (neu co)
_BGUTIL_HOME = os.path.join(os.path.expanduser("~"), "bgutil-ytdlp-pot-provider", "server")
if os.path.exists(os.path.join(_BGUTIL_HOME, "build", "generate_once.js")):
    YTDL_OPTS_BASE["extractor_args"] = {
        "youtube": {},
        "youtubepot-bgutilscript": {"server_home": [_BGUTIL_HOME]},
    }

# Bo sinh PO token bgutil (script mode) - khien link googlevideo khong bi 403.
# Plugin tu nhan dien khi repo nam tai ~/bgutil-ytdlp-pot-provider (mac dinh cua no).
_BGUTIL_SCRIPT = os.path.join(
    os.path.expanduser("~"), "bgutil-ytdlp-pot-provider", "server", "build", "generate_once.js")
if os.path.exists(_BGUTIL_SCRIPT):
    logger.info("✅ bgutil PO token provider sẵn sàng (script mode)")
else:
    logger.info("Không thấy bgutil script - phát nhạc không có PO token (có thể bị 403)")

# Cookies truyen qua env var (base64) - an toan tren Railway, khong luu file trong git
_cookie_b64 = os.getenv("YT_COOKIES_B64")
if _cookie_b64:
    try:
        _cookie_path = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
        with open(_cookie_path, "wb") as f:
            f.write(base64.b64decode(_cookie_b64))
        YTDL_OPTS_BASE["cookiefile"] = _cookie_path
        logger.info("✅ Đã nạp YouTube cookies từ biến môi trường YT_COOKIES_B64")
    except Exception as e:
        logger.warning(f"Không giải mã được YT_COOKIES_B64: {e}")

FFMPEG_OPTS = {
    # Tu noi lai stream khi mang chap chon
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin",
    "options": "-vn",
}

DEFAULT_VOLUME = 0.5          # 50%
ALONE_TIMEOUT_SEC = 60        # YEU CAU: moi nguoi out het -> 1 phut -> tu stop
IDLE_TIMEOUT_SEC = 300        # Queue trong sau khi het bai -> 5 phut -> roi phong
WATCHER_INTERVAL_SEC = 15
SEARCH_RESULTS = 5

# Radio stream TRUC TIEP - hop phap, khong quang cao, phat mai mai
RADIO_STATIONS = {
    "lofi":      {"name": "Lofi Classic (SomaFM)",   "url": "https://ice1.somafm.com/gsclassic-128-mp3"},
    "chill":     {"name": "Groove Salad (SomaFM)",   "url": "https://ice1.somafm.com/groovesalad-128-mp3"},
    "beat":      {"name": "Beat Blender (SomaFM)",   "url": "https://ice1.somafm.com/beatblender-128-mp3"},
    "space":     {"name": "Deep Space One (SomaFM)", "url": "https://ice1.somafm.com/deepspaceone-128-mp3"},
    "nightride": {"name": "Nightride FM",            "url": "https://stream.nightride.fm/nightride.mp3"},
}

URL_PREFIXES = ("http://", "https://")

LOOP_CHOICES = [
    app_commands.Choice(name="Tat lap", value="off"),
    app_commands.Choice(name="Lap lai 1 bai", value="track"),
    app_commands.Choice(name="Lap ca hang doi", value="queue"),
]
RADIO_CHANNEL_CHOICES = [app_commands.Choice(name=v["name"], value=k) for k, v in RADIO_STATIONS.items()]

MUSIC_DB = None          # MongoDB database, gan qua bind_db()
_bot: Optional[commands.Bot] = None


def bind_db(database):
    """Gan MongoDB database (goi tu bot.py sau init_mongodb)."""
    global MUSIC_DB
    MUSIC_DB = database


# ================= DATA MODELS =================

@dataclass
class Track:
    title: str
    webpage_url: str                 # link goc de hien thi / luu playlist
    requester_id: int
    stream_url: Optional[str] = None # URL am thanh truc tiep (resolve luc phat neu None)
    duration: Optional[int] = None   # giay; None voi radio/stream
    uploader: str = ""
    thumbnail: Optional[str] = None
    is_stream: bool = False          # radio/live -> khong co thoi luong


@dataclass
class GuildPlayer:
    queue: Deque[Track] = field(default_factory=deque)
    current: Optional[Track] = None
    loop_mode: str = "off"           # off | track | queue
    text_channel_id: Optional[int] = None
    volume: float = DEFAULT_VOLUME
    idle_since: Optional[float] = None
    alone_since: Optional[float] = None


players: Dict[int, GuildPlayer] = {}


def _get_player(guild_id: int) -> GuildPlayer:
    if guild_id not in players:
        players[guild_id] = GuildPlayer()
    return players[guild_id]


def _fmt_duration(seconds) -> str:
    if seconds is None:
        return "LIVE"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _can_moderate(interaction: discord.Interaction) -> bool:
    perms = interaction.user.guild_permissions
    return perms.manage_messages or perms.manage_guild or perms.administrator


# ================= YT-DLP HELPERS =================

def _sync_extract(query: str, search: bool = False, clients=None, use_cookies: bool = True,
                  engine: str = "yt"):
    """Chay blocking yt-dlp trong executor (client + cookies tuy chon tung lan thu)."""
    opts = dict(YTDL_OPTS_BASE)
    if not use_cookies:
        opts.pop("cookiefile", None)   # bo cookies cho client noman
    if search:
        opts["default_search"] = f"{engine}search{SEARCH_RESULTS}"
    if clients:
        ea = dict(opts.get("extractor_args") or {})
        ea["youtube"] = {"player_client": clients}
        opts["extractor_args"] = ea
    ydl = yt_dlp.YoutubeDL(opts)
    return ydl.extract_info(query, download=False)


def _is_retryable(err_text: str) -> bool:
    """Loi nen thu client khac (bot-check, stale page, het format...)."""
    t = err_text.lower()
    return ("sign in to confirm" in t or "not a bot" in t
            or ("cookies" in t and "youtube" in t)
            or "page needs to be reloaded" in t
            or "requested format is not available" in t
            or "no formats" in t
            or "request got redirected" in t)


def _is_bot_check(err_text: str) -> bool:
    """Giu ten cu de tuong thich noi dung bao loi."""
    return _is_retryable(err_text)


def _client_attempts():
    """
    Tra ve danh sach (player_clients, dung_cookies_khong).
    - tv/ios: KHONG can PO token nhung XUNG DOT voi cookies (loi 'reload page')
      -> chet che do noman danh.
    - android/web: can cookies de qua bot-check nhung co the bi loc format (PO token).
    Ket hop ca hai kieu de max kha nang thanh cong.
    """
    if "cookiefile" in YTDL_OPTS_BASE:
        return [
            (["tv", "ios"], False),                    # noman: khong xung dot cookies
            (["android", "web"], True),                # co cookies: dang nhap that
            (["tv_embedded", "android_vr"], False),
        ]
    return [(c, True) for c in YT_CLIENT_FALLBACKS]


async def _extract_with_fallback(query: str, search: bool = False):
    """
    Extract voi chuoi player_client du phong (co/ca cookies).
    Gap bot-check / stale page / het format -> doi cach truy cap den khi thanh cong.
    """
    last_err: Optional[Exception] = None
    attempts = _client_attempts()
    for i, (clients, use_cookies) in enumerate(attempts):
        try:
            # chay trong executor de khong block event loop
            data = await asyncio.get_event_loop().run_in_executor(
                None, lambda q=query, c=clients, u=use_cookies, s=search: _sync_extract(q, s, c, u))
            if data is not None and (not search or data.get("entries")):
                if i > 0:
                    logger.info(f"[Music] Cach truy cap #{i + 1} ({clients}, cookies={use_cookies}) hoat dong")
                return data
            last_err = last_err or RuntimeError("Khong co ket qua")
        except Exception as e:
            last_err = e
            if _is_retryable(str(e)):
                logger.warning(f"[Music] Client {clients} (cookies={use_cookies}) bi tu choi ({str(e)[:60]}...) -> thu cach tiep theo...")
                continue
            raise  # loi khac (URL sai...) -> bao ngay
    raise last_err


async def _resolve_search(query: str):
    """Tra ve toi da 5 ket qua tho cho o tim kiem."""
    data = await _extract_with_fallback(query, search=True)
    if data is None:
        return []
    entries = data.get("entries") or []
    return [e for e in entries if e][:SEARCH_RESULTS]


def _sync_extract_sc(query: str):
    """Tim kiem tren SoundCloud (on dinh, khong PO token)."""
    opts = dict(YTDL_OPTS_BASE)
    opts["default_search"] = f"scsearch{SEARCH_RESULTS}"
    ydl = yt_dlp.YoutubeDL(opts)
    return ydl.extract_info(f"scsearch{SEARCH_RESULTS}:{query}", download=False)


async def _resolve_search_sc(query: str):
    """Tim kiem SoundCloud - phuong an du phong khi YouTube khong cooperate."""
    data = await asyncio.get_event_loop().run_in_executor(
        None, lambda: _sync_extract_sc(query))
    if data is None:
        return []
    entries = data.get("entries") or []
    return [e for e in entries if e][:SEARCH_RESULTS]


def _entry_to_track(entry: dict, requester_id: int) -> Track:
    is_live = bool(entry.get("is_live"))
    thumb = entry.get("thumbnail")
    if not thumb and entry.get("thumbnails"):
        thumb = entry["thumbnails"][-1].get("url")
    return Track(
        title=entry.get("title") or "Khong ro tieu de",
        webpage_url=entry.get("webpage_url") or entry.get("url") or "",
        requester_id=requester_id,
        stream_url=None,   # resolve luc phat de nhe RAM
        duration=None if is_live else entry.get("duration"),
        uploader=entry.get("uploader") or entry.get("channel") or "",
        thumbnail=thumb,
        is_stream=is_live,
    )


async def _ensure_stream_url(track: Track):
    """Resolve URL am thanh truc tiep neu chua co (luoi hoa de nhanh & nhe RAM)."""
    if track.stream_url or track.is_stream or yt_dlp is None:
        return
    data = await _extract_with_fallback(track.webpage_url)
    if data:
        track.stream_url = data.get("url")
        if not track.duration:
            track.duration = data.get("duration")


# ================= PLAYBACK ENGINE =================

def _cleanup_guild(guild_id: int, disconnect: bool = True):
    gp = players.pop(guild_id, None)
    if not _bot or not disconnect:
        return
    guild = _bot.get_guild(guild_id)
    vc = guild.voice_client if guild else None
    if vc and vc.is_connected():
        asyncio.ensure_future(_safe_disconnect(vc))


async def _safe_disconnect(vc: discord.VoiceClient):
    try:
        if vc.is_connected():
            await vc.disconnect(force=True)
    except Exception as e:
        logger.warning(f"[Music] Loi khi ngat ket noi voice: {e}")


async def _send_to_text_channel(guild: discord.Guild, content: str = None, embed: discord.Embed = None):
    gp = players.get(guild.id)
    if gp and gp.text_channel_id:
        ch = guild.get_channel(gp.text_channel_id)
        if ch:
            try:
                await ch.send(content=content, embed=embed)
            except discord.HTTPException:
                pass


async def _play_next(guild: discord.Guild, prior_error: Optional[str] = None):
    """Ham trung tam: lay bai tiep theo trong queue va phat."""
    gp = players.get(guild.id)
    if not gp or not _bot:
        return
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        _cleanup_guild(guild.id)
        return

    if prior_error and gp.current:
        logger.error(f"[Music] Loi phat '{gp.current.title}': {prior_error}")
        await _send_to_text_channel(guild, f"⚠️ Loi phat bài hiện tại — bỏ qua.")

    finished = gp.current

    # --- Xu ly LOOP ---
    if finished is not None:
        if gp.loop_mode == "track":
            gp.queue.appendleft(finished)          # phat lai chinh no
        elif gp.loop_mode == "queue":
            gp.queue.append(finished)              # xep lai cuoi hang doi

    if not gp.queue:
        gp.current = None
        gp.idle_since = time.time()
        await _send_to_text_channel(guild, "🎵 Hết hàng đợi! Dùng </play> để thêm bài.")
        return

    track = gp.queue.popleft()
    gp.current = track
    gp.idle_since = None

    try:
        await _ensure_stream_url(track)
        if not track.stream_url:
            raise RuntimeError("Khong lay duoc stream URL")
        audio_src = discord.FFmpegPCMAudio(track.stream_url, **FFMPEG_OPTS)
        played = discord.PCMVolumeTransformer(audio_src, volume=gp.volume)
    except Exception as e:
        logger.error(f"[Music] Khong phat duoc '{track.title}': {e}")
        await _send_to_text_channel(guild, f"❌ Không phát được bài này, bỏ qua...")
        gp.current = None
        return await _play_next(guild)   # thu bai ke tiep

    def _after(err):
        fut = asyncio.run_coroutine_threadsafe(_play_next(guild, err), _bot.loop)
        try:
            fut.result(timeout=30)
        except Exception as e:
            logger.error(f"[Music] after-callback loi: {e}")

    vc.play(played, after=_after)

    embed = discord.Embed(
        title="🎧 Đang phát",
        description=f"**[{track.title}]({track.webpage_url})**",
        color=discord.Color.green(),
    )
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    req = guild.get_member(track.requester_id)
    embed.add_field(name="Người yêu cầu", value=req.mention if req else "Playlist", inline=True)
    embed.add_field(name="Thời lượng", value=_fmt_duration(track.duration), inline=True)
    if gp.loop_mode == "track":
        embed.set_footer(text="🔁 Loop 1 bài đang BẬT")
    await _send_to_text_channel(guild, embed=embed)


async def _join_channel(interaction: discord.Interaction) -> discord.VoiceClient:
    """Dua bot vao room voice cua nguoi goi lenh (hoac chuyen room)."""
    if not interaction.user.voice or not interaction.user.voice.channel:
        raise RuntimeError("Bạn phải vào một room voice trước đã! 🎤")
    channel = interaction.user.voice.channel
    vc = interaction.guild.voice_client
    if vc and vc.is_connected():
        if vc.channel != channel:
            await vc.move_to(channel)
        return vc
    return await channel.connect(self_deaf=True)


# ================= AUTO-DISCONNECT WATCHER =================

async def _voice_watcher():
    """
    Quet dinh ky:
      1. Room trong nguoi (chi con bot) qua 60s -> stop + roi (yeu cau cua sep).
      2. Khong phat gi + queue trong qua 5 phut -> roi de giai phong RAM.
    """
    await _bot.wait_until_ready()
    while not _bot.is_closed():
        try:
            for guild_id in list(players.keys()):
                gp = players.get(guild_id)
                if not gp:
                    continue
                guild = _bot.get_guild(guild_id)
                vc = guild.voice_client if guild else None
                if not vc or not vc.is_connected():
                    continue

                humans = [m for m in vc.channel.members if not m.bot]
                now = time.time()

                if not humans:
                    if gp.alone_since is None:
                        gp.alone_since = now
                    elif now - gp.alone_since >= ALONE_TIMEOUT_SEC:
                        logger.info(f"[Music] Room trong {ALONE_TIMEOUT_SEC}s tai guild {guild_id} -> tu ngat")
                        await _send_to_text_channel(
                            guild, "👋 Mọi người đã rời room hơn 1 phút, chuột tự tắt nhạc để tiết kiệm tài nguyên!")
                        _cleanup_guild(guild_id)
                        continue
                else:
                    gp.alone_since = None

                # Idle: khong phat, khong queue -> giai phong RAM
                playing_now = vc.is_playing() or vc.is_paused()
                if not playing_now and not gp.queue:
                    if gp.idle_since is None:
                        gp.idle_since = now
                    elif now - gp.idle_since >= IDLE_TIMEOUT_SEC:
                        logger.info(f"[Music] Idle {IDLE_TIMEOUT_SEC}s tai guild {guild_id} -> tu roi")
                        await _send_to_text_channel(guild, "💤 Không có nhạc nào phát trong 5 phút, chuột đi ngủ đây!")
                        _cleanup_guild(guild_id)
                else:
                    gp.idle_since = None
        except Exception as e:
            logger.error(f"[Music] Watcher loi: {e}")
        await asyncio.sleep(WATCHER_INTERVAL_SEC)


_started = False

def ensure_started():
    """Khoi dong watcher dung mot lan (goi tu on_ready)."""
    global _started
    if _started or _bot is None:
        return
    _bot.loop.create_task(_voice_watcher())
    _started = True
    logger.info("✅ Music watcher đã khởi động (auto-stop 60s khi room trống)")


# ================= SEARCH SELECT MENU =================

class TrackSelectView(discord.ui.View):
    """Menu chon bai tu ket qua tim kiem - de nhin, de chon."""

    def __init__(self, invoker_id: int, choices: List[Track]):
        super().__init__(timeout=90)
        self.invoker_id = invoker_id
        self.choices = choices
        self.chosen: Optional[Track] = None
        self.message: Optional[discord.Message] = None
        options = []
        for i, t in enumerate(choices):
            options.append(discord.SelectOption(
                label=f"{i + 1}. {t.title[:95]}",
                description=f"{t.uploader[:40]} • {_fmt_duration(t.duration)}"[:100],
                value=str(i),
            ))
        select = discord.ui.Select(placeholder="🎶 Chọn bài muốn phát...", options=options)

        async def _pick(sel_interaction: discord.Interaction):
            if sel_interaction.user.id != self.invoker_id:
                await sel_interaction.response.send_message("🚫 Đây không phải yêu cầu của bạn!", ephemeral=True)
                return
            self.chosen = self.choices[int(sel_interaction.data["values"][0])]
            self.stop()
            for item in self.children:
                item.disabled = True
            await sel_interaction.response.edit_message(view=self)

        select.callback = _pick
        self.add_item(select)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(content="⏰ Hết giờ chọn bài.", view=self)
            except discord.HTTPException:
                pass


# ================= SETUP (dang ky lenh) =================

def setup(bot: commands.Bot):
    global _bot
    _bot = bot

    # ---------------- /play ----------------
    @bot.tree.command(name="play", description="🎤 Phát nhạc từ tên bài hoặc URL (YouTube/SoundCloud)")
    async def play(interaction: discord.Interaction, query: str):
        if yt_dlp is None:
            return await interaction.response.send_message("❌ Máy chủ chưa cài yt-dlp!", ephemeral=True)
        await interaction.response.defer()
        try:
            vc = await _join_channel(interaction)
        except RuntimeError as e:
            return await interaction.followup.send(str(e))

        gp = _get_player(interaction.guild_id)
        gp.text_channel_id = interaction.channel_id

        is_url = any(query.startswith(p) for p in URL_PREFIXES)
        try:
            if is_url:
                data = await _extract_with_fallback(query)
                if not data:
                    return await interaction.followup.send("❌ Không đọc được URL này.")
                track = _entry_to_track(data, interaction.user.id)
                gp.queue.append(track)
                if not vc.is_playing() and not vc.is_paused():
                    await _play_next(interaction.guild)
                else:
                    await interaction.followup.send(
                        embed=discord.Embed(
                            title="✅ Đã thêm vào hàng đợi",
                            description=f"**[{track.title}]({track.webpage_url})** • {_fmt_duration(track.duration)}",
                            color=discord.Color.blue()))
            else:
                await interaction.followup.send(f"🔍 Đang tìm `{query}` trên YouTube...")
                try:
                    raw = await _resolve_search(query)
                    source_name = "YouTube"
                except Exception as yt_err:
                    # YouTube khó tính -> chuyển qua SoundCloud tự động
                    logger.warning(f"[Music] YouTube search fail ({str(yt_err)[:80]}...) -> fallback SoundCloud")
                    await interaction.followup.send(
                        "😕 YouTube đang khó tính với server... thử tìm trên **SoundCloud** nhé!")
                    raw = await _resolve_search_sc(query)
                    source_name = "SoundCloud"
                if not raw:
                    return await interaction.followup.send(
                        f"😢 Không tìm thấy kết quả nào cho `{query}` (đã thử cả YouTube & SoundCloud).")
                choices = [_entry_to_track(e, interaction.user.id) for e in raw]

                lines = "\n".join(
                    f"**{i + 1}.** [{t.title[:70]}]({t.webpage_url})\n"
                    f"↳ {t.uploader[:50]} • {_fmt_duration(t.duration)}"
                    for i, t in enumerate(choices))
                embed = discord.Embed(
                    title=f"🔎 Kết quả tìm kiếm ({source_name})",
                    description=lines,
                    color=discord.Color.gold())
                embed.set_footer(text="Chọn bài bên dưới ⬇️ hoặc đợi 90s để huỷ")

                view = TrackSelectView(interaction.user.id, choices)
                msg = await interaction.followup.send(embed=embed, view=view)
                view.message = msg
                await view.wait()
                if view.chosen:
                    gp.queue.append(view.chosen)
                    if not vc.is_playing() and not vc.is_paused():
                        await _play_next(interaction.guild)
                    else:
                        pos = len(gp.queue)
                        await interaction.followup.send(
                            f"✅ Đã thêm **{view.chosen.title[:60]}** vào hàng đợi (vị trí #{pos}).")
        except Exception as e:
            logger.error(f"[Music] /play loi: {e}")
            if _is_bot_check(str(e)):
                await interaction.followup.send(
                    "🤖 YouTube đang chặn bot-check từ server! Admin cần cung cấp cookies "
                    "(env YT_COOKIES_FILE) để vượt qua. Thử `/radio` trong lúc chờ nhé!")
            else:
                await interaction.followup.send(f"❌ Lỗi khi xử lý yêu cầu: `{str(e)[:150]}`")

    # ---------------- Dieu khien co ban ----------------
    @bot.tree.command(name="pause", description="⏸️ Tạm dừng bản nhạc hiện tại")
    async def pause(interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸️ Đã tạm dừng.")
        else:
            await interaction.response.send_message("🤔 Không có gì đang phát.", ephemeral=True)

    @bot.tree.command(name="resume", description="▶️ Tiếp tục phát nhạc")
    async def resume(interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ Tiếp tục phát!")
        else:
            await interaction.response.send_message("🤔 Nhạc không bị tạm dừng.", ephemeral=True)

    @bot.tree.command(name="skip", description="⏭️ Bỏ qua bài hiện tại")
    async def skip(interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()  # after-callback -> _play_next
            await interaction.response.send_message("⏭️ Đã bỏ qua!")
        else:
            await interaction.response.send_message("🤔 Không có gì để bỏ qua.", ephemeral=True)

    # ---------------- /stop (NGUY HIEM) ----------------
    @bot.tree.command(name="stop", description="🛑 [NGUY HIỂM] Dừng nhạc + xoá toàn bộ hàng đợi")
    @app_commands.default_permissions(manage_messages=True)
    async def stop(interaction: discord.Interaction):
        if not _can_moderate(interaction):
            return await interaction.response.send_message(
                "🚫 Lệnh này nguy hiểm, chỉ dành cho quản lý server!", ephemeral=True)
        gp = players.get(interaction.guild_id)
        if gp:
            gp.queue.clear()
            gp.loop_mode = "off"
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        if vc and vc.is_connected():
            _cleanup_guild(interaction.guild_id)
        await interaction.response.send_message("🛑 Đã dừng nhạc và xoá hàng đợi!")

    # ---------------- /disconnect (NGUY HIEM) ----------------
    @bot.tree.command(name="disconnect", description="🔌 [NGUY HIỂM] Rời room voice ngay lập tức")
    @app_commands.default_permissions(manage_messages=True)
    async def disconnect(interaction: discord.Interaction):
        if not _can_moderate(interaction):
            return await interaction.response.send_message(
                "🚫 Lệnh này nguy hiểm, chỉ dành cho quản lý server!", ephemeral=True)
        vc = interaction.guild.voice_client
        if vc and vc.is_connected():
            _cleanup_guild(interaction.guild_id)
            await interaction.response.send_message("🔌 Chuột đã rời room, bye bye! 🐭")
        else:
            await interaction.response.send_message("🤔 Bot không ở trong room nào.", ephemeral=True)

    # ---------------- /queue ----------------
    @bot.tree.command(name="queue", description="📜 Xem hàng đợi nhạc")
    async def queue_cmd(interaction: discord.Interaction):
        gp = players.get(interaction.guild_id)
        if not gp or (not gp.current and not gp.queue):
            return await interaction.response.send_message("📭 Hàng đợi trống trơn!", ephemeral=True)
        lines = []
        if gp.current:
            lines.append(f"**▶️ Đang phát:** [{gp.current.title[:60]}]({gp.current.webpage_url})")
            lines.append("")
        for i, t in enumerate(list(gp.queue)[:10]):
            req = interaction.guild.get_member(t.requester_id)
            lines.append(f"`{i + 1}.` [{t.title[:55]}]({t.webpage_url}) • {_fmt_duration(t.duration)}"
                         f" • {req.display_name if req else '?'}")
        if len(gp.queue) > 10:
            lines.append(f"*...và {len(gp.queue) - 10} bài nữa*")
        loop_icon = {"off": "➡️", "track": "🔁", "queue": "🔄"}[gp.loop_mode]
        embed = discord.Embed(
            title=f"📜 Hàng đợi ({len(gp.queue)} bài) {loop_icon}",
            description="\n".join(lines),
            color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed)

    # ---------------- /nowplaying ----------------
    @bot.tree.command(name="nowplaying", description="🎧 Bài đang phát là gì?")
    async def nowplaying(interaction: discord.Interaction):
        gp = players.get(interaction.guild_id)
        if not gp or not gp.current:
            return await interaction.response.send_message("🔇 Không có gì đang phát cả!", ephemeral=True)
        t = gp.current
        vc = interaction.guild.voice_client
        status = "⏸️ Tạm dừng" if (vc and vc.is_paused()) else "▶️ Đang phát"
        embed = discord.Embed(title=status, color=discord.Color.green(),
                              description=f"**[{t.title}]({t.webpage_url})**")
        if t.thumbnail:
            embed.set_thumbnail(url=t.thumbnail)
        embed.add_field(name="Kênh", value=t.uploader or "?", inline=True)
        embed.add_field(name="Thời lượng", value=_fmt_duration(t.duration), inline=True)
        embed.add_field(name="Loop", value=gp.loop_mode, inline=True)
        await interaction.response.send_message(embed=embed)

    # ---------------- /volume ----------------
    @bot.tree.command(name="volume", description="🔊 Chỉnh âm lượng (0-150%)")
    async def volume(interaction: discord.Interaction,
                     muc: app_commands.Range[int, 0, 150]):
        gp = _get_player(interaction.guild_id)
        vc = interaction.guild.voice_client
        gp.volume = max(0.0, min(muc / 100.0, 1.5))
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = gp.volume
        await interaction.response.send_message(f"🔊 Âm lượng: **{muc}%**")

    # ---------------- /loop ----------------
    @bot.tree.command(name="loop", description="🔁 Bật/tắt phát lặp lại liên tục")
    @app_commands.choices(che_do=LOOP_CHOICES)
    async def loop(interaction: discord.Interaction, che_do: app_commands.Choice[str] = None):
        gp = _get_player(interaction.guild_id)
        chosen = che_do.value if che_do else "off"
        gp.loop_mode = chosen
        text = {"off": "➡️ Đã TẮT lặp lại", "track": "🔁 Lặp lại 1 bài hiện tại",
                "queue": "🔄 Lặp lại CẢ HÀNG ĐỢI (phát liên tục)"}[chosen]
        await interaction.response.send_message(text)

    # ---------------- /radio ----------------
    @bot.tree.command(name="radio", description="📡 Phát radio lofi/chill 24-7, KHÔNG quảng cáo")
    @app_commands.choices(kenh=RADIO_CHANNEL_CHOICES)
    async def radio(interaction: discord.Interaction, kenh: app_commands.Choice[str]):
        station = RADIO_STATIONS.get(kenh.value)
        if not station:
            return await interaction.response.send_message("❌ Kênh không tồn tại.", ephemeral=True)
        await interaction.response.defer()
        try:
            vc = await _join_channel(interaction)
        except RuntimeError as e:
            return await interaction.followup.send(str(e))
        gp = _get_player(interaction.guild_id)
        gp.text_channel_id = interaction.channel_id
        track = Track(
            title=station["name"],
            webpage_url=station["url"],
            requester_id=interaction.user.id,
            stream_url=station["url"],
            is_stream=True,
        )
        gp.queue.clear()  # radio thay the toan bo
        gp.queue.append(track)
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        else:
            await _play_next(interaction.guild)
        await interaction.followup.send(f"📡 Đang phát **{station['name']}** — không quảng cáo, chill thôi! 🌙")

    # ---------------- PLAYLISTS (MongoDB) ----------------
    @bot.tree.command(name="taoplaylist", description="📝 Tạo playlist riêng của bạn")
    async def taoplaylist(interaction: discord.Interaction, ten: str):
        if MUSIC_DB is None:
            return await interaction.response.send_message("❌ Database chưa sẵn sàng!", ephemeral=True)
        ten = ten.strip()[:50]
        col = MUSIC_DB.music_playlists
        exists = await col.find_one({
            "guild_id": interaction.guild_id, "user_id": interaction.user.id, "name": ten})
        if exists:
            return await interaction.response.send_message(
                f"⚠️ Bạn đã có playlist tên **{ten}** rồi!", ephemeral=True)
        await col.insert_one({
            "guild_id": interaction.guild_id, "user_id": interaction.user.id,
            "name": ten, "tracks": [], "created_at": int(time.time())})
        await interaction.response.send_message(
            f"📝 Đã tạo playlist **{ten}**!\n➡️ Thêm bài: `/themvao` khi đang phát, hoặc `/themvao playlist:<tên> bai_moi:<tên bài>`")

    @bot.tree.command(name="themvao", description="➕ Thêm bài đang phát (hoặc tìm bài mới) vào playlist")
    async def themvao(interaction: discord.Interaction,
                      playlist: str, bai_moi: str = None):
        if MUSIC_DB is None:
            return await interaction.response.send_message("❌ Database chưa sẵn sàng!", ephemeral=True)
        await interaction.response.defer()
        col = MUSIC_DB.music_playlists
        pl = await col.find_one({
            "guild_id": interaction.guild_id, "user_id": interaction.user.id,
            "name": playlist.strip()})
        if not pl:
            return await interaction.followup.send(
                f"❌ Không tìm thấy playlist **{playlist}** của bạn. Dùng `/danhsachplaylist` xem nhé!")
        if bai_moi:
            is_url = any(bai_moi.startswith(p) for p in URL_PREFIXES)
            try:
                data = await _extract_with_fallback(bai_moi, search=not is_url)
                entry = data if is_url else next(iter([e for e in (data.get("entries") or []) if e]), None)
                if not entry:
                    return await interaction.followup.send("😢 Không tìm thấy bài đó.")
                track = _entry_to_track(entry, interaction.user.id)
            except Exception as e:
                return await interaction.followup.send(f"❌ Lỗi tìm bài: `{str(e)[:120]}`")
        else:
            gp = players.get(interaction.guild_id)
            if not gp or not gp.current:
                return await interaction.followup.send(
                    "🤔 Không có bài nào đang phát. Gõ kèm `bai_moi` để tìm bài nhé!")
            track = gp.current
        doc = {"title": track.title[:200], "webpage_url": track.webpage_url,
               "duration": track.duration, "uploader": track.uploader[:100]}
        await col.update_one({"_id": pl["_id"]}, {"$push": {"tracks": doc}})
        count = len(pl.get("tracks", [])) + 1
        await interaction.followup.send(
            f"➕ Đã thêm **{track.title[:60]}** vào playlist **{pl['name']}** ({count} bài).")

    @bot.tree.command(name="phatplaylist", description="▶️ Phát toàn bộ playlist của bạn")
    async def phatplaylist(interaction: discord.Interaction, playlist: str):
        if MUSIC_DB is None:
            return await interaction.response.send_message("❌ Database chưa sẵn sàng!", ephemeral=True)
        await interaction.response.defer()
        pl = await MUSIC_DB.music_playlists.find_one({
            "guild_id": interaction.guild_id, "user_id": interaction.user.id,
            "name": playlist.strip()})
        if not pl or not pl.get("tracks"):
            return await interaction.followup.send(f"📭 Playlist **{playlist}** không tồn tại hoặc trống!")
        try:
            vc = await _join_channel(interaction)
        except RuntimeError as e:
            return await interaction.followup.send(str(e))
        gp = _get_player(interaction.guild_id)
        gp.text_channel_id = interaction.channel_id
        for doc in pl["tracks"]:
            gp.queue.append(Track(
                title=doc.get("title", "?"),
                webpage_url=doc.get("webpage_url", ""),
                requester_id=interaction.user.id,
                duration=doc.get("duration"),
                uploader=doc.get("uploader", "")))
        if not vc.is_playing() and not vc.is_paused():
            await _play_next(interaction.guild)
        await interaction.followup.send(
            f"📋 Đã nạp **{len(pl['tracks'])} bài** từ playlist **{pl['name']}**!\n"
            f"💡 Mẹo: dùng `/loop che_do:Lap ca hang doi` để phát lặp lại liên tục cả ngày!")

    @bot.tree.command(name="xoaplaylist", description="🗑️ Xoá playlist của chính bạn")
    async def xoaplaylist(interaction: discord.Interaction, playlist: str):
        if MUSIC_DB is None:
            return await interaction.response.send_message("❌ Database chưa sẵn sàng!", ephemeral=True)
        res = await MUSIC_DB.music_playlists.delete_one({
            "guild_id": interaction.guild_id, "user_id": interaction.user.id,
            "name": playlist.strip()})
        if res.deleted_count:
            await interaction.response.send_message(f"🗑️ Đã xoá playlist **{playlist}**.")
        else:
            await interaction.response.send_message(f"❌ Không tìm thấy playlist **{playlist}** của bạn.", ephemeral=True)

    @bot.tree.command(name="danhsachplaylist", description="📂 Xem các playlist bạn đã tạo")
    async def danhsachplaylist(interaction: discord.Interaction):
        if MUSIC_DB is None:
            return await interaction.response.send_message("❌ Database chưa sẵn sàng!", ephemeral=True)
        pls = await MUSIC_DB.music_playlists.find({
            "guild_id": interaction.guild_id, "user_id": interaction.user.id}).to_list(length=25)
        if not pls:
            return await interaction.response.send_message(
                "📭 Bạn chưa có playlist nào. Tạo bằng `/taoplaylist`!", ephemeral=True)
        lines = "\n".join(f"• **{p['name']}** — {len(p.get('tracks', []))} bài" for p in pls)
        embed = discord.Embed(title="📂 Playlist của bạn", description=lines,
                              color=discord.Color.purple())
        await interaction.response.send_message(embed=embed)

    # ---- Lang nghe bot bi kick khoi room -> don state ----
    @bot.listen("on_voice_state_update")
    async def _music_vs_update(member, before, after):
        if member.id != _bot.user.id:
            return
        if before.channel and not after.channel:
            _cleanup_guild(member.guild.id, disconnect=False)
            logger.info("[Music] Bot bi roi room thu cong -> da don state")

    logger.info("✅ Đã đăng ký các lệnh nhạc (/play, /radio, playlist...)")
