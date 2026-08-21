import os
import re
import json
import asyncio
import logging
import random
import aiohttp
import discord
from discord.ext import commands
from discord import app_commands
from typing import Dict, List, Optional, Set, Any

logger = logging.getLogger("NoiTuGame")

# Environment variables
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
NOITU_CHANNEL_ID_RAW = os.getenv("NOITU_CHANNEL_ID")
NOITU_CHANNEL_ID = int(NOITU_CHANNEL_ID_RAW) if NOITU_CHANNEL_ID_RAW and NOITU_CHANNEL_ID_RAW.isdigit() else None

class GroqClient:
    """Async Groq API helper using aiohttp."""
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://api.groq.com/openai/v1/chat/completions"

    async def chat_completion(self, messages: List[Dict[str, str]], json_mode: bool = True, retries: int = 1) -> Optional[str]:
        if not self.api_key:
            logger.error("GROQ_API_KEY is not configured.")
            return None

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }
        data: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.5
        }
        if json_mode:
            data["response_format"] = {"type": "json_object"}

        for attempt in range(retries + 1):
            try:
                logger.info(f"Sending Groq API Request (Attempt {attempt+1}/{retries+1}) | Model: {self.model}")
                logger.debug(f"Groq Messages: {json.dumps(messages, ensure_ascii=False)}")
                async with aiohttp.ClientSession() as session:
                    async with session.post(self.base_url, headers=headers, json=data, timeout=12) as resp:
                        raw_text = await resp.text()
                        logger.info(f"Groq API Response Status: {resp.status} | Raw Content: {raw_text}")
                        if resp.status != 200:
                            logger.error(f"Groq API error HTTP {resp.status}: {raw_text}")
                            if attempt < retries:
                                await asyncio.sleep(1)
                                continue
                            return None

                        res_json = json.loads(raw_text)
                        content = res_json["choices"][0]["message"]["content"]
                        # Clean markdown
                        content = re.sub(r"^```json\s*", "", content, flags=re.MULTILINE)
                        content = re.sub(r"^```\s*", "", content, flags=re.MULTILINE)
                        content = re.sub(r"```$", "", content, flags=re.MULTILINE).strip()
                        return content
            except Exception as e:
                logger.error(f"Exception calling Groq API (Attempt {attempt+1}): {e}")
                if attempt < retries:
                    await asyncio.sleep(1)
                    continue
                return None
        return None

    async def validate_starter_phrase(self, phrase: str) -> Dict[str, Any]:
        """Validate starter phrase: 2 Vietnamese syllables."""
        sys_prompt = """Bạn là trọng tài trò chơi Nối Từ Tiếng Việt.
Nhiệm vụ: Kiểm tra cụm từ ra đề của người chơi.
YÊU CẦU BẮT BUỘC:
1. Cụm từ phải đúng CỤM 2 TIẾNG TIẾNG VIỆT CƠ BẢN, THÔNG DỤNG (có nghĩa trong tiếng Việt).
2. Tránh từ Hán Việt quá hiếm, từ chuyên ngành.
3. Trả về đúng định dạng JSON:
{
  "valid": true/false,
  "reason": "Lý do ngắn gọn nếu không hợp lệ",
  "last_syllable": "Tiếng thứ 2 của cụm từ (nếu valid=true)"
}"""
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"Kiểm tra cụm từ ra đề: '{phrase}'"}
        ]
        res = await self.chat_completion(messages, json_mode=True)
        words = phrase.strip().split()
        if not res:
            if len(words) == 2:
                return {"valid": True, "reason": "", "last_syllable": words[1]}
            return {"valid": False, "reason": "Cụm từ phải gồm đúng 2 tiếng tiếng Việt.", "last_syllable": None}

        try:
            data = json.loads(res)
            return {
                "valid": bool(data.get("valid", False)),
                "reason": str(data.get("reason", "Cụm từ không hợp lệ.")),
                "last_syllable": data.get("last_syllable") or (words[1] if len(words) == 2 else None)
            }
        except Exception as e:
            logger.error(f"JSON parse error in validate_starter_phrase: {e}")
            return {"valid": len(words) == 2, "reason": "Cụm từ phải đúng 2 tiếng.", "last_syllable": words[1] if len(words) == 2 else None}

    async def validate_and_next_singleplayer(self, current_word: str, expected_first_syllable: str, used_words: Set[str], is_starter: bool = False) -> Dict[str, Any]:
        """Validate player word and generate AI response for Singleplayer mode."""
        used_list_str = ", ".join(list(used_words))
        
        if is_starter:
            # When current_word is starter phrase (e.g. "Trồng cây"), current_word IS valid already.
            # AI just needs to generate the next word starting with expected_first_syllable (e.g. "cây")
            sys_prompt = f"""Bạn là đối thủ trò chơi Nối Từ Tiếng Việt.
Người chơi vừa ra đề bằng cụm từ: '{current_word}'.
Nhiệm vụ của bạn:
1. Tìm 1 CỤM 2 TIẾNG TIẾNG VIỆT CƠ BẢN, THÔNG DỤNG để nối tiếp.
2. Cụm từ của bạn BẮT BUỘC phải bắt đầu bằng tiếng: '{expected_first_syllable}'.
3. Cụm từ của bạn KHÔNG ĐƯỢC trùng với danh sách đã dùng: [{used_list_str}].
4. Tránh từ Hán Việt quá hiếm. Chỉ kèm giải nghĩa ngắn trong ngoặc nếu dùng từ khó.

Trả về duy nhất định dạng JSON:
{{
  "valid": true,
  "reason": "",
  "ai_word": "Cụm 2 tiếng nối tiếp của AI",
  "ai_last_syllable": "Tiếng thứ 2 trong cụm từ của AI"
}}"""
            messages = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": f"Ra đề: '{current_word}'. Bạn hãy nối tiếp từ bắt đầu bằng '{expected_first_syllable}'."}
            ]
        else:
            sys_prompt = f"""Bạn là trọng tài và đối thủ trò chơi Nối Từ Tiếng Việt.
QUY TẮC BẮT BUỘC CHO NGƯỜI CHƠI:
1. Cụm từ phải gồm đúng CỤM 2 TIẾNG TIẾNG VIỆT.
2. Tiếng thứ nhất BẮT BUỘC phải khớp chính xác với: '{expected_first_syllable}'.
3. Cụm từ KHÔNG ĐƯỢC trùng với danh sách đã dùng: [{used_list_str}].
4. Cụm từ phải có nghĩa thực tế trong tiếng Việt thông dụng.

NẾU CỤM TỪ CỦA NGƯỜI CHƠI HỢP LỆ (valid=true):
- Lấy tiếng thứ 2 trong cụm từ của người chơi làm tiếng đầu cho lượt của bạn.
- Bạn hãy tìm 1 CỤM 2 TIẾNG TIẾNG VIỆT CƠ BẢN, THÔNG DỤNG, DỄ NỐI TIẾP để nối lại.
- Tránh từ Hán Việt hiếm, từ chuyên ngành. Chỉ dùng từ khó hơn khi BẮT BUỘC, và kèm chú thích nghĩa ngắn trong ngoặc, ví dụ: 'kỳ dị (lạ lùng)'.
- Cụm từ của bạn KHÔNG ĐƯỢC trùng với danh sách đã dùng: [{used_list_str}].

Trả về duy nhất định dạng JSON:
{{
  "valid": true/false,
  "reason": "Lý do nếu không hợp lệ (không khớp từ / đã dùng / không có nghĩa / không đủ 2 tiếng)",
  "ai_word": "Cụm 2 tiếng nối tiếp của AI (nếu valid=true)",
  "ai_last_syllable": "Tiếng thứ 2 trong cụm từ của AI"
}}"""
            messages = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": f"Người chơi gửi: '{current_word}'"}
            ]

        res = await self.chat_completion(messages, json_mode=True)
        if not res:
            return {"valid": False, "reason": "Không thể kết nối API AI (timeout/network error)."}

        try:
            return json.loads(res)
        except Exception as e:
            logger.error(f"JSON parse error in validate_and_next_singleplayer: {e} | Raw content: {res}")
            return {"valid": False, "reason": "Lỗi định dạng dữ liệu kiểm tra từ AI."}

    async def validate_multiplayer_word(self, current_word: str, expected_first_syllable: str, used_words: Set[str]) -> Dict[str, Any]:
        """Validate player word in Multiplayer mode."""
        used_list_str = ", ".join(list(used_words))
        sys_prompt = f"""Bạn là trọng tài trò chơi Nối Từ Tiếng Việt.
QUY TẮC BẮT BUỘC:
1. Cụm từ phải gồm đúng CỤM 2 TIẾNG TIẾNG VIỆT CƠ BẢN, THÔNG DỤNG.
2. Tiếng thứ nhất BẮT BUỘC phải khớp chính xác với: '{expected_first_syllable}'.
3. Cụm từ KHÔNG ĐƯỢC trùng với danh sách đã dùng: [{used_list_str}].
4. Cụm từ phải có nghĩa trong tiếng Việt.

Trả về duy nhất định dạng JSON:
{{
  "valid": true/false,
  "reason": "Lý do ngắn gọn nếu sai (không đúng tiếng đầu / đã dùng / không có nghĩa / không đủ 2 tiếng)",
  "last_syllable": "Tiếng thứ 2 của cụm từ vừa gửi (nếu valid=true)"
}}"""
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"Người chơi gửi: '{current_word}'"}
        ]
        res = await self.chat_completion(messages, json_mode=True)
        words = current_word.strip().split()
        if not res:
            if len(words) == 2 and words[0].lower() == expected_first_syllable.lower() and current_word.lower() not in [w.lower() for w in used_words]:
                return {"valid": True, "reason": "", "last_syllable": words[1]}
            return {"valid": False, "reason": "Cụm từ không hợp lệ.", "last_syllable": None}

        try:
            data = json.loads(res)
            return {
                "valid": bool(data.get("valid", False)),
                "reason": str(data.get("reason", "Cụm từ không hợp lệ.")),
                "last_syllable": data.get("last_syllable") or (words[1] if len(words) == 2 else None)
            }
        except Exception as e:
            logger.error(f"JSON parse error in validate_multiplayer_word: {e}")
            return {"valid": False, "reason": "Lỗi định dạng kiểm tra từ AI."}


class NoiTuGameState:
    """State tracking for a game in a channel."""
    def __init__(self, channel_id: int):
        self.channel_id = channel_id
        self.status = "LOBBY"  # LOBBY, WAITING_STARTER, PLAYING, ENDED
        self.lobby_participants: List[int] = []  # User IDs in lobby
        self.players: List[int] = []  # Remaining active player IDs
        self.eliminated_players: List[Dict[str, Any]] = []  # [{user_id, order, reason}]
        self.is_single_player = False

        self.current_turn_user_id: Optional[int] = None
        self.turn_index = 0
        self.last_syllable: Optional[str] = None  # Syllable needed for next word
        self.used_words: Set[str] = set()
        self.used_words_history: List[str] = []

        self.timer_task: Optional[asyncio.Task] = None
        self.message_lock = asyncio.Lock()


# Store active games per channel_id
game_states: Dict[int, NoiTuGameState] = {}


class LobbyJoinView(discord.ui.View):
    def __init__(self, channel_id: int, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.channel_id = channel_id

    @discord.ui.button(label="🙋 Tham gia", style=discord.ButtonStyle.primary, custom_id="noitu_join_button")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = game_states.get(self.channel_id)
        if not state or state.status != "LOBBY":
            await interaction.response.send_message("❌ Ván chơi hiện tại không ở trong phòng chờ!", ephemeral=True)
            return

        user_id = interaction.user.id
        if user_id in state.lobby_participants:
            await interaction.response.send_message("⚠️ Bạn đã tham gia phòng chờ rồi!", ephemeral=True)
            return

        state.lobby_participants.append(user_id)
        count = len(state.lobby_participants)
        await interaction.response.send_message(f"✅ {interaction.user.mention} đã tham gia ván chơi! (Tổng: **{count}** người)", ephemeral=False)


def get_groq_client() -> GroqClient:
    return GroqClient(api_key=GROQ_API_KEY, model=GROQ_MODEL)


async def start_noitu_game(interaction: discord.Interaction):
    """Handler for /noitu start slash command."""
    channel_id = interaction.channel_id

    # Check allowed channel restriction
    if NOITU_CHANNEL_ID and channel_id != NOITU_CHANNEL_ID:
        allowed_channel = interaction.guild.get_channel(NOITU_CHANNEL_ID) if interaction.guild else None
        mention = allowed_channel.mention if allowed_channel else f"<#{NOITU_CHANNEL_ID}>"
        await interaction.response.send_message(f"❌ Lệnh Nối Từ chỉ được sử dụng trong kênh {mention}!", ephemeral=True)
        return

    # Check existing game state
    if channel_id in game_states and game_states[channel_id].status != "ENDED":
        await interaction.response.send_message("⚠️ Đang có một ván Nối Từ đang diễn ra hoặc ở phòng chờ trong kênh này!", ephemeral=True)
        return

    # Create new game state
    state = NoiTuGameState(channel_id)
    game_states[channel_id] = state

    # Send embed & join button
    embed = discord.Embed(
        title="🎮 MINIGAME NỐI TỪ TIẾNG VIỆT",
        description=(
            "**Luật chơi:**\n"
            "• Nối bằng cụm **2 tiếng tiếng Việt** cơ bản, thông dụng.\n"
            "• Tiếng đầu của cụm sau phải **trùng khớp** với tiếng cuối của cụm trước.\n"
            "• **Không trùng** với các cụm từ đã được sử dụng trong ván.\n"
            "• **Thời gian trả lời:** 20 giây / lượt. Quá giờ sẽ bị loại / thua cuộc!\n\n"
            "👉 Bấm nút **🙋 Tham gia** bên dưới! Phòng chờ mở trong **30 giây**."
        ),
        color=discord.Color.gold()
    )
    embed.set_footer(text="Hệ thống chấm/ra đề tự động bởi Groq AI")

    view = LobbyJoinView(channel_id, timeout=30.0)
    await interaction.response.send_message(embed=embed, view=view)

    # Automatically add creator to lobby
    state.lobby_participants.append(interaction.user.id)

    # Wait 30 seconds for lobby
    await asyncio.sleep(30)

    # Check if lobby still exists
    if game_states.get(channel_id) != state or state.status != "LOBBY":
        return

    # Finalize lobby participants
    participants = list(dict.fromkeys(state.lobby_participants)) # remove duplicates keeping order
    if not participants:
        await interaction.channel.send("❌ Không có ai tham gia. Ván chơi bị hủy!")
        game_states.pop(channel_id, None)
        return

    if len(participants) == 1:
        # 5A: Singleplayer vs AI
        state.is_single_player = True
        state.players = participants
        state.status = "WAITING_STARTER"
        state.current_turn_user_id = participants[0]

        user_mention = f"<@{participants[0]}>"
        await interaction.channel.send(
            f"🎮 **BẮT ĐẦU VÁN CHƠI 1 NGƯỜI (ĐẤU VỚI AI)**\n"
            f"👉 {user_mention}, tới lượt bạn! Hãy **ra đề** bằng 1 cụm 2 tiếng! (Bạn có 20 giây)"
        )
        # Start timer for starter word
        state.timer_task = asyncio.create_task(singleplayer_timeout_handler(interaction.channel, state, participants[0]))

    else:
        # 5B: Multiplayer
        state.is_single_player = False
        # Randomize order
        random.shuffle(participants)
        state.players = participants
        state.status = "WAITING_STARTER"
        state.turn_index = 0
        starter_id = participants[0]
        state.current_turn_user_id = starter_id

        player_mentions = ", ".join([f"<@{p}>" for p in participants])
        await interaction.channel.send(
            f"🎲 **BẮT ĐẦU VÁN CHƠI NHIỀU NGƯỜI**\n"
            f"👥 **Thứ tự lượt chơi ngẫu nhiên:** {player_mentions}\n"
            f"👉 Người đầu tiên <@{starter_id}> có 20 giây để **ra đề** bằng 1 cụm 2 tiếng!"
        )
        # Start timer for starter word
        state.timer_task = asyncio.create_task(multiplayer_timeout_handler(interaction.channel, state, starter_id))


async def singleplayer_timeout_handler(channel: discord.TextChannel, state: NoiTuGameState, user_id: int):
    """Timeout handler for singleplayer mode (20 seconds)."""
    try:
        await asyncio.sleep(20)
        async with state.message_lock:
            if state.status in ["ENDED"] or state.current_turn_user_id != user_id:
                return

            await channel.send(f"⏰ <@{user_id}> đã **quá 20 giây** không đưa ra cụm từ hợp lệ! Bạn đã THUA CUỘC.")
            await finish_game(channel, state, winner_text="Groq AI 🤖")
    except asyncio.CancelledError:
        pass


async def multiplayer_timeout_handler(channel: discord.TextChannel, state: NoiTuGameState, user_id: int):
    """Timeout handler for multiplayer mode (20 seconds)."""
    try:
        await asyncio.sleep(20)
        async with state.message_lock:
            if state.status in ["ENDED"] or state.current_turn_user_id != user_id:
                return

            await channel.send(f"⏰ <@{user_id}> đã **quá 20 giây**! <@{user_id}> BỊ LOẠI!")
            state.eliminated_players.append({
                "user_id": user_id,
                "order": len(state.eliminated_players) + 1,
                "reason": "Quá 20 giây"
            })
            if user_id in state.players:
                state.players.remove(user_id)

            # Check remaining players
            if len(state.players) == 1:
                winner_id = state.players[0]
                await finish_game(channel, state, winner_text=f"<@{winner_id}>")
            elif len(state.players) == 0:
                await finish_game(channel, state, winner_text="Không có ai")
            else:
                # Next player's turn
                if state.turn_index >= len(state.players):
                    state.turn_index = 0
                next_user_id = state.players[state.turn_index]
                state.current_turn_user_id = next_user_id

                if state.status == "WAITING_STARTER":
                    await channel.send(f"👉 Chuyển lượt ra đề cho <@{next_user_id}>! Bạn có 20 giây.")
                    state.timer_task = asyncio.create_task(multiplayer_timeout_handler(channel, state, next_user_id))
                else:
                    await channel.send(f"👉 Tới lượt <@{next_user_id}>! Cần nối bằng từ bắt đầu bằng **'{state.last_syllable}'** (Bạn có 20 giây)")
                    state.timer_task = asyncio.create_task(multiplayer_timeout_handler(channel, state, next_user_id))
    except asyncio.CancelledError:
        pass


async def handle_noitu_message(message: discord.Message):
    """Process message in NOITU_CHANNEL_ID."""
    channel_id = message.channel.id

    if NOITU_CHANNEL_ID and channel_id != NOITU_CHANNEL_ID:
        return

    state = game_states.get(channel_id)
    if not state or state.status in ["LOBBY", "ENDED"]:
        return

    # Ignore messages not from current turn user
    if message.author.id != state.current_turn_user_id:
        return

    async with state.message_lock:
        # Cancel active timer task
        if state.timer_task and not state.timer_task.done():
            state.timer_task.cancel()

        groq_client = get_groq_client()
        content = message.content.strip()

        # ================= 5A: SINGLEPLAYER (USER vs AI) =================
        if state.is_single_player:
            if state.status == "WAITING_STARTER":
                # Validate starter phrase
                val = await groq_client.validate_starter_phrase(content)
                if not val["valid"]:
                    await message.add_reaction("❌")
                    await message.reply(f"❌ {message.author.mention} Đề không hợp lệ: {val['reason']}. Vui lòng ra đề khác (cụm 2 tiếng)!")
                    state.timer_task = asyncio.create_task(singleplayer_timeout_handler(message.channel, state, message.author.id))
                    return

                await message.add_reaction("✅")
                state.used_words.add(content.lower())
                state.used_words_history.append(f"<@{message.author.id}>: {content}")
                words = content.split()
                expected_last_syllable = words[-1]
                state.status = "PLAYING"

                # AI turn to respond to starter phrase: expected first syllable is the last word of starter phrase!
                ai_res = await groq_client.validate_and_next_singleplayer(content, expected_last_syllable, state.used_words, is_starter=True)
                if not ai_res.get("valid") or not ai_res.get("ai_word"):
                    reason_msg = ai_res.get("reason", "")
                    logger.warning(f"AI failed to respond to starter phrase '{content}': {reason_msg}")
                    await message.channel.send(f"🎉 **AI KHÔNG NỐI TIẾP ĐƯỢC CỤM TỪ!** {message.author.mention} ĐÃ THẮNG CUỘC!")
                    await finish_game(message.channel, state, winner_text=f"{message.author.mention}")
                    return

                ai_word = ai_res["ai_word"]
                state.used_words.add(ai_word.lower())
                state.used_words_history.append(f"🤖 Groq AI: {ai_word}")
                state.last_syllable = ai_word.strip().split()[-1]

                await message.channel.send(f"🤖 **Groq AI:** `{ai_word}`")
                await message.channel.send(f"👉 Tới lượt {message.author.mention}! Cần nối cụm từ bắt đầu bằng **'{state.last_syllable}'** (Có 20 giây)")
                state.timer_task = asyncio.create_task(singleplayer_timeout_handler(message.channel, state, message.author.id))

            elif state.status == "PLAYING":
                # Validate player response phrase
                val = await groq_client.validate_and_next_singleplayer(content, state.last_syllable, state.used_words)
                if not val["valid"]:
                    await message.add_reaction("❌")
                    await message.reply(f"❌ {message.author.mention} Vui lòng gửi lại từ nối khác! Lý do: {val['reason']}")
                    state.timer_task = asyncio.create_task(singleplayer_timeout_handler(message.channel, state, message.author.id))
                    return

                await message.add_reaction("✅")
                state.used_words.add(content.lower())
                state.used_words_history.append(f"<@{message.author.id}>: {content}")

                ai_word = val.get("ai_word")
                if not ai_word:
                    await message.channel.send(f"🎉 **AI KHÔNG NỐI TIẾP ĐƯỢC CỤM TỪ!** {message.author.mention} ĐÃ THẮNG CUỘC!")
                    await finish_game(message.channel, state, winner_text=f"{message.author.mention}")
                    return

                state.used_words.add(ai_word.lower())
                state.used_words_history.append(f"🤖 Groq AI: {ai_word}")
                state.last_syllable = ai_word.strip().split()[-1]

                await message.channel.send(f"🤖 **Groq AI:** `{ai_word}`")
                await message.channel.send(f"👉 Tới lượt {message.author.mention}! Cần nối cụm từ bắt đầu bằng **'{state.last_syllable}'** (Có 20 giây)")
                state.timer_task = asyncio.create_task(singleplayer_timeout_handler(message.channel, state, message.author.id))

        # ================= 5B: MULTIPLAYER =================
        else:
            if state.status == "WAITING_STARTER":
                val = await groq_client.validate_starter_phrase(content)
                if not val["valid"]:
                    await message.add_reaction("❌")
                    await message.reply(f"❌ {message.author.mention} Đề không hợp lệ: {val['reason']}. Vui lòng ra đề khác (cụm 2 tiếng)!")
                    state.timer_task = asyncio.create_task(multiplayer_timeout_handler(message.channel, state, message.author.id))
                    return

                await message.add_reaction("✅")
                state.used_words.add(content.lower())
                state.used_words_history.append(f"<@{message.author.id}>: {content}")
                state.last_syllable = val["last_syllable"]
                state.status = "PLAYING"

                # Next player turn
                state.turn_index = (state.turn_index + 1) % len(state.players)
                next_user_id = state.players[state.turn_index]
                state.current_turn_user_id = next_user_id

                await message.channel.send(f"👉 Tới lượt <@{next_user_id}>! Cần nối bằng từ bắt đầu bằng **'{state.last_syllable}'** (Có 20 giây)")
                state.timer_task = asyncio.create_task(multiplayer_timeout_handler(message.channel, state, next_user_id))

            elif state.status == "PLAYING":
                val = await groq_client.validate_multiplayer_word(content, state.last_syllable, state.used_words)
                if not val["valid"]:
                    await message.add_reaction("❌")
                    await message.channel.send(f"❌ <@{message.author.id}> nối từ sai ({val['reason']})! <@{message.author.id}> BỊ LOẠI!")

                    state.eliminated_players.append({
                        "user_id": message.author.id,
                        "order": len(state.eliminated_players) + 1,
                        "reason": val["reason"]
                    })
                    if message.author.id in state.players:
                        state.players.remove(message.author.id)

                    if len(state.players) == 1:
                        await finish_game(message.channel, state, winner_text=f"<@{state.players[0]}>")
                    elif len(state.players) == 0:
                        await finish_game(message.channel, state, winner_text="Không có ai")
                    else:
                        if state.turn_index >= len(state.players):
                            state.turn_index = 0
                        next_user_id = state.players[state.turn_index]
                        state.current_turn_user_id = next_user_id
                        await message.channel.send(f"👉 Tới lượt <@{next_user_id}>! Cần nối bằng từ bắt đầu bằng **'{state.last_syllable}'** (Có 20 giây)")
                        state.timer_task = asyncio.create_task(multiplayer_timeout_handler(message.channel, state, next_user_id))
                    return

                await message.add_reaction("✅")
                state.used_words.add(content.lower())
                state.used_words_history.append(f"<@{message.author.id}>: {content}")
                state.last_syllable = val["last_syllable"]

                # Advance turn index
                state.turn_index = (state.turn_index + 1) % len(state.players)
                next_user_id = state.players[state.turn_index]
                state.current_turn_user_id = next_user_id

                await message.channel.send(f"👉 Tới lượt <@{next_user_id}>! Cần nối bằng từ bắt đầu bằng **'{state.last_syllable}'** (Có 20 giây)")
                state.timer_task = asyncio.create_task(multiplayer_timeout_handler(message.channel, state, next_user_id))


async def finish_game(channel: discord.TextChannel, state: NoiTuGameState, winner_text: str):
    """Conclude the game and print summary embed."""
    state.status = "ENDED"
    if state.timer_task and not state.timer_task.done():
        state.timer_task.cancel()

    embed = discord.Embed(
        title="🏆 TỔNG KẾT VÁN CHƠI NỐI TỪ",
        color=discord.Color.green()
    )
    embed.add_field(name="👑 Người chiến thắng", value=winner_text, inline=False)
    embed.add_field(name="📊 Tổng số cụm từ đã nối", value=str(len(state.used_words_history)), inline=True)

    if not state.is_single_player and state.eliminated_players:
        elim_text = "\n".join([f"{item['order']}. <@{item['user_id']}> ({item['reason']})" for item in state.eliminated_players])
        embed.add_field(name="💀 Thứ tự bị loại", value=elim_text, inline=False)

    if state.used_words_history:
        history_preview = "\n".join(state.used_words_history[-15:])
        if len(state.used_words_history) > 15:
            history_preview = "...\n" + history_preview
        embed.add_field(name="📝 Danh sách từ đã dùng (gần nhất)", value=history_preview, inline=False)

    await channel.send(embed=embed)
    game_states.pop(channel.id, None)
