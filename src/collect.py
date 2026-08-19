"""
collect.py
Tier 1: pulls sets from known recurring "home series" tournaments (e.g. your
local weeklies) and writes them to data/raw_sets.csv. See regional_backfill.py
for Tier 2 (pulling a specific player's sets from anywhere they've competed).

Usage:
    # Pull specific tournament slugs directly
    python src/collect.py tournament/<slug> [tournament/<slug> ...]

    # Or auto-discover every edition a given organizer has run (recommended
    # for recurring weeklies/monthlies - no need to hand-collect slugs)
    python src/collect.py --owner-id <id>

    # Cap how many events get pulled in one run (large series can have 100+)
    python src/collect.py --max-events 8 tournament/<slug>

Example:
    python src/collect.py tournament/atlantis-series
"""

import sys
import time

from set_parsing import parse_set, write_rows, SET_FIELDS
from startgg_client import ULTIMATE_VIDEOGAME_ID, post, require_api_key

EVENTS_QUERY = """
query TournamentEvents($slug: String!) {
  tournament(slug: $slug) {
    id
    name
    events {
      id
      name
      numEntrants
      videogame { id }
    }
  }
}
"""

SETS_QUERY = f"""
query EventSets($eventId: ID!, $page: Int!) {{
  event(id: $eventId) {{
    id
    name
    sets(page: $page, perPage: 30, sortType: STANDARD) {{
      pageInfo {{ totalPages }}
      nodes {{
        {SET_FIELDS}
      }}
    }}
  }}
}}
"""

# Enumerates every tournament an organizer has run, regardless of naming
# scheme - the reliable way to find all editions of a recurring series.
# (Full-text search on tournament name is not reliable via the public API.)
TOURNAMENTS_BY_OWNER_QUERY = """
query TournamentsByOwner($ownerId: ID!, $page: Int!) {
  tournaments(query: {
    page: $page
    perPage: 30
    filter: { ownerId: $ownerId, videogameIds: [1386] }
    sortBy: "startAt desc"
  }) {
    pageInfo { totalPages }
    nodes { slug name }
  }
}
"""


def discover_series_slugs(owner_id, max_tournaments=None):
    """Return tournament slugs for every Ultimate event a given organizer
    (start.gg owner id) has run. Use this instead of hand-collecting slugs
    for a recurring weekly/monthly series."""
    slugs = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        if max_tournaments is not None and len(slugs) >= max_tournaments:
            break
        data = post(TOURNAMENTS_BY_OWNER_QUERY, {"ownerId": owner_id, "page": page})
        block = data["tournaments"]
        total_pages = block["pageInfo"]["totalPages"] or 1
        slugs.extend(n["slug"] for n in block["nodes"])
        page += 1
        time.sleep(0.6)
    return slugs[:max_tournaments] if max_tournaments else slugs


def get_ultimate_events(tournament_slug):
    """Return list of (event_id, event_name) for Ultimate events in a tournament."""
    data = post(EVENTS_QUERY, {"slug": tournament_slug})
    tournament = data.get("tournament")
    if tournament is None:
        print(f"  ! tournament not found: {tournament_slug}")
        return []
    events = tournament.get("events") or []
    return [
        (e["id"], e["name"])
        for e in events
        if e.get("videogame") and e["videogame"]["id"] == ULTIMATE_VIDEOGAME_ID
    ]


def get_sets_for_event(event_id, event_name):
    rows = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        data = post(SETS_QUERY, {"eventId": event_id, "page": page})
        event = data.get("event")
        if event is None:
            break
        sets_block = event["sets"]
        total_pages = sets_block["pageInfo"]["totalPages"] or 1
        for node in sets_block["nodes"]:
            row = parse_set(node, event_id, event_name)
            if row:
                rows.append(row)
        print(f"  page {page}/{total_pages} -> {len(rows)} sets so far")
        page += 1
        time.sleep(0.6)  # be polite to the API / stay under rate limits
    return rows


def main(slugs, max_events=None):
    require_api_key()

    events_pulled = 0
    for slug in slugs:
        print(f"Tournament: {slug}")
        events = get_ultimate_events(slug)
        if not events:
            print("  no Ultimate events found")
            continue
        for event_id, event_name in events:
            if max_events is not None and events_pulled >= max_events:
                print(f"  reached max_events={max_events}, stopping")
                return
            print(f" Event: {event_name} ({event_id})")
            rows = get_sets_for_event(event_id, event_name)
            write_rows(rows)  # write per event so nothing is lost if interrupted
            events_pulled += 1


if __name__ == "__main__":
    args = sys.argv[1:]
    max_events = None
    if "--max-events" in args:
        i = args.index("--max-events")
        max_events = int(args[i + 1])
        del args[i : i + 2]

    if args and args[0] == "--owner-id":
        require_api_key()
        owner_id = int(args[1])
        print(f"Discovering tournaments for owner {owner_id}...")
        slugs = discover_series_slugs(owner_id)
        print(f"Found {len(slugs)} tournaments")
        main(slugs, max_events=max_events)
    elif args:
        main(args, max_events=max_events)
    else:
        print(__doc__)
        sys.exit(1)
