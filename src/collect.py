"""
collect.py
Pulls sets (seed, result, characters played) from start.gg for Smash Ultimate
events and writes them to a flat CSV for feature engineering / modeling.

Usage:
    python src/collect.py <tournament-slug> [<tournament-slug> ...]

Example:
    python src/collect.py tournament/atlantis-series
"""

import csv
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("STARTGG_API_KEY")
ENDPOINT = "https://api.start.gg/gql/alpha"
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

ULTIMATE_VIDEOGAME_ID = 1386

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "raw_sets.csv")

FIELDNAMES = [
    "set_id",
    "event_id",
    "event_name",
    "round",
    "completed_at",
    "entrant1_id",
    "entrant1_player_id",
    "entrant1_name",
    "entrant1_seed",
    "entrant1_characters",
    "entrant2_id",
    "entrant2_player_id",
    "entrant2_name",
    "entrant2_seed",
    "entrant2_characters",
    "winner_id",
    "winner_player_id",
    "loser_id",
    "loser_player_id",
    "display_score",
    "has_character_data",
    "is_upset",
]

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

SETS_QUERY = """
query EventSets($eventId: ID!, $page: Int!) {
  event(id: $eventId) {
    id
    name
    sets(page: $page, perPage: 30, sortType: STANDARD) {
      pageInfo { totalPages }
      nodes {
        id
        fullRoundText
        completedAt
        winnerId
        displayScore
        slots {
          entrant {
            id
            name
            seeds { seedNum }
            participants {
              player { id gamerTag }
            }
          }
        }
        games {
          selections {
            entrant { id }
            character { name }
          }
        }
      }
    }
  }
}
"""


def _post(query, variables, retries=5):
    """POST a GraphQL query, retrying with backoff on rate limits / transient errors."""
    for attempt in range(retries):
        resp = requests.post(
            ENDPOINT, json={"query": query, "variables": variables}, headers=HEADERS
        )
        if resp.status_code == 200:
            body = resp.json()
            if "errors" in body:
                raise RuntimeError(f"GraphQL error: {body['errors']}")
            return body["data"]
        if resp.status_code in (429, 500, 502, 503):
            wait = 2 ** attempt
            print(f"  ...got {resp.status_code}, retrying in {wait}s")
            time.sleep(wait)
            continue
        resp.raise_for_status()
    raise RuntimeError(f"Failed after {retries} retries: {resp.status_code} {resp.text}")


def get_ultimate_events(tournament_slug):
    """Return list of (event_id, event_name) for Ultimate events in a tournament."""
    data = _post(EVENTS_QUERY, {"slug": tournament_slug})
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


def _seed_for(entrant):
    seeds = entrant.get("seeds") or []
    return seeds[0]["seedNum"] if seeds else None


def _player_id_for(entrant):
    """Stable cross-event identity. entrant.id is scoped to one event's
    registration; participant.player.id follows the actual start.gg account.
    Falls back to a namespaced entrant id if no linked player (e.g. guest
    entries) so it never collides with a real player id."""
    participants = entrant.get("participants") or []
    if participants and participants[0].get("player"):
        return participants[0]["player"]["id"]
    return f"e{entrant['id']}"


def _characters_for(entrant_id, games):
    """Unique characters an entrant picked across all games in a set, in pick order."""
    seen = []
    for game in games or []:
        for sel in game.get("selections") or []:
            if sel["entrant"]["id"] == entrant_id and sel["character"]:
                name = sel["character"]["name"]
                if name not in seen:
                    seen.append(name)
    return seen


def parse_set(node, event_id, event_name):
    slots = node.get("slots") or []
    if len(slots) != 2 or not slots[0]["entrant"] or not slots[1]["entrant"]:
        return None  # bye or incomplete slot

    e1, e2 = slots[0]["entrant"], slots[1]["entrant"]
    winner_id = node.get("winnerId")
    if winner_id is None:
        return None  # not yet completed
    completed_at = node.get("completedAt")
    if completed_at is None:
        return None  # no timestamp to order by; skip rather than guess

    loser_id = e2["id"] if winner_id == e1["id"] else e1["id"]
    winner = e1 if winner_id == e1["id"] else e2
    loser = e2 if winner_id == e1["id"] else e1
    winner_seed, loser_seed = _seed_for(winner), _seed_for(loser)
    winner_player_id, loser_player_id = _player_id_for(winner), _player_id_for(loser)

    games = node.get("games") or []
    e1_chars = _characters_for(e1["id"], games)
    e2_chars = _characters_for(e2["id"], games)
    has_char_data = bool(e1_chars or e2_chars)

    is_upset = ""
    if winner_seed is not None and loser_seed is not None:
        is_upset = int(winner_seed > loser_seed)

    return {
        "set_id": node["id"],
        "event_id": event_id,
        "event_name": event_name,
        "round": node.get("fullRoundText"),
        "completed_at": completed_at,
        "entrant1_id": e1["id"],
        "entrant1_player_id": _player_id_for(e1),
        "entrant1_name": e1["name"],
        "entrant1_seed": _seed_for(e1),
        "entrant1_characters": ";".join(e1_chars),
        "entrant2_id": e2["id"],
        "entrant2_player_id": _player_id_for(e2),
        "entrant2_name": e2["name"],
        "entrant2_seed": _seed_for(e2),
        "entrant2_characters": ";".join(e2_chars),
        "winner_id": winner_id,
        "winner_player_id": winner_player_id,
        "loser_id": loser_id,
        "loser_player_id": loser_player_id,
        "display_score": node.get("displayScore"),
        "has_character_data": int(has_char_data),
        "is_upset": is_upset,
    }


def get_sets_for_event(event_id, event_name):
    rows = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        data = _post(SETS_QUERY, {"eventId": event_id, "page": page})
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


def load_existing_set_ids(path):
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {row["set_id"] for row in csv.DictReader(f)}


def write_rows(rows, path):
    file_exists = os.path.exists(path)
    existing_ids = load_existing_set_ids(path)
    new_rows = [r for r in rows if str(r["set_id"]) not in existing_ids]
    mode = "a" if file_exists else "w"
    with open(path, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        for row in new_rows:
            writer.writerow(row)
    print(f"  wrote {len(new_rows)} new rows ({len(rows) - len(new_rows)} already present)")


def main(slugs, max_events=None):
    if not API_KEY:
        print("STARTGG_API_KEY not set in .env")
        sys.exit(1)

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
            write_rows(rows, OUT_PATH)  # write per event so nothing is lost if interrupted
            events_pulled += 1


if __name__ == "__main__":
    args = sys.argv[1:]
    max_events = None
    if args and args[0] == "--max-events":
        max_events = int(args[1])
        args = args[2:]
    if not args:
        print(__doc__)
        sys.exit(1)
    main(args, max_events=max_events)
