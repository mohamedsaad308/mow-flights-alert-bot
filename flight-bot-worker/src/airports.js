// ---------------------------------------------------------------------------
// Airports are keyed by a neutral country id (not tied to any language),
// so callback_data and lookups stay stable regardless of the user's
// Telegram language. Each country and airport carries an { en, ru } name.
// ---------------------------------------------------------------------------

export const COUNTRY_ORDER = ["egypt", "russia", "belarus", "turkey", "uae", "saudi"];

export const COUNTRY_NAMES = {
  egypt: { en: "🇪🇬 Egypt", ru: "🇪🇬 Египет" },
  russia: { en: "🇷🇺 Russia", ru: "🇷🇺 Россия" },
  belarus: { en: "🇧🇾 Belarus", ru: "🇧🇾 Беларусь" },
  turkey: { en: "🇹🇷 Turkey", ru: "🇹🇷 Турция" },
  uae: { en: "🇦🇪 UAE", ru: "🇦🇪 ОАЭ" },
  saudi: { en: "🇸🇦 Saudi Arabia", ru: "🇸🇦 Саудовская Аравия" },
};

// code -> { en, ru }
export const AIRPORTS_BY_COUNTRY = {
  egypt: [
    ["CAI", { en: "Cairo (CAI)", ru: "Каир (CAI)" }],
    ["HRG", { en: "Hurghada (HRG)", ru: "Хургада (HRG)" }],
    ["SSH", { en: "Sharm El Sheikh (SSH)", ru: "Шарм-эль-Шейх (SSH)" }],
    ["HBE", { en: "Alexandria Borg El Arab (HBE)", ru: "Александрия — Борг-эль-Араб (HBE)" }],
  ],
  russia: [
    ["MOW", { en: "✈️ All Moscow (MOW)", ru: "✈️ Вся Москва (MOW)" }],
    ["SVO", { en: "Sheremetyevo (SVO)", ru: "Шереметьево (SVO)" }],
    ["DME", { en: "Domodedovo (DME)", ru: "Домодедово (DME)" }],
    ["VKO", { en: "Vnukovo (VKO)", ru: "Внуково (VKO)" }],
    ["LED", { en: "St. Petersburg (LED)", ru: "Санкт-Петербург (LED)" }],
    ["AER", { en: "Sochi (AER)", ru: "Сочи (AER)" }],
    ["KZN", { en: "Kazan (KZN)", ru: "Казань (KZN)" }],
    ["SVX", { en: "Yekaterinburg (SVX)", ru: "Екатеринбург (SVX)" }],
  ],
  belarus: [
    ["MSQ", { en: "Minsk (MSQ)", ru: "Минск (MSQ)" }],
  ],
  turkey: [
    ["IST", { en: "Istanbul (IST)", ru: "Стамбул (IST)" }],
    ["SAW", { en: "Istanbul Sabiha (SAW)", ru: "Стамбул — Сабиха (SAW)" }],
    ["AYT", { en: "Antalya (AYT)", ru: "Анталья (AYT)" }],
    ["ESB", { en: "Ankara (ESB)", ru: "Анкара (ESB)" }],
  ],
  uae: [
    ["DXB", { en: "Dubai (DXB)", ru: "Дубай (DXB)" }],
    ["AUH", { en: "Abu Dhabi (AUH)", ru: "Абу-Даби (AUH)" }],
    ["SHJ", { en: "Sharjah (SHJ)", ru: "Шарджа (SHJ)" }],
  ],
  saudi: [
    ["JED", { en: "Jeddah (JED)", ru: "Джидда (JED)" }],
    ["RUH", { en: "Riyadh (RUH)", ru: "Эр-Рияд (RUH)" }],
    ["DMM", { en: "Dammam (DMM)", ru: "Даммам (DMM)" }],
    ["MED", { en: "Medina (MED)", ru: "Медина (MED)" }],
  ],
};

export function getCountryName(countryKey, lang) {
  return COUNTRY_NAMES[countryKey]?.[lang] || COUNTRY_NAMES[countryKey]?.en || countryKey;
}

export function getAirportName(code, lang) {
  for (const hubs of Object.values(AIRPORTS_BY_COUNTRY)) {
    const found = hubs.find(([c]) => c === code);
    if (found) return found[1][lang] || found[1].en;
  }
  return code;
}