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

# Environment variables - Shared with bot.py via ROUTER_* vars
AI_MODEL = os.getenv("ROUTER_MODEL", "openrouter/nvidia/nemotron-3.5-lightning:free")
AI_API_KEY = os.getenv("ROUTER_API_KEY", "")
AI_BASE_URL = os.getenv("ROUTER_BASE_URL", "https://9router-production-efb2.up.railway.app/v1")
NOITU_CHANNEL_ID_RAW = os.getenv("NOITU_CHANNEL_ID")
NOITU_CHANNEL_ID = int(NOITU_CHANNEL_ID_RAW) if NOITU_CHANNEL_ID_RAW and NOITU_CHANNEL_ID_RAW.isdigit() else None

class AIClient:
    """Async AI API helper using aiohttp. Supports Groq, 9router, and OpenAI-compatible APIs."""
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model
        # Auto-append /chat/completions if base URL doesn't end with it
        base = AI_BASE_URL.rstrip('/')
        if not base.endswith('/chat/completions'):
            base = base + '/chat/completions'
        self.base_url = base

    async def chat_completion(self, messages: List[Dict[str, str]], json_mode: bool = True, retries: int = 1, temperature: float = 0.5) -> Optional[str]:
        if not self.api_key:
            logger.error("AI_API_KEY is not configured.")
            return None

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }
        data: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature
        }
        if json_mode:
            data["response_format"] = {"type": "json_object"}

        for attempt in range(retries + 1):
            try:
                logger.info(f"Sending Groq API Request (Attempt {attempt+1}/{retries+1}) | Model: {self.model}")
                logger.debug(f"Groq Messages: {json.dumps(messages, ensure_ascii=False)}")
                async with aiohttp.ClientSession() as session:
                    async with session.post(self.base_url, headers=headers, json=data, timeout=45) as resp:
                        raw_text = await resp.text()
                        logger.info(f"Groq API Response Status: {resp.status} | Raw Content: {raw_text}")
                        if resp.status != 200:
                            logger.error(f"Groq API error HTTP {resp.status}: {raw_text}")
                            if attempt < retries:
                                await asyncio.sleep(1)
                                continue
                            return None

                        # Handle both standard JSON and SSE streaming responses from 9router
                        raw_stripped = raw_text.strip()
                        
                        # Detect SSE streaming format (starts with "data:")
                        if raw_stripped.startswith('data:'):
                            # Parse SSE chunks and concatenate content deltas
                            content_parts = []
                            for line in raw_stripped.split('\n'):
                                line = line.strip()
                                if line.startswith('data:') and '[DONE]' not in line:
                                    try:
                                        chunk = json.loads(line[5:].strip())
                                        delta = chunk.get('choices', [{}])[0].get('delta', {})
                                        if 'content' in delta:
                                            content_parts.append(delta['content'])
                                    except (json.JSONDecodeError, IndexError, KeyError):
                                        pass
                            if not content_parts:
                                raise ValueError("SSE stream contained no content deltas")
                            content = ''.join(content_parts)
                        else:
                            # Standard JSON response with optional SSE trailer
                            # Use brace-matching to extract first complete JSON object
                            depth = 0
                            json_start = -1
                            json_end = -1
                            for idx, ch in enumerate(raw_stripped):
                                if ch == '{':
                                    if depth == 0:
                                        json_start = idx
                                    depth += 1
                                elif ch == '}':
                                    depth -= 1
                                    if depth == 0 and json_start >= 0:
                                        json_end = idx + 1
                                        break
                            if json_start < 0 or json_end < 0:
                                raise ValueError(f"No valid JSON object found in API response (length={len(raw_stripped)})")
                            res_json = json.loads(raw_stripped[json_start:json_end])
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

    async def is_real_vietnamese_word(self, word: str) -> bool:
        """Independent 2nd-layer validation: Check if a phrase is a natural, meaningful Vietnamese 2-syllable phrase used in daily speech."""
        if not word or len(word.strip().split()) != 2:
            return False

        sys_prompt = """Bạn là trọng tài ngôn ngữ tiếng Việt.
Nhiệm vụ: Trả lời xem cụm 2 tiếng dưới đây có phải là cách nói tự nhiên, có nghĩa thực tế mà người Việt thực sự dùng trong giao tiếp hàng ngày hay không (bao gồm từ ghép, cụm tính từ, cụm danh từ, phó từ thông dụng).

Ví dụ HOÀN TOÀN HỢP LỆ (is_real: true):
- 'đẹp quá', 'đẹp lắm', 'to lắm', 'vui vẻ', 'xa xôi', 'nhung hươu', 'trồng cây', 'yêu thương', 'đẹp trai', 'ăn cơm'.

Ví dụ GIẢ / BỊA VÔ NGHĨA (is_real: false):
- 'khoải hứng', 'nhung nhau', 'cây ghế', 'đẹp nhà'.

Trả về duy nhất JSON: {"is_real": true/false}"""

        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"Cụm từ: '{word}' có phải cách nói tự nhiên, thông dụng trong tiếng Việt không?"}
        ]
        res = await self.chat_completion(messages, json_mode=True, temperature=0.1)
        if not res:
            return True # Fallback if API fails

        try:
            data = json.loads(res)
            return bool(data.get("is_real", False))
        except Exception as e:
            logger.error(f"Error in is_real_vietnamese_word: {e}")
            return True

    async def validate_starter_phrase(self, phrase: str) -> Dict[str, Any]:
        """Validate starter phrase: 2 Vietnamese syllables."""
        sys_prompt = """Bạn là trọng tài trò chơi Nối Từ Tiếng Việt.
Nhiệm vụ: Kiểm tra cụm từ ra đề của người chơi.
YÊU CẦU BẮT BUỘC:
1. Cụm từ phải đúng CỤM 2 TIẾNG TIẾNG VIỆT CƠ BẢN, THÔNG DỤNG (có nghĩa trong tiếng Việt).
   - CHẤP NHẬN mọi loại từ ghép tự nhiên: danh từ ('con mèo', 'hoa hồng'), động từ ('ăn cơm', 'đi học'), tính từ ('vui vẻ', 'yêu thương'), cảm thán/phó từ ('đẹp quá', 'to lắm', 'đẹp lắm').
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
        res = await self.chat_completion(messages, json_mode=True, temperature=0.1)
        words = [clean_syllable(w) for w in phrase.strip().split() if clean_syllable(w)]
        if not res:
            if len(words) == 2:
                return {"valid": True, "reason": "", "last_syllable": words[1], "api_error": True}
            return {"valid": False, "reason": "Cụm từ phải gồm đúng 2 tiếng tiếng Việt.", "last_syllable": None, "api_error": True}

        try:
            data = json.loads(res)
            last_syl = data.get("last_syllable")
            cleaned_last = clean_syllable(last_syl) if last_syl else (words[1] if len(words) == 2 else None)
            return {
                "valid": bool(data.get("valid", False)),
                "reason": str(data.get("reason", "Cụm từ không hợp lệ.")),
                "last_syllable": cleaned_last
            }
        except Exception as e:
            logger.error(f"JSON parse error in validate_starter_phrase: {e}")
            return {"valid": len(words) == 2, "reason": "Cụm từ phải đúng 2 tiếng.", "last_syllable": words[1] if len(words) == 2 else None}

    async def validate_and_next_singleplayer(self, current_word: str, expected_first_syllable: str, used_words: Set[str], is_starter: bool = False) -> Dict[str, Any]:
        """Validate player word and generate AI response for Singleplayer mode."""
        recent_used = list(used_words)
        if len(recent_used) > 60:
            recent_used = recent_used[-60:]  # chỉ gửi 60 từ gần nhất vào prompt, tránh phình token
        used_list_str = ", ".join(recent_used)
        clean_exp_first = clean_syllable(expected_first_syllable)
        
        if is_starter:
            sys_prompt = f"""Bạn là đối thủ trò chơi Nối Từ Tiếng Việt.
Người chơi vừa ra đề bằng cụm từ: '{current_word}'.
Nhiệm vụ của bạn:
1. Tìm 1 CỤM 2 TIẾNG TIẾNG VIỆT TỰ NHIÊN, CÓ NGHĨA THỰC TẾ (bao gồm từ ghép, cụm tính từ, cụm danh từ thông dụng) để nối tiếp.
2. Cụm từ của bạn BẮT BUỘC phải BẮT ĐẦU BẰNG TIẾNG: '{clean_exp_first}' (LƯU Ý CỰC KỲ QUAN TRỌNG: TIẾNG ĐẦU TIÊN TRONG CỤM TỪ CỦA BẠN PHẢI LÀ '{clean_exp_first}', KHÔNG ĐƯỢC DÙNG TIẾNG ĐẦU NÀO KHÁC!).
3. Cụm từ của bạn KHÔNG ĐƯỢC trùng với danh sách đã dùng (so sánh không phân biệt viết hoa/thường): [{used_list_str}].
4. ⭐ ƯU TIÊN CHỌN TỪ PHỔ THÔNG, DỄ HIỂU mà ai cũng biết (ví dụ: 'hồng hào', 'hồng đào', 'hồng thắm'). CHỈ khi nào HẾT từ dễ mới được dùng từ khó/hiếm gặp (như 'hồng lụa', 'hồng bì'). TUYỆT ĐỐI TRÁNH chọn từ quá chuyên ngành hoặc ít người biết.

⛔ CẤM BỊA TỪ VÀ CẤM SAI TIẾNG ĐẦU:
- TIẾNG ĐẦU TIÊN của cụm từ bạn chọn BẮT BUỘC KHỚP VỚI '{clean_exp_first}'. Ví dụ: nếu yêu cầu bắt đầu bằng 'lắm', cụm từ của bạn PHẢI là 'lắm chuyện', 'lắm lời' (BẮT ĐẦU BẰNG 'lắm'). CẤM không được chọn 'đẹp trai' khi yêu cầu là 'lắm'!
- TUYỆT ĐỐI CẤM ghép 2 tiếng ngẫu nhiên vô nghĩa (CẤM 'khoải hứng', 'nhung nhau').
- ⛔ CẤM đảo ngược từ vừa được đưa ra (ví dụ: nếu người chơi nói 'khổ đau', CẤM trả lời 'đau khổ').
- ⛔ CHỈ ĐƯỢC DÙNG TIẾNG VIỆT. TUYỆT ĐỐI KHÔNG dùng từ tiếng Anh hay ngôn ngữ khác.
- Nếu không tìm thấy cụm từ hợp lệ bắt đầu bằng '{clean_exp_first}', bạn PHẢI CHẤP NHẬN THUA bằng cách trả về valid: false.

Trả về duy nhất định dạng JSON:
{{
  "valid": true/false,
  "reason": "Lý do nếu thua",
  "ai_word": "Cụm 2 tiếng bắt đầu bằng '{clean_exp_first}' của AI (nếu valid=true)",
  "ai_last_syllable": "Tiếng thứ 2 trong cụm từ của AI"
}}"""
            messages = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": f"Ra đề: '{current_word}'. Bạn hãy nối tiếp cụm 2 tiếng BẮT ĐẦU BẰNG TIẾNG '{clean_exp_first}'."}
            ]
        else:
            sys_prompt = f"""Bạn là ĐỐI THỦ (không phải trọng tài) trong trò chơi Nối Từ Tiếng Việt solo với người chơi.
Người chơi vừa gửi cụm từ: '{current_word}'.

NHIỆM VỤ CỦA BẠN:
1. Tìm 1 CỤM 2 TIẾNG TIẾNG VIỆT để nối tiếp. Cụm từ BẮT BUỘC PHẢI BẮT ĐẦU BẰNG TIẾNG: '{clean_exp_first}'.
2. KHÔNG ĐƯỢC trùng với các từ đã dùng: [{used_list_str}].
3. ⭐ ƯU TIÊN TUYỆT ĐỐI từ THÔNG DỤNG, DỄ HIỂU, ai cũng biết (ví dụ: 'con mèo', 'ăn cơm', 'đi học', 'vui vẻ'). CHỈ khi HẾT từ dễ mới dùng từ khó/hiếm.
4. Bạn có vốn từ vựng phong phú, hãy tự tin chọn từ phù hợp! Đừng ngại đưa ra từ hay.
5. TUYỆT ĐỐI CẤM ghép bừa vô nghĩa (CẤM 'khoải hứng', 'nhung nhau').
6. ⛔ CẤM đảo ngược từ vừa được đưa ra (ví dụ: 'khổ đau' → CẤM 'đau khổ', 'yêu thương' → CẤM 'thương yêu').
7. ⛔ CHỈ ĐƯỢC DÙNG TIẾNG VIỆT. TUYỆT ĐỐI KHÔNG dùng từ tiếng Anh hay ngôn ngữ khác (CẤM 'people', 'hello', 'world'...).
8. Nếu thực sự không tìm được từ nào bắt đầu bằng '{clean_exp_first}', trả về valid: false để chịu thua.

Trả về duy nhất JSON:
{{
  "valid": true/false,
  "reason": "Lý do nếu thua",
  "ai_word": "Cụm 2 tiếng bắt đầu bằng '{clean_exp_first}' (nếu valid=true)",
  "ai_last_syllable": "Tiếng thứ 2 trong cụm từ của AI"
}}"""
            messages = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": f"Người chơi gửi: '{current_word}'"}
            ]

        # Retry loop: nếu AI trả về từ vi phạm (sai tiếng đầu / đảo ngược / bịa từ),
        # cho AI thử lại 1 lần kèm phản hồi sửa lỗi thay vì thua ngay.
        last_reject_reason = None
        for attempt in range(2):
            attempt_messages = messages
            if attempt > 0 and last_reject_reason:
                attempt_messages = messages + [
                    {"role": "user", "content": f"Lần trả lời trước của bạn đã bị từ chối vì: {last_reject_reason}. Hãy đưa ra MỘT CỤM TỪ KHÁC hoàn toàn, tuân thủ đúng quy tắc (bắt đầu bằng tiếng '{clean_exp_first}', gồm đúng 2 tiếng tiếng Việt có thật)."}
                ]

            res = await self.chat_completion(attempt_messages, json_mode=True)
            if not res:
                if attempt == 0:
                    await asyncio.sleep(1)
                    continue
                return {"valid": False, "reason": "Không thể kết nối API AI (timeout/network error).", "api_error": True}

            try:
                data = json.loads(res)
            except Exception as e:
                logger.error(f"JSON parse error in validate_and_next_singleplayer: {e} | Raw content: {res}")
                if attempt == 0:
                    continue
                return {"valid": False, "reason": "Lỗi định dạng dữ liệu kiểm tra từ AI."}

            ai_word = data.get("ai_word")
            if not (data.get("valid") and ai_word):
                # AI tự nhận thua → tôn trọng quyết định
                return data

            # --- Code-level enforcement ---
            ai_words_list = [clean_syllable(w) for w in ai_word.strip().split() if clean_syllable(w)]
            if len(ai_words_list) != 2 or ai_words_list[0] != clean_exp_first:
                last_reject_reason = f"cụm từ '{ai_word}' không bắt đầu bằng tiếng '{clean_exp_first}'"
                logger.warning(f"AI word '{ai_word}' rejected (attempt {attempt+1}): {last_reject_reason}. Retrying...")
                continue

            player_words = [clean_syllable(w) for w in current_word.strip().split() if clean_syllable(w)]
            if len(player_words) == 2 and len(ai_words_list) == 2:
                if ai_words_list[0] == player_words[1] and ai_words_list[1] == player_words[0]:
                    last_reject_reason = f"đảo ngược từ '{current_word}' thành '{ai_word}' (CẤM đảo ngược)"
                    logger.warning(f"AI word '{ai_word}' rejected (attempt {attempt+1}): {last_reject_reason}. Retrying...")
                    continue

            is_real = await self.is_real_vietnamese_word(ai_word)
            if not is_real:
                last_reject_reason = f"cụm từ '{ai_word}' không phải là cách nói tự nhiên có thật"
                logger.warning(f"AI word '{ai_word}' rejected (attempt {attempt+1}): {last_reject_reason}. Retrying...")
                continue

            return data  # Hợp lệ hoàn toàn

        # Hết 2 lần thử mà vẫn vi phạm → AI thua
        return {"valid": False, "reason": f"AI không thể đưa ra cụm từ hợp lệ ({last_reject_reason or 'lỗi không xác định'})."}

    async def validate_multiplayer_word(self, current_word: str, expected_first_syllable: str, used_words: Set[str]) -> Dict[str, Any]:
        """Validate player word in Multiplayer mode."""
        recent_used = list(used_words)
        if len(recent_used) > 60:
            recent_used = recent_used[-60:]
        used_list_str = ", ".join(recent_used)
        clean_exp_first = clean_syllable(expected_first_syllable)
        sys_prompt = f"""Bạn là trọng tài trò chơi Nối Từ Tiếng Việt.
QUY TẮC BẮT BUỘC:
1. Cụm từ phải gồm đúng CỤM 2 TIẾNG TIẾNG VIỆT CÓ THẬT, THÔNG DỤNG trong từ điển và đời sống.
2. Tiếng thứ nhất BẮT BUỘC phải khớp với tiếng: '{clean_exp_first}' (KHÔNG PHÂN BIỆT VIẾT HOA/THƯỜNG).
3. Cụm từ KHÔNG ĐƯỢC trùng với danh sách đã dùng: [{used_list_str}].
4. Chấp nhận các cách nói tự nhiên như 'đẹp quá', 'to lắm', 'vui vẻ'. TUYỆT ĐỐI KHÔNG chấp nhận cụm từ ghép bừa vô nghĩa (CẤM 'khoải hứng', 'nhung nhau', 'chơi gà',...).

Trả về duy nhất định dạng JSON:
{{
  "valid": true/false,
  "reason": "Lý do ngắn gọn nếu sai (không có nghĩa / không đúng tiếng đầu / đã dùng / không đủ 2 tiếng)",
  "last_syllable": "Tiếng thứ 2 của cụm từ vừa gửi (nếu valid=true)"
}}"""
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"Người chơi gửi: '{current_word}'"}
        ]
        res = await self.chat_completion(messages, json_mode=True, temperature=0.1)
        words = [clean_syllable(w) for w in current_word.strip().split() if clean_syllable(w)]
        if not res:
            if len(words) == 2 and words[0] == clean_exp_first and normalize_word(current_word) not in used_words:
                return {"valid": True, "reason": "", "last_syllable": words[1], "api_error": True}
            return {"valid": False, "reason": "Cụm từ không hợp lệ.", "last_syllable": None, "api_error": True}

        try:
            data = json.loads(res)
            last_syl = data.get("last_syllable")
            cleaned_last = clean_syllable(last_syl) if last_syl else (words[1] if len(words) == 2 else None)
            is_valid = bool(data.get("valid", False))

            if is_valid:
                # 2nd-layer validation for Player word in Multiplayer
                is_real = await self.is_real_vietnamese_word(current_word)
                if not is_real:
                    return {"valid": False, "reason": f"Cụm từ '{current_word}' không phải từ ghép tiếng Việt có thật.", "last_syllable": None}

            return {
                "valid": is_valid,
                "reason": str(data.get("reason", "Cụm từ không hợp lệ.")),
                "last_syllable": cleaned_last
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


def rearm_timer(state: "NoiTuGameState", coro):
    """Hủy timer cũ (nếu còn) trước khi hẹn timer mới — chống chồng timer khiến người chơi bị loại sớm."""
    if state.timer_task and not state.timer_task.done():
        state.timer_task.cancel()
    state.timer_task = asyncio.create_task(coro)


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


def normalize_word(word: str) -> str:
    """Normalize word by lowering case, stripping whitespace, and collapsing inner spaces."""
    return re.sub(r'\s+', ' ', word.strip().lower())

def clean_syllable(text: str) -> str:
    """Clean a syllable/word by stripping punctuation and whitespace, converting to lowercase."""
    if not text:
        return ""
    cleaned = re.sub(r'^[^\w\s]+|[^\w\s]+$', '', text.strip(), flags=re.UNICODE)
    return cleaned.lower()

def get_groq_client() -> AIClient:
    return AIClient(api_key=AI_API_KEY, model=AI_MODEL)


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
    embed.set_footer(text="Hệ thống chấm/ra đề tự động bởi Chuột dethw 🐭")

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
        rearm_timer(state, singleplayer_timeout_handler(interaction.channel, state, participants[0]))

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
        rearm_timer(state, multiplayer_timeout_handler(interaction.channel, state, starter_id))


async def singleplayer_timeout_handler(channel: discord.TextChannel, state: NoiTuGameState, user_id: int):
    """Timeout handler for singleplayer mode (20 seconds)."""
    try:
        await asyncio.sleep(20)
        async with state.message_lock:
            if state.status in ["ENDED"] or state.current_turn_user_id != user_id:
                return

            await channel.send(f"⏰ <@{user_id}> đã **quá 20 giây** không đưa ra cụm từ hợp lệ! Bạn đã THUA CUỘC.")
            await finish_game(channel, state, winner_text="Chuột dethw 🐭")
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
                    rearm_timer(state, multiplayer_timeout_handler(channel, state, next_user_id))
                else:
                    await channel.send(f"👉 Tới lượt <@{next_user_id}>! Cần nối bằng từ bắt đầu bằng **'{state.last_syllable}'** (Bạn có 20 giây)")
                    rearm_timer(state, multiplayer_timeout_handler(channel, state, next_user_id))
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
        content = message.content.strip()
        norm_content = normalize_word(content)

        # Check for duplicate word BEFORE calling AI or timer cancellation
        if norm_content in state.used_words:
            await message.add_reaction("❌")
            expected_hint = f" **'{state.last_syllable}'**" if state.last_syllable else ""
            await message.reply(
                f"⚠️ Cụm từ **'{content}'** đã được sử dụng trong ván này rồi! "
                f"Vui lòng đưa ra cụm từ khác{expected_hint}."
            )
            # Re-arm or keep timer running for current turn (do NOT cancel/reset or eliminate)
            return

        # Cancel active timer task now that valid non-duplicate attempt is being processed
        if state.timer_task and not state.timer_task.done():
            state.timer_task.cancel()

        groq_client = get_groq_client()

        # ================= 5A: SINGLEPLAYER (USER vs AI) =================
        if state.is_single_player:
            if state.status == "WAITING_STARTER":
                # Validate starter phrase
                val = await groq_client.validate_starter_phrase(content)
                if not val["valid"]:
                    await message.add_reaction("❌")
                    await message.reply(f"❌ {message.author.mention} Đề không hợp lệ: {val['reason']}. Vui lòng ra đề khác (cụm 2 tiếng)!")
                    rearm_timer(state, singleplayer_timeout_handler(message.channel, state, message.author.id))
                    return

                await message.add_reaction("✅")
                state.used_words.add(norm_content)
                state.used_words_history.append(f"<@{message.author.id}>: {content}")
                words = [clean_syllable(w) for w in content.split() if clean_syllable(w)]
                expected_last_syllable = words[-1] if words else clean_syllable(val.get("last_syllable", ""))
                state.status = "PLAYING"

                # AI turn to respond to starter phrase: expected first syllable is the last word of starter phrase!
                ai_res = await groq_client.validate_and_next_singleplayer(content, expected_last_syllable, state.used_words, is_starter=True)
                if ai_res.get("api_error"):
                    # Lỗi hạ tầng → không kết thúc ván, cho người chơi ra đề lại
                    state.used_words.discard(norm_content)
                    await message.add_reaction("⚠️")
                    await message.reply("🐱 Chuột dethw vừa bị mèo tha mất, vui lòng liên hệ chủ nhân @phantrongkhangg (tiktok) để bắt chuột về!")
                    rearm_timer(state, singleplayer_timeout_handler(message.channel, state, message.author.id))
                    return
                if not ai_res.get("valid") or not ai_res.get("ai_word"):
                    reason_msg = ai_res.get("reason", "")
                    logger.warning(f"AI failed to respond to starter phrase '{content}': {reason_msg}")
                    await message.channel.send(f"🎉 **AI KHÔNG NỐI TIẾP ĐƯỢC CỤM TỪ!** {message.author.mention} ĐÃ THẮNG CUỘC!")
                    await finish_game(message.channel, state, winner_text=f"{message.author.mention}")
                    return

                ai_word = ai_res["ai_word"]
                norm_ai_word = normalize_word(ai_word)

                # Extra check if AI selected a duplicate despite prompt rules
                if norm_ai_word in state.used_words:
                    logger.warning(f"AI selected duplicate word '{ai_word}', retrying AI completion once...")
                    # Truyền bản sao có từ trùng để ép AI chọn khác, KHÔNG pollute bộ used thật
                    ai_res_retry = await groq_client.validate_and_next_singleplayer(
                        content, expected_last_syllable, state.used_words | {norm_ai_word}, is_starter=True)
                    if ai_res_retry.get("valid") and ai_res_retry.get("ai_word") and normalize_word(ai_res_retry["ai_word"]) not in state.used_words:
                        ai_word = ai_res_retry["ai_word"]
                        norm_ai_word = normalize_word(ai_word)
                if norm_ai_word in state.used_words:
                    # Vẫn trùng sau retry → AI thua công bằng (từ trùng không được chấp nhận)
                    await message.channel.send(f"🎉 **AI KHÔNG NỐI TIẾP ĐƯỢC CỤM TỪ!** {message.author.mention} ĐÃ THẮNG CUỘC!")
                    await finish_game(message.channel, state, winner_text=f"{message.author.mention}")
                    return

                state.used_words.add(norm_ai_word)
                state.used_words_history.append(f"🐭 Chuột dethw: {ai_word}")
                ai_words = [clean_syllable(w) for w in ai_word.split() if clean_syllable(w)]
                state.last_syllable = ai_words[-1] if ai_words else clean_syllable(ai_word)

                await message.channel.send(f"🐭 **Chuột dethw:** `{ai_word}`")
                await message.channel.send(f"👉 Tới lượt {message.author.mention}! Cần nối cụm từ bắt đầu bằng **'{state.last_syllable}'** (Có 20 giây)")
                rearm_timer(state, singleplayer_timeout_handler(message.channel, state, message.author.id))

            elif state.status == "PLAYING":
                # Validate player response phrase
                player_words_clean = [clean_syllable(w) for w in content.strip().split() if clean_syllable(w)]
                player_last_syllable = player_words_clean[-1] if len(player_words_clean) >= 2 else state.last_syllable

                # ⭐ Enforce chain rule: tiếng đầu của người chơi PHẢI khớp tiếng yêu cầu
                required_syl = clean_syllable(state.last_syllable) if state.last_syllable else None
                if required_syl and (len(player_words_clean) < 2 or player_words_clean[0] != required_syl):
                    await message.add_reaction("❌")
                    await message.reply(
                        f"❌ {message.author.mention} Cụm từ phải gồm đúng 2 tiếng và bắt đầu bằng **'{state.last_syllable}'**! Vui lòng gửi lại."
                    )
                    rearm_timer(state, singleplayer_timeout_handler(message.channel, state, message.author.id))
                    return

                # Thêm từ người chơi vào used TRƯỚC khi gọi AI để AI thấy và không trả lại chính từ đó
                state.used_words.add(norm_content)
                val = await groq_client.validate_and_next_singleplayer(content, player_last_syllable, state.used_words)

                if val.get("api_error"):
                    # Lỗi hạ tầng → không tính là thua, mời gửi lại
                    state.used_words.discard(norm_content)
                    await message.add_reaction("⚠️")
                    await message.reply("🐱 Chuột dethw vừa bị mèo tha mất, vui lòng liên hệ chủ nhân @phantrongkhangg (tiktok) để bắt chuột về!")
                    rearm_timer(state, singleplayer_timeout_handler(message.channel, state, message.author.id))
                    return

                if not val["valid"]:
                    state.used_words.discard(norm_content)
                    await message.add_reaction("❌")
                    await message.reply(f"❌ {message.author.mention} Vui lòng gửi lại từ nối khác! Lý do: {val['reason']}")
                    rearm_timer(state, singleplayer_timeout_handler(message.channel, state, message.author.id))
                    return

                # 2nd-layer validation for Player's word in Singleplayer
                is_real_player = await groq_client.is_real_vietnamese_word(content)
                if not is_real_player:
                    state.used_words.discard(norm_content)
                    await message.add_reaction("❌")
                    await message.reply(f"❌ {message.author.mention} Cụm từ **'{content}'** không phải là từ ghép tiếng Việt có thật! Vui lòng chọn từ khác.")
                    rearm_timer(state, singleplayer_timeout_handler(message.channel, state, message.author.id))
                    return

                await message.add_reaction("✅")
                state.used_words_history.append(f"<@{message.author.id}>: {content}")

                ai_word = val.get("ai_word")
                if not ai_word:
                    await message.channel.send(f"🎉 **AI KHÔNG NỐI TIẾP ĐƯỢC CỤM TỪ!** {message.author.mention} ĐÃ THẮNG CUỘC!")
                    await finish_game(message.channel, state, winner_text=f"{message.author.mention}")
                    return

                norm_ai_word = normalize_word(ai_word)

                # Extra check if AI selected a duplicate despite prompt rules
                if norm_ai_word in state.used_words:
                    logger.warning(f"AI selected duplicate word '{ai_word}', retrying AI completion once...")
                    # Truyền bản sao có từ trùng để ép AI chọn khác, KHÔNG pollute bộ used thật
                    ai_res_retry = await groq_client.validate_and_next_singleplayer(
                        content, player_last_syllable, state.used_words | {norm_ai_word})
                    if ai_res_retry.get("api_error"):
                        # Lỗi hạ tầng lúc retry → mời người chơi gửi lại, không kết thúc oan
                        state.used_words.discard(norm_content)
                        await message.add_reaction("⚠️")
                        await message.reply("🐱 Chuột dethw vừa bị mèo tha mất, vui lòng liên hệ chủ nhân @phantrongkhangg (tiktok) để bắt chuột về!")
                        rearm_timer(state, singleplayer_timeout_handler(message.channel, state, message.author.id))
                        return
                    if ai_res_retry.get("valid") and ai_res_retry.get("ai_word") and normalize_word(ai_res_retry["ai_word"]) not in state.used_words:
                        ai_word = ai_res_retry["ai_word"]
                        norm_ai_word = normalize_word(ai_word)
                if norm_ai_word in state.used_words:
                    # Vẫn trùng sau retry → AI thua công bằng (từ trùng không được chấp nhận)
                    await message.channel.send(f"🎉 **AI KHÔNG NỐI TIẾP ĐƯỢC CỤM TỪ!** {message.author.mention} ĐÃ THẮNG CUỘC!")
                    await finish_game(message.channel, state, winner_text=f"{message.author.mention}")
                    return

                state.used_words.add(norm_ai_word)
                state.used_words_history.append(f"🐭 Chuột dethw: {ai_word}")
                ai_words = [clean_syllable(w) for w in ai_word.split() if clean_syllable(w)]
                state.last_syllable = ai_words[-1] if ai_words else clean_syllable(ai_word)

                await message.channel.send(f"🐭 **Chuột dethw:** `{ai_word}`")
                await message.channel.send(f"👉 Tới lượt {message.author.mention}! Cần nối cụm từ bắt đầu bằng **'{state.last_syllable}'** (Có 20 giây)")
                rearm_timer(state, singleplayer_timeout_handler(message.channel, state, message.author.id))

        # ================= 5B: MULTIPLAYER =================
        else:
            if state.status == "WAITING_STARTER":
                val = await groq_client.validate_starter_phrase(content)
                if not val["valid"]:
                    await message.add_reaction("❌")
                    await message.reply(f"❌ {message.author.mention} Đề không hợp lệ: {val['reason']}. Vui lòng ra đề khác (cụm 2 tiếng)!")
                    rearm_timer(state, multiplayer_timeout_handler(message.channel, state, message.author.id))
                    return

                await message.add_reaction("✅")
                state.used_words.add(norm_content)
                state.used_words_history.append(f"<@{message.author.id}>: {content}")
                state.last_syllable = val["last_syllable"]
                state.status = "PLAYING"

                # Next player turn
                state.turn_index = (state.turn_index + 1) % len(state.players)
                next_user_id = state.players[state.turn_index]
                state.current_turn_user_id = next_user_id

                await message.channel.send(f"👉 Tới lượt <@{next_user_id}>! Cần nối bằng từ bắt đầu bằng **'{state.last_syllable}'** (Có 20 giây)")
                rearm_timer(state, multiplayer_timeout_handler(message.channel, state, next_user_id))

            elif state.status == "PLAYING":
                val = await groq_client.validate_multiplayer_word(content, state.last_syllable, state.used_words)

                if val.get("api_error"):
                    # Lỗi hạ tầng → KHÔNG loại người chơi, mời gửi lại
                    await message.add_reaction("⚠️")
                    await message.reply("🐱 Chuột dethw vừa bị mèo tha mất, vui lòng liên hệ chủ nhân @phantrongkhangg (tiktok) để bắt chuột về!")
                    rearm_timer(state, multiplayer_timeout_handler(message.channel, state, message.author.id))
                    return

                # ⭐ Code-level enforcement: không tin 100% phán quyết của LLM
                words_chk = [clean_syllable(w) for w in content.strip().split() if clean_syllable(w)]
                required_chk = clean_syllable(state.last_syllable) if state.last_syllable else None
                elim_reason = None
                if len(words_chk) != 2:
                    elim_reason = "Cụm từ không gồm đúng 2 tiếng"
                elif required_chk and words_chk[0] != required_chk:
                    elim_reason = f"Cụm từ phải bắt đầu bằng tiếng '{state.last_syllable}'"
                elif not val["valid"]:
                    elim_reason = str(val.get("reason", "Cụm từ không hợp lệ."))

                if elim_reason:
                    await message.add_reaction("❌")
                    await message.channel.send(f"❌ <@{message.author.id}> nối từ sai ({elim_reason})! <@{message.author.id}> BỊ LOẠI!")

                    state.eliminated_players.append({
                        "user_id": message.author.id,
                        "order": len(state.eliminated_players) + 1,
                        "reason": elim_reason
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
                        rearm_timer(state, multiplayer_timeout_handler(message.channel, state, next_user_id))
                    return

                await message.add_reaction("✅")
                state.used_words.add(norm_content)
                state.used_words_history.append(f"<@{message.author.id}>: {content}")
                state.last_syllable = val["last_syllable"]

                # Advance turn index
                state.turn_index = (state.turn_index + 1) % len(state.players)
                next_user_id = state.players[state.turn_index]
                state.current_turn_user_id = next_user_id

                await message.channel.send(f"👉 Tới lượt <@{next_user_id}>! Cần nối bằng từ bắt đầu bằng **'{state.last_syllable}'** (Có 20 giây)")
                rearm_timer(state, multiplayer_timeout_handler(message.channel, state, next_user_id))


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
