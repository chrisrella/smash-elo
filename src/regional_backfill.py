"""
regional_backfill.py
Tier 2: given a roster of "local" player IDs (by default, derived from
whoever already shows up in data/raw_sets.csv via collect.py's home-series
pull), fetch each player's full start.gg set history and merge it in.

This is how out-of-area events get captured (CT/NJ/Long Island/NYC, majors,
etc.) without having to pre-guess which regional tournaments matter - we
follow the players instead of the tournaments.

Usage:
    # Backfill everyone already seen in data/raw_sets.csv
    python src/regional_backfill.py

    # Backfill specific player ids instead
    python src/regional_backfill.py --player-ids 1005782 3760543

    # Cap how many pages (30 sets/page) to pull per player - a very active
    # competitor can have a long history, so this defaults to recent-only
    python src/regional_backfill.py --max-pages-per-player 3

    # Only backfill players with at least N sets already in raw_sets.csv -
    # a full home-series history can surface thousands of one-off visitors
    # who aren't really "in the scene"
    python src/regional_backfill.py --min-sets 10
"""

import csv
import os
import sys
import time

from set_parsing import OUT_PATH, parse_set, write_rows, SET_FIELDS
from startgg_client import post, require_api_key

DEFAULT_MAX_PAGES_PER_PLAYER = 5  # ~150 most recent sets per player

# Tracks which players have already been fully backfilled, so an interrupted
# run can resume without re-walking (and re-querying start.gg for) everyone
# already done - this can be a long-running job across many resumes.
PROGRESS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", ".regional_backfill_progress.txt")

PLAYER_SETS_PER_PAGE = 15  # lower than collect.py's 30 - player.sets nodes
# carry enough nested data (games/selections across many events) that 30/page
# occasionally exceeds start.gg's query complexity cap (max 1000 objects)

PLAYER_SETS_QUERY = f"""
query PlayerSets($playerId: ID!, $page: Int!) {{
  player(id: $playerId) {{
    id
    gamerTag
    sets(page: $page, perPage: {PLAYER_SETS_PER_PAGE}) {{
      pageInfo {{ totalPages }}
      nodes {{
        {SET_FIELDS}
        event {{
          id
          name
          videogame {{ id }}
          tournament {{ name addrState countryCode }}
        }}
      }}
    }}
  }}
}}
"""


def local_roster_from_csv(path=OUT_PATH, min_sets=1):
    """Real (non-fallback) player ids present in raw_sets.csv, optionally
    filtered to players with at least `min_sets` sets already recorded -
    a full home-series pull surfaces every one-off visitor along with the
    actual regulars, and most callers only want the latter."""
    if not os.path.exists(path):
        return []
    counts = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            for col in ("entrant1_player_id", "entrant2_player_id"):
                pid = row.get(col, "")
                if pid and not pid.startswith("e"):  # skip unlinked-guest fallback ids
                    counts[pid] = counts.get(pid, 0) + 1
    return sorted(pid for pid, n in counts.items() if n >= min_sets)


def load_progress(path=PROGRESS_PATH):
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def mark_done(player_id, path=PROGRESS_PATH):
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{player_id}\n")


def get_sets_for_player(player_id, max_pages=DEFAULT_MAX_PAGES_PER_PLAYER):
    rows = []
    page = 1
    total_pages = 1
    gamer_tag = None
    while page <= total_pages and page <= max_pages:
        data = post(PLAYER_SETS_QUERY, {"playerId": player_id, "page": page})
        player = data.get("player")
        if player is None:
            print(f"  ! player not found: {player_id}")
            return rows
        gamer_tag = player.get("gamerTag")
        sets_block = player["sets"]
        total_pages = sets_block["pageInfo"]["totalPages"] or 1
        for node in sets_block["nodes"]:
            event = node.get("event") or {}
            tournament = event.get("tournament") or {}
            event_id = event.get("id")
            event_name = f"{tournament.get('name', '')} - {event.get('name', '')}".strip(" -")
            videogame = event.get("videogame") or {}
            row = parse_set(
                node, event_id, event_name,
                videogame_id=videogame.get("id"),
                tournament_state=tournament.get("addrState"),
                tournament_country=tournament.get("countryCode"),
            )
            if row:
                rows.append(row)
        print(f"  {gamer_tag or player_id}: page {page}/{min(total_pages, max_pages)} -> {len(rows)} sets so far")
        page += 1
        time.sleep(0.6)
    return rows


def main(player_ids, max_pages_per_player=DEFAULT_MAX_PAGES_PER_PLAYER):
    require_api_key()
    done = load_progress()
    remaining = [p for p in player_ids if p not in done]
    if len(remaining) < len(player_ids):
        print(f"Skipping {len(player_ids) - len(remaining)} already-backfilled players (resume)")
    for player_id in remaining:
        print(f"Player {player_id}:")
        try:
            rows = get_sets_for_player(player_id, max_pages=max_pages_per_player)
        except Exception as e:
            # Don't let one problematic player (e.g. a query-complexity edge
            # case) kill the whole batch. Not marked done, so a future
            # resume will retry them.
            print(f"  ! failed on player {player_id}, skipping: {e}")
            continue
        write_rows(rows)
        mark_done(player_id)


if __name__ == "__main__":
    args = sys.argv[1:]
    max_pages = DEFAULT_MAX_PAGES_PER_PLAYER
    if "--max-pages-per-player" in args:
        i = args.index("--max-pages-per-player")
        max_pages = int(args[i + 1])
        del args[i : i + 2]

    min_sets = 1
    if "--min-sets" in args:
        i = args.index("--min-sets")
        min_sets = int(args[i + 1])
        del args[i : i + 2]

    if "--player-ids" in args:
        i = args.index("--player-ids")
        player_ids = args[i + 1 :]
    else:
        player_ids = local_roster_from_csv(min_sets=min_sets)
        print(f"No --player-ids given; derived {len(player_ids)} players from {OUT_PATH} (min_sets={min_sets})")

    if not player_ids:
        print("No players to backfill. Run collect.py first, or pass --player-ids.")
        sys.exit(1)

    main(player_ids, max_pages_per_player=max_pages)
