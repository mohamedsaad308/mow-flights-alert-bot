#!/usr/bin/env python3
"""
Direct-flight price watcher.
Dynamically tracks routes requested by users in the Telegram bot (via Supabase).
Fallback routes: MOW / LED / MSQ <-> HRG / CAI / SSH.
"""

# Load .env for local runs (no-op on GitHub Actions). Must come before db_state import.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except ImportError:
    from requests.packages.urllib3.util.retry import Retry

from db_state import (
    load_state,
    save_state,
    get_active_routes_from_subscribers,
    get_route_subscribers,
    mark_welcomed,
    disable_subscriptions,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TP_TOKEN = os.environ.get("TP_TOKEN", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")

# Default fallback routes if no users have subscribed yet
DEFAULT_ROUTES = [
    ("MOW", "HRG"), ("MOW", "SSH"), ("MOW", "CAI"),
    ("LED", "HRG"), ("LED", "SSH"),
    ("MSQ", "HRG"), ("MSQ", "SSH"),
    ("HRG", "MOW"), ("SSH", "MOW"), ("CAI", "MOW"),
    ("HRG", "LED"), ("SSH", "LED"),
    ("HRG", "MSQ"), ("SSH", "MSQ"),
]

CURRENCY = "usd"
DATE_RANGE_DAYS = 10
MAX_MISSES = 3
RECORD_TTL_DAYS = 2
EMPTY_GRACE_DAYS = 3
MIN_PRICE_DROP_USD = 10
REQUEST_PAUSE_SECONDS = 0.15
REQUEST_TIMEOUT = 20

AFFILIATE_MARKER = "776670"

TP_PRICES_FOR_DATES_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
TG_SEND_URL_TMPL = "https://api.telegram.org/bot{token}/sendMessage"

BIG_WARNING = "⚠️ <i>Cached data, not live — verify on Aviasales before booking.</i>"


def build_session():
    session = requests.Session()
    retry = Retry(
        total=4,
        connect=3,
        read=3,
        status=4,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "flight-price-watcher/3.0"})
    return session


def window_dates(today, days_ahead):
    return [today + timedelta(days=i) for i in range(days_ahead)]


def months_in_window(dates):
    seen = []
    for d in dates:
        key = d.strftime("%Y-%m")
        if key not in seen:
            seen.append(key)
    return seen


def _request_fares(session, origin, destination, departure_at, limit):
    """Returns (items, status) where status is "ok", "unsupported" (HTTP 400) or "error"."""
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
        resp = session.get(TP_PRICES_FOR_DATES_URL, params=params, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 400:
            print(
                f"[{origin}->{destination} {departure_at}] 400 Bad Request: {resp.text[:200]}",
                file=sys.stderr,
            )
            return [], "unsupported"
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[{origin}->{destination} {departure_at}] API error: {e}", file=sys.stderr)
        return [], "error"

    if not payload.get("success", True):
        return [], "error"

    return payload.get("data", []) or [], "ok"


def fetch_direct_fares(session, origin, destination, dates, daily=False):
    """Returns (fares, failed, unsupported).

    failed      = number of queries that did not succeed (errors + 400s)
    unsupported = number of queries that got HTTP 400
    """
    window = {d.isoformat() for d in dates}
    queries = [(d.isoformat(), 30) for d in dates] if daily else [(m, 1000) for m in months_in_window(dates)]

    results = []
    failed = 0
    unsupported = 0
    for departure_at, limit in queries:
        items, status = _request_fares(session, origin, destination, departure_at, limit)
        if status != "ok":
            failed += 1
            if status == "unsupported":
                unsupported += 1
            time.sleep(REQUEST_PAUSE_SECONDS)
            continue

        for item in items:
            if item.get("transfers", 0) != 0:
                continue
            depart_date = (item.get("departure_at") or "")[:10]
            if depart_date not in window or item.get("price") is None:
                continue
            results.append(item)

        time.sleep(REQUEST_PAUSE_SECONDS)

    return results, failed, unsupported


def aviasales_link(origin, destination, depart_date_str):
    d = datetime.strptime(depart_date_str, "%Y-%m-%d")
    segment = f"{origin}{d:%d%m}{destination}1"
    return f"https://www.aviasales.ru/search/{segment}?marker={AFFILIATE_MARKER}"


def format_duration(minutes):
    if minutes is None:
        return "unknown"
    hours, mins = divmod(int(minutes), 60)
    return f"{hours}h{mins:02d}m" if mins else f"{hours}h"


def format_departure(departure_at):
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
        "kind": kind,
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


def send_telegram(session, chat_id, text, dry_run=False):
    """Send a Telegram message. Returns True on success, False otherwise."""
    if dry_run:
        print(f"--- Would send to Chat ID {chat_id} ---")
        print(text)
        print("---------------------------------------")
        return True
    if not TG_BOT_TOKEN or not chat_id:
        return False
    url = TG_SEND_URL_TMPL.format(token=TG_BOT_TOKEN)
    try:
        resp = session.post(
            url,
            data={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if not resp.ok:
            print(
                f"Telegram send to {chat_id} returned {resp.status_code}: {resp.text[:200]}",
                file=sys.stderr,
            )
        return resp.ok
    except requests.RequestException as e:
        print(f"Telegram send to {chat_id} failed: {e}", file=sys.stderr)
        return False


def check_route(origin, destination, fares, failed, state, today):
    route_key = f"{origin}|{destination}"
    known_route = route_key in state["routes_with_fares"]
    today_str = today.isoformat()
    events = []

    if not fares:
        if failed == 0 and known_route:
            first_empty = state["empty_since"].get(route_key)
            if first_empty is None:
                state["empty_since"][route_key] = today_str
            elif (today - date.fromisoformat(first_empty)).days >= EMPTY_GRACE_DAYS:
                state["routes_with_fares"].remove(route_key)
                state["records"].pop(route_key, None)
                del state["empty_since"][route_key]
        return events

    state["empty_since"].pop(route_key, None)

    best = cheapest_of(fares)
    price = best["price"]
    depart_date = (best.get("departure_at") or "")[:10]

    def new_record():
        return {"price": price, "date": depart_date, "seen": today_str, "misses": 0}

    if not known_route:
        state["routes_with_fares"].append(route_key)
        state["records"][route_key] = new_record()
        events.append(make_event("wakeup", origin, destination, best))
        return events

    record = state["records"].get(route_key)
    if record is None or record.get("price") is None:
        state["records"][route_key] = new_record()
        return events

    old_price = record["price"]

    if price <= old_price:
        if old_price - price >= MIN_PRICE_DROP_USD:
            events.append(make_event("drop", origin, destination, best, old_price))
        state["records"][route_key] = new_record()
        return events

    if failed:
        return events

    misses = record.get("misses", 0) + 1
    cutoff = (today - timedelta(days=RECORD_TTL_DAYS)).isoformat()
    stale = record.get("seen", "") < cutoff

    if misses >= MAX_MISSES or stale:
        state["records"][route_key] = new_record()
    else:
        record["misses"] = misses

    return events


def build_event_block(ev):
    duration_note = f" · {format_duration(ev['duration'])}" if ev.get("duration") else ""
    if ev["kind"] == "wakeup":
        headline = f"🆕 <b>{ev['origin']} → {ev['destination']}</b> — direct flights detected!"
        price_line = f"💰 <b>${ev['price']}</b>"
    else:
        headline = f"📉 <b>{ev['origin']} → {ev['destination']}</b> — price drop detected!"
        price_line = f"💰 ${ev['old_price']} → <b>${ev['price']}</b>"
    return (
        f"{headline}\n\n"
        f"{price_line}{duration_note}\n"
        f"🛫 {format_departure(ev['departure_at'])}    ✈️ {ev['flight_number']}\n"
        f"<a href=\"{ev['link']}\">Book / Check on Aviasales</a>\n\n"
        f"{BIG_WARNING}"
    )


def build_snapshot_block(origin, destination, fare):
    """First message a new subscriber gets: the current cheapest fare (or 'no flights')."""
    if fare is None:
        return (
            f"ℹ️ <b>{origin} → {destination}</b> — no direct flights found "
            f"in the next {DATE_RANGE_DAYS} days right now.\n"
            f"I'll alert you as soon as one appears.\n\n"
            f"{BIG_WARNING}"
        )
    ev = make_event("snapshot", origin, destination, fare)
    duration_note = f" · {format_duration(ev['duration'])}" if ev.get("duration") else ""
    return (
        f"📊 <b>{origin} → {destination}</b> — current cheapest direct flight\n\n"
        f"💰 <b>${ev['price']}</b>{duration_note}\n"
        f"🛫 {format_departure(ev['departure_at'])}    ✈️ {ev['flight_number']}\n"
        f"<a href=\"{ev['link']}\">Book / Check on Aviasales</a>\n\n"
        f"{BIG_WARNING}"
    )


def build_unsupported_block(origin, destination):
    return (
        f"❌ <b>{origin} → {destination}</b> isn't supported by my price data source, "
        f"so I can't track it and have stopped watching this route.\n\n"
        f"Please double-check the airport codes (3-letter IATA), or try a nearby airport / "
        f"a city code (e.g. MOW for Moscow)."
    )


def main():
    parser = argparse.ArgumentParser(description="Direct flight price watcher")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--daily-queries", action="store_true")
    parser.add_argument("--days", type=int, default=DATE_RANGE_DAYS)
    args = parser.parse_args()

    if not TP_TOKEN:
        print("ERROR: TP_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)

    session = build_session()
    today = date.today()
    dates = window_dates(today, args.days)
    queries_per_route = len(dates) if args.daily_queries else len(months_in_window(dates))

    # Fetch routes actively subscribed to in the Telegram bot
    user_routes = get_active_routes_from_subscribers()
    routes_to_check = user_routes if user_routes else DEFAULT_ROUTES
    print(f"Tracking {len(routes_to_check)} distinct route(s)...", flush=True)

    state = load_state()

    unsupported_routes = []   # routes where every query returned HTTP 400
    healthy_routes = 0        # routes where every query succeeded (sanity check)

    # Check fares and dispatch alerts
    for origin, destination in routes_to_check:
        fares, failed, unsupported = fetch_direct_fares(
            session, origin, destination, dates, daily=args.daily_queries
        )

        # Every query rejected with 400 and nothing found -> route not supported by the API.
        if not fares and unsupported == queries_per_route:
            unsupported_routes.append((origin, destination))
            continue

        if failed == 0:
            healthy_routes += 1

        events = check_route(origin, destination, fares, failed, state, today)

        subs = get_route_subscribers(origin, destination)
        new_ids = [s["chat_id"] for s in subs if not s.get("welcomed")]
        welcomed_ids = [s["chat_id"] for s in subs if s.get("welcomed")]

        # 1) Welcome snapshot for brand-new subscribers of this route.
        #    Skipped if the API failed and returned nothing, so it retries next run.
        if new_ids and (fares or failed == 0):
            snapshot_text = build_snapshot_block(origin, destination, cheapest_of(fares))
            sent_ids = [
                cid for cid in new_ids
                if send_telegram(session, cid, snapshot_text, dry_run=args.dry_run)
            ]
            if not args.dry_run:
                mark_welcomed(origin, destination, sent_ids)

        # 2) Normal alerts (wakeup / drop) go to already-welcomed subscribers.
        for ev in events:
            message_text = build_event_block(ev)
            if welcomed_ids:
                for chat_id in welcomed_ids:
                    send_telegram(session, chat_id, message_text, dry_run=args.dry_run)
            elif not subs and TG_CHAT_ID:
                # If no individual user subscribers, broadcast to admin chat
                send_telegram(session, TG_CHAT_ID, message_text, dry_run=args.dry_run)

    # Handle unsupported routes: tell subscribers once, then stop tracking.
    # Only when at least one other route worked, so a bad TP_TOKEN (400 everywhere)
    # can't disable everyone's subscriptions.
    if unsupported_routes:
        if healthy_routes > 0:
            for origin, destination in unsupported_routes:
                print(f"Route {origin}->{destination} is unsupported; disabling.", flush=True)
                text = build_unsupported_block(origin, destination)
                for s in get_route_subscribers(origin, destination):
                    send_telegram(session, s["chat_id"], text, dry_run=args.dry_run)
                if not args.dry_run:
                    disable_subscriptions(origin, destination)
        else:
            print(
                "WARNING: every checked route returned 400 - check TP_TOKEN. "
                "Not disabling any subscriptions.",
                file=sys.stderr,
            )

    if not args.dry_run:
        save_state(state)
        print("State successfully synced with Supabase.")


if __name__ == "__main__":
    main()