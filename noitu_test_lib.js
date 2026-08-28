/**
 * NoiTu Stress Test Harness - simulates exact noitu.py API calls
 */
const AI_MODEL = 'qwen/qwen3.7-max';
const AI_BASE_URL = process.env.XKIRO_BASE_URL || 'https://api.xkiro.com/v1';
const AI_API_KEY = process.env.XKIRO_KEY;
const ENDPOINT = AI_BASE_URL.replace(/\/$/, '') + '/chat/completions';

function cleanSyllable(text) {
  if (!text) return "";
  return text.trim().replace(/^[^\p{L}\p{N}_\s]+|[^\p{L}\p{N}_\s]+$/gu, '').toLowerCase();
}

async function chatCompletion(messages, jsonMode = true) {
  const headers = {
    "Authorization": `Bearer ${AI_API_KEY}`,
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0"
  };
  const data = { model: AI_MODEL, messages, temperature: 0.5 };
  if (jsonMode) data.response_format = { type: "json_object" };
  
  const started = Date.now();
  try {
    const res = await fetch(ENDPOINT, {
      method: 'POST', headers,
      body: JSON.stringify(data),
      signal: AbortSignal.timeout(12000)
    });
    const latency = Date.now() - started;
    const raw = await res.text();
    if (res.status !== 200) return { ok: false, error: `HTTP ${res.status}: ${raw.substring(0, 80)}`, latency };
    
    // SSE detection (same as noitu.py)
    const stripped = raw.trim();
    let content;
    if (stripped.startsWith('data:')) {
      const parts = [];
      for (const line of stripped.split('\n')) {
        if (line.startsWith('data:') && !line.includes('[DONE]')) {
          try {
            const chunk = JSON.parse(line.slice(5).trim());
            const delta = chunk.choices?.[0]?.delta || {};
            if (delta.content) parts.push(delta.content);
          } catch {}
        }
      }
      content = parts.join('');
      if (!content) return { ok: false, error: 'empty-sse', latency };
    } else {
      let depth = 0, js = -1, je = -1;
      for (let i = 0; i < stripped.length; i++) {
        if (stripped[i] === '{') { if (depth === 0) js = i; depth++; }
        else if (stripped[i] === '}') { depth--; if (depth === 0 && js >= 0) { je = i + 1; break; } }
      }
      if (js < 0 || je < 0) return { ok: false, error: 'no-json', rawPreview: stripped.substring(0, 120), latency };
      try {
        content = JSON.parse(stripped.substring(js, je)).choices[0].message.content;
      } catch (e) {
        return { ok: false, error: 'parse-fail', rawPreview: stripped.substring(0, 120), latency };
      }
    }
    
    // strip markdown fences
    content = content.replace(/^\`\`\`json\s*/gm, '').replace(/^\`\`\`\s*/gm, '').replace(/\`\`\`$/gm, '').trim();
    return { ok: true, content, latency };
  } catch (e) {
    return { ok: false, error: e.name === 'TimeoutError' ? 'timeout' : e.message.substring(0, 50), latency: Date.now() - started };
  }
}

async function testStarterPhrase(phrase) {
  const sysPrompt = [
    'Bạn là trọng tài trò chơi Nối Từ Tiếng Việt.',
    'Nhiệm vụ: Kiểm tra cụm từ ra đề của người chơi.',
    'YÊU CẦU BẮT BUỘC:',
    '1. Cụm từ phải đúng CỤM 2 TIẾNG TIẾNG VIỆT CƠ BẢN, THÔNG DỤNG (có nghĩa trong tiếng Việt).',
    '2. Tránh từ Hán Việt quá hiếm, từ chuyên ngành.',
    '3. Trả về đúng định dạng JSON:',
    '{',
    '  "valid": true/false,',
    '  "reason": "Lý do ngắn gọn nếu không hợp lệ",',
    '  "last_syllable": "Tiếng thứ 2 của cụm từ (nếu valid=true)"',
    '}'
  ].join('\n');
  const r = await chatCompletion([
    { role: 'system', content: sysPrompt },
    { role: 'user', content: `Kiểm tra cụm từ ra đề: '${phrase}'` }
  ]);
  if (!r.ok) return { type: 'starter', phrase, apiError: r.error };
  try {
    const d = JSON.parse(r.content);
    return { type: 'starter', phrase, ...d, latency: r.latency };
  } catch {
    return { type: 'starter', phrase, parseError: true, raw: r.content?.substring(0, 100), latency: r.latency };
  }
}

async function testAiOpponent(expectedFirst, playerWord, usedWords) {
  const cleanExpFirst = cleanSyllable(expectedFirst);
  const usedStr = usedWords.join(', ');
  const sysPrompt = `Bạn là ĐỐI THỦ (không phải trọng tài) trong trò chơi Nối Từ Tiếng Việt solo với người chơi.
Người chơi vừa gửi cụm từ: '${playerWord}'.

NHIỆM VỤ CỦA BẠN:
1. Tìm 1 CỤM 2 TIẾNG TIẾNG VIỆT để nối tiếp. Cụm từ BẮT BUỘC PHẢI BẮT ĐẦU BẰNG TIẾNG: '${cleanExpFirst}'.
2. KHÔNG ĐƯỢC trùng với các từ đã dùng: [${usedStr}].
3. ⭐ ƯU TIÊN TUYỆT ĐỐI từ THÔNG DỤNG, DỄ HIỂU, ai cũng biết (ví dụ: 'con mèo', 'ăn cơm', 'đi học', 'vui vẻ'). CHỈ khi HẾT từ dễ mới dùng từ khó/hiếm.
4. Bạn có vốn từ vựng phong phú, hãy tự tin chọn từ phù hợp! Đừng ngại đưa ra từ hay.
5. TUYỆT ĐỐI CẤM ghép bừa vô nghĩa (CẤM 'khoải hứng', 'nhung nhau').
7. ⛔ CẤM đảo ngược từ vừa được đưa ra (ví dụ: 'khổ đau' → CẤM 'đau khổ', 'yêu thương' → CẤM 'thương yêu').
6. ⛔ CHỈ ĐƯỢC DÙNG TIẾNG VIỆT. TUYỆT ĐỐI KHÔNG dùng từ tiếng Anh hay ngôn ngữ khác (CẤM 'people', 'hello', 'world'...).
6. Nếu thực sự không tìm được từ nào bắt đầu bằng '${cleanExpFirst}', trả về valid: false để chịu thua.

Trả về duy nhất JSON:
{
  "valid": true/false,
  "reason": "Lý do nếu thua",
  "ai_word": "Cụm 2 tiếng bắt đầu bằng '${cleanExpFirst}' (nếu valid=true)",
  "ai_last_syllable": "Tiếng thứ 2 trong cụm từ của AI"
}`;
  const r = await chatCompletion([
    { role: 'system', content: sysPrompt },
    { role: 'user', content: `Người chơi gửi: '${playerWord}'` }
  ]);
  if (!r.ok) return { type: 'opponent', expectedFirst: cleanExpFirst, playerWord, apiError: r.error, latency: r.latency };
  try {
    const d = JSON.parse(r.content);
    return { type: 'opponent', expectedFirst: cleanExpFirst, playerWord, ...d, latency: r.latency };
  } catch {
    return { type: 'opponent', expectedFirst: cleanExpFirst, playerWord, parseError: true, raw: r.content?.substring(0, 150), latency: r.latency };
  }
}

async function testMultiplayerValidate(word, expectedFirst, usedWords) {
  const cleanExpFirst = cleanSyllable(expectedFirst);
  const usedStr = usedWords.join(', ');
  const sysPrompt = `Bạn là trọng tài trò chơi Nối Từ Tiếng Việt.
QUY TẮC BẮT BUỘC:
1. Cụm từ phải gồm đúng CỤM 2 TIẾNG TIẾNG VIỆT CÓ THẬT, THÔNG DỤNG trong từ điển và đời sống.
2. Tiếng thứ nhất BẮT BUỘC phải khớp với tiếng: '${cleanExpFirst}' (KHÔNG PHÂN BIỆT VIẾT HOA/THƯỜNG).
3. Cụm từ KHÔNG ĐƯỢC trùng với danh sách đã dùng: [${usedStr}].
4. Chấp nhận các cách nói tự nhiên như 'đẹp quá', 'to lắm', 'vui vẻ'. TUYỆT ĐỐI KHÔNG chấp nhận cụm từ ghép bừa vô nghĩa (CẤM 'khoải hứng', 'nhung nhau', 'chơi gà',...).

Trả về duy nhất định dạng JSON:
{
  "valid": true/false,
  "reason": "Lý do ngắn gọn nếu sai (không có nghĩa / không đúng tiếng đầu / đã dùng / không đủ 2 tiếng)",
  "last_syllable": "Tiếng thứ 2 của cụm từ vừa gửi (nếu valid=true)"
}`;
  const r = await chatCompletion([
    { role: 'system', content: sysPrompt },
    { role: 'user', content: `Người chơi gửi: '${word}'` }
  ]);
  if (!r.ok) return { type: 'multiplayer', word, expectedFirst: cleanExpFirst, apiError: r.error };
  try {
    const d = JSON.parse(r.content);
    return { type: 'multiplayer', word, expectedFirst: cleanExpFirst, ...d, latency: r.latency };
  } catch {
    return { type: 'multiplayer', word, expectedFirst: cleanExpFirst, parseError: true, raw: r.content?.substring(0, 100), latency: r.latency };
  }
}

async function testIsRealWord(word) {
  const sysPrompt = `Bạn là trọng tài ngôn ngữ tiếng Việt.
Nhiệm vụ: Trả lời xem cụm 2 tiếng dưới đây có phải là cách nói tự nhiên, có nghĩa thực tế mà người Việt thực sự dùng trong giao tiếp hàng ngày hay không (bao gồm từ ghép, cụm tính từ, cụm danh từ, phó từ thông dụng).

Ví dụ HOÀN TOÀN HỢP LỆ (is_real: true):
- 'đẹp quá', 'đẹp lắm', 'to lắm', 'vui vẻ', 'xa xôi', 'nhung hươu', 'trồng cây', 'yêu thương', 'đẹp trai', 'ăn cơm'.

Ví dụ GIẢ / BỊA VÔ NGHĨA (is_real: false):
- 'khoải hứng', 'nhung nhau', 'cây ghế', 'đẹp nhà'.

Trả về duy nhất JSON: {"is_real": true/false}`;
  const r = await chatCompletion([
    { role: 'system', content: sysPrompt },
    { role: 'user', content: `Cụm từ: '${word}' có phải cách nói tự nhiên, thông dụng trong tiếng Việt không?` }
  ]);
  if (!r.ok) return { type: 'isreal', word, apiError: r.error };
  try {
    const d = JSON.parse(r.content);
    return { type: 'isreal', word, is_real: d.is_real, latency: r.latency };
  } catch {
    return { type: 'isreal', word, parseError: true, raw: r.content?.substring(0, 100), latency: r.latency };
  }
}

module.exports = { chatCompletion, testStarterPhrase, testAiOpponent, testMultiplayerValidate, testIsRealWord, cleanSyllable, AI_MODEL };
