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
"""

import sys
import time

from set_parsing import OUT_PATH, load_existing_set_ids, parse_set, write_rows, SET_FIELDS
from startgg_client import post, require_api_key

DEFAULT_MAX_PAGES_PER_PLAYER = 5  # ~150 most recent sets per player

PLAYER_SETS_QUERY = f"""
query PlayerSets($playerId: ID!, $page: Int!) {{
  player(id: $playerId) {{
    id
    gamerTag
    sets(page: $page, perPage: 30) {{
      pageInfo {{ totalPages }}
      nodes {{
        {SET_FIELDS}
        event {{
          id
          name
          tournament {{ name }}
        }}
      }}
    }}
  }}
}}
"""


def local_roster_from_csv(path=OUT_PATH):
    """Every real (non-fallback) player id already present in raw_sets.csv."""
    import csv
    import os

    if not os.path.exists(path):
        return []
    ids = set()
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            for col in ("entrant1_player_id", "entrant2_player_id"):
                pid = row.get(col, "")
                if pid and not pid.startswith("e"):  # skip unlinked-guest fallback ids
                    ids.add(pid)
    return sorted(ids)


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
            row = parse_set(node, event_id, event_name)
            if row:
                rows.append(row)
        print(f"  {gamer_tag or player_id}: page {page}/{min(total_pages, max_pages)} -> {len(rows)} sets so far")
        page += 1
        time.sleep(0.6)
    return rows


def main(player_ids, max_pages_per_player=DEFAULT_MAX_PAGES_PER_PLAYER):
    require_api_key()
    for player_id in player_ids:
        print(f"Player {player_id}:")
        rows = get_sets_for_player(player_id, max_pages=max_pages_per_player)
        write_rows(rows)


if __name__ == "__main__":
    args = sys.argv[1:]
    max_pages = DEFAULT_MAX_PAGES_PER_PLAYER
    if "--max-pages-per-player" in args:
        i = args.index("--max-pages-per-player")
        max_pages = int(args[i + 1])
        del args[i : i + 2]

    if "--player-ids" in args:
        i = args.index("--player-ids")
        player_ids = args[i + 1 :]
    else:
        player_ids = local_roster_from_csv()
        print(f"No --player-ids given; derived {len(player_ids)} players from {OUT_PATH}")

    if not player_ids:
        print("No players to backfill. Run collect.py first, or pass --player-ids.")
        sys.exit(1)

    main(player_ids, max_pages_per_player=max_pages)
