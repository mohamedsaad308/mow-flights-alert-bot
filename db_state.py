"""
Database-backed state persistence and user subscription queries using Supabase.
"""

import functools
import os
import time
from datetime import date

import httpx
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
STATE_VERSION = 3


@functools.lru_cache(maxsize=1)
def get_supabase() -> Client:
    """One shared client (avoids a new TLS handshake on every call)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError(
            "SUPABASE_URL or SUPABASE_KEY is missing from environment variables."
        )
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def retry_network(times=4, delay=2):
    """Retry a DB call on timeouts / connection errors."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            for attempt in range(times):
                try:
                    return fn(*args, **kwargs)
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    if attempt == times - 1:
                        raise
                    print(
                        f"Supabase error in {fn.__name__}: {e!r}, retrying...",
                        flush=True,
                    )
                    time.sleep(delay * (attempt + 1))

        return wrapper

    return deco


@retry_network()
def get_active_routes_from_subscribers():
    """Distinct (origin, destination) pairs subscribed to by any enabled subscription."""
    supabase = get_supabase()
    res = (
        supabase.table("subscriptions")
        .select("origin, destination")
        .eq("disabled", False)
        .execute()
    )
    routes = set()
    for row in (res.data or []):
        routes.add((row["origin"], row["destination"]))
    return sorted(list(routes))


@retry_network()
def get_route_subscribers(origin: str, destination: str):
    """Enabled subscription rows (chat_id + welcomed flag) for a specific route."""
    supabase = get_supabase()
    res = (
        supabase.table("subscriptions")
        .select("chat_id, welcomed")
        .eq("origin", origin)
        .eq("destination", destination)
        .eq("disabled", False)
        .execute()
    )
    return res.data or []


@retry_network()
def mark_welcomed(origin: str, destination: str, chat_ids: list):
    """Mark these subscribers as having received their first price snapshot."""
    if not chat_ids:
        return
    supabase = get_supabase()
    (
        supabase.table("subscriptions")
        .update({"welcomed": True})
        .eq("origin", origin)
        .eq("destination", destination)
        .in_("chat_id", chat_ids)
        .execute()
    )


@retry_network()
def disable_subscriptions(origin: str, destination: str):
    """Stop tracking an unsupported route for everyone subscribed to it."""
    supabase = get_supabase()
    (
        supabase.table("subscriptions")
        .update({"disabled": True, "welcomed": True})
        .eq("origin", origin)
        .eq("destination", destination)
        .execute()
    )


@retry_network()
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


@retry_network()
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