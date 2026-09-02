"""
set_parsing.py
Turns a raw start.gg `Set` node (the same shape whether it came from
`event.sets` or `player.sets`) into a flat row dict, plus the shared CSV
schema and read/write helpers. Used by both collect.py (home series) and
regional_backfill.py (player-centric pull) so the two collectors always
produce rows in the same shape.
"""

import csv
import os

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "raw_sets.csv")

FIELDNAMES = [
    "set_id",
    "event_id",
    "event_name",
    "videogame_id",
    "tournament_state",
    "tournament_country",
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

# Fields every `Set` query needs to request for parse_set() to work,
# regardless of whether it's reached via event.sets or player.sets.
SET_FIELDS = """
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
"""


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


def parse_set(node, event_id, event_name, videogame_id=None, tournament_state=None, tournament_country=None):
    """node: a raw `Set` GraphQL node (must include SET_FIELDS).
    videogame_id/tournament_state/tournament_country: caller-supplied context
    (not part of SET_FIELDS itself, since collect.py already knows these per
    event/tournament and regional_backfill.py fetches them per-node - see
    each caller for how they're sourced).
    Returns a FIELDNAMES-shaped row dict, or None for byes/incomplete sets."""
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
        "videogame_id": videogame_id,
        "tournament_state": tournament_state,
        "tournament_country": tournament_country,
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


def load_existing_set_ids(path=OUT_PATH):
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {row["set_id"] for row in csv.DictReader(f)}


def write_rows(rows, path=OUT_PATH):
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
    return new_rows


# Substrings identifying a home-series (Tier 1) event_name - these are the
# only tournaments collect.py pulls, so any row matching one of these came
# from your actual recurring local series, not a one-off regional visit
# picked up via regional_backfill.py.
HOME_SERIES_MARKERS = ("Encore Smash", "Undiscovered Smash", "Back From")


def home_series_player_ids(rows):
    """Player ids with at least one home-series appearance. Used to keep a
    player's *results* in the rating calculation (their wins/losses are
    still real signal about the locals who played them) while excluding
    them from the ranked leaderboard itself if they never actually showed
    up to a home series - e.g. a strong player who drove in for one big
    regional invitational and topped the leaderboard off a handful of sets
    without ever being "in the scene." """
    ids = set()
    for row in rows:
        if any(marker in row["event_name"] for marker in HOME_SERIES_MARKERS):
            ids.add(row["entrant1_player_id"])
            ids.add(row["entrant2_player_id"])
    return ids
