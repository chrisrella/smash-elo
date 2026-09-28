"""
event_metadata.py
Fetches per-event metadata that raw_sets.csv doesn't carry - entrant count,
online flag, team size, start time, and the parent tournament's venue - for
every event_id in data/raw_sets.csv, and resolves each venue to a county.
Output: data/event_metadata.csv, consumed by eligibility.py.

Why a separate lookup instead of widening raw_sets.csv's schema: the
collectors have already pulled ~70k sets, and PR eligibility only needs
one row per *event*, not per set - so this is a few dozen batched queries
instead of a full re-collection.

County comes from the FCC's free census Area API (lat/lng -> county, no
key needed). start.gg's own address fields only give city/state/zip, and
the eligibility rules hinge on county-level distinctions (Westchester vs
the Bronx vs Rockland) that a state code can't express. Only public venue
coordinates are sent - nothing about players.

Incremental: events and coordinates already in the output are skipped, so
re-running after a new collect.py/regional_backfill.py pull only fetches
what's new.

Usage:
    python src/event_metadata.py
"""

import csv
import os

import requests

from set_parsing import OUT_PATH as RAW_SETS_PATH
from startgg_client import post, require_api_key

META_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "event_metadata.csv")
FCC_AREA_URL = "https://geo.fcc.gov/api/census/area"

EVENTS_PER_QUERY = 50  # aliased event lookups per request - well under start.gg's complexity cap

META_FIELDS = [
    "event_id",
    "event_name",
    "videogame_id",
    "num_entrants",
    "is_online",
    "team_size",
    "start_at",
    "tournament_id",
    "tournament_name",
    "tournament_slug",
    "city",
    "state",
    "postal_code",
    "lat",
    "lng",
    "county",
]

EVENT_FIELDS = """
    id
    name
    numEntrants
    isOnline
    startAt
    teamRosterSize { maxPlayers }
    videogame { id }
    tournament { id name slug city addrState postalCode lat lng }
"""


def load_metadata(path=META_PATH):
    if not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {row["event_id"]: row for row in csv.DictReader(f)}


def save_metadata(meta, path=META_PATH):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=META_FIELDS)
        writer.writeheader()
        for event_id in sorted(meta, key=int):
            writer.writerow(meta[event_id])


def raw_event_ids(path=RAW_SETS_PATH):
    with open(path, newline="", encoding="utf-8") as f:
        return {row["event_id"] for row in csv.DictReader(f)}


def fetch_events(event_ids):
    """One aliased GraphQL request for a batch of events. Events start.gg no
    longer returns (deleted/private) come back as None and are skipped."""
    query = "query { " + " ".join(f"e{eid}: event(id: {eid}) {{ {EVENT_FIELDS} }}" for eid in event_ids) + " }"
    data = post(query, {})
    return [node for node in data.values() if node]


def to_row(node):
    t = node.get("tournament") or {}
    roster = node.get("teamRosterSize") or {}
    return {
        "event_id": str(node["id"]),
        "event_name": node["name"],
        "videogame_id": (node.get("videogame") or {}).get("id"),
        "num_entrants": node.get("numEntrants") or 0,
        "is_online": int(bool(node.get("isOnline"))),
        "team_size": roster.get("maxPlayers") or 1,
        "start_at": node.get("startAt"),
        "tournament_id": t.get("id"),
        "tournament_name": t.get("name"),
        "tournament_slug": t.get("slug"),
        "city": t.get("city"),
        "state": t.get("addrState"),
        "postal_code": t.get("postalCode"),
        "lat": t.get("lat"),
        "lng": t.get("lng"),
        "county": "",
    }


def lookup_county(lat, lng):
    """'Westchester County', 'Fairfield County', etc. via the FCC census Area
    API, or '' if the point isn't in a US county (or the lookup fails)."""
    try:
        resp = requests.get(FCC_AREA_URL, params={"lat": lat, "lon": lng, "format": "json"}, timeout=30)
        resp.raise_for_status()
        results = resp.json().get("results") or []
    except (requests.exceptions.RequestException, ValueError) as e:
        print(f"  ...county lookup failed for ({lat}, {lng}): {e.__class__.__name__}")
        return ""
    return results[0]["county_name"] if results else ""


def fill_counties(meta):
    """Resolve county for every row that has coordinates but no county yet,
    one FCC call per distinct venue (many events share a venue)."""
    known = {(r["lat"], r["lng"]): r["county"] for r in meta.values() if r["county"]}
    pending = {(r["lat"], r["lng"]) for r in meta.values() if not r["county"] and r["lat"] and r["lng"]}
    pending -= known.keys()
    if pending:
        print(f"Resolving counties for {len(pending)} venues...")
    for i, (lat, lng) in enumerate(sorted(pending), start=1):
        known[(lat, lng)] = lookup_county(lat, lng)
        if i % 50 == 0:
            print(f"  {i}/{len(pending)}")
    for row in meta.values():
        if not row["county"]:
            row["county"] = known.get((row["lat"], row["lng"]), "")


def main():
    require_api_key()
    meta = load_metadata()
    missing = sorted(raw_event_ids() - meta.keys(), key=int)
    print(f"{len(meta)} events cached, {len(missing)} to fetch")

    for start in range(0, len(missing), EVENTS_PER_QUERY):
        batch = missing[start:start + EVENTS_PER_QUERY]
        for node in fetch_events(batch):
            row = to_row(node)
            # Normalize coordinates to strings so they key identically to
            # rows read back from the CSV on later runs.
            row["lat"] = "" if row["lat"] is None else str(row["lat"])
            row["lng"] = "" if row["lng"] is None else str(row["lng"])
            meta[row["event_id"]] = row
        print(f"  fetched {min(start + EVENTS_PER_QUERY, len(missing))}/{len(missing)}")
        save_metadata(meta)  # checkpoint each batch so an interrupted run resumes

    fill_counties(meta)
    save_metadata(meta)
    print(f"Wrote {len(meta)} events to {META_PATH}")


if __name__ == "__main__":
    main()
