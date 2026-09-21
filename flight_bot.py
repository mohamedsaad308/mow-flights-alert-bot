#!/usr/bin/env python3
"""
Direct-flight price watcher (Database-backed multi-user version).

Reads active subscriptions from Supabase, deduplicates routes, checks Travelpayouts,
updates route baselines in PostgreSQL, and dispatches price alerts directly to
each subscriber's Telegram chat.
"""

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta

import requests
from requests.adapters import HTTPAdapter
from supabase import Client, create_client

try:
    from urllib3.util.retry import Retry
except ImportError:
    from requests.packages.urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# Configuration & Environment Variables
# ---------------------------------------------------------------------------

TP_TOKEN = os.environ.get("TP_TOKEN", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

CURRENCY = "usd"
DATE_RANGE_DAYS = 30
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

IGNORED_FLIGHTS = set()


# ---------------------------------------------------------------------------
# HTTP Session with Retries
# ---------------------------------------------------------------------------

def build_session():
    session = requests.Session()
    retry = Retry(
        total=4,
        connect=3,
        read=3,
        status=4,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "flight-price-watcher/4.0"})
    return session


# ---------------------------------------------------------------------------
# Supabase Database Operations
# ---------------------------------------------------------------------------

def get_supabase_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: SUPABASE_URL and SUPABASE_KEY must be set.", file=sys.stderr)
        sys.exit(1)
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def fetch_active_subscriptions(client: Client):
    """
    Returns:
      routes: list of unique (origin, destination) tuples
      route_subscribers: dict mapping (origin, destination) -> list of chat_ids
    """
    res = client.table("subscriptions").select("chat_id, origin, destination").execute()
    data = res.data or []

    route_subscribers = {}
    for row in data:
        pair = (row["origin"].upper(), row["destination"].upper())
        route_subscribers.setdefault(pair, []).append(row["chat_id"])

    routes = sorted(list(route_subscribers.keys()))
    return routes, route_subscribers


def load_state_from_db(client: Client):
    """Reconstructs the state dict from PostgreSQL."""
    rec_res = client.table("route_baselines").select("*").execute()
    known_res = client.table("known_routes").select("*").execute()

    records = {}
    for r in (rec_res.data or []):
        records[r["route_key"]] = {
            "price": float(r["lowest_price"]),
            "date": r["flight_date"],
            "seen": r["last_seen"],
            "misses": r.get("misses", 0),
        }

    routes_with_fares = []
    empty_since = {}
    for kr in (known_res.data or []):
        routes_with_fares.append(kr["route_key"])
        if kr.get("empty_since"):
            empty_since[kr["route_key"]] = kr["empty_since"]

    return {
        "records": records,
        "routes_with_fares": routes_with_fares,
        "empty_since": empty_since,
    }


def save_state_to_db(client: Client, state: dict):
    """Persists records and route presence back to PostgreSQL."""
    # 1. Save or update route baselines
    for key, val in state["records"].items():
        client.table("route_baselines").upsert({
            "route_key": key,
            "lowest_price": val["price"],
            "flight_date": val["date"],
            "last_seen": val["seen"],
            "misses": val.get("misses", 0),
            "updated_at": datetime.utcnow().isoformat(),
        }).execute()

    # 2. Synchronize known routes state
    for key in state["routes_with_fares"]:
        client.table("known_routes").upsert({
            "route_key": key,
            "empty_since": state["empty_since"].get(key),
        }).execute()


# ---------------------------------------------------------------------------
# Travelpayouts API
# ---------------------------------------------------------------------------

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
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[{origin}->{destination} {departure_at}] API error: {e}", file=sys.stderr)
        return [], False

    if not payload.get("success", True):
        print(f"[{origin}->{destination} {departure_at}] API failure: {payload.get('error')}", file=sys.stderr)
        return [], False

    return payload.get("data", []) or [], True


def fetch_direct_fares(session, origin, destination, dates, daily=False):
    window = {d.isoformat() for d in dates}
    queries = [(d.isoformat(), 30) for d in dates] if daily else [(m, 1000) for m in months_in_window(dates)]

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
                continue
            depart_date = (item.get("departure_at") or "")[:10]
            if depart_date not in window:
                continue
            flight_number = f"{item.get('airline', '')}{item.get('flight_number', '')}"
            if (origin, destination, flight_number) in IGNORED_FLIGHTS:
                continue
            if item.get("price") is None:
                continue
            results.append(item)

        time.sleep(REQUEST_PAUSE_SECONDS)

    return results, failed


# ---------------------------------------------------------------------------
# Formatting & Telegram
# ---------------------------------------------------------------------------

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


def send_telegram_message(session, text, chat_id, dry_run=False):
    if dry_run:
        print(f"--- [Dry Run] Telegram -> Chat ID: {chat_id} ---")
        print(text)
        print("-----------------------------------------------")
        return
    if not TG_BOT_TOKEN or not chat_id:
        return

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
            print(f"Failed sending to {chat_id}: {resp.status_code} {resp.text}", file=sys.stderr)
    except requests.RequestException as e:
        print(f"Telegram network error ({chat_id}): {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Baseline Evaluation
# ---------------------------------------------------------------------------

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
                state["empty_since"].pop(route_key, None)
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


def build_alert_message(ev):
    duration_str = f" · {format_duration(ev['duration'])}" if ev.get("duration") else ""
    if ev["kind"] == "wakeup":
        headline = f"🆕 <b>{ev['origin']} → {ev['destination']}</b> — Direct flight detected!"
        price_line = f"💰 <b>${ev['price']}</b>"
    else:
        headline = f"📉 <b>{ev['origin']} → {ev['destination']}</b> — Price drop!"
        price_line = f"💰 ${ev['old_price']} → <b>${ev['price']}</b>"

    return (
        f"{headline}\n\n"
        f"{price_line}{duration_str}\n"
        f"🛫 {format_departure(ev['departure_at'])}   ✈️ {ev['flight_number']}\n"
        f"<a href=\"{ev['link']}\">Book / Check on Aviasales</a>\n\n"
        f"{BIG_WARNING}"
    )


# ---------------------------------------------------------------------------
# Main Execution
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Multi-user Direct Flight Price Watcher")
    parser.add_argument("--dry-run", action="store_true", help="Print alerts without sending Telegram messages.")
    parser.add_argument("--verbose", action="store_true", help="Print details of every direct fare found.")
    parser.add_argument("--daily-queries", action="store_true", help="Query day-by-day instead of by month.")
    parser.add_argument("--days", type=int, default=DATE_RANGE_DAYS, help="Window size in days.")
    args = parser.parse_args()

    if not TP_TOKEN:
        print("ERROR: TP_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)

    supabase = get_supabase_client()
    routes, route_subscribers = fetch_active_subscriptions(supabase)

    if not routes:
        print("No active subscriptions found in the database. Exiting.")
        sys.exit(0)

    print(f"Monitoring {len(routes)} unique route(s) across active subscribers:")
    for orig, dest in routes:
        sub_count = len(route_subscribers.get((orig, dest), []))
        print(f"  • {orig} -> {dest} ({sub_count} subscriber{'s' if sub_count > 1 else ''})")

    session = build_session()
    today = date.today()
    dates = window_dates(today, args.days)

    state = load_state_from_db(supabase)

    # 1. Fetch fares for each distinct route
    route_data = {}
    for origin, destination in routes:
        fares, failed = fetch_direct_fares(session, origin, destination, dates, daily=args.daily_queries)
        route_data[(origin, destination)] = (fares, failed)

        if args.verbose and fares:
            print(f"\n--- {origin} -> {destination} ({len(fares)} fares) ---")
            for f in sorted(fares, key=lambda x: x.get("departure_at") or ""):
                print(f"  {format_departure(f.get('departure_at'))} | ${f.get('price')} | {f.get('airline')}{f.get('flight_number')}")

    # 2. Check for alerts and notify matching subscribers
    total_events = 0
    for (origin, destination), (fares, failed) in route_data.items():
        events = check_route(origin, destination, fares, failed, state, today)
        for ev in events:
            total_events += 1
            alert_text = build_alert_message(ev)
            target_chat_ids = route_subscribers.get((origin, destination), [])

            print(f"Event: {ev['kind']} on {origin}->{destination}. Alerting {len(target_chat_ids)} user(s).")
            for chat_id in target_chat_ids:
                send_telegram_message(session, alert_text, chat_id=chat_id, dry_run=args.dry_run)

    print(f"\nCompleted: {total_events} alert event(s) fired across all routes.")

    # 3. Persist baselines to Supabase
    if not args.dry_run:
        save_state_to_db(supabase, state)
        print("Updated state persisted to Supabase.")
    else:
        print("(Dry run: DB write skipped.)")


if __name__ == "__main__":
    main()