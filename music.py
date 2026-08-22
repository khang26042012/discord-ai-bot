"""
Music system powered by Lavalink v4 (tren Khangvanila - nvnmc.asia:26014).
Engine: node Java xu ly toan bo YouTube/streaming -> khong con bi 403/bot-check.
Giao dien lenh giu nguyen nhu cu: /play /pause /resume /skip /queue /nowplaying
/volume /loop /radio /stop /disconnect + playlist commands.
"""

import asyncio
import json
import logging
import os
import random
import time
from typing import Any, Dict, List, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

try:
    import motor.motor_asyncio as _motor  # noqa: F401
    HAS_MOTOR = True
except Exception:
    HAS_MOTOR = False

logger = logging.getLogger("DiscordBot")

# ================= CONFIG =================

LAV_HOST = os.getenv("LAVALINK_HOST", "nvnmc.asia")
LAV_PORT = int(os.getenv("LAVALINK_PORT", "26014"))
LAV_PASSWORD = os.getenv("LAVALINK_PASSWORD", "")
LAV_REST = f"http://{LAV_HOST}:{LAV_PORT}/v4"
LAV_WS = f"ws://{LAV_HOST}:{LAV_PORT}/v4/websocket"

SEARCH_RESULTS = 5
ALONE_TIMEOUT_SEC = 60      # out het nguoi 1 phut -> tu stop
IDLE_TIMEOUT_SEC = 300       # noi khong phat 5 phut -> roi room
WATCHER_INTERVAL_SEC = 15

RADIO_STATIONS = [
    {"ten": "🌙 Lofi Hip Hop Radio (SomaFM)", "url": "https://ice1.somafm.com/groovesalad-128-mp3"},
    {"ten": "🌧️ Chill / Ambient (SomaFM)", "url": "https://ice1.somafm.com/dronezone-128-mp3"},
    {"ten": "🎸 Indie Rock (SomaFM)", "url": "https://ice1.somafm.com/indiepop-128-mp3"},
    {"ten": "🎹 Piano Jazz (SomaFM)", "url": "https://ice1.somafm.com/sonicuniverse-128-mp3"},
    {"ten": "👾 Synthwave (Nightride FM)", "url": "https://stream.nightride.fm/nightride.m4a"},
]

LOOP_CHOICES = [
    app_commands.Choice(name="🔁 Tắt lặp", value="off"),
    app_commands.Choice(name="🔂 Lặp bài hiện tại", value="track"),
    app_commands.Choice(name="🔃 Lặp cả hàng đợi", value="queue"),
]

_db = None
_bot: Optional[commands.Bot] = None


class GuildPlayer:
    """Trang thai phat nhac cua 1 server."""

    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.queue: List[Dict[str, Any]] = []   # cac track da encode san
        self.now: Optional[Dict[str, Any]] = None
        self.loop: str = "off"
        self.volume: int = 100
        self.text_channel_id: Optional[int] = None
        self.radio_url: Optional[str] = None    # dang che do radio
        self.paused: bool = False
        self.last_active = time.time()

        self.voice_token: Optional[str] = None
        self.voice_endpoint: Optional[str] = None
        self.voice_session: Optional[str] = None


_guild_players: Dict[int, GuildPlayer] = {}
_lav_session_id: Optional[str] = None     # session cua websocket den node
_lav_ws_task: Optional[asyncio.Task] = None
_watcher_task: Optional[asyncio.Task] = None
_http: Optional[aiohttp.ClientSession] = None


def _gp(guild_id: int) -> GuildPlayer:
    gp = _guild_players.get(guild_id)
    if gp is None:
        gp = GuildPlayer(guild_id)
        _guild_players[guild_id] = gp
    return gp


def _can_moderate(interaction: discord.Interaction) -> bool:
    perms = interaction.user.guild_permissions
    return perms.manage_messages or perms.manage_guild or perms.administrator


# ================= HTTP HELPERS =================

async def _api(method: str, path: str, payload: Optional[Dict] = None,
               params: Optional[Dict] = None) -> Any:
    """Goij REST cua Lavalink."""
    global _http
    if _http is None:
        _http = aiohttp.ClientSession()
    url = f"{LAV_REST}{path}"
    headers = {"Authorization": LAV_PASSWORD, "Content-Type": "application/json"}
    async with _http.request(method, url, json=payload, params=params,
                             headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as r:
        text = await r.text()
        if r.status >= 400:
            raise RuntimeError(f"Lavalink {r.status}: {text[:150]}")
        return json.loads(text) if text else None


async def _load_tracks(identifier: str) -> List[Dict[str, Any]]:
    """Tra ve danh sach track (da co encoded) tu node."""
    data = await _api("GET", "/loadtracks", params={"identifier": identifier})
    lt = data.get("loadType")
    if lt in ("search", "track"):
        return data.get("data", [])
    if lt == "playlist":
        return data.get("data", {}).get("tracks", [])
    return []


async def _update_player(guild_id: int, payload: Dict[str, Any]) -> None:
    """Cap nhat player tren node (play/volume/voice...)."""
    if not _lav_session_id:
        raise RuntimeError("Chua co session voi Lavalink")
    await _api("PATCH",
               f"/sessions/{_lav_session_id}/players/{guild_id}?noReplace=false",
               payload=payload)


async def _destroy_player(guild_id: int) -> None:
    try:
        if _lav_session_id:
            await _api("DELETE", f"/sessions/{_lav_session_id}/players/{guild_id}")
    except Exception:
        pass


def _is_busy(gp: Optional[GuildPlayer]) -> bool:
    """Dang phat/tam dung? (do ta tu theo doi, khong qua voice_client)"""
    return gp is not None and gp.now is not None


def _voice_block(gp: GuildPlayer) -> Optional[Dict[str, str]]:
    if gp.voice_token and gp.voice_endpoint and gp.voice_session:
        return {"token": gp.voice_token, "endpoint": gp.voice_endpoint,
                "sessionId": gp.voice_session}
    return None


# ================= WEBSOCKET DEN NODE =================

async def _ws_loop():
    """Giữ websocket tới node; nhận event TrackEnd để chuyển bài."""
    global _lav_session_id, _http
    backoff = 5
    while True:
        try:
            if _http is None:
                _http = aiohttp.ClientSession()
            headers = {
                "Authorization": LAV_PASSWORD,
                "User-Id": str(_bot.user.id),
                "Num-Shards": "1",
                "Client-Name": "KhangBot/1.0",
            }
            async with _http.ws_connect(f"{LAV_WS}", headers=headers,
                                        heartbeat=None, timeout=aiohttp.ClientWSTimeout(ws_close=30)) as ws:
                logger.info("✅ Đã kết nối Lavalink node")
                backoff = 5
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        op_data = json.loads(msg.data)
                    except Exception:
                        continue
                    op = op_data.get("op")
                    if op == "ready":
                        _lav_session_id = op_data.get("sessionId")
                        logger.info(f"🎵 Lavalink session: {_lav_session_id}")
                        # resume cac player dang ton tai (neu co)
                        for gid, gp in list(_guild_players.items()):
                            vb = _voice_block(gp)
                            if vb:
                                try:
                                    await _update_player(gid, {"voice": vb})
                                except Exception:
                                    pass
                    elif op == "event":
                        await asyncio.get_event_loop().run_in_executor(
                            None, lambda d=op_data: _queue_event(d))
        except Exception as e:
            logger.warning(f"[Music] Mat ket noi Lavalink ({e}) — thu lai sau {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


_event_queue: "asyncio.Queue[Dict]" = asyncio.Queue()


def _queue_event(d: Dict):
    _event_queue.put_nowait(d)


async def _event_consumer():
    while True:
        ev = await _event_queue.get()
        try:
            await _handle_event(ev)
        except Exception as e:
            logger.warning(f"[Music] Loi xu ly event: {e}")


async def _handle_event(ev: Dict):
    etype = ev.get("type")
    if etype not in ("TrackEndEvent", "TrackExceptionEvent", "TrackStuckEvent"):
        return
    try:
        guild_id = int(ev.get("guildId"))
    except Exception:
        return
    gp = _guild_players.get(guild_id)
    if gp is None:
        return
    reason = ev.get("reason", "")

    if etype == "TrackEndEvent" and reason not in ("FINISHED", "LOAD_FAILED"):
        # stopped/replaced do lenh dieu khien -> khong tu chuyen bai
        return

    finished = gp.now
    gp.now = None

    if gp.radio_url:
        # radio bi dut -> thu phat lai
        try:
            trs = await _load_tracks(gp.radio_url)
            if trs:
                gp.now = trs[0]
                payload = {"track": {"encoded": trs[0]["encoded"]},
                           "volume": gp.volume}
                vb = _voice_block(gp)
                if vb:
                    payload["voice"] = vb
                await _update_player(guild_id, payload)
                return
        except Exception:
            pass
        gp.radio_url = None

    if gp.loop == "track" and finished:
        gp.queue.insert(0, finished)
    if gp.loop == "queue" and finished:
        gp.queue.append(finished)

    await _play_next_by_id(guild_id)


# ================= PLAYBACK CORE =================

async def _ensure_voice(interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
    """Join/move voice channel cua nguoi dung."""
    if not interaction.user.voice or not interaction.user.voice.channel:
        return None
    ch = interaction.user.voice.channel
    vc = interaction.guild.voice_client
    if vc is None:
        vc = await ch.connect(self_deaf=True)
    elif vc.channel.id != ch.id:
        await vc.move_to(ch, self_deaf=True)
    gp = _gp(interaction.guild_id)
    gp.text_channel_id = interaction.channel_id
    gp.last_active = time.time()
    return vc


def _sync_voice_from_vc(vc: discord.VoiceClient, gp: GuildPlayer) -> bool:
    """Doc session/token tu discord.py de nap cho node."""
    ok = False
    sess = getattr(vc, "session_id", None) or getattr(getattr(vc, "_connection", None), "session_id", None)
    if sess and not gp.voice_session:
        gp.voice_session = str(sess)
        ok = True
    token = getattr(vc, "token", None) or getattr(getattr(vc, "_connection", None), "token", None)
    if token and not gp.voice_token:
        gp.voice_token = str(token)
        ok = True
    endpoint = getattr(getattr(vc, "_connection", None), "endpoint", None)
    if endpoint and not gp.voice_endpoint:
        ep = str(endpoint).replace("wss://", "").replace("wss:////", "")
        gp.voice_endpoint = ep
        ok = True
    return ok


async def _play_next_by_id(guild_id: int):
    """Chuyen sang track ke tieu trong queue (khong can interaction)."""
    gp = _guild_players.get(guild_id)
    if gp is None:
        return
    vc = _bot.get_guild(guild_id).voice_client if _bot.get_guild(guild_id) else None
    if vc is None or not vc.is_connected():
        await _cleanup_guild(guild_id, notify=False)
        return
    _sync_voice_from_vc(vc, gp)
    vb = _voice_block(gp)
    if vb is None:
        logger.warning("[Music] Thieu voice data khi play")
        return

    if not gp.queue:
        return
    track = gp.queue.pop(0)
    gp.now = track
    gp.last_active = time.time()
    payload = {"track": {"encoded": track["encoded"]}, "volume": gp.volume, "voice": vb}
    await _update_player(guild_id, payload)


# ================= VOICE STATE CAPTURE =================

async def on_socket_response(data: Dict):
    """Bat VOICE_SERVER_UPDATE / VOICE_STATE_UPDATE de cap voice block."""
    if not isinstance(data, dict):
        return
    t = data.get("t")
    d = data.get("d") or {}
    if t == "VOICE_SERVER_UPDATE":
        try:
            gid = int(d["guild_id"])
        except Exception:
            return
        gp = _gp(gid)
        gp.voice_token = d.get("token")
        ep = (d.get("endpoint") or "").replace("wss://", "")
        gp.voice_endpoint = ep
        # nap ngay cho node neu dang co session
        if _lav_session_id and gp.voice_session:
            try:
                await _update_player(gid, {"voice": _voice_block(gp)})
            except Exception:
                pass
    elif t == "VOICE_STATE_UPDATE":
        try:
            uid = int(d.get("user_id", 0))
            gid = int(d.get("guild_id", 0))
        except Exception:
            return
        me = _bot.user.id if _bot else 0
        if uid == me and gid:
            gp = _gp(gid)
            sid = d.get("session_id")
            if sid:
                gp.voice_session = sid


# ================= WATCHER (auto-stop khi trong/idle) =================

async def _voice_watcher():
    while True:
        try:
            for gid in list(_guild_players.keys()):
                gp = _guild_players.get(gid)
                if gp is None:
                    continue
                g = _bot.get_guild(gid)
                if g is None:
                    continue
                vc = g.voice_client
                if vc is None or not vc.is_connected():
                    await _cleanup_guild(gid, notify=False)
                    continue
                humans = [m for m in vc.channel.members if not m.bot]
                playing = _is_busy(gp)
                if not humans:
                    if time.time() - gp.last_active >= ALONE_TIMEOUT_SEC:
                        ch_id = gp.text_channel_id
                        await _cleanup_guild(gid, notify=False)
                        if ch_id:
                            ch = _bot.get_channel(ch_id)
                            if ch:
                                try:
                                    await ch.send("👋 Không ai ở trong kênh nữa nên bot đã rời đi để tiết kiệm tài nguyên!")
                                except Exception:
                                    pass
                        continue
                elif not playing:
                    if time.time() - gp.last_active >= IDLE_TIMEOUT_SEC:
                        await _cleanup_guild(gid, notify=False)
                        continue
                else:
                    gp.last_active = time.time()
        except Exception as e:
            logger.warning(f"[Music] Watcher loi: {e}")
        await asyncio.sleep(WATCHER_INTERVAL_SEC)


async def _cleanup_guild(guild_id: int, notify: bool = True):
    gp = _guild_players.pop(guild_id, None)
    if gp is None:
        return
    await _destroy_player(guild_id)
    g = _bot.get_guild(guild_id)
    if g:
        vc = g.voice_client
        if vc:
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass


# ================= UI CHON BAI =================

class TrackSelectView(discord.ui.View):
    def __init__(self, invoker_id: int, choices: List[Dict]):
        super().__init__(timeout=90)
        self.invoker_id = invoker_id
        self.choices = choices
        self.chosen: Optional[Dict] = None
        self.message: Optional[discord.Message] = None
        options = []
        for i, t in enumerate(choices[: SEARCH_RESULTS]):
            dur = t.get("length") or 0
            mins, secs = divmod(int(dur) // 1000, 60)
            options.append(discord.SelectOption(
                label=f"{i + 1}. {t.get('title','?')[:90]}",
                description=f"{t.get('author','?')[:40]} • {mins}:{secs:02d}",
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


# ================= HELPERS HIEN THI =================

def _fmt_duration(ms: Optional[float]) -> str:
    if not ms:
        return "∞ (stream)"
    s = int(ms) // 1000
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _track_line(t: Dict, idx: Optional[int] = None) -> str:
    prefix = f"**{idx}.** " if idx else ""
    uri = t.get("uri") or ""
    title = (t.get("title") or "?")[:70]
    return f"{prefix}[{title}]({uri}) ↳ {t.get('author','?')[:50]} • {_fmt_duration(t.get('length'))}"


# ================= SETUP LENH =================

def setup(bot: commands.Bot):
    global _bot
    _bot = bot
    bot.add_listener(on_socket_response, name="on_socket_response")

    # ---------------- PHAT NHAC ----------------
    @bot.tree.command(name="play", description="▶️ Phát nhạc từ tên bài hát hoặc link (YouTube/SoundCloud)")
    @app_commands.describe(query="Tên bài hát hoặc link YouTube/SoundCloud")
    async def play(interaction: discord.Interaction, query: str):
        if not _lav_session_id:
            return await interaction.response.send_message(
                "⏳ Đang kết nối tới node nhạc, thử lại sau ít phút!", ephemeral=True)
        vc = await _ensure_voice(interaction)
        if vc is None:
            return await interaction.response.send_message(
                "🤔 Bạn cần vào một kênh thoại trước đã!", ephemeral=True)
        await interaction.response.defer()

        # tim kiem qua node
        identifier = query if query.startswith("http") else f"ytsearch{SEARCH_RESULTS}:{query}"
        if "soundcloud.com" in query:
            identifier = query
        try:
            tracks = await _load_tracks(identifier)
        except Exception as e:
            return await interaction.followup.send(f"❌ Lỗi khi tìm nhạc: `{str(e)[:120]}`")
        if not tracks and not query.startswith("http"):
            try:
                tracks = await _load_tracks(f"scsearch{SEARCH_RESULTS}:{query}")
            except Exception:
                tracks = []
        if not tracks:
            return await interaction.followup.send(f"😢 Không tìm thấy kết quả nào cho `{query}`.")

        gp = _gp(interaction.guild_id)

        if query.startswith("http"):
            track = tracks[0]
            gp.queue.append(track)
            pos = len(gp.queue)
            if _is_busy(gp):
                embed = discord.Embed(
                    title="✅ Đã thêm vào hàng đợi",
                    description=f"**[{track['info']['title'][:60]}]({track['info'].get('uri','')})** • {_fmt_duration(track['info'].get('length'))}",
                    color=discord.Color.blue())
                embed.set_footer(text=f"Vị trí #{pos}")
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send(
                    embed=discord.Embed(title="🎵 Đang phát",
                                        description=f"**[{track['info']['title'][:60]}]({track['info'].get('uri','')})**",
                                        color=discord.Color.green()))
                await _play_next_by_id(interaction.guild_id)
        else:
            choices = tracks[:SEARCH_RESULTS]
            infos = [t["info"] for t in choices]
            lines = "\n".join(_track_line(infos[i], i + 1) for i in range(len(infos)))
            embed = discord.Embed(title="🔎 Kết quả tìm kiếm (YouTube)",
                                  description=lines, color=discord.Color.gold())
            embed.set_footer(text="Chọn bài bên dưới ⬇️ hoặc đợi 90s để huỷ")
            view = TrackSelectView(interaction.user.id, choices)
            msg = await interaction.followup.send(embed=embed, view=view)
            view.message = msg
            timed_out = await view.wait()
            if timed_out or view.chosen is None:
                return
            chosen = view.chosen
            gp.queue.append(chosen)
            if _is_busy(gp):
                await interaction.followup.send(
                    f"✅ Đã thêm **{chosen['info']['title'][:60]}** vào hàng đợi (vị trí #{len(gp.queue)}).")
            else:
                await interaction.followup.send(
                    f"🎵 Đang phát **{chosen['info']['title'][:60]}**...")
                await _play_next_by_id(interaction.guild_id)

    # ---------------- DIEU KHIEN CO BAN ----------------
    @bot.tree.command(name="pause", description="⏸️ Tạm dừng bản nhạc hiện tại")
    async def pause(interaction: discord.Interaction):
        gp = _guild_players.get(interaction.guild_id)
        if _is_busy(gp) and not gp.paused:
            await _update_player(interaction.guild_id, {"paused": True})
            gp.paused = True
            await interaction.response.send_message("⏸️ Đã tạm dừng.")
        else:
            await interaction.response.send_message("🤔 Không có gì đang phát.", ephemeral=True)

    @bot.tree.command(name="resume", description="▶️ Tiếp tục phát nhạc")
    async def resume(interaction: discord.Interaction):
        gp = _guild_players.get(interaction.guild_id)
        if _is_busy(gp) and gp.paused:
            await _update_player(interaction.guild_id, {"paused": False})
            gp.paused = False
            await interaction.response.send_message("▶️ Tiếp tục phát!")
        else:
            await interaction.response.send_message("🤔 Nhạc không bị dừng.", ephemeral=True)

    @bot.tree.command(name="skip", description="⏭️ Bỏ qua bài hiện tại")
    async def skip(interaction: discord.Interaction):
        gp = _guild_players.get(interaction.guild_id)
        if _is_busy(gp):
            gp.now = None          # chan loop/replay trong event handler
            await _update_player(interaction.guild_id, {"track": None})   # node stop -> STOPPED event
            await interaction.followup.send("⏭️ Đã bỏ qua!") if interaction.response.is_done() \
                else await interaction.response.send_message("⏭️ Đã bỏ qua!")
            await _play_next_by_id(interaction.guild_id)   # tu chuyen bai ke tiep
        else:
            await interaction.response.send_message("🤔 Không có gì để bỏ qua.", ephemeral=True)

    @bot.tree.command(name="queue", description="📋 Xem hàng đợi nhạc")
    async def queue_cmd(interaction: discord.Interaction):
        gp = _guild_players.get(interaction.guild_id)
        if gp is None or (not gp.now and not gp.queue):
            return await interaction.response.send_message("📭 Hàng đợi trống trơn!", ephemeral=True)
        desc = ""
        if gp.now:
            desc += "🎵 **Đang phát:**\n" + _track_line(gp.now["info"]) + "\n\n"
        if gp.loop == "track":
            desc += "🔁 *(đang lặp bài này)*\n"
        if gp.queue:
            desc += "📋 **Tiếp theo:**\n"
            for i, t in enumerate(gp.queue[:10], 1):
                desc += _track_line(t["info"], i) + "\n"
            if len(gp.queue) > 10:
                desc += f"*... và {len(gp.queue) - 10} bài khác*\n"
        embed = discord.Embed(title="📋 Hàng đợi nhạc", description=desc[:3900],
                              color=discord.Color.blue())
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="nowplaying", description="🎧 Bài đang phát là gì?")
    async def nowplaying(interaction: discord.Interaction):
        gp = _guild_players.get(interaction.guild_id)
        if gp is None or not gp.now:
            return await interaction.response.send_message("🤫 Không có gì đang phát cả.", ephemeral=True)
        info = gp.now["info"]
        embed = discord.Embed(title="🎧 Đang phát",
                              description=f"**[{info['title']}]({info.get('uri','')})**",
                              color=discord.Color.purple())
        embed.add_field(name="Kênh", value=info.get("author", "?"))
        embed.add_field(name="Thời lượng", value=_fmt_duration(info.get("length")))
        if gp.radio_url:
            embed.set_footer(text="📻 Chế độ radio")
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="volume", description="🔊 Chỉnh âm lượng (0-200)")
    @app_commands.describe(muc="Mức âm lượng từ 0 đến 200")
    async def volume(interaction: discord.Interaction, muc: int):
        muc = max(0, min(200, muc))
        gp = _gp(interaction.guild_id)
        gp.volume = muc
        try:
            await _update_player(interaction.guild_id, {"volume": muc})
            await interaction.response.send_message(f"🔊 Âm lượng: **{muc}%**")
        except Exception as e:
            await interaction.response.send_message(f"❌ Không chỉnh được: `{str(e)[:80]}`", ephemeral=True)

    @bot.tree.command(name="loop", description="🔁 Bật/tắt chế độ lặp nhạc")
    @app_commands.describe(che_do="Chọn kiểu lặp")
    @app_commands.choices(che_do=LOOP_CHOICES)
    async def loop(interaction: discord.Interaction, che_do: app_commands.Choice[str] = None):
        gp = _gp(interaction.guild_id)
        gp.loop = che_do.value if che_do else "off"
        ten = {"off": "🔁 Tắt lặp", "track": "🔂 Lặp bài hiện tại", "queue": "🔃 Lặp cả hàng đợi"}
        await interaction.response.send_message(ten.get(gp.loop, "Đã đặt"))

    # ---------------- RADIO ----------------
    @bot.tree.command(name="radio", description="📻 Phát radio 24/7 (lofi, chill, jazz...)")
    @app_commands.describe(kenh="Chọn đài phát thanh")
    @app_commands.choices(kenh=[app_commands.Choice(name=s["ten"], value=str(i)) for i, s in enumerate(RADIO_STATIONS)])
    async def radio(interaction: discord.Interaction, kenh: app_commands.Choice[str] = None):
        if kenh is None:
            lines = "\n".join(f"**{i + 1}.** {s['ten']}" for i, s in enumerate(RADIO_STATIONS))
            embed = discord.Embed(title="📻 Danh sách đài radio", description=lines,
                                  color=discord.Color.blurple())
            embed.set_footer(text="Dùng /radio chọn số để nghe!")
            return await interaction.response.send_message(embed=embed)
        idx = int(kenh.value)
        station = RADIO_STATIONS[idx]
        vc = await _ensure_voice(interaction)
        if vc is None:
            return await interaction.response.send_message(
                "🤔 Bạn cần vào một kênh thoại trước đã!", ephemeral=True)
        await interaction.response.defer()
        gp = _gp(interaction.guild_id)
        gp.queue.clear()
        gp.radio_url = station["url"]
        try:
            tracks = await _load_tracks(station["url"])
        except Exception as e:
            gp.radio_url = None
            return await interaction.followup.send(f"❌ Không bắt được sóng: `{str(e)[:100]}`")
        if not tracks:
            gp.radio_url = None
            return await interaction.followup.send("❌ Đài này hiện không phát được.")
        gp.now = tracks[0]
        vb = _voice_block(gp)
        payload = {"track": {"encoded": tracks[0]["encoded"]}, "volume": gp.volume}
        if vb:
            payload["voice"] = vb
        try:
            await _update_player(interaction.guild_id, payload)
            await interaction.followup.send(f"📻 Đang phát **{station['ten']}** — enjoy!")
        except Exception as e:
            await interaction.followup.send(f"❌ Lỗi: `{str(e)[:100]}`")

    # ---------------- STOP / DISCONNECT ----------------
    @bot.tree.command(name="stop", description="🛑 Dừng nhạc và xoá hàng đợi (cần quyền quản lý)")
    @app_commands.default_permissions(manage_messages=True)
    async def stop(interaction: discord.Interaction):
        if not _can_moderate(interaction):
            return await interaction.response.send_message(
                "🚫 Lệnh này cần quyền **Quản lý tin nhắn**!", ephemeral=True)
        gp = _guild_players.get(interaction.guild_id)
        if gp is None and interaction.guild.voice_client is None:
            return await interaction.response.send_message("🤔 Không có gì để dừng.", ephemeral=True)
        if gp:
            gp.queue.clear()
            gp.now = None
            gp.radio_url = None
        try:
            await _update_player(interaction.guild_id, {"track": None})
        except Exception:
            pass
        await interaction.response.send_message("🛑 Đã dừng nhạc và xoá hàng đợi!")

    @bot.tree.command(name="disconnect", description="👋 Rời khỏi kênh thoại (cần quyền quản lý)")
    @app_commands.default_permissions(manage_messages=True)
    async def disconnect(interaction: discord.Interaction):
        if not _can_moderate(interaction):
            return await interaction.response.send_message(
                "🚫 Lệnh này cần quyền **Quản lý tin nhắn**!", ephemeral=True)
        if interaction.guild.voice_client is None:
            return await interaction.response.send_message("🤖 Bot không ở trong kênh thoại.", ephemeral=True)
        await _cleanup_guild(interaction.guild_id, notify=False)
        await interaction.response.send_message("👋 Đã rời khỏi kênh thoại!")

    # ---------------- PLAYLIST (MongoDB) ----------------
    @bot.tree.command(name="taoplaylist", description="💾 Tạo playlist mới")
    @app_commands.describe(ten="Tên playlist", bai_moi="Bài đầu tiên (tuỳ chọn)")
    async def taoplaylist(interaction: discord.Interaction, ten: str, bai_moi: str = None):
        if _db is None:
            return await interaction.response.send_message("⚠️ Database chưa sẵn sàng!", ephemeral=True)
        col = _db.music_playlists
        if await col.find_one({"guild_id": interaction.guild_id, "name": ten.lower(), "user_id": interaction.user.id}):
            return await interaction.response.send_message(f"📛 Bạn đã có playlist tên `{ten}` rồi!", ephemeral=True)
        doc = {"guild_id": interaction.guild_id, "user_id": interaction.user.id,
               "name": ten.lower(), "tracks": [], "created_at": discord.utils.utcnow().isoformat()}
        if bai_moi:
            if not _lav_session_id:
                return await interaction.response.send_message("⏳ Node chưa sẵn sàng!", ephemeral=True)
            trs = await _load_tracks(bai_moi if bai_moi.startswith("http") else f"ytsearch1:{bai_moi}")
            if trs:
                doc["tracks"].append({"title": trs[0]["info"]["title"],
                                      "identifier": trs[0]["info"]["identifier"],
                                      "uri": trs[0]["info"].get("uri", "")})
        await col.insert_one(doc)
        msg = f"✅ Đã tạo playlist **{ten}**"
        if bai_moi and doc["tracks"]:
            msg += f"\n➕ Thêm: {doc['tracks'][0]['title'][:60]}"
        await interaction.response.send_message(msg)

    @bot.tree.command(name="themvao", description="➕ Thêm bài vào playlist")
    @app_commands.describe(ten="Tên playlist", bai_moi="Tên bài hoặc link")
    async def themvao(interaction: discord.Interaction, ten: str, bai_moi: str):
        if _db is None:
            return await interaction.response.send_message("⚠️ Database chưa sẵn sàng!", ephemeral=True)
        pl = await _db.music_playlists.find_one({"guild_id": interaction.guild_id, "name": ten.lower(),
                                                 "user_id": interaction.user.id})
        if not pl:
            return await interaction.response.send_message(f"❓ Không tìm thấy playlist `{ten}`.", ephemeral=True)
        if not _lav_session_id:
            return await interaction.response.send_message("⏳ Node chưa sẵn sàng!", ephemeral=True)
        trs = await _load_tracks(bai_moi if bai_moi.startswith("http") else f"ytsearch1:{bai_moi}")
        if not trs:
            return await interaction.response.send_message("😢 Không tìm thấy bài đó.", ephemeral=True)
        info = trs[0]["info"]
        await _db.music_playlists.update_one(
            {"_id": pl["_id"]},
            {"$push": {"tracks": {"title": info["title"], "identifier": info["identifier"],
                                  "uri": info.get("uri", "")}}})
        await interaction.response.send_message(f"➕ Đã thêm **{info['title'][:60]}** vào `{ten}`!")

    @bot.tree.command(name="phatplaylist", description="▶️ Phát toàn bộ playlist")
    @app_commands.describe(ten="Tên playlist", xao_tron="Xáo trộn bài? (mặc định có)")
    async def phatplaylist(interaction: discord.Interaction, ten: str, xao_tron: bool = True):
        if _db is None:
            return await interaction.response.send_message("⚠️ Database chưa sẵn sàng!", ephemeral=True)
        pl = await _db.music_playlists.find_one({"guild_id": interaction.guild_id, "name": ten.lower(),
                                                 "user_id": interaction.user.id})
        if not pl or not pl["tracks"]:
            return await interaction.response.send_message(f"❓ Playlist `{ten}` trống hoặc không tồn tại.", ephemeral=True)
        vc = await _ensure_voice(interaction)
        if vc is None:
            return await interaction.response.send_message("🤔 Vào kênh thoại trước nhé!", ephemeral=True)
        await interaction.response.defer()
        gp = _gp(interaction.guild_id)
        loaded: List[Dict] = []
        for item in pl["tracks"]:
            try:
                trs = await _load_tracks(item["identifier"])
                if trs:
                    loaded.append(trs[0])
            except Exception:
                continue
        if not loaded:
            return await interaction.followup.send("❌ Không tải được bài nào trong playlist.")
        if xao_tron:
            random.shuffle(loaded)
        gp.queue.extend(loaded)
        if not _is_busy(gp):
            await _play_next_by_id(interaction.guild_id)
        await interaction.followup.send(f"📃 Đã nạp **{len(loaded)}/{len(pl['tracks'])}** bài từ `{ten}` vào hàng đợi!")

    @bot.tree.command(name="xoaplaylist", description="🗑️ Xoá playlist (cần quyền quản lý nếu không phải chủ)")
    @app_commands.describe(ten="Tên playlist")
    async def xoaplaylist(interaction: discord.Interaction, ten: str):
        if _db is None:
            return await interaction.response.send_message("⚠️ Database chưa sẵn sàng!", ephemeral=True)
        q = {"guild_id": interaction.guild_id, "name": ten.lower()}
        if not _can_moderate(interaction):
            q["user_id"] = interaction.user.id
        res = await _db.music_playlists.delete_one(q)
        if res.deleted_count:
            await interaction.response.send_message(f"🗑️ Đã xoá playlist `{ten}`.")
        else:
            await interaction.response.send_message("❓ Không tìm thấy playlist để xoá.", ephemeral=True)

    @bot.tree.command(name="danhsachplaylist", description="📂 Xem các playlist trên server này")
    async def danhsachplaylist(interaction: discord.Interaction):
        if _db is None:
            return await interaction.response.send_message("⚠️ Database chưa sẵn sàng!", ephemeral=True)
        pls = [pl async for pl in _db.music_playlists.find({"guild_id": interaction.guild_id}).limit(15)]
        if not pls:
            return await interaction.response.send_message("📭 Chưa có playlist nào. Dùng /taoplaylist nhé!", ephemeral=True)
        lines = []
        for pl in pls:
            u = await _bot.fetch_user(pl["user_id"]) if _bot else None
            uname = u.name if u else "?"
            lines.append(f"**{pl['name']}** — {len(pl['tracks'])} bài — bởi {uname}")
        embed = discord.Embed(title="📂 Playlists của server",
                              description="\n".join(lines)[:3800], color=discord.Color.green())
        await interaction.response.send_message(embed=embed)


# ================= PUBLIC API =================

def bind_db(database):
    global _db
    _db = database


async def ensure_started():
    global _lav_ws_task, _watcher_task
    if LAV_PASSWORD == "":
        logger.warning("⚠️ Thiếu LAVALINK_PASSWORD - music system off")
        return
    if (_lav_ws_task is None or _lav_ws_task.done()) and _bot:
        _lav_ws_task = asyncio.create_task(_ws_loop())
        _watcher_task = asyncio.create_task(_voice_watcher())
        asyncio.create_task(_event_consumer())
        logger.info("✅ Music watcher đã khởi động (auto-stop 60s khi room trống)")
