# ✈️ Direct Flight Alerts: Russia/Belarus ↔ Egypt

A free Telegram bot that watches **direct (non-stop) flights** between Moscow, Saint Petersburg, Minsk and other cities and Egyptian airports (Hurghada, Cairo, Sharm El Sheikh), and notifies you when direct flights appear or the cheapest fare drops.

Built because checking Aviasales and ticket channels by hand every hour is exhausting.

**Channel:** https://t.me/mosccowflightalerts

> ⚠️ **Data is cached, not live.** Prices come from Travelpayouts' cache of real user searches. A fare can be stale, sold out, or occasionally mislabeled as direct. Treat every alert as a *candidate* and always verify through the included Aviasales link before booking.

---

## What it does

- Tracks **direct flights only**, one-way, in both directions.
- Looks at a rolling **30-day window** from the day it runs.
- Compares the **cheapest fare per route** (any airline, any flight, any date in the window). Airline and flight number are shown for information only.
- Sends a Telegram alert when:
  - 🆕 direct flights appear on a route that had none, or
  - 📉 the cheapest fare drops meaningfully (small few-dollar wobbles are ignored).
- Routes with no direct flights are shown as **"none found"** rather than silently omitted, and an API failure is shown as **"unknown"** so it is never mistaken for "no flights".
- Every alert includes an **Aviasales deep link** so you can check the real price in one tap.
- Prices are shown in rubles with an approximate dollar equivalent (CBR rate).
- No change, no message.

## How it works

```
GitHub Actions (every 20 min)
        │
        ▼
 flight_bot.py (Python)
   ├─ Travelpayouts  aviasales/v3/prices_for_dates   (direct=true, one-way)
   ├─ keep only transfers == 0, trim to the date window
   ├─ compare against stored baselines (database)
   └─ send alerts via Telegram Bot API

 Cloudflare Worker (TypeScript)
   └─ Telegram side: user commands, route subscriptions,
      airport list and validation
```

### Baseline logic (why it doesn't go silent)

A naive "cheapest price ever seen" tracker breaks quickly: one stale cached fare sets a floor that real prices never beat, and the rolling window compares unlike dates. This bot avoids both:

- **Price-based per route.** The baseline is the current record low for the route, regardless of which flight produced it.
- **Miss counter.** If no fare at or below the recorded price appears for **N consecutive runs** (default `N = 3`), the baseline is reset silently to the current cheapest. A bogus low heals itself within a few runs instead of muting the route forever.
- **Long TTL as a safety net.** Very old baselines are expired regardless.
- **Empty-result grace period.** A route is only treated as "gone" after several confirmed-empty days, so one empty cache read doesn't cause the same fare to be re-announced as new.
- **API errors ≠ no flights.** Failed requests are counted separately from genuinely empty results. HTTP 400 from Travelpayouts is treated as an *unsupported route* (the subscriber is told and retries stop).

## Setup

### Requirements

- Python 3.12+
- A [Travelpayouts](https://www.travelpayouts.com/) API token (free)
- A Telegram bot token (from [@BotFather](https://t.me/BotFather)) and a chat or channel ID
- A Supabase project for state storage *(if you use the hosted-state version)*
- A Cloudflare account for the Worker *(only for the subscription/commands side)*

### Install

```bash
git clone <your-repo-url>
cd <your-repo>
pip install -r requirements.txt
```

### Environment variables

| Variable | Purpose |
|---|---|
| `TP_TOKEN` | Travelpayouts API token |
| `TG_BOT_TOKEN` | Telegram bot token |
| `TG_CHAT_ID` | Chat/channel that receives alerts |
| `SUPABASE_URL` | Supabase project URL *(name may differ in your code)* |
| `SUPABASE_SECRET_KEY` | Supabase **secret** key. Server-side only, never ship it to a client. RLS is enabled on public tables. |

Never commit these. In GitHub, add them under **Settings → Secrets and variables → Actions**.

### Run locally

```bash
python flight_bot.py --dry-run --verbose   # prints alerts instead of sending, shows every fare found
python flight_bot.py                       # real run
```

| Flag | Effect |
|---|---|
| `--dry-run` | Print alerts to the terminal instead of sending to Telegram; state is not written |
| `--verbose` | Print every direct fare found in the window |
| `--force-summary` | Send the summary even if nothing changed |
| `--daily-queries` | Query one date at a time instead of one month at a time (more thorough, about 30 calls per route) |
| `--days N` | Size of the rolling window (default 30) |

### Scheduling

The workflow runs on a schedule via GitHub Actions (every 20 minutes). GitHub's own cron can drift at short intervals; if that bites you, trigger `workflow_dispatch` from an external scheduler such as cron-job.org instead. The workflow uses a `concurrency` group so overlapping runs queue rather than collide.

### Testing changes safely

Use a separate branch with its own bot token, chat ID and secrets, verify with `--dry-run`, then merge into `master`. That keeps experiments away from real subscribers.

## Configuration

Main settings live at the top of `flight_bot.py`:

| Setting | Meaning |
|---|---|
| `DATE_RANGE_DAYS` | Rolling window size (30) |
| `MIN_PRICE_DROP_USD` | Ignore drops smaller than this |
| `MAX_DROPS_PER_ROUTE` | Cap on drops reported per route per run |
| `EMPTY_GRACE_DAYS` | Confirmed-empty days before a route is forgotten |
| `CURRENCY` | API currency |

Supported airports are defined in the Worker's `airports.js`. Adding a city means adding its IATA code there.

## Known limitations

- **Cached data.** Not a live fare check. This is a deliberate trade-off: the real-time search API requires large-scale partner access that a personal project can't get.
- **Direct flag isn't proof.** `direct=true` in the cache can be wrong; the Aviasales link is the source of truth.
- **Charter flights** often show up as the cheapest fares and may have strict rules (no changes or refunds, baggage limits). Check the fare conditions.
- **Free-tier limits.** Everything is designed to stay within free tiers of GitHub Actions, Supabase, Cloudflare Workers and Travelpayouts.

## Roadmap

- Send new subscribers the current price immediately instead of waiting for the next drop
- Admin-only Worker commands (e.g. a strict-format command to add airports by IATA code)
- More origin cities (e.g. Yekaterinburg)

## Contributing

Ideas, bug reports and route requests are welcome. Open an issue, or message the Telegram channel.

## License

Add your license here (e.g. MIT).
