#!/usr/bin/env python3
"""
Direct-flight price watcher: MOW (Moscow) / LED (Saint Petersburg) / MSQ (Minsk)
-> HRG (Hurghada) / CAI (Cairo)

Checks a rolling date window (today through DATE_RANGE_DAYS-1 days ahead)
using Travelpayouts' aviasales/v3/prices_for_dates endpoint.

IMPORTANT LIMITATION: this data comes from Travelpayouts' cache of real user
searches (NOT a live check), and this endpoint doesn't expose a freshness
timestamp for individual fares. A fare tagged "direct" here can be stale or
occasionally mislabeled (e.g. a route that's since become connection-only,
or already sold out). Treat every alert as a candidate to verify via the
included link, never as a guarantee of what you'll find when you click
through. This has already been observed in practice (a cached $159 direct
LED->HRG/UJ736 fare that did not appear as non-stop on the live site) --
that specific fare is now hardcoded into IGNORED_FLIGHTS below.

What it does
------------
- For each route, for each day in the window, asks for the cheapest direct
  (non-stop) one-way fare(s) found for that specific date.
- Keeps a small JSON cache (flight_state.json) keyed by route+date+flight
  number, so it can tell "new flight appeared" from "price changed on a
  flight we already know about."
- Only sends Telegram messages when something actually changed since the
  last run -- an unchanged run sends nothing, so hourly scheduling (e.g.
  via GitHub Actions) doesn't spam you every hour with the same data.
- Use --dry-run to print alerts to the terminal instead of sending them,
  and --verbose to see every fare found, not just new/changed ones.

Setup
-----
1. pip install -r requirements.txt
2. Set environment variables: TP_TOKEN, TG_BOT_TOKEN, TG_CHAT_ID
3. Run:  python flight_bot.py --dry-run --verbose   (for testing)
         python flight_bot.py                        (for real alerts)
4. Schedule with cron, Task Scheduler, or GitHub Actions (see README notes).

Route codes
-----------
- MOW = Moscow (city code, covers SVO/DME/VKO)
- LED = Saint Petersburg
- MSQ = Minsk
- HRG = Hurghada, CAI = Cairo
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TP_TOKEN = os.environ.get("TP_TOKEN", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

# (origin, destination) pairs to check. City codes cover all airports in
# that city (e.g. MOW = all Moscow airports, LED = Saint Petersburg).
ROUTES = [
    ("MOW", "HRG"),  # Moscow -> Hurghada
    ("MOW", "CAI"),  # Moscow -> Cairo
    ("LED", "HRG"),  # Saint Petersburg -> Hurghada
    ("LED", "CAI"),  # Saint Petersburg -> Cairo
    ("MSQ", "HRG"),  # Minsk -> Hurghada
    ("MSQ", "CAI"),  # Minsk -> Cairo
]

# Specific (origin, destination, flight_number) combos to hardcode-ignore --
# confirmed via manual check on aviasales.com to be stale/mislabeled cache
# entries that don't reflect a real bookable non-stop.
IGNORED_FLIGHTS = {
    ("LED", "HRG", "UJ736"),
}

CURRENCY = "usd"
DATE_RANGE_DAYS = 7  # today through today + (DATE_RANGE_DAYS - 1)
REQUEST_PAUSE_SECONDS = 0.15  # small pause between API calls to be gentle on rate limits
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flight_state.json")
MIN_PRICE_DROP_USD = 10  # ignore drops smaller than this -- avoid $1 noise

TP_PRICES_FOR_DATES_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
TG_SEND_URL_TMPL = "https://api.telegram.org/bot{token}/sendMessage"

BIG_WARNING = (
    "⚠️⚠️⚠️ <b>IMPORTANT</b> ⚠️⚠️⚠️\n"
    "This data is from a search <b>cache</b>, not a live check. Fares shown may "
    "already be sold out, expired, or actually require a connection. "
    "<b>Always confirm on Aviasales before booking anything.</b>"
)


# ---------------------------------------------------------------------------
# State (JSON file -- easy to commit/diff in git, unlike a binary DB file)
# ---------------------------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def state_key(origin, destination, depart_date, flight_number):
    return f"{origin}|{destination}|{depart_date}|{flight_number}"


# ---------------------------------------------------------------------------
# Travelpayouts API
# ---------------------------------------------------------------------------

def daterange(days_ahead):
    today = datetime.now().date()
    for i in range(days_ahead):
        yield today + timedelta(days=i)


def fetch_direct_fares(origin, destination):
    """Return a list of direct (0-stop) one-way fares for a route, across
    the configured date window, using aviasales/v3/prices_for_dates."""
    results = []
    for day in daterange(DATE_RANGE_DAYS):
        params = {
            "origin": origin,
            "destination": destination,
            "departure_at": day.strftime("%Y-%m-%d"),
            "one_way": "true",
            "direct": "true",
            "sorting": "price",
            "limit": 30,
            "currency": CURRENCY,
            "token": TP_TOKEN,
        }
        try:
            resp = requests.get(TP_PRICES_FOR_DATES_URL, params=params, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"[{origin}->{destination} {day}] API error: {e}", file=sys.stderr)
            continue

        payload = resp.json()
        for item in payload.get("data", []):
            if item.get("transfers", 0) != 0:
                continue  # defensive: enforce non-stop ourselves too
            flight_number = f"{item.get('airline', '')}{item.get('flight_number', '')}"
            if (origin, destination, flight_number) in IGNORED_FLIGHTS:
                continue
            results.append(item)

        time.sleep(REQUEST_PAUSE_SECONDS)
    return results


# ---------------------------------------------------------------------------
# Aviasales deep link + formatting helpers
# ---------------------------------------------------------------------------

def aviasales_link(origin, destination, depart_date_str):
    d = datetime.strptime(depart_date_str, "%Y-%m-%d")
    segment = f"{origin}{d:%d%m}{destination}1"  # 1 adult passenger
    return f"https://www.aviasales.com/search/{segment}"


def format_duration(minutes):
    if minutes is None:
        return "unknown"
    hours, mins = divmod(int(minutes), 60)
    return f"{hours}h{mins:02d}m" if mins else f"{hours}h"


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram_message(text, dry_run=False):
    if dry_run:
        print("--- Would send to Telegram ---")
        print(text)
        print("-------------------------------")
        return
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("Telegram not configured; message would have been:\n", text)
        return
    url = TG_SEND_URL_TMPL.format(token=TG_BOT_TOKEN)
    resp = requests.post(
        url,
        data={
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    if not resp.ok:
        print(f"Telegram send failed: {resp.status_code} {resp.text}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------

def summarize_route(origin, destination, fares):
    """Print + return the one-line 'does this route have direct flights' summary."""
    route_label = f"{origin} -> {destination}"
    if fares:
        cheapest = min(fares, key=lambda f: f.get("price", float("inf")))
        depart_date = cheapest.get("departure_at", "")[:10]
        link = aviasales_link(origin, destination, depart_date)
        line = (f"[{route_label}] {len(fares)} direct flight(s) found in the next "
                f"{DATE_RANGE_DAYS} days (cheapest: ${cheapest.get('price')} on "
                f"{depart_date}, {format_duration(cheapest.get('duration'))}) -> {link}")
    else:
        line = f"[{route_label}] No direct flights found in the next {DATE_RANGE_DAYS} days."
    print(line)
    return line


def check_route(origin, destination, fares, state):
    """Compare the cheapest fare found this run against the recorded 'best
    price ever seen' for this route. Returns a single event dict if this
    run's cheapest fare is a new record low, else None. Mutates `state`."""
    if not fares:
        return None

    cheapest = min(fares, key=lambda f: f.get("price", float("inf")))
    price = cheapest.get("price")
    if price is None:
        return None

    key = f"cheapest|{origin}|{destination}"
    baseline = state.get(key)

    if baseline is not None and (baseline - price) < MIN_PRICE_DROP_USD:
        return None  # not a new low, or too small a drop to bother you about

    depart_date = cheapest.get("departure_at", "").split("T")[0]
    event = {
        "origin": origin,
        "destination": destination,
        "price": price,
        "old_price": baseline,  # None on first-ever sighting for this route
        "departure_at": cheapest.get("departure_at"),
        "flight_number": f"{cheapest.get('airline', '')}{cheapest.get('flight_number', '')}",
        "duration": cheapest.get("duration"),
        "link": aviasales_link(origin, destination, depart_date),
    }
    state[key] = price
    return event


def build_event_message(ev):
    duration_note = f" ({format_duration(ev['duration'])} flight)" if ev.get("duration") else ""
    if ev["old_price"] is None:
        headline = f"🆕 First direct fare seen for <b>{ev['origin']} → {ev['destination']}</b>"
    else:
        headline = (f"📉 New record-low price for <b>{ev['origin']} → {ev['destination']}</b>: "
                    f"${ev['old_price']} → <b>${ev['price']}</b>")
    return (f"{headline}\n"
            f"Departs: {ev['departure_at']}  Flight: {ev['flight_number']}{duration_note}\n"
            f"{ev['link']}")


def main():
    parser = argparse.ArgumentParser(description="Direct flight price watcher")
    parser.add_argument("--dry-run", action="store_true",
                         help="Print alerts to the terminal instead of sending them to Telegram.")
    parser.add_argument("--verbose", action="store_true",
                         help="Print every direct fare found in the window, not just record-low ones.")
    parser.add_argument("--force-summary", action="store_true",
                         help="Send the summary message even if nothing changed this run.")
    args = parser.parse_args()

    if not TP_TOKEN:
        print("ERROR: TP_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)

    state = load_state()

    # Pass 1: fetch every route's fares once.
    route_fares = {}
    for origin, destination in ROUTES:
        route_fares[(origin, destination)] = fetch_direct_fares(origin, destination)

    if args.verbose:
        for (origin, destination), fares in route_fares.items():
            if fares:
                print(f"\n--- {origin} -> {destination} details ---")
                for f in fares:
                    print(f"    {f.get('departure_at')}  ${f.get('price')}  "
                          f"{f.get('airline')}{f.get('flight_number')}  "
                          f"duration={format_duration(f.get('duration'))}")

    # Pass 2: check each route's cheapest fare against its recorded low.
    events = []
    for (origin, destination), fares in route_fares.items():
        ev = check_route(origin, destination, fares, state)
        if ev:
            events.append(ev)

    print(f"\n=== Summary (next {DATE_RANGE_DAYS} days) ===")
    summary_lines = [f"📊 <b>Summary</b> (next {DATE_RANGE_DAYS} days):"]
    for (origin, destination), fares in route_fares.items():
        summary_lines.append(summarize_route(origin, destination, fares))

    # Always record a timestamp so the state file changes every run --
    # keeps GitHub Actions from auto-disabling the schedule after 60 days
    # of "no commits" if prices happen to stay flat that whole time.
    state["_last_checked"] = datetime.now().isoformat()

    if not events and not args.force_summary:
        print("\nNo new record-low prices since last run -- not sending anything to Telegram.")
    else:
        send_telegram_message(BIG_WARNING + "\n\n" + "\n".join(summary_lines), dry_run=args.dry_run)
        for ev in events:
            send_telegram_message(build_event_message(ev), dry_run=args.dry_run)

    save_state(state)


if __name__ == "__main__":
    main()