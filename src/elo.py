"""
elo.py
Computes Elo ratings for players from data/raw_sets.csv and prints/saves a
leaderboard. Sets are processed in chronological order (completedAt), each
set is one Elo update between the two entrants (set-level, not game-level).

Every run also saves a timestamped snapshot to data/elo_history/, so
successive pipeline runs (e.g. before/after a data-quality fix) can be
compared against each other rather than only ever seeing the latest.

Ratings are computed from every set in raw_sets.csv (including a one-time
visitor's results - those are still real signal about the locals who
played them), but the leaderboard itself only ranks players with at least
one home-series (Encore/Undiscovered/BFTD) appearance. Otherwise someone
who drove in once for a single big regional invitational can top the
leaderboard off a handful of sets without ever being part of the scene.

Usage:
    python src/elo.py
    python src/elo.py --label post-region-filter   # tag the snapshot
"""

import csv
import datetime
import os
import sys

from set_parsing import home_series_player_ids

IN_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "raw_sets.csv")
OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "elo_ratings.csv")
HISTORY_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "elo_history")

STARTING_RATING = 1500
K_FACTOR = 32


def expected_score(rating_a, rating_b):
    return 1 / (1 + 10 ** ((rating_b - rating_a) / 400))


def load_sets(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("completed_at")]
    rows.sort(key=lambda r: int(r["completed_at"]))
    return rows


def compute_elo(rows, record_history=False):
    """record_history=True additionally returns pre_match: dict of
    set_id -> (r1_pre, r2_pre, games1_pre, games2_pre), i.e. each player's
    rating and experience *before* that set was applied. This is what a
    predictive model has to train on - using the final leaderboard rating
    to "predict" an old match would be leaking the outcome of every match
    that happened after it."""
    ratings = {}  # entrant_id -> rating
    names = {}  # entrant_id -> most recently seen name
    games_played = {}  # entrant_id -> count
    pre_match = {} if record_history else None

    for row in rows:
        p1, p2 = row["entrant1_player_id"], row["entrant2_player_id"]
        names[p1] = row["entrant1_name"]
        names[p2] = row["entrant2_name"]
        r1 = ratings.setdefault(p1, STARTING_RATING)
        r2 = ratings.setdefault(p2, STARTING_RATING)
        g1, g2 = games_played.get(p1, 0), games_played.get(p2, 0)

        if record_history:
            pre_match[row["set_id"]] = (r1, r2, g1, g2)

        winner = row["winner_player_id"]
        score1 = 1.0 if winner == p1 else 0.0
        score2 = 1.0 - score1

        exp1 = expected_score(r1, r2)
        exp2 = 1 - exp1

        ratings[p1] = r1 + K_FACTOR * (score1 - exp1)
        ratings[p2] = r2 + K_FACTOR * (score2 - exp2)

        games_played[p1] = g1 + 1
        games_played[p2] = g2 + 1

    if record_history:
        return ratings, names, games_played, pre_match
    return ratings, names, games_played


def write_leaderboard(ratings, names, games_played, path, eligible_ids=None):
    pool = ratings.items() if eligible_ids is None else (
        (pid, r) for pid, r in ratings.items() if pid in eligible_ids
    )
    leaderboard = sorted(pool, key=lambda kv: kv[1], reverse=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["rank", "entrant_id", "name", "elo", "sets_played"])
        for rank, (entrant_id, rating) in enumerate(leaderboard, start=1):
            writer.writerow(
                [rank, entrant_id, names[entrant_id], round(rating, 1), games_played[entrant_id]]
            )
    return leaderboard


def snapshot_path(label=None):
    os.makedirs(HISTORY_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"elo_ratings_{stamp}" + (f"_{label}" if label else "") + ".csv"
    return os.path.join(HISTORY_DIR, name)


def main(label=None):
    rows = load_sets(IN_PATH)
    print(f"Processing {len(rows)} sets (chronological)...")
    ratings, names, games_played = compute_elo(rows)
    eligible = home_series_player_ids(rows)
    print(f"{len(eligible)}/{len(ratings)} players have a home-series appearance (leaderboard-eligible)")
    leaderboard = write_leaderboard(ratings, names, games_played, OUT_PATH, eligible_ids=eligible)

    print(f"\n{'Rank':<5}{'Name':<20}{'Elo':<8}{'Sets':<6}")
    for rank, (entrant_id, rating) in enumerate(leaderboard[:25], start=1):
        print(f"{rank:<5}{names[entrant_id]:<20}{round(rating):<8}{games_played[entrant_id]:<6}")
    print(f"\nFull leaderboard written to {OUT_PATH}")

    snap_path = snapshot_path(label)
    write_leaderboard(ratings, names, games_played, snap_path, eligible_ids=eligible)
    print(f"Snapshot saved to {snap_path}")


if __name__ == "__main__":
    args = sys.argv[1:]
    label = None
    if "--label" in args:
        i = args.index("--label")
        label = args[i + 1]
    main(label=label)
