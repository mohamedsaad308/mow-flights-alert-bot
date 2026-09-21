#!/usr/bin/env python3
"""
Interactive Telegram Bot for Flight Price Alert Subscriptions.
Provides inline keyboards for selecting countries, curated hubs,
and managing active alerts in Supabase.

Includes a lightweight background HTTP server to satisfy Web Service health checks.
"""

import os
import sys
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from supabase import create_client, Client
from postgrest.exceptions import APIError

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Environment variables
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

if not TG_BOT_TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: TG_BOT_TOKEN, SUPABASE_URL, and SUPABASE_KEY must be set.", file=sys.stderr)
    sys.exit(1)

# Initialize Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ---------------------------------------------------------------------------
# Background HTTP Server for Web Service Health Checks
# ---------------------------------------------------------------------------

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Flight Bot is running!")

    def log_message(self, format, *args):
        # Silence routine health check GET logs to keep terminal/output clean
        pass


def start_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"Health check HTTP server started on port {port}")
    server.serve_forever()


# ---------------------------------------------------------------------------
# Curated Hubs (Traffic-ordered international airports)
# ---------------------------------------------------------------------------
AIRPORTS_BY_COUNTRY = {
    "🇪🇬 Egypt": [
        ("Cairo (CAI)", "CAI"),
        ("Hurghada (HRG)", "HRG"),
        ("Sharm El Sheikh (SSH)", "SSH"),
        ("Alexandria Borg El Arab (HBE)", "HBE"),
    ],
    "🇷🇺 Russia": [
        ("✈️ All Moscow (MOW)", "MOW"),
        ("St. Petersburg (LED)", "LED"),
        ("Sochi (AER)", "AER"),
        ("Kazan (KZN)", "KZN"),
        ("Yekaterinburg (SVX)", "SVX"),
    ],
    "🇹🇷 Turkey": [
        ("Istanbul (IST)", "IST"),
        ("Istanbul Sabiha (SAW)", "SAW"),
        ("Antalya (AYT)", "AYT"),
        ("Ankara (ESB)", "ESB"),
    ],
    "🇦🇪 UAE": [
        ("Dubai (DXB)", "DXB"),
        ("Abu Dhabi (AUH)", "AUH"),
        ("Sharjah (SHJ)", "SHJ"),
    ],
    "🇸🇦 Saudi Arabia": [
        ("Jeddah (JED)", "JED"),
        ("Riyadh (RUH)", "RUH"),
        ("Dammam (DMM)", "DMM"),
        ("Medina (MED)", "MED"),
    ],
}


def get_airport_name(code: str) -> str:
    for country, hubs in AIRPORTS_BY_COUNTRY.items():
        for name, c in hubs:
            if c == code:
                return name
    return code


# ---------------------------------------------------------------------------
# Setup Telegram Commands Menu
# ---------------------------------------------------------------------------

async def setup_bot_commands(application: Application):
    """Registers commands in the Telegram client UI menu button."""
    commands = [
        BotCommand("start", "Launch main menu"),
        BotCommand("add", "Track a new flight route"),
        BotCommand("watchlist", "View & manage your active alerts"),
        BotCommand("help", "How this bot works"),
    ]
    await application.bot.set_my_commands(commands)


# ---------------------------------------------------------------------------
# Command Handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Register user and show main menu."""
    chat_id = update.effective_chat.id
    username = update.effective_user.username or update.effective_user.first_name

    # Register/update user
    try:
        supabase.table("users").upsert({
            "chat_id": chat_id,
            "username": username,
        }, on_conflict="chat_id").execute()
    except Exception as e:
        logger.error(f"Error saving user: {e}")

    keyboard = [
        [InlineKeyboardButton("➕ Track New Route", callback_data="menu:add")],
        [InlineKeyboardButton("📋 My Watchlist", callback_data="menu:list")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    welcome_text = (
        f"👋 Hello <b>{username}</b>!\n\n"
        "Welcome to the <b>Flight Price Watcher</b>.\n"
        "I track direct flight drops across top hubs in Egypt, Russia, Turkey, UAE, and Saudi Arabia.\n\n"
        "Choose an option below or use the menu commands:"
    )

    if update.message:
        await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode="HTML")
    elif update.callback_query:
        await update.callback_query.edit_message_text(welcome_text, reply_markup=reply_markup, parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "💡 <b>How it works:</b>\n\n"
        "1. Tap <b>Track New Route</b> and select your origin and destination hubs.\n"
        "2. The system checks prices regularly via Aviasales/Travelpayouts.\n"
        "3. You will receive an instant Telegram alert whenever a cheaper direct fare appears!\n\n"
        "Commands:\n"
        "/start - Main menu\n"
        "/add - Add a route\n"
        "/watchlist - View & delete routes"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick command to jump straight to route creation."""
    buttons = [
        [InlineKeyboardButton(country, callback_data=f"dep_country:{country}")]
        for country in AIRPORTS_BY_COUNTRY.keys()
    ]
    buttons.append([InlineKeyboardButton("« Back", callback_data="menu:home")])
    await update.message.reply_text(
        "🛫 <b>Step 1/4:</b> Select <b>Departure Country</b>:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="HTML",
    )


async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick command to view active alerts."""
    chat_id = update.effective_chat.id
    res = supabase.table("subscriptions").select("id, origin, destination").eq("chat_id", chat_id).execute()
    subs = res.data or []

    if not subs:
        keyboard = [
            [InlineKeyboardButton("➕ Track a Route", callback_data="menu:add")],
            [InlineKeyboardButton("« Main Menu", callback_data="menu:home")],
        ]
        await update.message.reply_text(
            "📋 <b>Your Watchlist is empty.</b>\nYou are not tracking any routes yet.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )
        return

    buttons = []
    for s in subs:
        orig = s["origin"]
        dest = s["destination"]
        buttons.append([
            InlineKeyboardButton(
                f"❌ Remove: {orig} ➡️ {dest}",
                callback_data=f"delete_sub:{s['id']}:{orig}:{dest}",
            )
        ])
    buttons.append([InlineKeyboardButton("➕ Add Another", callback_data="menu:add")])
    buttons.append([InlineKeyboardButton("« Main Menu", callback_data="menu:home")])

    await update.message.reply_text(
        "📋 <b>Your Active Watchlist:</b>\n"
        "Tap any route below to stop tracking it:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Callback Query Handler
# ---------------------------------------------------------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id

    # 1. Main menu
    if data == "menu:home":
        await start(update, context)

    # 2. Step 1: Select Departure Country
    elif data == "menu:add":
        buttons = [
            [InlineKeyboardButton(country, callback_data=f"dep_country:{country}")]
            for country in AIRPORTS_BY_COUNTRY.keys()
        ]
        buttons.append([InlineKeyboardButton("« Back", callback_data="menu:home")])
        await query.edit_message_text(
            "🛫 <b>Step 1/4:</b> Select <b>Departure Country</b>:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="HTML",
        )

    # 3. Step 2: Select Departure Hub
    elif data.startswith("dep_country:"):
        country = data.split(":", 1)[1]
        airports = AIRPORTS_BY_COUNTRY.get(country, [])
        buttons = [
            [InlineKeyboardButton(name, callback_data=f"set_origin:{code}")]
            for name, code in airports
        ]
        buttons.append([InlineKeyboardButton("« Back", callback_data="menu:add")])
        await query.edit_message_text(
            f"🛫 <b>Step 2/4:</b> Select <b>Departure Hub</b> in {country}:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="HTML",
        )

    # 4. Step 3: Select Arrival Country
    elif data.startswith("set_origin:"):
        origin_code = data.split(":", 1)[1]
        context.user_data["origin"] = origin_code
        origin_name = get_airport_name(origin_code)

        buttons = [
            [InlineKeyboardButton(country, callback_data=f"arr_country:{country}")]
            for country in AIRPORTS_BY_COUNTRY.keys()
        ]
        buttons.append([InlineKeyboardButton("« Back", callback_data="menu:add")])
        await query.edit_message_text(
            f"Selected Departure: <b>{origin_name}</b>\n\n"
            f"🛬 <b>Step 3/4:</b> Select <b>Arrival Country</b>:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="HTML",
        )

    # 5. Step 4: Select Arrival Hub
    elif data.startswith("arr_country:"):
        country = data.split(":", 1)[1]
        origin_code = context.user_data.get("origin")
        airports = AIRPORTS_BY_COUNTRY.get(country, [])

        buttons = [
            [InlineKeyboardButton(name, callback_data=f"confirm_sub:{code}")]
            for name, code in airports
            if code != origin_code
        ]
        buttons.append([InlineKeyboardButton("« Back", callback_data="menu:add")])
        await query.edit_message_text(
            f"🛬 <b>Step 4/4:</b> Select <b>Arrival Hub</b> in {country}:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="HTML",
        )

    # 6. Save Subscription (with duplicate handling)
    elif data.startswith("confirm_sub:"):
        dest_code = data.split(":", 1)[1]
        origin_code = context.user_data.get("origin")

        if not origin_code:
            await query.edit_message_text(
                "⚠️ Session expired. Please start over.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Restart", callback_data="menu:home")]]),
            )
            return

        orig_name = get_airport_name(origin_code)
        dest_name = get_airport_name(dest_code)

        existing = (
            supabase.table("subscriptions")
            .select("id")
            .eq("chat_id", chat_id)
            .eq("origin", origin_code)
            .eq("destination", dest_code)
            .execute()
        )

        keyboard = [
            [InlineKeyboardButton("➕ Add Another Route", callback_data="menu:add")],
            [InlineKeyboardButton("📋 View Watchlist", callback_data="menu:list")],
        ]

        if existing.data and len(existing.data) > 0:
            await query.edit_message_text(
                f"ℹ️ <b>Already Tracking!</b>\n\n"
                f"You are already receiving alerts for:\n"
                f"<b>{orig_name}</b> ➡️ <b>{dest_name}</b>",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML",
            )
            return

        try:
            supabase.table("subscriptions").upsert({
                "chat_id": chat_id,
                "origin": origin_code,
                "destination": dest_code,
            }, on_conflict="chat_id,origin,destination").execute()

            await query.edit_message_text(
                f"✅ <b>Alert Active!</b>\n\n"
                f"Route: <b>{orig_name}</b> ➡️ <b>{dest_name}</b>\n\n"
                f"You will get a direct notification whenever a direct flight price drops!",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML",
            )
        except APIError as e:
            logger.warning(f"Database duplicate caught: {e}")
            await query.edit_message_text(
                f"ℹ️ <b>Already in Watchlist:</b>\n"
                f"<b>{orig_name}</b> ➡️ <b>{dest_name}</b>",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML",
            )

    # 7. List Active Subscriptions
    elif data == "menu:list":
        res = supabase.table("subscriptions").select("id, origin, destination").eq("chat_id", chat_id).execute()
        subs = res.data or []

        if not subs:
            keyboard = [
                [InlineKeyboardButton("➕ Track a Route", callback_data="menu:add")],
                [InlineKeyboardButton("« Back", callback_data="menu:home")],
            ]
            await query.edit_message_text(
                "📋 <b>Your Watchlist is empty.</b>\nYou are not tracking any routes yet.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML",
            )
            return

        buttons = []
        for s in subs:
            orig = s["origin"]
            dest = s["destination"]
            buttons.append([
                InlineKeyboardButton(
                    f"❌ Remove: {orig} ➡️ {dest}",
                    callback_data=f"delete_sub:{s['id']}:{orig}:{dest}",
                )
            ])
        buttons.append([InlineKeyboardButton("➕ Add Another", callback_data="menu:add")])
        buttons.append([InlineKeyboardButton("« Main Menu", callback_data="menu:home")])

        await query.edit_message_text(
            "📋 <b>Your Active Watchlist:</b>\n"
            "Tap any route below to stop tracking it:",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="HTML",
        )

    # 8. Delete Subscription
    elif data.startswith("delete_sub:"):
        parts = data.split(":")
        sub_id = int(parts[1])
        orig = parts[2]
        dest = parts[3]

        supabase.table("subscriptions").delete().eq("id", sub_id).execute()

        keyboard = [
            [InlineKeyboardButton("📋 Back to Watchlist", callback_data="menu:list")],
            [InlineKeyboardButton("« Main Menu", callback_data="menu:home")],
        ]
        await query.edit_message_text(
            f"🗑 Removed <b>{orig} ➡️ {dest}</b> from your watchlist.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)


# ---------------------------------------------------------------------------
# Main Entrypoint
# ---------------------------------------------------------------------------

def main():
    # 1. Start the HTTP health server on a background daemon thread
    http_thread = threading.Thread(target=start_health_server, daemon=True)
    http_thread.start()

    # 2. Build and run the Telegram bot
    application = (
        Application.builder()
        .token(TG_BOT_TOKEN)
        .post_init(setup_bot_commands)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", start))
    application.add_handler(CommandHandler("add", add_command))
    application.add_handler(CommandHandler("watchlist", watchlist_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_error_handler(global_error_handler)

    print("🤖 Flight Watcher Bot is running in polling mode...")
    application.run_polling()


if __name__ == "__main__":
    main()