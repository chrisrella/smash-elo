"""
eligibility.py
Power-rankings (PR) eligibility for a quarterly season, per the panel's
rules:

- Seasons are calendar quarters (Q1 = Jan-Mar, ..., Q4 = Oct-Dec).
- A Westchester resident qualifies by attending 3+ Westchester tournaments
  that season.
- A resident of a neighboring county (Bronx, Rockland, Putnam, Fairfield CT)
  qualifies by attending 3+ Westchester tournaments AND Westchester being
  the plurality of all tournaments they attended that season. Ties go to
  inclusion (Westchester only has to tie the busiest other region).
- Nobody else qualifies.

"Attending a tournament" means appearing in that tournament's *main*
bracket: the Ultimate 1v1, in-person event - redemption/amateur/side
brackets, doubles and online events don't count, and a tournament only
counts once. Arcadians, invitationals and Encore Smash 101 (a practice
series) don't count at all. Only tournaments whose main bracket had 8+ entrants count,
in every region (applying the floor everywhere, not just to Westchester,
keeps tiny out-of-region events from outvoting Westchester in the
plurality check - consistent with leaning toward inclusion).

Plurality is compared by region, not county: NYC (all five boroughs),
Long Island, Connecticut and New Jersey each count as one region, as does
Westchester; any other county is its own region.

Residence can't be derived from start.gg (profile locations are optional
and mostly blank), so it comes from a hand-maintained
data/player_residences.csv - editable from the dashboard's PR Eligibility
tab. It's gitignored along with the rest of data/, since the repo is
public and this is where people live. Anyone with 3+ Westchester
tournaments but no residence on file is reported as "needs residence"
rather than silently dropped.

Requires data/event_metadata.csv (run src/event_metadata.py first).

Usage:
    python src/eligibility.py                  # most recent season in the data
    python src/eligibility.py --season 2026Q2
"""

import argparse
import csv
import datetime
import os
import re
from zoneinfo import ZoneInfo

import pandas as pd

from event_metadata import META_PATH
from set_parsing import OUT_PATH as RAW_SETS_PATH
from startgg_client import ULTIMATE_VIDEOGAME_ID

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
RESIDENCES_PATH = os.path.join(DATA_DIR, "player_residences.csv")
GLICKO_PATH = os.path.join(DATA_DIR, "glicko_ratings.csv")

EASTERN = ZoneInfo("America/New_York")

MIN_ENTRANTS = 8
MIN_WESTCHESTER_TOURNAMENTS = 3

HOME_REGION = "Westchester"
RESIDENCE_WESTCHESTER = "Westchester"
NEIGHBOR_RESIDENCES = ("Bronx", "Rockland", "Putnam", "Fairfield CT")
RESIDENCE_OTHER = "Other (not eligible)"
RESIDENCE_OPTIONS = (RESIDENCE_WESTCHESTER, *NEIGHBOR_RESIDENCES, RESIDENCE_OTHER)

NYC_COUNTIES = {"Bronx County", "New York County", "Kings County", "Queens County", "Richmond County"}
LONG_ISLAND_COUNTIES = {"Nassau County", "Suffolk County"}
WHOLE_STATE_REGIONS = {"CT": "Connecticut", "NJ": "New Jersey"}

# Event-name patterns for side brackets. Only used to *prefer* other events
# at the same tournament - if a tournament's only 1v1 event matches one of
# these, it's still that tournament's main bracket (whether the tournament
# counts at all is decided afterwards, by EXCLUDED_TOURNAMENT_PATTERN).
SIDE_BRACKET_PATTERN = re.compile(
    r"amateur|ladder|side|arcadian|low tier|random|all pt|main swap|pro[- ]?am|novice|beginner|"
    r"squad|crew|doubles|amiibo|final smash|items|hardcore|joust|character bracket|\bonly\b|%",
    re.IGNORECASE,
)

# Events that are never a tournament's main bracket, even as its only
# Ultimate 1v1 event in our data - redemption brackets (the main bracket's
# sets just weren't collected) and HDR, a mod rather than Ultimate proper.
NEVER_MAIN_PATTERN = re.compile(r"redemption|2nd chance|second chance|hdr", re.IGNORECASE)

# Tournaments that never count toward PR eligibility, matched against the
# tournament name or its main bracket's name: arcadians and invitationals
# (panel rule), and Encore Smash 101, a practice series meant not to count.
# A regular tournament with an arcadian *side* bracket still counts - its
# main bracket is picked first, and only then checked here.
EXCLUDED_TOURNAMENT_PATTERN = re.compile(r"arcadian|invitational|encore (?:smash )?101", re.IGNORECASE)


def season_of(ts):
    """Unix timestamp -> '2026Q3', using Eastern time so a late-night
    bracket on the last day of a quarter stays in that quarter."""
    d = datetime.datetime.fromtimestamp(int(ts), tz=EASTERN)
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def region_of(state, county):
    if state == "NY" and county == "Westchester County":
        return HOME_REGION
    if state == "NY" and county in NYC_COUNTIES:
        return "NYC"
    if state == "NY" and county in LONG_ISLAND_COUNTIES:
        return "Long Island"
    if state in WHOLE_STATE_REGIONS:
        return WHOLE_STATE_REGIONS[state]
    if county:
        return f"{county}, {state}"
    return f"Unknown ({state or '?'})"


def load_metadata(path=META_PATH):
    if not os.path.exists(path):
        raise SystemExit(f"{path} not found - run `python src/event_metadata.py` first.")
    meta = pd.read_csv(path, dtype={"event_id": str, "tournament_id": str, "county": str, "state": str})
    meta["county"] = meta["county"].fillna("")
    meta["state"] = meta["state"].fillna("")
    return meta


def main_events(meta):
    """One row per tournament: its main Ultimate singles bracket, plus that
    tournament's season and region."""
    singles = meta[
        (meta["videogame_id"] == ULTIMATE_VIDEOGAME_ID)
        & (meta["team_size"] == 1)
        & (meta["is_online"] == 0)
        & meta["start_at"].notna()
        & ~meta["event_name"].str.contains(NEVER_MAIN_PATTERN)
    ].copy()
    singles["is_side"] = singles["event_name"].str.contains(SIDE_BRACKET_PATTERN)
    # Non-side events first, then biggest - the first row per tournament wins.
    singles = singles.sort_values(["is_side", "num_entrants"], ascending=[True, False])
    main = singles.drop_duplicates("tournament_id")
    excluded = main["tournament_name"].str.contains(EXCLUDED_TOURNAMENT_PATTERN) | main["event_name"].str.contains(
        EXCLUDED_TOURNAMENT_PATTERN
    )
    main = main[~excluded].copy()
    main["season"] = main["start_at"].map(season_of)
    main["region"] = [region_of(s, c) for s, c in zip(main["state"], main["county"])]
    return main


def attendance(main, raw_path=RAW_SETS_PATH):
    """(player_id, tournament) rows for every counted tournament a player
    appeared in, with the player's most recent gamertag."""
    sets = pd.read_csv(
        raw_path,
        usecols=["event_id", "completed_at", "entrant1_player_id", "entrant1_name", "entrant2_player_id", "entrant2_name"],
        dtype=str,
    )
    counted = main[main["num_entrants"] >= MIN_ENTRANTS]
    sets = sets[sets["event_id"].isin(counted["event_id"])]

    sides = [
        sets[["event_id", "completed_at", f"entrant{i}_player_id", f"entrant{i}_name"]].set_axis(
            ["event_id", "completed_at", "player_id", "name"], axis=1
        )
        for i in (1, 2)
    ]
    appearances = pd.concat(sides, ignore_index=True).dropna(subset=["player_id"])
    # Guest entries without a linked start.gg account get a namespaced
    # entrant id (see set_parsing._player_id_for) - not trackable across events.
    appearances = appearances[~appearances["player_id"].str.startswith("e")]

    latest_names = (
        appearances.assign(completed_at=appearances["completed_at"].astype(int))
        .sort_values("completed_at")
        .drop_duplicates("player_id", keep="last")
        .set_index("player_id")["name"]
    )
    attended = appearances.drop_duplicates(["player_id", "event_id"])[["player_id", "event_id"]]
    attended = attended.merge(
        counted[["event_id", "tournament_id", "tournament_name", "season", "region", "num_entrants"]], on="event_id"
    )
    attended["name"] = attended["player_id"].map(latest_names)
    return attended


def load_residences(path=RESIDENCES_PATH):
    """player_id -> residence (one of RESIDENCE_OPTIONS)."""
    if not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {row["player_id"]: row["residence"] for row in csv.DictReader(f) if row.get("residence")}


def save_residences(residences, names, path=RESIDENCES_PATH):
    """residences: player_id -> residence. names: player_id -> gamertag, kept
    in the file only so it's readable when edited by hand."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["player_id", "name", "residence"])
        writer.writeheader()
        for pid in sorted(residences, key=lambda p: names.get(p, "").lower()):
            writer.writerow({"player_id": pid, "name": names.get(pid, ""), "residence": residences[pid]})


def load_ratings(path=GLICKO_PATH):
    if not os.path.exists(path):
        return pd.DataFrame(columns=["player_id", "conservative_rating"])
    return pd.read_csv(path, dtype={"player_id": str})[["player_id", "conservative_rating"]]


def evaluate_season(attended, season, residences):
    """One row per player with 3+ Westchester tournaments in `season` (the
    only players who can possibly qualify), with their status and the
    counts behind it. Sorted eligible-first, then by conservative Glicko."""
    season_rows = attended[attended["season"] == season]
    counts = season_rows.groupby(["player_id", "region"]).size().unstack(fill_value=0)
    if HOME_REGION not in counts.columns:
        return pd.DataFrame()
    names = season_rows.drop_duplicates("player_id").set_index("player_id")["name"]

    rows = []
    for pid, by_region in counts.iterrows():
        wc = int(by_region[HOME_REGION])
        if wc < MIN_WESTCHESTER_TOURNAMENTS:
            continue
        others = by_region.drop(HOME_REGION)
        others = others[others > 0].sort_values(ascending=False)
        top_other = others.index[0] if len(others) else ""
        top_other_count = int(others.iloc[0]) if len(others) else 0
        residence = residences.get(pid, "")

        if not residence:
            status, reason = "needs residence", "Set residence to decide"
        elif residence == RESIDENCE_WESTCHESTER:
            status, reason = "eligible", "Westchester resident"
        elif residence in NEIGHBOR_RESIDENCES:
            if wc >= top_other_count:
                status, reason = "eligible", "Neighbor county, Westchester is plurality"
            else:
                status, reason = "not eligible", f"Plurality is {top_other} ({top_other_count} vs {wc})"
        else:
            status, reason = "not eligible", "Lives outside Westchester/neighbor counties"

        rows.append(
            {
                "player_id": pid,
                "name": names[pid],
                "residence": residence,
                "status": status,
                "reason": reason,
                "westchester": wc,
                "top_other_region": top_other,
                "top_other_count": top_other_count,
                "total": int(by_region.sum()),
            }
        )

    result = pd.DataFrame(rows)
    if result.empty:
        return result
    result = result.merge(load_ratings(), on="player_id", how="left")
    status_order = {"eligible": 0, "needs residence": 1, "not eligible": 2}
    result["_order"] = result["status"].map(status_order)
    result = result.sort_values(["_order", "conservative_rating"], ascending=[True, False], na_position="last")
    return result.drop(columns="_order").reset_index(drop=True)


def other_westchester_tournaments(main, season):
    """Counted Westchester tournaments in `season` that aren't from the known
    home series - one-offs worth a sanity check."""
    series = re.compile(r"encore|undiscovered|back from the ded", re.IGNORECASE)
    wc = main[(main["season"] == season) & (main["region"] == HOME_REGION) & (main["num_entrants"] >= MIN_ENTRANTS)]
    return wc[~wc["tournament_name"].str.contains(series)][["tournament_name", "event_name", "num_entrants"]]


def main_cli(season=None):
    meta = load_metadata()
    main = main_events(meta)
    attended = attendance(main)
    season = season or max(attended["season"])
    result = evaluate_season(attended, season, load_residences())

    print(f"\nPR eligibility - {season}\n")
    if result.empty:
        print(f"Nobody attended {MIN_WESTCHESTER_TOURNAMENTS}+ Westchester tournaments in {season}.")
        return
    for status in ("eligible", "needs residence", "not eligible"):
        group = result[result["status"] == status]
        if group.empty:
            continue
        print(f"{status.upper()} ({len(group)})")
        print(f"  {'Name':<24}{'Residence':<16}{'WC':>4}{'Top other':>26}{'Total':>7}{'Glicko':>8}")
        for _, r in group.iterrows():
            other = f"{r['top_other_region']} ({r['top_other_count']})" if r["top_other_region"] else "-"
            rating = "" if pd.isna(r["conservative_rating"]) else f"{r['conservative_rating']:.0f}"
            print(f"  {r['name'][:23]:<24}{r['residence'] or '?':<16}{r['westchester']:>4}{other:>26}{r['total']:>7}{rating:>8}")
        print()

    one_offs = other_westchester_tournaments(main, season)
    if not one_offs.empty:
        print(f"Non-Encore/Undiscovered Westchester tournaments counted this season ({len(one_offs)}):")
        for _, r in one_offs.iterrows():
            print(f"  {r['tournament_name']} - {r['event_name']} ({r['num_entrants']} entrants)")

    out_path = os.path.join(DATA_DIR, f"pr_eligibility_{season}.csv")
    result.to_csv(out_path, index=False)
    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--season", help="e.g. 2026Q3 (default: most recent season in the data)")
    args = parser.parse_args()
    main_cli(args.season)
