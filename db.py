import os
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

def get_db() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set.")
    return create_client(SUPABASE_URL, SUPABASE_KEY)

def get_active_routes(supabase: Client):
    """Fetch distinct (origin, destination) pairs subscribed by any user."""
    res = supabase.table("subscriptions").select("origin, destination").execute()
    routes = set()
    for row in (res.data or []):
        routes.add((row["origin"], row["destination"]))
    return sorted(list(routes))

def get_subscribers_for_route(supabase: Client, origin: str, destination: str):
    """Return all chat_ids subscribed to this route."""
    res = supabase.table("subscriptions") \
        .select("chat_id") \
        .eq("origin", origin) \
        .eq("destination", destination) \
        .execute()
    return [row["chat_id"] for row in (res.data or [])]

def load_baselines(supabase: Client):
    """Load all tracked price records and known routes from DB."""
    records_res = supabase.table("route_baselines").select("*").execute()
    known_res = supabase.table("known_routes").select("*").execute()

    records = {}
    for r in (records_res.data or []):
        records[r["route_key"]] = {
            "price": float(r["lowest_price"]),
            "date": r["flight_date"],
            "seen": r["last_seen"],
            "misses": r["misses"]
        }

    routes_with_fares = []
    empty_since = {}
    for kr in (known_res.data or []):
        routes_with_fares.append(kr["route_key"])
        if kr["empty_since"]:
            empty_since[kr["route_key"]] = kr["empty_since"]

    return {
        "records": records,
        "routes_with_fares": routes_with_fares,
        "empty_since": empty_since
    }

def save_baselines(supabase: Client, state: dict):
    """Upsert updated baselines back to Supabase."""
    # 1. Upsert records
    for key, data in state["records"].items():
        supabase.table("route_baselines").upsert({
            "route_key": key,
            "lowest_price": data["price"],
            "flight_date": data["date"],
            "last_seen": data["seen"],
            "misses": data.get("misses", 0)
        }).execute()

    # 2. Sync known_routes
    for key in state["routes_with_fares"]:
        empty_val = state["empty_since"].get(key)
        supabase.table("known_routes").upsert({
            "route_key": key,
            "empty_since": empty_val
        }).execute()