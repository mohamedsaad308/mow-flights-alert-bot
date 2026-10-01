#!/usr/bin/env python3
"""
Direct-flight price watcher: MOW (Moscow) / LED (Saint Petersburg) / MSQ (Minsk)
<-> HRG (Hurghada) / CAI (Cairo) / SSH (Sharm El Sheikh) -- both directions.

Checks a rolling date window (today through DATE_RANGE_DAYS-1 days ahead)
using Travelpayouts' aviasales/v3/prices_for_dates endpoint.

IMPORTANT LIMITATION: this data comes from Travelpayouts' cache of real user
searches (NOT a live check), and this endpoint doesn't expose a freshness
timestamp for individual fares. A fare tagged "direct" here can be stale or
occasionally mislabeled (e.g. a route that's since become connection-only,
or already sold out). Treat every alert as a candidate to verify via the
included link, never as a guarantee of what you'll find when you click
through. This has already been observed in practice (a cached $159 direct
LED->HRG/UJ736 fare that did not appear as non-stop on the live site).

How the alerting works (and why it's built this way)
----------------------------------------------------
Baselines are tracked per ROUTE, not per (route, departure date). Each route
has exactly one record: the cheapest direct fare seen anywhere in the
30-day window, for any date and any airline. Alerts are purely price-based;
airline and flight number are shown in the alert but never affect whether
one fires.

Each run, the route's cheapest fare is compared with the record price:

- A fare at or below the record exists: the record price is still on the
  market. The miss counter goes back to 0. If the fare is at least
  MIN_PRICE_DROP_USD below the record, an alert fires. The record is
  lowered to the new price.
- No fare at or below the record exists: the record price was not
  reconfirmed. The miss counter goes up by 1 and nothing else changes.
  This run does NOT count as a miss if some API calls failed (the record
  fare may just be in the part we couldn't see).
- The miss counter reaches MAX_MISSES: the record price is considered gone
  (sold out / expired from the cache). The record is silently reset to the
  current cheapest fare. No alert.

Why a counter instead of resetting on the first miss: the cached data
flickers. A fare can vanish for one run and come back, which would cause a
duplicate "new best price" alert if we reset immediately.

- RECORD_TTL_DAYS is only a safety net for the case where the bot didn't run
  for days (so misses couldn't accumulate). A record not reconfirmed for that
  long is reset silently on the next miss.
- A route's FIRST sighting of any fares does NOT alert -- it just records
  the baseline (the "wakeup" case below still fires once, see next point).
- A route DOES alert when it wakes up: if it had no known fares at all and
  now has some, that's a "direct flights appeared" event (uses the cheapest
  fare found).
- Routes that go confirmed-empty for several days in a row are forgotten
  (see EMPTY_GRACE_DAYS), so a route reappearing later re-triggers a
  wakeup event instead of staying silent forever. An empty result is "no
  data", not a miss on the record price, so it doesn't touch the counter.

What it sends
-------------
At most three Telegram messages per run, only when something changed:
(1) one overall summary covering every route in both directions,
(2) one message combining all "to Egypt" events,
(3) one message combining all "from Egypt" events.
A direction with no events that run simply doesn't get a message.

Use --dry-run to print alerts to the terminal instead of sending them,
and --verbose to see every fare found. Note: --dry-run does not save state,
so the miss counter never accumulates in a dry run.

Setup
-----
1. pip install -r requirements.txt
2. Set environment variables: TP_TOKEN, TG_BOT_TOKEN, TG_CHAT_ID
3. Run:  python flight_bot.py --dry-run --verbose   (for testing)
         python flight_bot.py                        (for real alerts)
4. Trigger externally (e.g. cron-job.org hitting workflow_dispatch) since
   GitHub's own `schedule` trigger is unreliable at short intervals.

Route codes
-----------
- MOW = Moscow (city code, covers SVO/DME/VKO)
- LED = Saint Petersburg
- MSQ = Minsk
- HRG = Hurghada, CAI = Cairo, SSH = Sharm El Sheikh
"""

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:  # very old urllib3
    from requests.packages.urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TP_TOKEN = os.environ.get("TP_TOKEN", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

# Outbound: home cities -> Egypt.
TO_EGYPT_ROUTES = [
    ("MOW", "HRG"),  # Moscow -> Hurghada
    ("MOW", "SSH"),  # Moscow -> Sharm El Sheikh
    ("MOW", "CAI"),  # Moscow -> Cairo
    ("LED", "HRG"),  # Saint Petersburg -> Hurghada
    ("LED", "SSH"),  # Saint Petersburg -> Sharm El Sheikh
    ("MSQ", "HRG"),  # Minsk -> Hurghada
    ("MSQ", "SSH"),  # Minsk -> Sharm El Sheikh
]

# Return leg: Egypt -> home cities (mirror of the above).
FROM_EGYPT_ROUTES = [(dst, src) for (src, dst) in TO_EGYPT_ROUTES]

ROUTES = TO_EGYPT_ROUTES + FROM_EGYPT_ROUTES
TO_EGYPT_SET = set(TO_EGYPT_ROUTES)

# Specific (origin, destination, flight_number) combos to hardcode-ignore --
# confirmed via manual check on aviasales.com to be stale/mislabeled cache
# entries that don't reflect a real bookable non-stop.
IGNORED_FLIGHTS = {
    # ("LED", "HRG", "UJ736"),
}

CURRENCY = "usd"
DATE_RANGE_DAYS = 10          # today through today + (DATE_RANGE_DAYS - 1)
MAX_MISSES = 3                 # consecutive runs without a fare at/below the record before it's declared gone
RECORD_TTL_DAYS = 2            # safety net only: covers the bot not running for days (misses can't count then)
EMPTY_GRACE_DAYS = 3           # confirmed-empty days required before forgetting a route
MIN_PRICE_DROP_USD = 10        # ignore drops smaller than this -- avoid $1 noise
REQUEST_PAUSE_SECONDS = 0.15   # small pause between API calls
REQUEST_TIMEOUT = 20

STATE_FILE = os.environ.get("STATE_FILE", "flight_state.json")
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), STATE_FILE)
STATE_VERSION = 3

AFFILIATE_MARKER = "776670"  # Travelpayouts affiliate marker -- appended to every link

TP_PRICES_FOR_DATES_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
TG_SEND_URL_TMPL = "https://api.telegram.org/bot{token}/sendMessage"

BIG_WARNING = (
    "⚠️ <i>Cached data, not live — verify on Aviasales before booking.</i>"
)


# ---------------------------------------------------------------------------
# HTTP session with retries (429 / 5xx are common on the free tier)
# ---------------------------------------------------------------------------

def build_session():
    session = requests.Session()
    retry = Retry(
        total=4,
        connect=3,
        read=3,
        status=4,
        backoff_factor=1.5,  # 0s, 1.5s, 3s, 6s, 12s
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "flight-price-watcher/3.0"})
    return session


# ---------------------------------------------------------------------------
# State
#
# Schema (v3):
# {
#   "version": 3,
#   "records": {
#       "MOW|HRG": {"price": 212, "date": "2026-10-04", "seen": "2026-09-15", "misses": 0},
#       ...
#   },
#   "routes_with_fares": ["MOW|HRG", ...]   # routes known to have direct fares
#   "empty_since": {"MOW|SSH": "2026-09-15"}  # first day a known route came
#                                              # back with zero confirmed fares
# }
#
# One record per ROUTE (not per date): "price" is the cheapest direct fare
# currently on record for that route within the rolling window, "date" is
# which departure date it was on (informational only), "seen" is the last run
# on which that price was matched or beaten by something in the window, and
# "misses" is how many consecutive runs have gone by without a fare at or
# below that price (reset to 0 whenever one shows up). Records written before
# the miss counter existed have no "misses" key and are treated as 0.
# ---------------------------------------------------------------------------

def load_state():
    empty_shell = {"version": STATE_VERSION, "records": {}, "routes_with_fares": [], "empty_since": {}}
    if not os.path.exists(STATE_PATH):
        return empty_shell
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return empty_shell

    if raw.get("version") != STATE_VERSION:
        # Older schemas tracked baselines per (route, date) or as a single
        # ratcheting "cheapest ever" string key. Rather than try to migrate
        # exact numbers across a schema change (risk of carrying forward a
        # stale/bogus low), we start records clean but preserve which routes
        # were already known to have fares, so we don't re-announce all of
        # them as "wakeup" events on the next run.
        known = set()
        for key in raw.get("routes_with_fares", []):
            known.add(key)
        for key in raw.get("records", {}):
            parts = key.split("|")
            if len(parts) == 3:  # v2 per-date key: ORIGIN|DEST|DATE
                known.add(f"{parts[0]}|{parts[1]}")
            elif len(parts) == 3 and parts[0] == "cheapest":  # v1 key
                known.add(f"{parts[1]}|{parts[2]}")
        return {"version": STATE_VERSION, "records": {}, "routes_with_fares": sorted(known), "empty_since": {}}

    raw.setdefault("records", {})
    raw.setdefault("routes_with_fares", [])
    raw.setdefault("empty_since", {})
    return raw


def save_state(state):
    tmp_path = STATE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp_path, STATE_PATH)  # atomic -- never leaves a half-written file


def prune_state(state):
    """Drop records for routes that are no longer tracked at all (e.g. the
    ROUTES list changed). Route-level records don't go stale by date the way
    per-date records used to -- the miss counter in check_route() already
    handles a record that's stopped being reconfirmed. Returns the number
    removed."""
    valid_keys = {f"{o}|{d}" for o, d in ROUTES}
    doomed = [key for key in state["records"] if key not in valid_keys]
    for key in doomed:
        del state["records"][key]
    return len(doomed)


# ---------------------------------------------------------------------------
# Travelpayouts API
# ---------------------------------------------------------------------------

def window_dates(today, days_ahead):
    return [today + timedelta(days=i) for i in range(days_ahead)]


def months_in_window(dates):
    """Distinct YYYY-MM strings covered by the window, in order."""
    seen = []
    for d in dates:
        key = d.strftime("%Y-%m")
        if key not in seen:
            seen.append(key)
    return seen


def _request_fares(session, origin, destination, departure_at, limit):
    """One API call. Returns (list_of_items, ok_flag)."""
    params = {
        "origin": origin,
        "destination": destination,
        "departure_at": departure_at,
        "one_way": "true",
        "direct": "true",
        "sorting": "price",
        "limit": limit,
        "currency": CURRENCY,
        "token": TP_TOKEN,
    }
    try:
        resp = session.get(TP_PRICES_FOR_DATES_URL, params=params,
                           timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()  # parsed inside the try -- an HTML error page
                               # here used to crash the whole run
    except (requests.RequestException, ValueError) as e:
        print(f"[{origin}->{destination} {departure_at}] API error: {e}",
              file=sys.stderr)
        return [], False

    if not payload.get("success", True):
        print(f"[{origin}->{destination} {departure_at}] API returned failure: "
              f"{payload.get('error')}", file=sys.stderr)
        return [], False

    return payload.get("data", []) or [], True


def fetch_direct_fares(session, origin, destination, dates, daily=False):
    """Return (fares, failed_calls).

    `fares` is a list of direct (0-stop) one-way fares whose departure date
    falls inside the window. `failed_calls` lets the caller distinguish
    "this route genuinely has no non-stops" from "we couldn't reach the API",
    which the previous version collapsed into the same empty list.

    By default this queries one month at a time (1-2 calls per route for a
    30-day window). --daily-queries falls back to one call per date, which is
    more thorough but costs ~30 calls per route.
    """
    window = {d.isoformat() for d in dates}
    if daily:
        queries = [(d.isoformat(), 30) for d in dates]
    else:
        queries = [(m, 1000) for m in months_in_window(dates)]

    results = []
    failed = 0
    for departure_at, limit in queries:
        items, ok = _request_fares(session, origin, destination, departure_at, limit)
        if not ok:
            failed += 1
            time.sleep(REQUEST_PAUSE_SECONDS)
            continue

        for item in items:
            if item.get("transfers", 0) != 0:
                continue  # defensive: enforce non-stop ourselves too
            depart_date = (item.get("departure_at") or "")[:10]
            if depart_date not in window:
                continue  # month query returns the whole month; trim to window
            flight_number = f"{item.get('airline', '')}{item.get('flight_number', '')}"
            if (origin, destination, flight_number) in IGNORED_FLIGHTS:
                continue
            if item.get("price") is None:
                continue
            results.append(item)

        time.sleep(REQUEST_PAUSE_SECONDS)

    return results, failed


# ---------------------------------------------------------------------------
# Aviasales deep link + formatting helpers
# ---------------------------------------------------------------------------

def aviasales_link(origin, destination, depart_date_str):
    d = datetime.strptime(depart_date_str, "%Y-%m-%d")
    segment = f"{origin}{d:%d%m}{destination}1"  # 1 adult passenger
    return f"https://www.aviasales.ru/search/{segment}?marker={AFFILIATE_MARKER}"


def format_duration(minutes):
    if minutes is None:
        return "unknown"
    hours, mins = divmod(int(minutes), 60)
    return f"{hours}h{mins:02d}m" if mins else f"{hours}h"


def format_departure(departure_at):
    """'2026-10-04T10:30:00+03:00' -> '2026-10-04 10:30'."""
    if not departure_at:
        return "unknown"
    try:
        return datetime.fromisoformat(departure_at).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return departure_at[:16]


def cheapest_of(fares):
    return min(fares, key=lambda f: f.get("price", float("inf"))) if fares else None


def make_event(kind, origin, destination, fare, old_price=None):
    depart_date = (fare.get("departure_at") or "")[:10]
    return {
        "kind": kind,  # "wakeup" | "drop"
        "origin": origin,
        "destination": destination,
        "date": depart_date,
        "price": fare.get("price"),
        "old_price": old_price,
        "departure_at": fare.get("departure_at"),
        "flight_number": f"{fare.get('airline', '')}{fare.get('flight_number', '')}",
        "duration": fare.get("duration"),
        "link": aviasales_link(origin, destination, depart_date),
    }


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram_message(session, text, dry_run=False):
    if dry_run:
        print("--- Would send to Telegram ---")
        print(text)
        print("-------------------------------")
        return
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("Telegram not configured; message would have been:\n", text)
        return
    url = TG_SEND_URL_TMPL.format(token=TG_BOT_TOKEN)
    try:
        resp = session.post(
            url,
            data={
                "chat_id": TG_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        print(f"Telegram send failed: {e}", file=sys.stderr)
        return
    if not resp.ok:
        print(f"Telegram send failed: {resp.status_code} {resp.text}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Core comparison
# ---------------------------------------------------------------------------

def check_route(origin, destination, fares, failed, state, today):
    """Compare this run's cheapest fare (any date, any airline) against the
    route's record price. Returns a list of at most one event. Mutates `state`.

    Record = {"price", "date", "seen", "misses"}
      - a fare at or below the record exists  -> misses = 0, alert if it is
        MIN_PRICE_DROP_USD or more below, lower the record
      - no such fare                          -> misses += 1, record untouched
      - misses reaches MAX_MISSES             -> record price is gone (sold out),
        silently adopt the current cheapest fare
    """
    route_key = f"{origin}|{destination}"
    known_route = route_key in state["routes_with_fares"]
    today_str = today.isoformat()
    events = []

    if not fares:
        # Confirmed empty (not an API failure). This is "no data", not a miss
        # on the record price, so the miss counter is NOT touched here.
        # A single empty run is common cache noise, not proof the route died
        # -- forgetting it immediately meant the very next run's fare, even
        # at an unchanged price, would re-fire as "direct flights appeared".
        # Only forget after several straight confirmed-empty days.
        if failed == 0 and known_route:
            first_empty = state["empty_since"].get(route_key)
            if first_empty is None:
                state["empty_since"][route_key] = today_str
            elif (today - date.fromisoformat(first_empty)).days >= EMPTY_GRACE_DAYS:
                state["routes_with_fares"].remove(route_key)
                state["records"].pop(route_key, None)
                del state["empty_since"][route_key]
        return events

    # Fares present this run -- route is alive, clear any empty streak so it
    # doesn't carry over to some future gap.
    state["empty_since"].pop(route_key, None)

    best = cheapest_of(fares)  # cheapest fare on ANY date in this run's window
    price = best["price"]
    depart_date = (best.get("departure_at") or "")[:10]

    def new_record():
        return {"price": price, "date": depart_date, "seen": today_str, "misses": 0}

    if not known_route:
        # Route had no fares on record and now does -- announce once. This
        # also seeds the baseline, so it does NOT also fire a drop event.
        state["routes_with_fares"].append(route_key)
        state["records"][route_key] = new_record()
        events.append(make_event("wakeup", origin, destination, best))
        return events

    record = state["records"].get(route_key)
    if record is None or record.get("price") is None:
        # Known route but no baseline yet (defensive -- e.g. state file was
        # hand-edited, or migrated from an older schema). Seed silently.
        state["records"][route_key] = new_record()
        return events

    old_price = record["price"]

    if price <= old_price:
        # The record price (or better) is still on the market.
        if old_price - price >= MIN_PRICE_DROP_USD:
            events.append(make_event("drop", origin, destination, best, old_price))
        state["records"][route_key] = new_record()  # ratchet down, misses back to 0
        return events

    # Cheapest fare is above the record: the record price was not reconfirmed.
    if failed:
        # Some API calls failed, so the record fare may simply be in the part
        # we couldn't see. Don't count this run as a miss.
        return events

    misses = record.get("misses", 0) + 1
    cutoff = (today - timedelta(days=RECORD_TTL_DAYS)).isoformat()
    stale = record.get("seen", "") < cutoff

    if misses >= MAX_MISSES or stale:
        # Record price is gone: adopt the current market silently.
        state["records"][route_key] = new_record()
    else:
        record["misses"] = misses  # keep the record, just count the miss

    return events


# ---------------------------------------------------------------------------
# Message building
# ---------------------------------------------------------------------------

def summarize_route(origin, destination, fares, failed, total_calls):
    """Return (printable_line, telegram_line) for one route."""
    route_label = f"{origin} → {destination}"

    if fares:
        best = cheapest_of(fares)
        depart_date = (best.get("departure_at") or "")[:10]
        link = aviasales_link(origin, destination, depart_date)
        warn = f"  ⚠️ {failed}/{total_calls} queries failed" if failed else ""
        tg = (
            f"<b>{route_label}</b>  ({len(fares)} found){warn}\n"
            f"    💰 <b>${best.get('price')}</b> · {depart_date} · "
            f"{format_duration(best.get('duration'))}\n"
            f"    <a href=\"{link}\">View on Aviasales</a>"
        )
        printed = (f"[{route_label}] {len(fares)} direct fare(s), cheapest "
                   f"${best.get('price')} on {depart_date}"
                   + (f" ({failed} failed queries)" if failed else ""))
    elif failed:
        # Previously this was indistinguishable from "no direct flights" --
        # an API hiccup looked exactly like a route with no non-stops.
        tg = (f"<b>{route_label}</b>  — ⚠️ <i>unknown</i> "
              f"({failed}/{total_calls} queries failed)")
        printed = f"[{route_label}] UNKNOWN — {failed}/{total_calls} queries failed"
    else:
        tg = f"<b>{route_label}</b>  — none found"
        printed = f"[{route_label}] No direct flights found"

    return printed, tg


def build_summary_message(route_data):
    lines = [f"📊 <b>Flight Summary</b> · next {DATE_RANGE_DAYS} days"]

    for title, routes in (("\n✈️ <b>To Egypt</b>", TO_EGYPT_ROUTES),
                          ("\n🛬 <b>From Egypt</b>", FROM_EGYPT_ROUTES)):
        lines.append(title)
        for origin, destination in routes:
            fares, failed, total_calls = route_data[(origin, destination)]
            printed, tg = summarize_route(origin, destination, fares, failed, total_calls)
            print(printed)
            lines.append(tg)

    return "\n\n".join(lines)


def build_event_block(ev):
    duration_note = f" · {format_duration(ev['duration'])}" if ev.get("duration") else ""
    if ev["kind"] == "wakeup":
        headline = f"🆕 <b>{ev['origin']} → {ev['destination']}</b> — direct flights appeared"
        price_line = f"💰 <b>${ev['price']}</b>"
    else:
        headline = f"📉 <b>{ev['origin']} → {ev['destination']}</b> — new best price"
        price_line = f"💰 ${ev['old_price']} → <b>${ev['price']}</b>"
    return (
        f"{headline}\n"
        f"{price_line}{duration_note}\n"
        f"🛫 {format_departure(ev['departure_at'])}   ✈️ {ev['flight_number']}\n"
        f"<a href=\"{ev['link']}\">Book / verify</a>"
    )


def build_direction_message(title, events):
    if not events:
        return None
    parts = [f"<b>{title}</b>"]
    parts.extend(build_event_block(ev) for ev in events)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Direct flight price watcher")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print alerts to the terminal instead of sending them to Telegram.")
    parser.add_argument("--verbose", action="store_true",
                        help="Print every direct fare found in the window, not just alerting ones.")
    parser.add_argument("--force-summary", action="store_true",
                        help="Send the summary message even if nothing changed this run.")
    parser.add_argument("--daily-queries", action="store_true",
                        help="Query one date at a time instead of one month at a time "
                             "(more thorough, ~30 calls per route).")
    parser.add_argument("--days", type=int, default=DATE_RANGE_DAYS,
                        help=f"Size of the rolling window in days (default {DATE_RANGE_DAYS}).")
    args = parser.parse_args()

    if not TP_TOKEN:
        print("ERROR: TP_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)

    session = build_session()
    today = date.today()  # NOTE: UTC on a GitHub runner, not Africa/Cairo
    dates = window_dates(today, args.days)
    calls_per_route = len(dates) if args.daily_queries else len(months_in_window(dates))

    state = load_state()
    pruned = prune_state(state)

    # Pass 1: fetch every route once (both directions).
    route_data = {}
    for origin, destination in ROUTES:
        fares, failed = fetch_direct_fares(session, origin, destination, dates,
                                           daily=args.daily_queries)
        route_data[(origin, destination)] = (fares, failed, calls_per_route)

    if args.verbose:
        for (origin, destination), (fares, failed, _) in route_data.items():
            if not fares:
                continue
            print(f"\n--- {origin} -> {destination} details ---")
            for f in sorted(fares, key=lambda x: x.get("departure_at") or ""):
                print(f"    {format_departure(f.get('departure_at'))}  ${f.get('price')}  "
                      f"{f.get('airline')}{f.get('flight_number')}  "
                      f"duration={format_duration(f.get('duration'))}")

    # Pass 2: compare each route's cheapest-this-run fare against its record.
    to_egypt_events, from_egypt_events = [], []
    for (origin, destination), (fares, _failed, _) in route_data.items():
        for ev in check_route(origin, destination, fares, _failed, state, today):
            if (origin, destination) in TO_EGYPT_SET:
                to_egypt_events.append(ev)
            else:
                from_egypt_events.append(ev)

    total_events = len(to_egypt_events) + len(from_egypt_events)
    total_failed = sum(failed for _, failed, _ in route_data.values())

    print(f"\n=== Summary (next {args.days} days) ===")
    summary_text = build_summary_message(route_data)
    print(f"\nEvents: {total_events} | failed API calls: {total_failed} | "
          f"records tracked: {len(state['records'])} | pruned: {pruned}")

    if total_events:
        send_telegram_message(session, BIG_WARNING + "\n\n" + summary_text,
                              dry_run=args.dry_run)
        msg = build_direction_message("✈️ To Egypt — updates", to_egypt_events)
        if msg:
            send_telegram_message(session, msg, dry_run=args.dry_run)
        msg = build_direction_message("🛬 From Egypt — updates", from_egypt_events)
        if msg:
            send_telegram_message(session, msg, dry_run=args.dry_run)
    elif args.force_summary:
        send_telegram_message(session, BIG_WARNING + "\n\n" + summary_text,
                             dry_run=args.dry_run)
        print("\n(Forced summary sent.)")
    else:
        print("\nNo alert-worthy changes since last run -- nothing sent.")

    # Baselines can move every run (new records seeded, timestamps refreshed,
    # miss counters bumped), so state is always persisted, not just on an alert.
    if not args.dry_run:
        save_state(state)
    else:
        print("(Dry run: state not written.)")


if __name__ == "__main__":
    main()