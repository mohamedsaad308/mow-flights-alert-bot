import { Bot, InlineKeyboard, webhookCallback } from "grammy";
import { createClient } from "@supabase/supabase-js";
import {
  COUNTRY_ORDER,
  AIRPORTS_BY_COUNTRY,
  getCountryName,
  getAirportName,
} from "./airports.js";
import { resolveLang, t } from "./i18n.js";

// ---------------------------------------------------------------------------
// Language: Telegram sends the user's client language on every update as
// ctx.from.language_code (e.g. "ru", "ru-RU", "en"). We resolve it once per
// update and thread it through every reply — no user setting needed, no
// storage required. Unsupported languages silently fall back to English.
//
// Statelessness note (unchanged from before): the selected origin airport
// still travels inside callback_data between steps, since Workers has no
// memory between requests. Country selection uses neutral ids (e.g.
// "egypt") instead of display names now, so callback_data never contains
// language-specific text.
// ---------------------------------------------------------------------------

const TP_PRICES_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates";
const AFFILIATE_MARKER = "776670"; // keep in sync with flight_bot.py
const DATE_RANGE_DAYS = 10;        // keep in sync with flight_bot.py
const FETCH_TIMEOUT_MS = 5000;

// ---------------------------------------------------------------------------
// Travelpayouts helpers (mirror the logic in flight_bot.py)
// ---------------------------------------------------------------------------

function windowDates(days) {
  const now = new Date();
  const out = [];
  for (let i = 0; i < days; i++) {
    const d = new Date(
      Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + i)
    );
    out.push(d.toISOString().slice(0, 10));
  }
  return out;
}

// Returns { status, fare }:
//   "unsupported" -> every query got HTTP 400 and nothing was found
//   "ok"          -> fare is the cheapest direct flight in the window, or null if none
//   "error"       -> API failed and nothing was found (fail open: caller still subscribes)
async function fetchCheapestFare(origin, destination, env) {
  const dates = windowDates(DATE_RANGE_DAYS);
  const inWindow = new Set(dates);
  const months = [...new Set(dates.map((d) => d.slice(0, 7)))];

  const results = await Promise.all(
    months.map(async (month) => {
      const url =
        `${TP_PRICES_URL}?origin=${origin}&destination=${destination}` +
        `&departure_at=${month}&one_way=true&direct=true&sorting=price` +
        `&limit=1000&currency=usd&token=${env.TP_TOKEN}`;
      try {
        const res = await fetch(url, { signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
        if (res.status === 400) return { kind: "unsupported" };
        if (!res.ok) {
          const body = (await res.text()).slice(0, 200);
          console.error(`Travelpayouts ${origin}->${destination} ${month}: HTTP ${res.status} ${body}`);
          return { kind: "error" };
        }
        const payload = await res.json();
        if (payload.success === false) {
          console.error(`Travelpayouts ${origin}->${destination} ${month}: success=false`, JSON.stringify(payload).slice(0, 200));
          return { kind: "error" };
        }
        return { kind: "ok", data: payload.data || [] };
      } catch (e) {
        console.error(`Fare fetch failed ${origin}->${destination} ${month}:`, e);
        return { kind: "error" };
      }
    })
  );

  let best = null;
  let unsupported = 0;
  let failed = 0;
  for (const r of results) {
    if (r.kind === "unsupported") { unsupported++; failed++; continue; }
    if (r.kind === "error") { failed++; continue; }
    for (const f of r.data) {
      if ((f.transfers ?? 0) !== 0 || f.price == null) continue;
      if (!inWindow.has((f.departure_at || "").slice(0, 10))) continue;
      if (!best || f.price < best.price) best = f;
    }
  }

  if (best) return { status: "ok", fare: best };
  if (unsupported === months.length) return { status: "unsupported", fare: null };
  if (failed > 0) return { status: "error", fare: null };
  return { status: "ok", fare: null };
}

function aviasalesLink(origin, destination, departDate) {
  const [, month, day] = departDate.split("-");
  return `https://www.aviasales.ru/search/${origin}${day}${month}${destination}1?marker=${AFFILIATE_MARKER}`;
}

function formatDuration(minutes) {
  if (!minutes) return "";
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return ` · ${h}h${m ? String(m).padStart(2, "0") + "m" : ""}`;
}

function formatDeparture(departureAt) {
  if (!departureAt) return "unknown";
  return departureAt.slice(0, 16).replace("T", " ");
}

function snapshotText(lang, originCode, destCode, origName, destName, fare) {
  if (!fare) {
    return t(lang, "snapshot_none", {
      origin: origName,
      destination: destName,
      days: String(DATE_RANGE_DAYS),
    });
  }
  return t(lang, "snapshot_found", {
    origin: origName,
    destination: destName,
    price: String(fare.price),
    duration: formatDuration(fare.duration),
    departure: formatDeparture(fare.departure_at),
    flight: `${fare.airline || ""}${fare.flight_number || ""}`,
    link: aviasalesLink(originCode, destCode, (fare.departure_at || "").slice(0, 10)),
  });
}

function buildBot(env) {
  const bot = new Bot(env.TG_BOT_TOKEN);
  const supabase = createClient(env.SUPABASE_URL, env.SUPABASE_KEY);

  const mainMenuKeyboard = (lang) =>
    new InlineKeyboard()
      .text(t(lang, "btn_track_new"), "menu:add").row()
      .text(t(lang, "btn_watchlist"), "menu:list");

  const countryKeyboard = (prefix, lang) => {
    const kb = new InlineKeyboard();
    for (const key of COUNTRY_ORDER) {
      kb.text(getCountryName(key, lang), `${prefix}${key}`).row();
    }
    return kb;
  };

  // Sends the instant price message after a subscription is saved.
  // The subscription row was already saved with welcomed = (status === "ok").
  // If sending fails, welcomed goes back to false so the scheduled job
  // sends the snapshot instead.
  async function deliverSnapshot(ctx, lang, chatId, originCode, destCode, origName, destName, result) {
    try {
      if (result.status === "ok") {
        await ctx.reply(
          snapshotText(lang, originCode, destCode, origName, destName, result.fare),
          { parse_mode: "HTML", link_preview_options: { is_disabled: true } }
        );
      } else {
        await ctx.reply(t(lang, "snapshot_delayed"), { parse_mode: "HTML" });
      }
    } catch (e) {
      console.error("Snapshot send failed:", e);
      if (result.status === "ok") {
        await supabase
          .from("subscriptions")
          .update({ welcomed: false })
          .eq("chat_id", chatId)
          .eq("origin", originCode)
          .eq("destination", destCode);
      }
    }
  }

  async function showMainMenu(ctx) {
    const lang = resolveLang(ctx.from?.language_code);
    const chatId = ctx.chat.id;
    const username = ctx.from.username || ctx.from.first_name;

    try {
      await supabase
        .from("users")
        .upsert({ chat_id: chatId, username }, { onConflict: "chat_id" });
    } catch (e) {
      console.error("Error saving user:", e);
    }

    const text = t(lang, "welcome", { username });
    const opts = { reply_markup: mainMenuKeyboard(lang), parse_mode: "HTML" };
    if (ctx.callbackQuery) {
      await ctx.editMessageText(text, opts);
    } else {
      await ctx.reply(text, opts);
    }
  }

  async function showWatchlist(ctx) {
    const lang = resolveLang(ctx.from?.language_code);
    const chatId = ctx.chat.id;
    const { data: subs } = await supabase
      .from("subscriptions")
      .select("id, origin, destination")
      .eq("chat_id", chatId)
      .eq("disabled", false);

    if (!subs || subs.length === 0) {
      const kb = new InlineKeyboard()
        .text(t(lang, "btn_add_route"), "menu:add").row()
        .text(t(lang, "btn_main_menu"), "menu:home");
      const opts = { reply_markup: kb, parse_mode: "HTML" };
      if (ctx.callbackQuery) await ctx.editMessageText(t(lang, "watchlist_empty"), opts);
      else await ctx.reply(t(lang, "watchlist_empty"), opts);
      return;
    }

    const kb = new InlineKeyboard();
    for (const s of subs) {
      kb.text(
        t(lang, "remove_label", { origin: s.origin, destination: s.destination }),
        `delete_sub:${s.id}:${s.origin}:${s.destination}`
      ).row();
    }
    kb.text(t(lang, "btn_add_another"), "menu:add").row();
    kb.text(t(lang, "btn_main_menu"), "menu:home");

    const opts = { reply_markup: kb, parse_mode: "HTML" };
    if (ctx.callbackQuery) await ctx.editMessageText(t(lang, "watchlist_active"), opts);
    else await ctx.reply(t(lang, "watchlist_active"), opts);
  }

  // -------------------------------------------------------------------------
  // Commands
  // -------------------------------------------------------------------------

  bot.command(["start", "menu"], showMainMenu);

  bot.command("help", async (ctx) => {
    const lang = resolveLang(ctx.from?.language_code);
    await ctx.reply(t(lang, "help_text"), { parse_mode: "HTML" });
  });

  bot.command("add", async (ctx) => {
    const lang = resolveLang(ctx.from?.language_code);
    const kb = countryKeyboard("dep_country:", lang);
    kb.text(t(lang, "btn_back"), "menu:home");
    await ctx.reply(t(lang, "step1"), { reply_markup: kb, parse_mode: "HTML" });
  });

  bot.command("watchlist", showWatchlist);

  // -------------------------------------------------------------------------
  // Callback query router
  // -------------------------------------------------------------------------

  bot.on("callback_query:data", async (ctx) => {
    const lang = resolveLang(ctx.from?.language_code);
    const data = ctx.callbackQuery.data;
    const chatId = ctx.chat.id;
    await ctx.answerCallbackQuery();

    // 1. Main menu
    if (data === "menu:home") {
      return showMainMenu(ctx);
    }

    // 2. Step 1: Select Departure Country
    if (data === "menu:add") {
      const kb = countryKeyboard("dep_country:", lang);
      kb.text(t(lang, "btn_back"), "menu:home");
      return ctx.editMessageText(t(lang, "step1"), { reply_markup: kb, parse_mode: "HTML" });
    }

    // 3. Step 2: Select Departure Hub
    if (data.startsWith("dep_country:")) {
      const countryKey = data.split(":")[1];
      const airports = AIRPORTS_BY_COUNTRY[countryKey] || [];
      const kb = new InlineKeyboard();
      for (const [code, names] of airports) {
        kb.text(names[lang] || names.en, `set_origin:${code}`).row();
      }
      kb.text(t(lang, "btn_back"), "menu:add");
      return ctx.editMessageText(
        t(lang, "step2", { country: getCountryName(countryKey, lang) }),
        { reply_markup: kb, parse_mode: "HTML" }
      );
    }

    // 4. Step 3: Select Arrival Country (origin carried in callback_data)
    if (data.startsWith("set_origin:")) {
      const originCode = data.split(":")[1];
      const originName = getAirportName(originCode, lang);
      const kb = new InlineKeyboard();
      for (const key of COUNTRY_ORDER) {
        kb.text(getCountryName(key, lang), `arr_country:${originCode}:${key}`).row();
      }
      kb.text(t(lang, "btn_back"), "menu:add");
      return ctx.editMessageText(t(lang, "step3", { origin: originName }), {
        reply_markup: kb,
        parse_mode: "HTML",
      });
    }

    // 5. Step 4: Select Arrival Hub
    if (data.startsWith("arr_country:")) {
      const [, originCode, countryKey] = data.split(":");
      const airports = AIRPORTS_BY_COUNTRY[countryKey] || [];
      const kb = new InlineKeyboard();
      for (const [code, names] of airports) {
        if (code !== originCode) {
          kb.text(names[lang] || names.en, `confirm_sub:${originCode}:${code}`).row();
        }
      }
      kb.text(t(lang, "btn_back"), "menu:add");
      return ctx.editMessageText(
        t(lang, "step4", { country: getCountryName(countryKey, lang) }),
        { reply_markup: kb, parse_mode: "HTML" }
      );
    }

    // 6. Save Subscription: validate route + fetch current price in one go,
    //    save, confirm, then send the instant price message.
    if (data.startsWith("confirm_sub:")) {
      const [, originCode, destCode] = data.split(":");
      const origName = getAirportName(originCode, lang);
      const destName = getAirportName(destCode, lang);

      const result = await fetchCheapestFare(originCode, destCode, env);

      // Reject routes the price API doesn't support
      if (result.status === "unsupported") {
        const back = new InlineKeyboard()
          .text(t(lang, "btn_add_another"), "menu:add").row()
          .text(t(lang, "btn_main_menu"), "menu:home");
        return ctx.editMessageText(
          t(lang, "route_unsupported", { origin: origName, destination: destName }),
          { reply_markup: back, parse_mode: "HTML" }
        );
      }

      // welcomed = true only if we have a price result to send right now;
      // otherwise the scheduled job sends the first snapshot.
      const welcomedNow = result.status === "ok";

      const { data: existing } = await supabase
        .from("subscriptions")
        .select("id, disabled")
        .eq("chat_id", chatId)
        .eq("origin", originCode)
        .eq("destination", destCode);

      const kb = new InlineKeyboard()
        .text(t(lang, "btn_add_another"), "menu:add").row()
        .text(t(lang, "btn_view_watchlist"), "menu:list");

      if (existing && existing.length > 0) {
        // Previously auto-disabled as unsupported, but supported now: re-enable
        if (existing[0].disabled) {
          await supabase
            .from("subscriptions")
            .update({ disabled: false, welcomed: welcomedNow })
            .eq("id", existing[0].id);
          await ctx.editMessageText(
            t(lang, "alert_active", { origin: origName, destination: destName }),
            { reply_markup: kb, parse_mode: "HTML" }
          );
          return deliverSnapshot(ctx, lang, chatId, originCode, destCode, origName, destName, result);
        }
        return ctx.editMessageText(
          t(lang, "already_tracking", { origin: origName, destination: destName }),
          { reply_markup: kb, parse_mode: "HTML" }
        );
      }

      const { error } = await supabase
        .from("subscriptions")
        .upsert(
          {
            chat_id: chatId,
            origin: originCode,
            destination: destCode,
            welcomed: welcomedNow,
            disabled: false,
          },
          { onConflict: "chat_id,origin,destination" }
        );

      if (error) {
        console.warn("Database duplicate caught:", error);
        return ctx.editMessageText(
          t(lang, "already_in_watchlist", { origin: origName, destination: destName }),
          { reply_markup: kb, parse_mode: "HTML" }
        );
      }

      await ctx.editMessageText(
        t(lang, "alert_active", { origin: origName, destination: destName }),
        { reply_markup: kb, parse_mode: "HTML" }
      );
      return deliverSnapshot(ctx, lang, chatId, originCode, destCode, origName, destName, result);
    }

    // 7. List Active Subscriptions
    if (data === "menu:list") {
      return showWatchlist(ctx);
    }

    // 8. Delete Subscription
    if (data.startsWith("delete_sub:")) {
      const [, subId, orig, dest] = data.split(":");
      await supabase.from("subscriptions").delete().eq("id", Number(subId));

      const kb = new InlineKeyboard()
        .text(t(lang, "btn_back_to_watchlist"), "menu:list").row()
        .text(t(lang, "btn_main_menu"), "menu:home");
      return ctx.editMessageText(t(lang, "removed", { origin: orig, destination: dest }), {
        reply_markup: kb,
        parse_mode: "HTML",
      });
    }
  });

  bot.catch((err) => {
    console.error("Bot error:", err);
  });

  return bot;
}

export default {
  async fetch(request, env) {
    try {
      const bot = buildBot(env);
      return await webhookCallback(bot, "cloudflare-mod")(request);
    } catch (err) {
      console.error("Worker error:", err);
      return new Response("Internal error", { status: 500 });
    }
  },
};