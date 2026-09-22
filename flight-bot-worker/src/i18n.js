// ---------------------------------------------------------------------------
// Minimal i18n layer.
// Add a new language by adding a new top-level key here (e.g. "ar") and
// filling in every string. Any language not present falls back to "en".
// ---------------------------------------------------------------------------

export const SUPPORTED_LANGS = ["en", "ru"];
const DEFAULT_LANG = "en";

// Telegram gives us an IETF tag like "ru", "ru-RU", "en-GB", "ar" etc.
// We only match on the primary subtag (before any "-").
export function resolveLang(languageCode) {
  if (!languageCode) return DEFAULT_LANG;
  const primary = languageCode.split("-")[0].toLowerCase();
  return SUPPORTED_LANGS.includes(primary) ? primary : DEFAULT_LANG;
}

const STRINGS = {
  en: {
    welcome:
      "👋 Hello <b>{username}</b>!\n\n" +
      "Welcome to the <b>Flight Price Watcher</b>.\n" +
      "I track direct flight drops across top hubs in Egypt, Russia, Turkey, UAE, and Saudi Arabia.\n\n" +
      "Choose an option below or use the menu commands:",
    btn_track_new: "➕ Track New Route",
    btn_watchlist: "📋 My Watchlist",
    btn_add_another: "➕ Add Another Route",
    btn_add_route: "➕ Track a Route",
    btn_view_watchlist: "📋 View Watchlist",
    btn_back: "« Back",
    btn_main_menu: "« Main Menu",
    btn_back_to_watchlist: "📋 Back to Watchlist",
    btn_restart: "Restart",
    help_text:
      "💡 <b>How it works:</b>\n\n" +
      "1. Tap <b>Track New Route</b> and select your origin and destination hubs.\n" +
      "2. The system checks prices regularly via Aviasales/Travelpayouts.\n" +
      "3. You will receive an instant Telegram alert whenever a cheaper direct fare appears!\n\n" +
      "Commands:\n/start - Main menu\n/add - Add a route\n/watchlist - View & delete routes",
    step1: "🛫 <b>Step 1/4:</b> Select <b>Departure Country</b>:",
    step2: "🛫 <b>Step 2/4:</b> Select <b>Departure Hub</b> in {country}:",
    step3: "Selected Departure: <b>{origin}</b>\n\n🛬 <b>Step 3/4:</b> Select <b>Arrival Country</b>:",
    step4: "🛬 <b>Step 4/4:</b> Select <b>Arrival Hub</b> in {country}:",
    session_expired: "⚠️ Session expired. Please start over.",
    watchlist_empty: "📋 <b>Your Watchlist is empty.</b>\nYou are not tracking any routes yet.",
    watchlist_active: "📋 <b>Your Active Watchlist:</b>\nTap any route below to stop tracking it:",
    remove_label: "❌ Remove: {origin} ➡️ {destination}",
    already_tracking:
      "ℹ️ <b>Already Tracking!</b>\n\nYou are already receiving alerts for:\n<b>{origin}</b> ➡️ <b>{destination}</b>",
    already_in_watchlist: "ℹ️ <b>Already in Watchlist:</b>\n<b>{origin}</b> ➡️ <b>{destination}</b>",
    alert_active:
      "✅ <b>Alert Active!</b>\n\nRoute: <b>{origin}</b> ➡️ <b>{destination}</b>\n\n" +
      "You will get a direct notification whenever a direct flight price drops!",
    removed: "🗑 Removed <b>{origin} ➡️ {destination}</b> from your watchlist.",
  },
  ru: {
    welcome:
      "👋 Привет, <b>{username}</b>!\n\n" +
      "Добро пожаловать в <b>Flight Price Watcher</b>.\n" +
      "Я отслеживаю снижение цен на прямые рейсы в ключевые аэропорты Египта, России, Турции, ОАЭ и Саудовской Аравии.\n\n" +
      "Выберите вариант ниже или используйте команды меню:",
    btn_track_new: "➕ Отследить маршрут",
    btn_watchlist: "📋 Мой список",
    btn_add_another: "➕ Добавить ещё маршрут",
    btn_add_route: "➕ Отследить маршрут",
    btn_view_watchlist: "📋 Показать список",
    btn_back: "« Назад",
    btn_main_menu: "« Главное меню",
    btn_back_to_watchlist: "📋 Назад к списку",
    btn_restart: "Начать заново",
    help_text:
      "💡 <b>Как это работает:</b>\n\n" +
      "1. Нажмите <b>Отследить маршрут</b> и выберите аэропорты отправления и прибытия.\n" +
      "2. Система регулярно проверяет цены через Aviasales/Travelpayouts.\n" +
      "3. Вы мгновенно получите уведомление в Telegram, как только появится более дешёвый прямой рейс!\n\n" +
      "Команды:\n/start - Главное меню\n/add - Добавить маршрут\n/watchlist - Список и удаление маршрутов",
    step1: "🛫 <b>Шаг 1/4:</b> Выберите <b>страну отправления</b>:",
    step2: "🛫 <b>Шаг 2/4:</b> Выберите <b>аэропорт отправления</b> в {country}:",
    step3: "Отправление: <b>{origin}</b>\n\n🛬 <b>Шаг 3/4:</b> Выберите <b>страну прибытия</b>:",
    step4: "🛬 <b>Шаг 4/4:</b> Выберите <b>аэропорт прибытия</b> в {country}:",
    session_expired: "⚠️ Сессия истекла. Пожалуйста, начните заново.",
    watchlist_empty: "📋 <b>Ваш список пуст.</b>\nВы пока не отслеживаете ни одного маршрута.",
    watchlist_active: "📋 <b>Ваши активные маршруты:</b>\nНажмите на маршрут, чтобы прекратить его отслеживание:",
    remove_label: "❌ Удалить: {origin} ➡️ {destination}",
    already_tracking:
      "ℹ️ <b>Уже отслеживается!</b>\n\nВы уже получаете уведомления по маршруту:\n<b>{origin}</b> ➡️ <b>{destination}</b>",
    already_in_watchlist: "ℹ️ <b>Уже в списке:</b>\n<b>{origin}</b> ➡️ <b>{destination}</b>",
    alert_active:
      "✅ <b>Маршрут добавлен!</b>\n\nМаршрут: <b>{origin}</b> ➡️ <b>{destination}</b>\n\n" +
      "Вы получите уведомление, как только цена на прямой рейс снизится!",
    removed: "🗑 Маршрут <b>{origin} ➡️ {destination}</b> удалён из списка.",
  },
};

// Simple {placeholder} interpolation.
export function t(lang, key, vars = {}) {
  const table = STRINGS[lang] || STRINGS[DEFAULT_LANG];
  let str = table[key] ?? STRINGS[DEFAULT_LANG][key] ?? key;
  for (const [k, v] of Object.entries(vars)) {
    str = str.replaceAll(`{${k}}`, v);
  }
  return str;
}
