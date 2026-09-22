#!/usr/bin/env node
/**
 * Load test for the flight-bot Cloudflare Worker.
 *
 * Simulates concurrent Telegram users sending /start by POSTing synthetic
 * Telegram Update payloads directly to your deployed Worker URL — no real
 * Telegram traffic involved.
 *
 * ⚠️  This WILL write real rows into your Supabase `users` table (one per
 * fake chat_id). Use a chat_id range you can identify and clean up
 * afterward (this script uses 900000000+N by default) — don't run it
 * against a production table without a cleanup plan.
 *
 * Usage:
 *   node load-test.js <worker-url> [concurrency] [total-requests]
 *
 * Example:
 *   node load-test.js https://flight-bot-worker.you.workers.dev 20 500
 */

const WORKER_URL = process.argv[2];
const CONCURRENCY = parseInt(process.argv[3] || "10", 10);
const TOTAL_REQUESTS = parseInt(process.argv[4] || "200", 10);
const FAKE_CHAT_ID_BASE = 900000000;

if (!WORKER_URL) {
  console.error("Usage: node load-test.js <worker-url> [concurrency] [total-requests]");
  process.exit(1);
}

function buildStartUpdate(updateId, chatId) {
  return {
    update_id: updateId,
    message: {
      message_id: updateId,
      date: Math.floor(Date.now() / 1000),
      chat: { id: chatId, type: "private" },
      from: {
        id: chatId,
        is_bot: false,
        first_name: "LoadTest",
        username: `loadtest_${chatId}`,
        language_code: Math.random() < 0.5 ? "ru" : "en",
      },
      text: "/start",
      entities: [{ offset: 0, length: 6, type: "bot_command" }],
    },
  };
}

async function sendOne(index) {
  const chatId = FAKE_CHAT_ID_BASE + index;
  const payload = buildStartUpdate(index, chatId);
  const started = Date.now();
  try {
    const res = await fetch(WORKER_URL, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    const ms = Date.now() - started;
    return { ok: res.ok, status: res.status, ms };
  } catch (err) {
    return { ok: false, status: 0, ms: Date.now() - started, error: String(err) };
  }
}

async function runPool(total, concurrency) {
  const results = [];
  let next = 0;

  async function worker() {
    while (next < total) {
      const i = next++;
      results.push(await sendOne(i));
    }
  }

  const workers = Array.from({ length: concurrency }, () => worker());
  await Promise.all(workers);
  return results;
}

function summarize(results) {
  const ok = results.filter((r) => r.ok);
  const failed = results.filter((r) => !r.ok);
  const durations = results.map((r) => r.ms).sort((a, b) => a - b);
  const p50 = durations[Math.floor(durations.length * 0.5)];
  const p95 = durations[Math.floor(durations.length * 0.95)];
  const p99 = durations[Math.floor(durations.length * 0.99)];
  const avg = durations.reduce((a, b) => a + b, 0) / durations.length;

  console.log("\n--- Load test results ---");
  console.log(`Total requests : ${results.length}`);
  console.log(`Succeeded      : ${ok.length}`);
  console.log(`Failed         : ${failed.length}`);
  console.log(`Avg latency    : ${avg.toFixed(0)} ms`);
  console.log(`p50 latency    : ${p50} ms`);
  console.log(`p95 latency    : ${p95} ms`);
  console.log(`p99 latency    : ${p99} ms`);

  if (failed.length > 0) {
    const statusCounts = {};
    for (const f of failed) {
      const key = f.error ? `error: ${f.error}` : `status ${f.status}`;
      statusCounts[key] = (statusCounts[key] || 0) + 1;
    }
    console.log("\nFailure breakdown:");
    for (const [key, count] of Object.entries(statusCounts)) {
      console.log(`  ${key}: ${count}`);
    }
  }
}

(async () => {
  console.log(`Firing ${TOTAL_REQUESTS} requests at ${WORKER_URL} (concurrency ${CONCURRENCY})...`);
  const start = Date.now();
  const results = await runPool(TOTAL_REQUESTS, CONCURRENCY);
  const totalSeconds = (Date.now() - start) / 1000;
  summarize(results);
  console.log(`\nWall time: ${totalSeconds.toFixed(1)}s (${(TOTAL_REQUESTS / totalSeconds).toFixed(1)} req/s)`);
})();
