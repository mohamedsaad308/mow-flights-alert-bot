// Curated Hubs (Traffic-ordered international airports)
// Ported directly from bot.py's AIRPORTS_BY_COUNTRY
export const AIRPORTS_BY_COUNTRY = {
  "🇪🇬 Egypt": [
    ["Cairo (CAI)", "CAI"],
    ["Hurghada (HRG)", "HRG"],
    ["Sharm El Sheikh (SSH)", "SSH"],
    ["Alexandria Borg El Arab (HBE)", "HBE"],
  ],
  "🇷🇺 Russia": [
    ["✈️ All Moscow (MOW)", "MOW"],
    ["St. Petersburg (LED)", "LED"],
    ["Sochi (AER)", "AER"],
    ["Kazan (KZN)", "KZN"],
    ["Yekaterinburg (SVX)", "SVX"],
  ],
  "🇹🇷 Turkey": [
    ["Istanbul (IST)", "IST"],
    ["Istanbul Sabiha (SAW)", "SAW"],
    ["Antalya (AYT)", "AYT"],
    ["Ankara (ESB)", "ESB"],
  ],
  "🇦🇪 UAE": [
    ["Dubai (DXB)", "DXB"],
    ["Abu Dhabi (AUH)", "AUH"],
    ["Sharjah (SHJ)", "SHJ"],
  ],
  "🇸🇦 Saudi Arabia": [
    ["Jeddah (JED)", "JED"],
    ["Riyadh (RUH)", "RUH"],
    ["Dammam (DMM)", "DMM"],
    ["Medina (MED)", "MED"],
  ],
};

export function getAirportName(code) {
  for (const hubs of Object.values(AIRPORTS_BY_COUNTRY)) {
    const found = hubs.find(([, c]) => c === code);
    if (found) return found[0];
  }
  return code;
}
