import asyncio
import os
import re
import logging
import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional, List, Dict, Any

logger = logging.getLogger("DiscordBot")

# ================= Khởi tạo Opus & Cấu hình Âm thanh =================
if not discord.opus.is_loaded():
    for opus_candidate in ['libopus.so.0', 'libopus.so', '/usr/lib/x86_64-linux-gnu/libopus.so.0', 'opus']:
        try:
            discord.opus.load_opus(opus_candidate)
            if discord.opus.is_loaded():
                logger.info(f"[Music] Đã nạp thành công thư viện Opus: {opus_candidate}")
                break
        except Exception:
            pass

COOKIE_PATHS = [
    os.path.expanduser("~/.credentials/youtube_cookies.txt"),
    os.path.join(os.path.dirname(__file__), "cookies.txt"),
    "/home/container/discord-ai-bot/cookies.txt"
]

def get_cookie_file() -> Optional[str]:
    for p in COOKIE_PATHS:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
    return None

AUTHORIZED_OWNER_ID = 1263707768876044382

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

def format_duration(seconds: int) -> str:
    if not seconds:
        return "Live / N/A"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def progress_bar(current: int, total: int, length: int = 12) -> str:
    if not total or total <= 0:
        return "🔘" + "─" * (length - 1)
    progress = min(max(current / total, 0.0), 1.0)
    pos = int(progress * (length - 1))
    bar = ""
    for i in range(length):
        if i == pos:
            bar += "🔘"
        else:
            bar += "─"
    return bar

# ================= Song & Audio Extractor =================
class Song:
    def __init__(self, data: Dict[str, Any], requester: discord.Member):
        self.title: str = data.get("title", "Không rõ tiêu đề")
        self.webpage_url: str = data.get("webpage_url", "")
        self.stream_url: str = data.get("url", "")
        self.duration: int = int(data.get("duration", 0) or 0)
        self.thumbnail: Optional[str] = data.get("thumbnail")
        self.uploader: str = data.get("uploader", "Không rõ")
        self.requester: discord.Member = requester

    @classmethod
    async def search(cls, query: str, requester: discord.Member, loop: Optional[asyncio.AbstractEventLoop] = None):
        import yt_dlp
        loop = loop or asyncio.get_event_loop()

        ydl_opts = {
            'format': 'bestaudio/best',
            'restrictfilenames': True,
            'noplaylist': True,
            'nocheckcertificate': True,
            'ignoreerrors': False,
            'logtostderr': False,
            'quiet': True,
            'no_warnings': True,
            'default_search': 'ytsearch',
            'source_address': '0.0.0.0',
        }

        cookie_file = get_cookie_file()
        if cookie_file:
            ydl_opts['cookiefile'] = cookie_file

        def _extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    if query.startswith("http://") or query.startswith("https://"):
                        return ydl.extract_info(query, download=False)
                    return ydl.extract_info(f"ytsearch:{query}", download=False)
                except Exception as e_yt:
                    logger.warning(f"[Music] YouTube extract failed: {e_yt}. Fallback sang SoundCloud...")
                    ydl_sc_opts = dict(ydl_opts)
                    ydl_sc_opts['default_search'] = 'scsearch'
                    with yt_dlp.YoutubeDL(ydl_sc_opts) as ydl_sc:
                        return ydl_sc.extract_info(f"scsearch:{query}", download=False)

        info = await loop.run_in_executor(None, _extract)
        if not info:
            return None

        if 'entries' in info:
            entries = [e for e in info['entries'] if e]
            if not entries:
                return None
            return cls(entries[0], requester)

        return cls(info, requester)

# ================= Music Player Per Guild =================
class GuildMusicPlayer:
    def __init__(self, bot: commands.Bot, guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.queue: List[Song] = []
        self.current: Optional[Song] = None
        self.voice_client: Optional[discord.VoiceClient] = None
        self.volume: float = 0.8
        self.is_paused: bool = False
        self.loop_mode: str = "off"
        self.text_channel: Optional[discord.TextChannel] = None
        self.disconnect_task: Optional[asyncio.Task] = None
        self.now_playing_msg: Optional[discord.Message] = None
        self.started_at: float = 0.0

    def get_elapsed(self) -> int:
        if not self.started_at or not self.voice_client or not self.voice_client.is_playing():
            return 0
        import time
        return int(time.time() - self.started_at)

    async def connect_voice(self, channel: discord.VoiceChannel) -> discord.VoiceClient:
        if self.voice_client and self.voice_client.is_connected():
            if self.voice_client.channel.id != channel.id:
                await self.voice_client.move_to(channel)
        else:
            self.voice_client = await channel.connect(reconnect=True, self_deaf=True)
        return self.voice_client

    def play_next(self):
        if self.loop_mode == "song" and self.current:
            pass
        elif self.loop_mode == "queue" and self.current:
            self.queue.append(self.current)
            self.current = self.queue.pop(0) if self.queue else None
        else:
            self.current = self.queue.pop(0) if self.queue else None

        if self.current:
            import time
            self.started_at = time.time()
            logger.info(f"[Music] Chuẩn bị phát: '{self.current.title}' (Stream URL: {self.current.stream_url[:50]}...)")
            try:
                audio_source = discord.FFmpegPCMAudio(self.current.stream_url, **FFMPEG_OPTIONS)
                volume_source = discord.PCMVolumeTransformer(audio_source, volume=self.volume)

                def after_callback(err):
                    if err:
                        logger.error(f"[Music] Playback error: {err}")
                    coro = self._after_song()
                    asyncio.run_coroutine_threadsafe(coro, self.bot.loop)

                self.voice_client.play(volume_source, after=after_callback)
                self.is_paused = False

                if self.disconnect_task and not self.disconnect_task.done():
                    self.disconnect_task.cancel()
                    self.disconnect_task = None

                asyncio.run_coroutine_threadsafe(self.send_now_playing(), self.bot.loop)
            except Exception as e:
                logger.error(f"[Music] Lỗi khởi chạy phát nhạc: {e}")
                self.play_next()
        else:
            self.cancel_now_playing_msg()
            if self.text_channel:
                asyncio.run_coroutine_threadsafe(
                    self.text_channel.send("⏹️ **Hàng đợi đã hết.** Bot sẽ tự rời phòng sau 3 phút nếu không có bài mới!"),
                    self.bot.loop
                )
            self.disconnect_task = self.bot.loop.create_task(self._auto_disconnect_countdown(180))

    async def _after_song(self):
        self.play_next()

    async def _auto_disconnect_countdown(self, seconds: int):
        await asyncio.sleep(seconds)
        if not self.current and self.voice_client and self.voice_client.is_connected():
            if not self.voice_client.is_playing():
                await self.voice_client.disconnect()
                if self.text_channel:
                    await self.text_channel.send("👋 **Đã tự động rời kênh Voice để tiết kiệm tài nguyên.**")
                self.voice_client = None

    async def send_now_playing(self):
        if not self.text_channel or not self.current:
            return
        embed = discord.Embed(
            title="🎵 ĐANG PHÁT NHẠC",
            description=f"[{self.current.title}]({self.current.webpage_url})",
            color=0x9b59b6
        )
        if self.current.thumbnail:
            embed.set_thumbnail(url=self.current.thumbnail)

        dur_str = format_duration(self.current.duration)
        embed.add_field(name="⏱️ Thời lượng", value=f"`{dur_str}`", inline=True)
        embed.add_field(name="🔊 Âm lượng", value=f"`{int(self.volume * 100)}%`", inline=True)
        embed.add_field(name="🔁 Lặp", value=f"`{self.loop_mode.upper()}`", inline=True)
        embed.add_field(name="👑 Yêu cầu bởi", value=self.current.requester.mention, inline=True)

        if self.queue:
            embed.add_field(name="📜 Bài kế tiếp", value=f"**{self.queue[0].title}**", inline=False)

        embed.set_footer(text="Hệ Thống Âm Nhạc Độc Quyền KhangSMP • Bấm nút bên dưới để điều khiển", icon_url=self.bot.user.display_avatar.url if self.bot.user else None)

        view = MusicControlView(self)
        try:
            self.now_playing_msg = await self.text_channel.send(embed=embed, view=view)
        except Exception as e:
            logger.error(f"[Music] Lỗi gửi tin nhắn now playing: {e}")

    def cancel_now_playing_msg(self):
        if self.now_playing_msg:
            try:
                asyncio.run_coroutine_threadsafe(self.now_playing_msg.edit(view=None), self.bot.loop)
            except Exception:
                pass
            self.now_playing_msg = None

    async def stop(self):
        self.queue.clear()
        self.current = None
        self.cancel_now_playing_msg()
        if self.voice_client and self.voice_client.is_connected():
            if self.voice_client.is_playing() or self.voice_client.is_paused():
                self.voice_client.stop()
            await self.voice_client.disconnect()
            self.voice_client = None

_players: Dict[int, GuildMusicPlayer] = {}

def get_player(bot: commands.Bot, guild: discord.Guild) -> GuildMusicPlayer:
    if guild.id not in _players:
        _players[guild.id] = GuildMusicPlayer(bot, guild)
    return _players[guild.id]

# ================= Dàn Nút Bấm Điều Khiển (Button UI) =================
class MusicControlView(discord.ui.View):
    def __init__(self, player: GuildMusicPlayer):
        super().__init__(timeout=None)
        self.player = player

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != AUTHORIZED_OWNER_ID:
            await interaction.response.send_message(
                "⛔ **Từ chối:** Bảng điều khiển nhạc chỉ dành riêng cho anh Khang (`@khangmc_vn`). Mọi người chỉ ngồi nghe thôi nha! 😉",
                ephemeral=True
            )
            return False
        return True

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.primary, custom_id="btn_play_pause")
    async def btn_pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.player.voice_client
        if not vc or not vc.is_connected():
            await interaction.response.send_message("❌ Bot không ở trong phòng voice!", ephemeral=True)
            return
        if vc.is_playing():
            vc.pause()
            self.player.is_paused = True
            await interaction.response.send_message("⏸️ **Đã tạm dừng bài hát.**", ephemeral=True)
        elif vc.is_paused():
            vc.resume()
            self.player.is_paused = False
            await interaction.response.send_message("▶️ **Tiếp tục phát nhạc.**", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Hiện không có bài hát nào để tạm dừng.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, custom_id="btn_skip")
    async def btn_skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = self.player.voice_client
        if not vc or not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message("❌ Không có bài nào đang phát để chuyển bài!", ephemeral=True)
            return
        vc.stop()
        await interaction.response.send_message("⏭️ **Đã chuyển sang bài kế tiếp!**", ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="btn_loop")
    async def btn_loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        modes = ["off", "song", "queue"]
        idx = (modes.index(self.player.loop_mode) + 1) % len(modes)
        self.player.loop_mode = modes[idx]
        mode_names = {"off": "TẮT", "song": "LẶP 1 BÀI 🔂", "queue": "LẶP HÀNG ĐỢI 🔃"}
        await interaction.response.send_message(f"🔁 Chế độ lặp: **{mode_names[self.player.loop_mode]}**", ephemeral=True)

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary, custom_id="btn_queue")
    async def btn_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.player.current and not self.player.queue:
            await interaction.response.send_message("📜 Hàng đợi hiện đang trống!", ephemeral=True)
            return
        lines = []
        if self.player.current:
            elapsed = self.player.get_elapsed()
            total = self.player.current.duration
            bar = progress_bar(elapsed, total)
            lines.append(f"**Đang phát:** [{self.player.current.title}]({self.player.current.webpage_url})")
            lines.append(f"`{bar}` `{format_duration(elapsed)} / {format_duration(total)}`\n")
        if self.player.queue:
            lines.append("**Bài chờ kế tiếp:**")
            for i, s in enumerate(self.player.queue[:8], 1):
                lines.append(f"`{i}.` [{s.title}]({s.webpage_url}) - `{format_duration(s.duration)}`")
            if len(self.player.queue) > 8:
                lines.append(f"*... và còn {len(self.player.queue) - 8} bài nữa trong hàng đợi*")
        embed = discord.Embed(
            title="📜 DANH SÁCH HÀNG ĐỢI ÂM NHẠC",
            description="\n".join(lines),
            color=0x3498db
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, custom_id="btn_stop")
    async def btn_stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.player.stop()
        await interaction.response.send_message("⏹️ **Đã dừng phát nhạc, xóa hàng đợi và rời phòng voice!**", ephemeral=True)

# ================= Music Cog & Slash Commands =================
class MusicCog(commands.Cog, name="Music"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _check_owner(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == AUTHORIZED_OWNER_ID

    @app_commands.command(name="play", description="🎵 Phát nhạc từ YouTube/SoundCloud hoặc theo từ khóa (Độc quyền anh Khang)")
    @app_commands.describe(query="Tên bài hát, từ khóa tìm kiếm hoặc đường link")
    async def play_command(self, interaction: discord.Interaction, query: str):
        if not self._check_owner(interaction):
            await interaction.response.send_message("⛔ **Từ chối:** Lệnh phát nhạc được khóa độc quyền cho anh Khang (`@khangmc_vn`)!", ephemeral=True)
            return
        if not interaction.guild:
            await interaction.response.send_message("❌ Lệnh này chỉ dùng được trong server!", ephemeral=True)
            return
        user = interaction.user
        if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
            await interaction.response.send_message("⚠️ Anh cần tham gia vào một phòng Voice trước khi gọi lệnh phát nhạc nha!", ephemeral=True)
            return
        voice_channel = user.voice.channel
        await interaction.response.defer(thinking=True)
        player = get_player(self.bot, interaction.guild)
        player.text_channel = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
        try:
            await player.connect_voice(voice_channel)
        except Exception as e:
            logger.error(f"[Music] Lỗi kết nối Voice: {e}")
            await interaction.followup.send(f"❌ Không thể kết nối vào kênh Voice: `{e}`")
            return
        song = await Song.search(query, requester=user, loop=self.bot.loop)
        if not song:
            await interaction.followup.send(f"❌ Không tìm thấy bài hát nào phù hợp với từ khóa: `{query}`")
            return
        player.queue.append(song)
        if not player.voice_client.is_playing() and not player.voice_client.is_paused():
            player.play_next()
            await interaction.followup.send(f"🎶 **Bắt đầu phát:** [{song.title}]({song.webpage_url}) (`{format_duration(song.duration)}`)")
        else:
            await interaction.followup.send(f"📥 **Đã thêm vào hàng đợi (#{len(player.queue)}):** [{song.title}]({song.webpage_url}) (`{format_duration(song.duration)}`)")

    @app_commands.command(name="skip", description="⏭️ Chuyển sang bài hát kế tiếp")
    async def skip_command(self, interaction: discord.Interaction):
        if not self._check_owner(interaction):
            await interaction.response.send_message("⛔ Lệnh chỉ dành riêng cho anh Khang!", ephemeral=True)
            return
        player = get_player(self.bot, interaction.guild)
        if player.voice_client and (player.voice_client.is_playing() or player.voice_client.is_paused()):
            player.voice_client.stop()
            await interaction.response.send_message("⏭️ **Đã bỏ qua bài hát hiện tại!**")
        else:
            await interaction.response.send_message("❌ Hiện không có bài hát nào đang phát.", ephemeral=True)

    @app_commands.command(name="pause", description="⏸️ Tạm dừng bài hát")
    async def pause_command(self, interaction: discord.Interaction):
        if not self._check_owner(interaction):
            await interaction.response.send_message("⛔ Lệnh chỉ dành riêng cho anh Khang!", ephemeral=True)
            return
        player = get_player(self.bot, interaction.guild)
        if player.voice_client and player.voice_client.is_playing():
            player.voice_client.pause()
            player.is_paused = True
            await interaction.response.send_message("⏸️ **Đã tạm dừng bài hát.**")
        else:
            await interaction.response.send_message("❌ Không có bài hát nào đang phát để tạm dừng.", ephemeral=True)

    @app_commands.command(name="resume", description="▶️ Tiếp tục phát nhạc")
    async def resume_command(self, interaction: discord.Interaction):
        if not self._check_owner(interaction):
            await interaction.response.send_message("⛔ Lệnh chỉ dành riêng cho anh Khang!", ephemeral=True)
            return
        player = get_player(self.bot, interaction.guild)
        if player.voice_client and player.voice_client.is_paused():
            player.voice_client.resume()
            player.is_paused = False
            await interaction.response.send_message("▶️ **Tiếp tục phát nhạc!**")
        else:
            await interaction.response.send_message("❌ Nhạc đang không ở trạng thái tạm dừng.", ephemeral=True)

    @app_commands.command(name="stop", description="⏹️ Dừng phát nhạc, xóa hàng đợi và rời phòng voice")
    async def stop_command(self, interaction: discord.Interaction):
        if not self._check_owner(interaction):
            await interaction.response.send_message("⛔ Lệnh chỉ dành riêng cho anh Khang!", ephemeral=True)
            return
        player = get_player(self.bot, interaction.guild)
        await player.stop()
        await interaction.response.send_message("⏹️ **Đã dừng phát nhạc, xóa hàng đợi và rời kênh Voice!**")

    @app_commands.command(name="queue", description="📜 Xem danh sách bài hát đang chờ phát")
    async def queue_command(self, interaction: discord.Interaction):
        if not self._check_owner(interaction):
            await interaction.response.send_message("⛔ Lệnh chỉ dành riêng cho anh Khang!", ephemeral=True)
            return
        player = get_player(self.bot, interaction.guild)
        if not player.current and not player.queue:
            await interaction.response.send_message("📜 Hàng đợi hiện đang trống!", ephemeral=True)
            return
        lines = []
        if player.current:
            elapsed = player.get_elapsed()
            total = player.current.duration
            lines.append(f"**Đang phát:** [{player.current.title}]({player.current.webpage_url})")
            lines.append(f"`{progress_bar(elapsed, total)}` `{format_duration(elapsed)} / {format_duration(total)}`\n")
        if player.queue:
            lines.append("**Danh sách chờ:**")
            for i, s in enumerate(player.queue[:10], 1):
                lines.append(f"`{i}.` [{s.title}]({s.webpage_url}) - `{format_duration(s.duration)}`")
            if len(player.queue) > 10:
                lines.append(f"*... và còn {len(player.queue) - 10} bài nữa trong danh sách*")
        embed = discord.Embed(title="📜 HÀNG ĐỢI ÂM NHẠC KHANGSMP", description="\n".join(lines), color=0x3498db)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="nowplaying", description="🎧 Xem chi tiết bài hát đang phát")
    async def np_command(self, interaction: discord.Interaction):
        if not self._check_owner(interaction):
            await interaction.response.send_message("⛔ Lệnh chỉ dành riêng cho anh Khang!", ephemeral=True)
            return
        player = get_player(self.bot, interaction.guild)
        if not player.current:
            await interaction.response.send_message("❌ Hiện không có bài hát nào đang phát.", ephemeral=True)
            return
        elapsed = player.get_elapsed()
        total = player.current.duration
        bar = progress_bar(elapsed, total, length=15)
        embed = discord.Embed(
            title="🎧 BÀI HÁT ĐANG PHÁT",
            description=f"[{player.current.title}]({player.current.webpage_url})\n\n`{bar}`\n`{format_duration(elapsed)} / {format_duration(total)}`",
            color=0x9b59b6
        )
        if player.current.thumbnail:
            embed.set_thumbnail(url=player.current.thumbnail)
        embed.add_field(name="🔊 Âm lượng", value=f"`{int(player.volume * 100)}%`", inline=True)
        embed.add_field(name="🔁 Lặp", value=f"`{player.loop_mode.upper()}`", inline=True)
        embed.add_field(name="👑 Yêu cầu bởi", value=player.current.requester.mention, inline=True)
        view = MusicControlView(player)
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="volume", description="🔊 Điều chỉnh âm lượng nhạc (1 - 100)")
    @app_commands.describe(level="Mức âm lượng từ 1 đến 100%")
    async def volume_command(self, interaction: discord.Interaction, level: int):
        if not self._check_owner(interaction):
            await interaction.response.send_message("⛔ Lệnh chỉ dành riêng cho anh Khang!", ephemeral=True)
            return
        level = max(1, min(100, level))
        player = get_player(self.bot, interaction.guild)
        player.volume = level / 100.0
        if player.voice_client and player.voice_client.source:
            if isinstance(player.voice_client.source, discord.PCMVolumeTransformer):
                player.voice_client.source.volume = player.volume
        await interaction.response.send_message(f"🔊 Đã đặt âm lượng thành: **{level}%**")

    @app_commands.command(name="loop", description="🔁 Cài đặt chế độ lặp lại bài hát")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Tắt lặp", value="off"),
        app_commands.Choice(name="Lặp bài hiện tại 🔂", value="song"),
        app_commands.Choice(name="Lặp cả hàng đợi 🔃", value="queue"),
    ])
    async def loop_command(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        if not self._check_owner(interaction):
            await interaction.response.send_message("⛔ Lệnh chỉ dành riêng cho anh Khang!", ephemeral=True)
            return
        player = get_player(self.bot, interaction.guild)
        player.loop_mode = mode.value
        await interaction.response.send_message(f"🔁 Đã chuyển chế độ lặp thành: **{mode.name}**")

    @app_commands.command(name="shuffle", description="🔀 Xáo trộn ngẫu nhiên danh sách hàng đợi")
    async def shuffle_command(self, interaction: discord.Interaction):
        if not self._check_owner(interaction):
            await interaction.response.send_message("⛔ Lệnh chỉ dành riêng cho anh Khang!", ephemeral=True)
            return
        player = get_player(self.bot, interaction.guild)
        if len(player.queue) < 2:
            await interaction.response.send_message("⚠️ Cần ít nhất 2 bài trong hàng đợi để xáo trộn!", ephemeral=True)
            return
        import random
        random.shuffle(player.queue)
        await interaction.response.send_message(f"🔀 Đã xáo trộn ngẫu nhiên **{len(player.queue)}** bài hát trong hàng đợi!")

async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
    logger.info("[Music] Đã nạp thành công MusicCog với khóa bảo mật độc quyền!")
