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

// Only a 400 means "unsupported pair". Network errors / 5xx fail open,
// so an API hiccup never blocks users from subscribing.
async function isRouteSupported(origin, destination, env) {
  const month = new Date().toISOString().slice(0, 7); // e.g. 2026-09
  const url =
    `${TP_PRICES_URL}?origin=${origin}&destination=${destination}` +
    `&departure_at=${month}&one_way=true&direct=true&limit=1&currency=usd` +
    `&token=${env.TP_TOKEN}`;
  try {
    const res = await fetch(url);
    return res.status !== 400;
  } catch (e) {
    console.error("Route check failed, allowing subscription:", e);
    return true;
  }
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

    // 6. Save Subscription (validate route, then handle duplicates)
    if (data.startsWith("confirm_sub:")) {
      const [, originCode, destCode] = data.split(":");
      const origName = getAirportName(originCode, lang);
      const destName = getAirportName(destCode, lang);

      // Reject routes the price API doesn't support
      if (!(await isRouteSupported(originCode, destCode, env))) {
        const back = new InlineKeyboard()
          .text(t(lang, "btn_add_another"), "menu:add").row()
          .text(t(lang, "btn_main_menu"), "menu:home");
        return ctx.editMessageText(
          t(lang, "route_unsupported", { origin: origName, destination: destName }),
          { reply_markup: back, parse_mode: "HTML" }
        );
      }

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
            .update({ disabled: false, welcomed: false })
            .eq("id", existing[0].id);
          return ctx.editMessageText(
            t(lang, "alert_active", { origin: origName, destination: destName }),
            { reply_markup: kb, parse_mode: "HTML" }
          );
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
            welcomed: false,
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

      return ctx.editMessageText(
        t(lang, "alert_active", { origin: origName, destination: destName }),
        { reply_markup: kb, parse_mode: "HTML" }
      );
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