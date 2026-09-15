/**
 * NoiTu Stress Test Runner - 15 minutes
 * Phase A: AI opponent generation across many topics/syllables
 * Phase B: Validator accuracy (starter + multiplayer + is_real)
 */
const lib = require('./noitu_test_lib.js');
const fs = require('fs');

const DURATION_MS = parseInt(process.env.TEST_DURATION_MS || '900000');
const CONCURRENCY = parseInt(process.env.CONCURRENCY || '4');

// Diverse test syllables covering many topics
const topicSyllables = [
  // Animals
  ['con', 'con mèo'], ['mèo', 'con mèo'], ['gà', 'con gà'], ['voi', 'con voi'], ['thỏ', 'con thỏ'],
  // Food & drink  
  ['cơm', 'ăn cơm'], ['trà', 'uống trà'], ['bánh', 'làm bánh'], ['phở', 'ăn phở'], ['cà', 'cà phê'],
  // Emotions & adjectives
  ['vui', 'vui vẻ'], ['đẹp', 'rất đẹp'], ['khổ', 'khổ đau'], ['yêu', 'yêu thương'], ['buồn', 'buồn ngủ'],
  // Actions
  ['đi', 'đi học'], ['học', 'học bài'], ['chạy', 'chạy nhanh'], ['ngủ', 'đi ngủ'], ['làm', 'làm việc'],
  // Nature & objects
  ['hoa', 'hoa hồng'], ['cây', 'cây xanh'], ['nước', 'uống nước'], ['trời', 'bầu trời'], ['biển', 'ra biển'],
  // Household
  ['bàn', 'bàn ghế'], ['ghế', 'ghế gỗ'], ['nhà', 'ngôi nhà'], ['cửa', 'cửa sổ'], ['đèn', 'cái đèn'],
  // People & relations
  ['người', 'con người'], ['bạn', 'bạn bè'], ['gia', 'gia đình'], ['anh', 'anh trai'], ['em', 'em gái'],
  // Abstract
  ['sự', 'sự việc'], ['cuộc', 'cuộc sống'], ['niềm', 'niềm vui'], ['tâm', 'tâm hồn'], ['ý', 'ý nghĩa'],
  // Tricky ones (hard to chain)
  ['lắm', 'to lắm'], ['quá', 'đẹp quá'], ['rất', 'rất tốt'], ['hơn', 'nhiều hơn'], ['nhất', 'tốt nhất'],
  // Edge-prone syllables
  ['khoản', 'khoản tiền'], ['nghiệp', 'sự nghiệp'], ['dụng', 'sử dụng'], ['tính', 'tính cách'], ['luận', 'bàn luận'],
];

const starterPhrases = [
  // Valid starters
  { phrase: 'con mèo', expectValid: true },
  { phrase: 'ăn cơm', expectValid: true },
  { phrase: 'vui vẻ', expectValid: true },
  { phrase: 'hoa hồng', expectValid: true },
  { phrase: 'đi học', expectValid: true },
  { phrase: 'bầu trời', expectValid: true },
  { phrase: 'đẹp quá', expectValid: true },
  { phrase: 'yêu thương', expectValid: true },
  // Invalid starters  
  { phrase: 'khoải hứng', expectValid: false },  // fake word
  { phrase: 'nhung nhau', expectValid: false },  // fake word
  { phrase: 'hello world', expectValid: false }, // English
  { phrase: 'xyz abc', expectValid: false },     // nonsense
  { phrase: 'hệ thống', expectValid: null },     // technical term - borderline
  { phrase: 'vô tuyến', expectValid: null },     // Hán Việt - borderline
];

const isRealWords = [
  // Should be TRUE
  { w: 'đẹp trai', expect: true }, { w: 'ăn cơm', expect: true }, { w: 'xa xôi', expect: true },
  { w: 'nhung hươu', expect: true }, { w: 'trồng cây', expect: true }, { w: 'đi học', expect: true },
  { w: 'bàn ghế', expect: true }, { w: 'mưa rào', expect: true },
  // Should be FALSE
  { w: 'khoải hứng', expect: false }, { w: 'nhung nhau', expect: false }, 
  { w: 'cây ghế', expect: false }, { w: 'đẹp nhà', expect: false },
  { w: 'bàn bay', expect: false }, { w: 'trời ăn', expect: false },
];

async function main() {
  const startTime = Date.now();
  const results = [];
  let seq = 0;
  
  console.log(`=== NoiTu Stress Test | ${DURATION_MS/1000}s | concurrency ${CONCURRENCY} ===`);
  console.log(`Model: ${lib.AI_MODEL}`);
  console.log(`Start: ${new Date().toISOString()}`);

  // Track used words per game simulation
  const globalUsed = new Set();

  async function worker(workerId) {
    let i = workerId;
    while (Date.now() - startTime < DURATION_MS) {
      const idx = i % topicSyllables.length;
      const [syllable, playerWord] = topicSyllables[idx];
      
      try {
        // Main test: AI opponent generation
        const r = await lib.testAiOpponent(syllable, playerWord, [...globalUsed].slice(-30));
        results.push(r);
        
        if (r.valid && r.ai_word) {
          globalUsed.add(r.ai_word.toLowerCase());
          // Every 3rd result: also validate the AI's word via multiplayer validator
          if (results.filter(x => x.type === 'opponent' && x.valid).length % 3 === 0) {
            const mv = await lib.testMultiplayerValidate(r.ai_word, syllable, []);
            results.push({ ...mv, crossCheckOf: r.ai_word });
            // Also check is_real on AI's last syllable chains occasionally
          }
        }
      } catch (e) {
        results.push({ type: 'opponent', expectedFirst: syllable, crashError: e.message.substring(0, 80) });
      }
      
      // Rotate through validator tests every N iterations
      if ((i + workerId) % 7 === 0 && Date.now() - startTime < DURATION_MS) {
        const sp = starterPhrases[(i / 7 | 0) % starterPhrases.length];
        try {
          const sr = await lib.testStarterPhrase(sp.phrase);
          sr.expectValid = sp.expectValid;
          results.push(sr);
        } catch (e) {
          results.push({ type: 'starter', phrase: sp.phrase, crashError: e.message.substring(0, 80) });
        }
      }
      
      if ((i + workerId) % 11 === 0 && Date.now() - startTime < DURATION_MS) {
        const rw = isRealWords[(i / 11 | 0) % isRealWords.length];
        try {
          const ir = await lib.testIsRealWord(rw.w);
          ir.expect = rw.expect;
          results.push(ir);
        } catch (e) {
          results.push({ type: 'isreal', word: rw.w, crashError: e.message.substring(0, 80) });
        }
      }
      
      i += CONCURRENCY;
    }
  }

  // Progress reporter
  const progressTimer = setInterval(() => {
    const elapsed = Math.round((Date.now() - startTime) / 1000);
    console.log(`[${elapsed}s] requests done: ${results.length}`);
    fs.writeFileSync('/home/dsh/workspace/Server/discord-ai-bot/noitu_test_results.json', JSON.stringify(results));
  }, 30000);

  // Launch workers
  await Promise.all(Array.from({ length: CONCURRENCY }, (_, w) => worker(w)));
  clearInterval(progressTimer);
  
  fs.writeFileSync('/home/dsh/workspace/Server/discord-ai-bot/noitu_test_results.json', JSON.stringify(results, null, 2));
  console.log(`\n=== DONE: ${results.length} total requests in ${Math.round((Date.now()-startTime)/1000)}s ===`);
}

main().catch(e => { console.error('FATAL:', e); process.exit(1); });
