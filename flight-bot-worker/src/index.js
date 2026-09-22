import { Bot, InlineKeyboard, webhookCallback } from "grammy";
import { createClient } from "@supabase/supabase-js";
import { AIRPORTS_BY_COUNTRY, getAirportName } from "./airports.js";

// ---------------------------------------------------------------------------
// NOTE on statelessness:
// python-telegram-bot's context.user_data persists in memory between a
// user's messages because polling runs one long-lived process. Cloudflare
// Workers has no persistent memory between requests, so instead of storing
// the selected origin airport in memory, we encode it directly into the
// callback_data string for the next step (e.g. "arr_country:CAI:Turkey").
// This needs no KV/session storage and matches how Workers is meant to run.
// ---------------------------------------------------------------------------

function buildBot(env) {
  const bot = new Bot(env.TG_BOT_TOKEN);
  const supabase = createClient(env.SUPABASE_URL, env.SUPABASE_KEY);

  const mainMenuKeyboard = new InlineKeyboard()
    .text("➕ Track New Route", "menu:add").row()
    .text("📋 My Watchlist", "menu:list");

  const countryKeyboard = (prefix) => {
    const kb = new InlineKeyboard();
    for (const country of Object.keys(AIRPORTS_BY_COUNTRY)) {
      kb.text(country, `${prefix}${country}`).row();
    }
    return kb;
  };

  async function showMainMenu(ctx) {
    const chatId = ctx.chat.id;
    const username = ctx.from.username || ctx.from.first_name;

    try {
      await supabase
        .from("users")
        .upsert({ chat_id: chatId, username }, { onConflict: "chat_id" });
    } catch (e) {
      console.error("Error saving user:", e);
    }

    const text =
      `👋 Hello <b>${username}</b>!\n\n` +
      `Welcome to the <b>Flight Price Watcher</b>.\n` +
      `I track direct flight drops across top hubs in Egypt, Russia, Turkey, UAE, and Saudi Arabia.\n\n` +
      `Choose an option below or use the menu commands:`;

    if (ctx.callbackQuery) {
      await ctx.editMessageText(text, { reply_markup: mainMenuKeyboard, parse_mode: "HTML" });
    } else {
      await ctx.reply(text, { reply_markup: mainMenuKeyboard, parse_mode: "HTML" });
    }
  }

  async function showWatchlist(ctx) {
    const chatId = ctx.chat.id;
    const { data: subs } = await supabase
      .from("subscriptions")
      .select("id, origin, destination")
      .eq("chat_id", chatId);

    if (!subs || subs.length === 0) {
      const kb = new InlineKeyboard()
        .text("➕ Track a Route", "menu:add").row()
        .text("« Main Menu", "menu:home");
      const text = "📋 <b>Your Watchlist is empty.</b>\nYou are not tracking any routes yet.";
      if (ctx.callbackQuery) {
        await ctx.editMessageText(text, { reply_markup: kb, parse_mode: "HTML" });
      } else {
        await ctx.reply(text, { reply_markup: kb, parse_mode: "HTML" });
      }
      return;
    }

    const kb = new InlineKeyboard();
    for (const s of subs) {
      kb.text(`❌ Remove: ${s.origin} ➡️ ${s.destination}`, `delete_sub:${s.id}:${s.origin}:${s.destination}`).row();
    }
    kb.text("➕ Add Another", "menu:add").row();
    kb.text("« Main Menu", "menu:home");

    const text = "📋 <b>Your Active Watchlist:</b>\nTap any route below to stop tracking it:";
    if (ctx.callbackQuery) {
      await ctx.editMessageText(text, { reply_markup: kb, parse_mode: "HTML" });
    } else {
      await ctx.reply(text, { reply_markup: kb, parse_mode: "HTML" });
    }
  }

  // -------------------------------------------------------------------------
  // Commands
  // -------------------------------------------------------------------------

  bot.command(["start", "menu"], showMainMenu);

  bot.command("help", async (ctx) => {
    const text =
      "💡 <b>How it works:</b>\n\n" +
      "1. Tap <b>Track New Route</b> and select your origin and destination hubs.\n" +
      "2. The system checks prices regularly via Aviasales/Travelpayouts.\n" +
      "3. You will receive an instant Telegram alert whenever a cheaper direct fare appears!\n\n" +
      "Commands:\n/start - Main menu\n/add - Add a route\n/watchlist - View & delete routes";
    await ctx.reply(text, { parse_mode: "HTML" });
  });

  bot.command("add", async (ctx) => {
    const kb = countryKeyboard("dep_country:");
    kb.text("« Back", "menu:home");
    await ctx.reply("🛫 <b>Step 1/4:</b> Select <b>Departure Country</b>:", {
      reply_markup: kb,
      parse_mode: "HTML",
    });
  });

  bot.command("watchlist", showWatchlist);

  // -------------------------------------------------------------------------
  // Callback query router
  // -------------------------------------------------------------------------

  bot.on("callback_query:data", async (ctx) => {
    const data = ctx.callbackQuery.data;
    const chatId = ctx.chat.id;
    await ctx.answerCallbackQuery();

    // 1. Main menu
    if (data === "menu:home") {
      return showMainMenu(ctx);
    }

    // 2. Step 1: Select Departure Country
    if (data === "menu:add") {
      const kb = countryKeyboard("dep_country:");
      kb.text("« Back", "menu:home");
      return ctx.editMessageText("🛫 <b>Step 1/4:</b> Select <b>Departure Country</b>:", {
        reply_markup: kb,
        parse_mode: "HTML",
      });
    }

    // 3. Step 2: Select Departure Hub
    if (data.startsWith("dep_country:")) {
      const country = data.split(":").slice(1).join(":");
      const airports = AIRPORTS_BY_COUNTRY[country] || [];
      const kb = new InlineKeyboard();
      for (const [name, code] of airports) kb.text(name, `set_origin:${code}`).row();
      kb.text("« Back", "menu:add");
      return ctx.editMessageText(`🛫 <b>Step 2/4:</b> Select <b>Departure Hub</b> in ${country}:`, {
        reply_markup: kb,
        parse_mode: "HTML",
      });
    }

    // 4. Step 3: Select Arrival Country (origin carried in callback_data)
    if (data.startsWith("set_origin:")) {
      const originCode = data.split(":")[1];
      const originName = getAirportName(originCode);
      const kb = new InlineKeyboard();
      for (const country of Object.keys(AIRPORTS_BY_COUNTRY)) {
        kb.text(country, `arr_country:${originCode}:${country}`).row();
      }
      kb.text("« Back", "menu:add");
      return ctx.editMessageText(
        `Selected Departure: <b>${originName}</b>\n\n🛬 <b>Step 3/4:</b> Select <b>Arrival Country</b>:`,
        { reply_markup: kb, parse_mode: "HTML" }
      );
    }

    // 5. Step 4: Select Arrival Hub
    if (data.startsWith("arr_country:")) {
      const [, originCode, ...rest] = data.split(":");
      const country = rest.join(":");
      const airports = AIRPORTS_BY_COUNTRY[country] || [];
      const kb = new InlineKeyboard();
      for (const [name, code] of airports) {
        if (code !== originCode) kb.text(name, `confirm_sub:${originCode}:${code}`).row();
      }
      kb.text("« Back", "menu:add");
      return ctx.editMessageText(`🛬 <b>Step 4/4:</b> Select <b>Arrival Hub</b> in ${country}:`, {
        reply_markup: kb,
        parse_mode: "HTML",
      });
    }

    // 6. Save Subscription (with duplicate handling)
    if (data.startsWith("confirm_sub:")) {
      const [, originCode, destCode] = data.split(":");
      const origName = getAirportName(originCode);
      const destName = getAirportName(destCode);

      const { data: existing } = await supabase
        .from("subscriptions")
        .select("id")
        .eq("chat_id", chatId)
        .eq("origin", originCode)
        .eq("destination", destCode);

      const kb = new InlineKeyboard()
        .text("➕ Add Another Route", "menu:add").row()
        .text("📋 View Watchlist", "menu:list");

      if (existing && existing.length > 0) {
        return ctx.editMessageText(
          `ℹ️ <b>Already Tracking!</b>\n\nYou are already receiving alerts for:\n<b>${origName}</b> ➡️ <b>${destName}</b>`,
          { reply_markup: kb, parse_mode: "HTML" }
        );
      }

      const { error } = await supabase
        .from("subscriptions")
        .upsert(
          { chat_id: chatId, origin: originCode, destination: destCode },
          { onConflict: "chat_id,origin,destination" }
        );

      if (error) {
        console.warn("Database duplicate caught:", error);
        return ctx.editMessageText(
          `ℹ️ <b>Already in Watchlist:</b>\n<b>${origName}</b> ➡️ <b>${destName}</b>`,
          { reply_markup: kb, parse_mode: "HTML" }
        );
      }

      return ctx.editMessageText(
        `✅ <b>Alert Active!</b>\n\nRoute: <b>${origName}</b> ➡️ <b>${destName}</b>\n\n` +
          `You will get a direct notification whenever a direct flight price drops!`,
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
        .text("📋 Back to Watchlist", "menu:list").row()
        .text("« Main Menu", "menu:home");
      return ctx.editMessageText(`🗑 Removed <b>${orig} ➡️ ${dest}</b> from your watchlist.`, {
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
