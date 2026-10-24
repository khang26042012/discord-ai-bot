import asyncio
import os
import re
import json
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
DEFAULT_VOICE_CHANNEL_ID = int(os.getenv('DEFAULT_VOICE_CHANNEL_ID', '1561433897928233024'))

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 15',
    'options': '-vn -loglevel error'
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
            'extractor_args': {
                'youtube': {
                    'player_client': ['android', 'ios']
                }
            }
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

# ================= Music Player Per Guild (24/7 Voice & Autoplay) =================
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
        self.autoplay: bool = True  # Mặc định luôn tự động đổi bài mới khi hết danh sách
        self.history: List[str] = []
        self.text_channel: Optional[discord.TextChannel] = None
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

    async def ensure_247_voice(self):
        """Cắm chốt 24/7 trong kênh Voice mặc định."""
        channel = self.bot.get_channel(DEFAULT_VOICE_CHANNEL_ID)
        if channel and isinstance(channel, discord.VoiceChannel):
            try:
                await self.connect_voice(channel)
                logger.info(f"[Music-24/7] Bot đã cắm chốt an toàn tại: '{channel.name}'")
            except Exception as e:
                logger.error(f"[Music-24/7] Lỗi cắm chốt voice: {e}")

    async def fetch_ai_next_song(self, current_title: str) -> Optional[Song]:
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=os.getenv("ROUTER_API_KEY", "sk-c276eac9b4322b74-tyrxri-1fc67c68"), base_url=os.getenv("ROUTER_BASE_URL", "http://nvnmc.asia:26627/v1"))
            hist_str = ", ".join(self.history[-6:]) if self.history else current_title
            prompt = f"Các bài hát vừa nghe gần đây: {hist_str}. Bài gần nhất là: '{current_title}'. Dựa vào phong cách, ca sĩ, giai điệu và thể loại của các bài trên, hãy gợi ý ĐÚNG 1 BÀI HÁT tiếp theo để phát nối tiếp (cùng gu nhưng KHÔNG ĐƯỢC TRÙNG với các bài trên, ngắn gọn chỉ trả về đúng tên bài hát và ca sĩ/remix, không có lời dẫn hay dấu ngoặc kép)."
            resp = await client.chat.completions.create(model=os.getenv("ROUTER_MODEL", "ag/gemini-3.8-flash-low"), messages=[{"role": "user", "content": prompt}], max_tokens=60)
            suggested = resp.choices[0].message.content.strip().replace('"', "").replace("'", "")
            if suggested:
                logger.info(f"[Radio] AI đề xuất bài tiếp theo: {suggested}")
                song = await Song.search(suggested, requester=self.bot.user, loop=self.bot.loop)
                return song
        except Exception as e:
            logger.error(f"[Radio] Lỗi gọi AI gợi ý bài: {e}")
        return None

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
            if self.current.title not in self.history:
                self.history.append(self.current.title)
                self.history = self.history[-15:]
            logger.info(f"[Music] Chuẩn bị phát: '{self.current.title}'")
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
                asyncio.run_coroutine_threadsafe(self.send_now_playing(), self.bot.loop)
            except Exception as e:
                logger.error(f"[Music] Lỗi khởi chạy phát nhạc: {e}")
                self.play_next()
        else:
            self.cancel_now_playing_msg()
            # Ở chế độ 24/7: Hết nhạc bot vẫn ở trong phòng voice, không bao giờ disconnect
            if self.text_channel:
                asyncio.run_coroutine_threadsafe(
                    self.text_channel.send("⏹️ **Hàng đợi đã hết.** Bot vẫn ở trong phòng voice 24/7 sẵn sàng nhận lệnh tiếp theo! 🎧"),
                    self.bot.loop
                )

    async def _after_song(self):
        # Tự động tìm và nối bài nếu bật Autoplay và không phải lặp 1 bài
        if not self.queue and self.autoplay and self.current and self.loop_mode != 'song':
            last_title = self.current.title
            next_song = await self.fetch_ai_next_song(last_title)
            if next_song:
                self.queue.append(next_song)
                if self.text_channel:
                    await self.text_channel.send(f"📻 **[AI Radio]** Tự động đổi sang bài tiếp theo: **{next_song.title}**")
        self.play_next()

    async def send_now_playing(self):
        if not self.text_channel or not self.current:
            return
        embed = discord.Embed(
            title="🎵 ĐANG PHÁT NHẠC (24/7 LIVE)",
            description=f"[{self.current.title}]({self.current.webpage_url})",
            color=0x9b59b6
        )
        if self.current.thumbnail:
            embed.set_thumbnail(url=self.current.thumbnail)

        dur_str = format_duration(self.current.duration)
        embed.add_field(name="⏱️ Thời lượng", value=f"`{dur_str}`", inline=True)
        embed.add_field(name="🔊 Âm lượng", value=f"`{int(self.volume * 100)}%`", inline=True)
        embed.add_field(name="🔁 Lặp", value=f"`{self.loop_mode.upper()}`", inline=True)
        embed.add_field(name="📻 Tự đổi bài", value=f"`{'BẬT' if self.autoplay else 'TẮT'}`", inline=True)
        embed.add_field(name="👑 Yêu cầu bởi", value=self.current.requester.mention, inline=True)

        if self.queue:
            embed.add_field(name="📜 Bài kế tiếp", value=f"**{self.queue[0].title}**", inline=False)

        embed.set_footer(text="Hệ Thống Âm Nhạc Độc Quyền KhangSMP 24/7 • Bấm nút bên dưới để điều khiển", icon_url=self.bot.user.display_avatar.url if self.bot.user else None)

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

    async def stop(self, disconnect: bool = False):
        self.queue.clear()
        self.current = None
        self.cancel_now_playing_msg()
        if self.voice_client and self.voice_client.is_connected():
            if self.voice_client.is_playing() or self.voice_client.is_paused():
                self.voice_client.stop()
            if disconnect:
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

    @discord.ui.button(emoji="📻", style=discord.ButtonStyle.secondary, custom_id="btn_autoplay")
    async def btn_autoplay(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player.autoplay = not self.player.autoplay
        st = "BẬT 🟢 (Tự tìm bài nối tiếp)" if self.player.autoplay else "TẮT 🔴"
        await interaction.response.send_message(f"📻 Chế độ Autoplay: **{st}**", ephemeral=True)

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
            for i, s in enumerate(player.queue[:8], 1):
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
        await self.player.stop(disconnect=False)
        await interaction.response.send_message("⏹️ **Đã dừng phát nhạc và xóa hàng đợi (vẫn giữ bot trong phòng voice 24/7)!**", ephemeral=True)

# ================= BỘ CÔNG CỤ TOOLS CHO AI (NATIVE TOOL CALLING) =================
MUSIC_TOOLS_DEFINITIONS = [
    {
        'type': 'function',
        'function': {
            'name': 'get_now_playing',
            'description': 'Lấy thông tin chi tiết bài hát đang phát hiện tại (tiêu đề, thời lượng, thời gian đã phát, uploader, link)',
            'parameters': {'type': 'object', 'properties': {}}
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_queue',
            'description': 'Lấy danh sách các bài hát đang chờ trong hàng đợi và trạng thái lặp/autoplay',
            'parameters': {'type': 'object', 'properties': {}}
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'play_music',
            'description': 'Tìm kiếm và phát một bài hát hoặc nhạc theo tâm trạng/thể loại (chỉ chủ nhân phantrongkhangg mới có quyền)',
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string', 'description': 'Tên bài hát, từ khóa tìm kiếm hoặc đường link (YouTube / SoundCloud)'}
                },
                'required': ['query']
            }
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'pause_music',
            'description': 'Tạm dừng bài hát đang phát (chỉ chủ nhân phantrongkhangg mới có quyền)',
            'parameters': {'type': 'object', 'properties': {}}
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'resume_music',
            'description': 'Tiếp tục phát nhạc khi đang tạm dừng (chỉ chủ nhân phantrongkhangg mới có quyền)',
            'parameters': {'type': 'object', 'properties': {}}
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'skip_music',
            'description': 'Bỏ qua bài hát hiện tại để chuyển sang bài tiếp theo (chỉ chủ nhân phantrongkhangg mới có quyền)',
            'parameters': {'type': 'object', 'properties': {}}
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'stop_music',
            'description': 'Dừng phát nhạc và xóa sạch danh sách hàng đợi (chỉ chủ nhân phantrongkhangg mới có quyền)',
            'parameters': {'type': 'object', 'properties': {}}
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'set_loop',
            'description': 'Cài đặt chế độ lặp lại bài hát (chỉ chủ nhân phantrongkhangg mới có quyền)',
            'parameters': {
                'type': 'object',
                'properties': {
                    'mode': {
                        'type': 'string',
                        'enum': ['off', 'song', 'queue'],
                        'description': "Chế độ lặp: off (tắt lặp), song (lặp đúng 1 bài), queue (lặp cả danh sách)"
                    }
                },
                'required': ['mode']
            }
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'set_volume',
            'description': 'Điều chỉnh âm lượng âm nhạc từ 1 đến 100% (chỉ chủ nhân phantrongkhangg mới có quyền)',
            'parameters': {
                'type': 'object',
                'properties': {
                    'level': {'type': 'integer', 'description': 'Mức âm lượng từ 1 đến 100'}
                },
                'required': ['level']
            }
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'set_autoplay',
            'description': 'Bật hoặc tắt chế độ AI Radio tự động đổi bài mới khi hết danh sách (chỉ chủ nhân phantrongkhangg mới có quyền)',
            'parameters': {
                'type': 'object',
                'properties': {
                    'enabled': {'type': 'boolean', 'description': 'True nếu muốn tự động nối bài 24/7, False nếu muốn dừng khi hết bài'}
                },
                'required': ['enabled']
            }
        }
    }
]

async def handle_music_tool_call(bot: commands.Bot, message: discord.Message, player: GuildMusicPlayer, tool_name: str, args: dict) -> dict:
    is_owner = (message.author.id == AUTHORIZED_OWNER_ID)
    
    if tool_name == 'get_now_playing':
        if not player.current:
            return {'status': 'idle', 'message': 'Hiện tại không có bài hát nào đang phát.'}
        elapsed = player.get_elapsed()
        total = player.current.duration
        return {
            'status': 'playing' if (player.voice_client and player.voice_client.is_playing()) else 'paused',
            'title': player.current.title,
            'uploader': player.current.uploader,
            'url': player.current.webpage_url,
            'elapsed': format_duration(elapsed),
            'duration': format_duration(total),
            'progress_bar': progress_bar(elapsed, total),
            'loop_mode': player.loop_mode,
            'autoplay': player.autoplay,
            'volume_percent': int(player.volume * 100),
            'requester': getattr(player.current.requester, 'display_name', str(player.current.requester))
        }

    if tool_name == 'get_queue':
        cur_title = player.current.title if player.current else None
        queue_items = [{'index': i, 'title': s.title, 'duration': format_duration(s.duration)} for i, s in enumerate(player.queue[:10], 1)]
        return {
            'current_song': cur_title,
            'queue_count': len(player.queue),
            'queue_preview': queue_items,
            'loop_mode': player.loop_mode,
            'autoplay': player.autoplay
        }

    if not is_owner:
        return {
            'success': False,
            'error': 'Từ chối: Chỉ có chủ nhân phantrongkhangg (@khangmc_vn) mới được phép điều khiển phát/dừng/chuyển nhạc! Người khác chỉ có quyền xem thông tin bài hát.'
        }

    if tool_name == 'play_music':
        query = args.get('query', '').strip()
        if not query:
            return {'success': False, 'error': 'Thiếu tên bài hát.'}
        voice_ch = getattr(message.author.voice, 'channel', None)
        if not voice_ch and player.voice_client and player.voice_client.is_connected():
            voice_ch = player.voice_client.channel
        if not voice_ch:
            voice_ch = bot.get_channel(DEFAULT_VOICE_CHANNEL_ID)
        if voice_ch:
            player.text_channel = message.channel if isinstance(message.channel, discord.TextChannel) else None
            try:
                await player.connect_voice(voice_ch)
            except Exception as e:
                return {'success': False, 'error': f'Lỗi kết nối voice: {e}'}
        song = await Song.search(query, requester=message.author, loop=bot.loop)
        if not song:
            return {'success': False, 'error': f'Không tìm thấy bài hát nào cho: {query}'}
        player.queue.append(song)
        player.autoplay = True
        player.loop_mode = 'off'
        if not player.voice_client.is_playing() and not player.voice_client.is_paused():
            player.play_next()
            return {'success': True, 'action': 'playing_now', 'title': song.title, 'duration': format_duration(song.duration)}
        else:
            return {'success': True, 'action': 'queued', 'title': song.title, 'position': len(player.queue), 'duration': format_duration(song.duration)}

    if tool_name == 'pause_music':
        if player.voice_client and player.voice_client.is_playing():
            player.voice_client.pause()
            player.is_paused = True
            return {'success': True, 'message': f'Đã tạm dừng bài hát: {player.current.title if player.current else ""}'}
        return {'success': False, 'message': 'Không có bài hát nào đang phát để tạm dừng.'}

    if tool_name == 'resume_music':
        if player.voice_client and player.voice_client.is_paused():
            player.voice_client.resume()
            player.is_paused = False
            return {'success': True, 'message': f'Đã tiếp tục phát nhạc: {player.current.title if player.current else ""}'}
        return {'success': False, 'message': 'Nhạc đang không ở trạng thái tạm dừng.'}

    if tool_name == 'skip_music':
        if player.voice_client and (player.voice_client.is_playing() or player.voice_client.is_paused()):
            old_title = player.current.title if player.current else 'bài hát'
            player.voice_client.stop()
            return {'success': True, 'message': f'Đã bỏ qua bài: {old_title}'}
        return {'success': False, 'message': 'Không có bài nào đang phát để bỏ qua.'}

    if tool_name == 'stop_music':
        await player.stop(disconnect=False)
        return {'success': True, 'message': 'Đã dừng phát nhạc và xóa sạch hàng đợi (bot vẫn ở trong phòng voice 24/7).'}

    if tool_name == 'set_loop':
        mode = args.get('mode', 'off').lower()
        if mode in ['off', 'song', 'queue']:
            player.loop_mode = mode
            if mode == 'song':
                player.autoplay = False
            return {'success': True, 'loop_mode': mode}
        return {'success': False, 'error': f'Chế độ lặp không hợp lệ: {mode}'}

    if tool_name == 'set_volume':
        level = max(1, min(100, int(args.get('level', 80))))
        player.volume = level / 100.0
        if player.voice_client and player.voice_client.source and isinstance(player.voice_client.source, discord.PCMVolumeTransformer):
            player.voice_client.source.volume = player.volume
        return {'success': True, 'volume': level}

    if tool_name == 'set_autoplay':
        enabled = bool(args.get('enabled', True))
        player.autoplay = enabled
        if enabled:
            player.loop_mode = 'off'
        return {'success': True, 'autoplay': enabled}

    return {'success': False, 'error': f'Tool không xác định: {tool_name}'}

# ================= Music Cog & Slash Commands =================
class MusicCog(commands.Cog, name="Music"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _check_owner(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == AUTHORIZED_OWNER_ID

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        # Tự động kết nối lại 24/7 nếu bot bị rớt khỏi phòng voice
        if member.id == self.bot.user.id and after.channel is None:
            logger.warning("[Music-24/7] Phát hiện bot bị ngắt kết nối voice! Đang tự động kết nối lại sau 3s...")
            await asyncio.sleep(3)
            player = get_player(self.bot, member.guild)
            await player.ensure_247_voice()

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
        player.autoplay = True
        player.loop_mode = 'off'
        if not player.voice_client.is_playing() and not player.voice_client.is_paused():
            player.play_next()
            await interaction.followup.send(f"🎶 **Bắt đầu phát:** [{song.title}]({song.webpage_url}) (`{format_duration(song.duration)}`)")
        else:
            await interaction.followup.send(f"📥 **Đã thêm vào hàng đợi (#{len(player.queue)}):** [{song.title}]({song.webpage_url}) (`{format_duration(song.duration)}`)")

    @app_commands.command(name="radio", description="📻 Bật đài Radio AI: Tự động đổi bài liên tục theo tâm trạng/gu nhạc xuyên đêm")
    @app_commands.describe(query="Tâm trạng (buồn, vui, chill, vinahouse, lofi...) hoặc tên bài hát mở đầu")
    async def radio_command(self, interaction: discord.Interaction, query: Optional[str] = None):
        if not self._check_owner(interaction):
            await interaction.response.send_message("⛔ Lệnh chỉ dành riêng cho anh Khang (`@khangmc_vn`)!", ephemeral=True)
            return
        if not interaction.guild:
            await interaction.response.send_message("❌ Lệnh này chỉ dùng được trong server!", ephemeral=True)
            return
        user = interaction.user
        if not isinstance(user, discord.Member) or not user.voice or not user.voice.channel:
            await interaction.response.send_message("⚠️ Anh cần tham gia vào một phòng Voice trước nha!", ephemeral=True)
            return
        voice_channel = user.voice.channel
        await interaction.response.defer(thinking=True)
        player = get_player(self.bot, interaction.guild)
        player.text_channel = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
        player.autoplay = True
        player.loop_mode = "off"
        try:
            await player.connect_voice(voice_channel)
        except Exception as e:
            logger.error(f"[Music] Lỗi kết nối Voice: {e}")
            await interaction.followup.send(f"❌ Không thể kết nối vào kênh Voice: {e}")
            return
        target_query = query
        if not target_query:
            if player.current:
                await interaction.followup.send(f"📻 **Đã bật Chế độ Radio AI!** Bot sẽ tự động đổi bài tương tự sau khi bài **{player.current.title}** kết thúc.")
                return
            else:
                target_query = "nhạc lofi chill suy ngẫm"
        song = await Song.search(target_query, requester=user, loop=self.bot.loop)
        if not song:
            await interaction.followup.send(f"❌ Không tìm thấy bài hát nào cho: {target_query}")
            return
        player.queue.append(song)
        if not player.voice_client.is_playing() and not player.voice_client.is_paused():
            player.play_next()
            await interaction.followup.send(f"📻 **Đã kích hoạt Đài Radio AI!**\n🎶 **Bắt đầu với:** [{song.title}]({song.webpage_url})\n*(Bot sẽ tự động đề xuất và đổi bài liên tục theo gu xuyên đêm)*")
        else:
            await interaction.followup.send(f"📻 **Đã kích hoạt Đài Radio AI!** Bài [{song.title}]({song.webpage_url}) đã được xếp vào hàng đợi.")

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

    @app_commands.command(name="stop", description="⏹️ Dừng phát nhạc và xóa hàng đợi (vẫn giữ bot 24/7)")
    async def stop_command(self, interaction: discord.Interaction):
        if not self._check_owner(interaction):
            await interaction.response.send_message("⛔ Lệnh chỉ dành riêng cho anh Khang!", ephemeral=True)
            return
        player = get_player(self.bot, interaction.guild)
        await player.stop(disconnect=False)
        await interaction.response.send_message("⏹️ **Đã dừng phát nhạc và xóa hàng đợi (bot vẫn cắm chốt trong phòng voice 24/7)!**")

    @app_commands.command(name="autoplay", description="📻 Bật/Tắt chế độ tự động nối bài khi hết hàng đợi")
    async def autoplay_command(self, interaction: discord.Interaction):
        if not self._check_owner(interaction):
            await interaction.response.send_message("⛔ Lệnh chỉ dành riêng cho anh Khang!", ephemeral=True)
            return
        player = get_player(self.bot, interaction.guild)
        player.autoplay = not player.autoplay
        st = "BẬT 🟢 (Tự động đề xuất & phát tiếp liên tục)" if player.autoplay else "TẮT 🔴"
        await interaction.response.send_message(f"📻 Chế độ AI Autoplay: **{st}**")

    @app_commands.command(name="queue", description="📜 Xem danh sách bài hát đang chờ phát")
    async def queue_command(self, interaction: discord.Interaction):
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
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="nowplaying", description="🎧 Xem chi tiết bài hát đang phát")
    async def np_command(self, interaction: discord.Interaction):
        player = get_player(self.bot, interaction.guild)
        if not player.current:
            await interaction.response.send_message("❌ Hiện không có bài hát nào đang phát.", ephemeral=True)
            return
        elapsed = player.get_elapsed()
        total = player.current.duration
        bar = progress_bar(elapsed, total, length=15)
        embed = discord.Embed(
            title="🎧 BÀI HÁT ĐANG PHÁT (24/7 LIVE)",
            description=f"[{player.current.title}]({player.current.webpage_url})\n\n`{bar}`\n`{format_duration(elapsed)} / {format_duration(total)}`",
            color=0x9b59b6
        )
        if player.current.thumbnail:
            embed.set_thumbnail(url=player.current.thumbnail)
        embed.add_field(name="🔊 Âm lượng", value=f"`{int(player.volume * 100)}%`", inline=True)
        embed.add_field(name="🔁 Lặp", value=f"`{player.loop_mode.upper()}`", inline=True)
        embed.add_field(name="📻 Tự đổi bài", value=f"`{'BẬT' if player.autoplay else 'TẮT'}`", inline=True)
        embed.add_field(name="👑 Yêu cầu bởi", value=player.current.requester.mention, inline=True)
        view = MusicControlView(player)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

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
    logger.info("[Music] Đã nạp thành công MusicCog với 24/7 Voice & Native Tool Calling!")
