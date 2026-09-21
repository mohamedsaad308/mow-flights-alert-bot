"""
Database-backed state persistence and user subscription queries using Supabase.
"""

import os
from datetime import date
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
STATE_VERSION = 3


def get_supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError(
            "SUPABASE_URL or SUPABASE_KEY is missing from environment variables."
        )
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def get_active_routes_from_subscribers():
    """Fetch distinct (origin, destination) pairs subscribed by any active Telegram user."""
    supabase = get_supabase()
    res = supabase.table("subscriptions").select("origin, destination").execute()
    routes = set()
    for row in (res.data or []):
        routes.add((row["origin"], row["destination"]))
    return sorted(list(routes))


def get_subscribers_for_route(origin: str, destination: str):
    """Return all chat_ids subscribed to a specific route."""
    supabase = get_supabase()
    res = (
        supabase.table("subscriptions")
        .select("chat_id")
        .eq("origin", origin)
        .eq("destination", destination)
        .execute()
    )
    return [row["chat_id"] for row in (res.data or [])]


def load_state() -> dict:
    """Fetches route states from Supabase."""
    supabase = get_supabase()
    resp = supabase.table("route_state").select("*").execute()

    state = {
        "version": STATE_VERSION,
        "records": {},
        "routes_with_fares": [],
        "empty_since": {},
    }

    for row in (resp.data or []):
        r_key = row["route_key"]
        if row.get("is_active"):
            state["routes_with_fares"].append(r_key)

        if row.get("price") is not None:
            state["records"][r_key] = {
                "price": float(row["price"]),
                "date": str(row["depart_date"]) if row.get("depart_date") else None,
                "seen": str(row["last_seen"]),
                "misses": int(row.get("misses", 0)),
            }

        if row.get("empty_since"):
            state["empty_since"][r_key] = str(row["empty_since"])

    return state


def save_state(state: dict):
    """Upserts modified state dictionaries back into Supabase."""
    supabase = get_supabase()
    upsert_rows = []

    all_keys = (
        set(state.get("records", {}).keys())
        | set(state.get("routes_with_fares", []))
        | set(state.get("empty_since", {}).keys())
    )

    for r_key in all_keys:
        rec = state.get("records", {}).get(r_key, {})
        upsert_rows.append(
            {
                "route_key": r_key,
                "price": rec.get("price"),
                "depart_date": rec.get("date"),
                "last_seen": rec.get("seen", date.today().isoformat()),
                "misses": rec.get("misses", 0),
                "is_active": r_key in state.get("routes_with_fares", []),
                "empty_since": state.get("empty_since", {}).get(r_key),
            }
        )

    if upsert_rows:
        supabase.table("route_state").upsert(
            upsert_rows, on_conflict="route_key"
        ).execute()