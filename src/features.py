"""
features.py
Builds the modeling dataset for src/upset_model.py: one row per set in
data/raw_sets.csv, with features computed from what was *actually knowable
right before that set was played* - each player's pre-match Elo, pre-match
Glicko-2 rating/RD, seed, and character matchup (when available) - plus the
actual outcome as the label.

This is deliberately not just "join raw_sets.csv against the final
elo_ratings.csv/glicko_ratings.csv leaderboards" - those reflect every set
up through the most recent one, so using them to featurize an old set would
leak the outcome of every later set the players went on to play. Instead
this replays the same chronological update loops elo.py/glicko.py already
do (record_history=True), and snapshots each player's state immediately
before their rating is touched by that set.

Label convention: entrant1_wins (1/0). Every diff feature is signed
(entrant1 minus entrant2), so a trained model predicts P(entrant1 wins)
directly, and "upset probability" for any set is just that probability
evaluated on whichever side the ratings made the underdog.

Usage:
    python src/features.py                 # writes data/model_features.csv
    from features import build_features     # or import the DataFrame directly
"""

import os

import pandas as pd

from elo import compute_elo, load_sets
from glicko import compute_glicko

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
RAW_PATH = os.path.join(DATA_DIR, "raw_sets.csv")
MATCHUP_PATH = os.path.join(DATA_DIR, "character-matchup-matrix.csv")
OUT_PATH = os.path.join(DATA_DIR, "model_features.csv")

# Characters that appear in raw_sets.csv but not as their own row/column in
# the matchup matrix - all of them are echo fighters (mechanical clones) or
# alternate names, so they're mapped to the matrix entry that actually
# describes their matchups.
CHARACTER_ALIASES = {
    "Daisy": "Peach",
    "Dark Pit": "Pit",
    "Dark Samus": "Samus",
    "Richter": "Simon",
    "Simon Belmont": "Simon",
    "Rosalina": "Rosalina & Luma",
    "Banjo-Kazooie": "Banjo & Kazooie",
    "Pyra & Mythra": "Pyra/Mythra",
}


def load_matchup_matrix(path=MATCHUP_PATH):
    return pd.read_csv(path, index_col=0)


def _normalize_char(name):
    return CHARACTER_ALIASES.get(name, name)


def _main_character(characters_field):
    """First character listed (pick order) for an entrant, or None."""
    if not characters_field:
        return None
    chars = str(characters_field).split(";")
    return _normalize_char(chars[0]) if chars and chars[0] else None


def matchup_delta(matrix, char1, char2):
    """Advantage for entrant1's character over entrant2's, or None if
    either character is unknown or not in the matrix."""
    if char1 is None or char2 is None:
        return None
    if char1 not in matrix.index or char2 not in matrix.columns:
        return None
    val = matrix.loc[char1, char2]
    return float(val) if pd.notna(val) else None


def build_features(raw_path=RAW_PATH, matchup_path=MATCHUP_PATH):
    rows = load_sets(raw_path)
    _, _, _, elo_pre = compute_elo(rows, record_history=True)
    _, _, _, _, glicko_pre = compute_glicko(rows, record_history=True)
    matrix = load_matchup_matrix(matchup_path)

    records = []
    for row in rows:
        set_id = row["set_id"]
        r1, r2, eg1, eg2 = elo_pre[set_id]
        grat1, grd1, grat2, grd2, gg1, gg2 = glicko_pre[set_id]

        seed1, seed2 = row["entrant1_seed"], row["entrant2_seed"]
        seed_known = bool(seed1) and bool(seed2)
        seed_diff = (int(seed2) - int(seed1)) if seed_known else 0

        char1 = _main_character(row["entrant1_characters"])
        char2 = _main_character(row["entrant2_characters"])
        delta = matchup_delta(matrix, char1, char2)
        matchup_known = delta is not None

        entrant1_wins = int(row["winner_player_id"] == row["entrant1_player_id"])

        records.append({
            "set_id": set_id,
            "completed_at": int(row["completed_at"]),
            "entrant1_player_id": row["entrant1_player_id"],
            "entrant2_player_id": row["entrant2_player_id"],
            "entrant1_name": row["entrant1_name"],
            "entrant2_name": row["entrant2_name"],
            "elo_diff": r1 - r2,
            "glicko_diff": grat1 - grat2,
            "glicko_rd_sum": grd1 + grd2,
            "games_min_pre": min(eg1, eg2),
            "seed_diff": seed_diff,
            "seed_known": int(seed_known),
            "matchup_delta": delta if matchup_known else 0.0,
            "matchup_known": int(matchup_known),
            "entrant1_wins": entrant1_wins,
        })

    df = pd.DataFrame.from_records(records).sort_values("completed_at").reset_index(drop=True)
    return df


def main():
    df = build_features()
    df.to_csv(OUT_PATH, index=False)
    print(f"Wrote {len(df)} feature rows to {OUT_PATH}")
    print(f"  seed known:     {df['seed_known'].mean():.1%}")
    print(f"  matchup known:  {df['matchup_known'].mean():.1%}")
    print(f"  entrant1 win rate: {df['entrant1_wins'].mean():.1%} (expect ~50%, no systematic slot bias)")


if __name__ == "__main__":
    main()
