# Flight Bot — Cloudflare Worker Deployment (from zero)

This is bot.py ported to JavaScript (grammY) so it can run on Cloudflare
Workers' free plan — no credit card required. flight_bot.py (your GitHub
Actions batch job) and your Supabase schema are unchanged; only the
interactive bot.py component moved here.

## 0. Prerequisites

- Node.js installed on your machine (v18+). Check with `node -v`.
  If you don't have it: https://nodejs.org (LTS version).
- Your Telegram bot token (from @BotFather).
- Your Supabase project URL and service/anon key (same ones bot.py used).

## 1. Create your Cloudflare account

1. Go to https://dash.cloudflare.com/sign-up
2. Sign up with email + password (no card, no domain needed).
3. Verify your email via the link Cloudflare sends you.

## 2. Get this project onto your machine

Download/copy the `flight-bot-worker` folder (the one this README is in)
onto your computer, then open a terminal inside it:

```bash
cd flight-bot-worker
```

## 3. Install dependencies

```bash
npm install
```

This pulls in `grammy` (Telegram bot framework), `@supabase/supabase-js`,
and `wrangler` (Cloudflare's CLI).

## 4. Log in to Cloudflare from the CLI

```bash
npx wrangler login
```

This opens a browser tab — click "Allow" to authorize the CLI against the
account you just created.

## 5. Add your secrets

These are stored encrypted on Cloudflare's side, never in your code:

```bash
npx wrangler secret put TG_BOT_TOKEN
npx wrangler secret put SUPABASE_URL
npx wrangler secret put SUPABASE_KEY
```

Each command pauses and asks you to paste the value, then hit Enter.

## 6. Test it locally (optional but recommended)

```bash
npx wrangler dev
```

This runs the Worker on your machine at a local URL. You can't receive
real Telegram webhooks here without extra tunneling setup, so this step
is mainly to catch syntax/import errors before deploying. Ctrl+C to stop.

## 7. Deploy

```bash
npx wrangler deploy
```

Wrangler builds and uploads the Worker, then prints a live URL like:

```
https://flight-bot-worker.<your-subdomain>.workers.dev
```

Copy that URL — you need it next.

## 8. Point Telegram at your Worker

Run this once (replace `<TOKEN>` and `<YOUR_WORKER_URL>`):

```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=<YOUR_WORKER_URL>"
```

You should get back `{"ok":true,"result":true,...}`.

## 9. Test in Telegram

Open your bot in Telegram and send `/start`. You should see the welcome
message and the same 4-step Track New Route flow as before.

## Redeploying after code changes

Any time you edit files in `src/`, just run:

```bash
npx wrangler deploy
```

No need to re-set the webhook or secrets — those stay in place.

## Notes on what changed vs. bot.py

- **No `context.user_data` / session storage.** Workers has no memory
  between requests, so the selected departure airport is passed forward
  inside the callback_data string itself (e.g. `arr_country:CAI:Turkey`)
  instead of being stored server-side. Functionally identical to the
  user, just implemented statelessly.
- **Logging** goes to `console.log`/`console.error`, viewable live with
  `npx wrangler tail` after deploying, instead of Python's `logging`
  module.
- Everything else (menu structure, Supabase table/column names, upsert
  logic, duplicate handling) mirrors bot.py exactly.
