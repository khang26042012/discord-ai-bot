/**
 * Test script: Gửi embed giả lập BetterAntiDupe webhook để test bot
 * 
 * Cách dùng:
 *   1. Tạo webhook trong kênh Discord ID 1540691749754769499
 *   2. Copy URL webhook vào file .env: TEST_WEBHOOK_URL=https://discord.com/api/webhooks/xxxxx/yyyyy
 *   3. Chạy: node test_webhook.js
 */

require('dotenv').config();

const WEBHOOK_URL = process.env.TEST_WEBHOOK_URL;

if (!WEBHOOK_URL) {
  console.error('❌ Thiếu TEST_WEBHOOK_URL trong file .env');
  console.error('   Thêm dòng: TEST_WEBHOOK_URL=https://discord.com/api/webhooks/ID/TOKEN');
  process.exit(1);
}

async function sendFakeDupeAlert() {
  // Cấu trúc embed mô phỏng BetterAntiDupe webhook thật
  const embed = {
    title: '⚠️ Dupe Detected',
    description: 'Player **PE_KhangKYT** was caught attempting to duplicate items.',
    color: 0xFF0000,
    fields: [
      { name: 'Player', value: 'PE_KhangKYT', inline: true },
      { name: 'Type', value: 'Shulker Box Dupe', inline: true },
      { name: 'Location', value: 'World: world | X: 123 Y: 64 Z: -456', inline: false },
      { name: 'Action Taken', value: 'Items removed & logged', inline: false }
    ],
    footer: { text: 'BetterAntiDupe v3.2.1' },
    timestamp: new Date().toISOString()
  };

  const payload = {
    username: 'BetterAntiDupe',
    avatar_url: 'https://cdn.discordapp.com/embed/avatars/0.png',
    embeds: [embed]
  };

  console.log('📤 Sending fake BetterAntiDupe alert...');
  console.log('   Channel: 1540691749754769499');
  console.log('   Player: PE_KhangKYT');
  console.log('');

  try {
    const res = await fetch(WEBHOOK_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });

    if (res.ok || res.status === 204) {
      console.log('✅ Embed sent successfully!');
      console.log('   → Bot should now delete this message and post the custom replacement.');
    } else {
      const errText = await res.text();
      console.error(`❌ Failed to send: HTTP ${res.status}`);
      console.error(`   Response: ${errText.substring(0, 300)}`);
    }
  } catch (e) {
    console.error(`❌ Error: ${e.message}`);
  }
}

sendFakeDupeAlert();
