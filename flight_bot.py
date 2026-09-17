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
The naive version of this bot tracks one "cheapest ever seen" number per
route. That breaks in two ways: (1) a single stale/bogus cached fare sets a
floor no real fare can ever beat, silencing the route forever, and (2) the
window rolls forward daily, so you end up comparing a Tuesday fare against a
Saturday record -- numbers that were never comparable to begin with.

So instead:

- Baselines are stored per (route, departure date), not per route. Each
  date is compared only against its own history.
- Baselines EXPIRE after RECORD_TTL_DAYS. An expired baseline is silently
  reset to the current price rather than alerting, so a bogus low self-heals
  within two weeks instead of poisoning the route indefinitely.
- A date entering the window for the first time does NOT alert -- it just
  records a baseline. Otherwise the rolling window would fire one "new fare"
  alert per route per day, forever.
- A route DOES alert when it wakes up: if it had no known fares at all and
  now has some, that's a "direct flights appeared" event.
- Past dates and long-untouched entries are pruned every run, so the state
  file doesn't grow without bound.

What it sends
-------------
At most three Telegram messages per run, only when something changed:
(1) one overall summary covering every route in both directions,
(2) one message combining all "to Egypt" events,
(3) one message combining all "from Egypt" events.
A direction with no events that run simply doesn't get a message.

Use --dry-run to print alerts to the terminal instead of sending them,
and --verbose to see every fare found.

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
DATE_RANGE_DAYS = 30           # today through today + (DATE_RANGE_DAYS - 1)
RECORD_TTL_DAYS = 14           # baselines older than this are reset, not trusted
EMPTY_GRACE_DAYS = 3           # confirmed-empty days required before forgetting a route
MIN_PRICE_DROP_USD = 10        # ignore drops smaller than this -- avoid $1 noise
MAX_DROPS_PER_ROUTE = 3        # only report the N best drops per route, not a wall
REQUEST_PAUSE_SECONDS = 0.15   # small pause between API calls
REQUEST_TIMEOUT = 20

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flight_state.json")
STATE_VERSION = 2

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
    session.headers.update({"User-Agent": "flight-price-watcher/2.0"})
    return session


# ---------------------------------------------------------------------------
# State
#
# Schema (v2):
# {
#   "version": 2,
#   "records": {
#       "MOW|HRG|2026-10-04": {"price": 212, "seen": "2026-09-15"},
#       ...
#   },
#   "routes_with_fares": ["MOW|HRG", ...]   # routes known to have direct fares
#   "empty_since": {"MOW|SSH": "2026-09-15"}  # first day a known route came
#                                              # back with zero confirmed fares
# }
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
        # v1 kept a single "cheapest|ORIGIN|DEST" number per route, which is
        # exactly the ratcheting baseline this version exists to replace.
        # Nothing to migrate -- start clean, but remember which routes were
        # already known to have fares so we don't re-announce all of them.
        known = []
        for key in raw:
            parts = key.split("|")
            if len(parts) == 3 and parts[0] == "cheapest":
                known.append(f"{parts[1]}|{parts[2]}")
        return {"version": STATE_VERSION, "records": {}, "routes_with_fares": known, "empty_since": {}}

    raw.setdefault("records", {})
    raw.setdefault("routes_with_fares", [])
    raw.setdefault("empty_since", {})
    return raw


def save_state(state):
    tmp_path = STATE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp_path, STATE_PATH)  # atomic -- never leaves a half-written file


def prune_state(state, today):
    """Drop records for dates that have already departed, and records whose
    last sighting is older than the TTL. Returns the number removed."""
    cutoff = (today - timedelta(days=RECORD_TTL_DAYS)).isoformat()
    doomed = []
    for key, rec in state["records"].items():
        try:
            depart_date = key.rsplit("|", 1)[1]
        except IndexError:
            doomed.append(key)
            continue
        if depart_date < today.isoformat() or rec.get("seen", "") < cutoff:
            doomed.append(key)
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
    return f"https://www.aviasales.com/search/{segment}?marker={AFFILIATE_MARKER}"


def format_duration(minutes):
    if minutes is None:
        return "unknown"
    hours, mins = divmod(int(minutes), 60)
    return f"{hours}h{mins:02d}m" if mins else f"{hours}h"


def format_departure(departure_at):
    """'2026-10-04T10:30:00+03:00' -> '2026-10-04 10:30'.

    The old version did .replace("T", "  ")[:16], which swapped one character
    for two and then sliced at the original offset -- chopping the minutes in
    half ('10:3')."""
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
    """Compare each date's cheapest fare against that date's own baseline.

    Returns a list of events. Mutates `state`.
    """
    route_key = f"{origin}|{destination}"
    known_route = route_key in state["routes_with_fares"]
    events = []

    if not fares:
        # Confirmed empty (not an API failure). A single empty run is common
        # cache noise, not proof the route died -- forgetting it immediately
        # (as v2.0 did) meant the very next run's fare, even at an unchanged
        # price, re-fired as "direct flights appeared" because the route was
        # no longer "known". Only forget after several straight confirmed-
        # empty days, so a real fare reappearing at the same price stays
        # silent like it should.
        if failed == 0 and known_route:
            first_empty = state["empty_since"].get(route_key)
            if first_empty is None:
                state["empty_since"][route_key] = today.isoformat()
            elif (today - date.fromisoformat(first_empty)).days >= EMPTY_GRACE_DAYS:
                state["routes_with_fares"].remove(route_key)
                del state["empty_since"][route_key]
        return events

    # Fares present this run -- route is alive, clear any empty streak so it
    # doesn't carry over to some future gap.
    state["empty_since"].pop(route_key, None)

    # Cheapest fare per departure date.
    by_date = {}
    for fare in fares:
        depart_date = (fare.get("departure_at") or "")[:10]
        if not depart_date:
            continue
        current = by_date.get(depart_date)
        if current is None or fare["price"] < current["price"]:
            by_date[depart_date] = fare

    today_str = today.isoformat()
    drops = []

    for depart_date, fare in by_date.items():
        key = f"{origin}|{destination}|{depart_date}"
        record = state["records"].get(key)
        price = fare["price"]

        if record is None:
            # First time we've seen this date. Record it silently -- alerting
            # here would fire once per route per day as the window rolls.
            state["records"][key] = {"price": price, "seen": today_str}
            continue

        expired = record.get("seen", "") < (today - timedelta(days=RECORD_TTL_DAYS)).isoformat()
        if expired:
            # Stale baseline (possibly a bogus cached low). Reset, don't alert.
            state["records"][key] = {"price": price, "seen": today_str}
            continue

        old_price = record.get("price")
        if old_price is not None and (old_price - price) >= MIN_PRICE_DROP_USD:
            drops.append(make_event("drop", origin, destination, fare, old_price))
            state["records"][key] = {"price": price, "seen": today_str}
        elif old_price is not None and price < old_price:
            # Real but tiny drop: track it, stay quiet.
            state["records"][key] = {"price": price, "seen": today_str}
        else:
            # Same or higher: refresh the timestamp so a live baseline isn't
            # expired out from under us, but keep the lower recorded price.
            record["seen"] = today_str

    if not known_route:
        # This route had no fares on record and now does -- worth announcing
        # once, which is the original "direct flights appeared" alert.
        state["routes_with_fares"].append(route_key)
        best = cheapest_of(fares)
        events.append(make_event("wakeup", origin, destination, best))
        return events

    drops.sort(key=lambda e: (e["old_price"] - e["price"]), reverse=True)
    events.extend(drops[:MAX_DROPS_PER_ROUTE])
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
        headline = f"📉 <b>{ev['origin']} → {ev['destination']}</b> — price drop"
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
    pruned = prune_state(state, today)

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

    # Pass 2: compare each date against its own baseline.
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

    # Baselines move every run (new dates recorded, timestamps refreshed), so
    # state is always persisted -- unlike v1, which only wrote on an alert and
    # therefore kept re-detecting the same things.
    if not args.dry_run:
        save_state(state)
    else:
        print("(Dry run: state not written.)")


if __name__ == "__main__":
    main()