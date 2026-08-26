# smash-elo

Elo power rankings for a Super Smash Bros. Ultimate local scene, built from
real bracket results pulled via the [start.gg](https://start.gg) API.

## What it does today

Two-tier collection, one shared data model:

- `src/startgg_client.py` - shared GraphQL transport (auth, retry/backoff).
- `src/set_parsing.py` - turns a raw start.gg `Set` node into a CSV row, plus
  the shared schema/read/write helpers. Both collectors below produce rows
  in this same shape.
- `src/collect.py` **(Tier 1 - home series)** pulls every set from known
  recurring tournaments (your local weeklies/monthlies) into
  `data/raw_sets.csv`. Can auto-discover every edition of a series by
  organizer id (`--owner-id`) instead of hand-collecting tournament slugs -
  full-text search on tournament names isn't reliable via the API, but
  every tournament has a stable owner.
- `src/regional_backfill.py` **(Tier 2 - regional travel)** takes the
  roster of players already seen in `data/raw_sets.csv` and pulls each of
  their full set histories from start.gg directly. This is how out-of-area
  events (CT/NJ/Long Island/NYC, majors, etc.) get captured without having
  to pre-guess which regional tournaments matter - we follow the players,
  not the tournaments.
- `src/elo.py` processes `data/raw_sets.csv` in chronological order and
  computes an Elo rating per player, correctly tracked across events via
  start.gg's stable player id (not the event-scoped entrant id). Output
  goes to `data/elo_ratings.csv`, and every run also drops a timestamped
  snapshot in `data/elo_history/` (optionally tagged with `--label`) so
  successive runs can be compared against each other.
- `src/glicko.py` computes Glicko-2 ratings instead - unlike Elo, it
  tracks a rating deviation (RD, confidence) per player and ranks by a
  conservative estimate (`rating - 2*RD`), so a high rating built on a
  small or weakly-connected sample doesn't outrank a well-tested one.
  Games are batched into weekly rating periods per the Glicko-2 spec.
  Self-validates against Glickman's published reference values on import.
  Output/history work the same way as `elo.py` (`data/glicko_ratings.csv`,
  `data/glicko_history/`).
- `config/organizers.json` - known/candidate organizer ids for each home
  series, kept separate from code so it's easy to edit by hand.

# cr- Things to consider going forward
1. How we used to use Ryan/Merro's elo system to help decide who gets PR tracked- gives us a solid 15-20 players to include in the paneled discussion. Ensures we're not missing anyone
2. What weekly/monthly brackets to include in the automated collection script. Will likely focus on tristate at first so consider the same tournaments we always slug (NY, NJ, CT locals and monthlies)
3. Write an add-on for when players enter a foreign bracket like a major or out-of-region event
4. how to treat over-attendance vs. under-attendance when it comes to ELO. Huge problem we always face in our paneled discussions. I tend to prefer under-attendance with high consistency, but it depends on context

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # add your start.gg API token
```

Get a start.gg API token from your [start.gg developer settings](https://start.gg/admin/profile/developer).

## Usage

```bash
# Tier 1: pull sets from a specific tournament (slug from the start.gg URL)
python src/collect.py tournament/<slug>

# Tier 1: auto-discover + pull every edition an organizer has run
python src/collect.py --owner-id <id>

# Cap how many events to pull in one run (useful for large recurring series)
python src/collect.py --max-events 8 --owner-id <id>

# Tier 2: backfill regional/out-of-area sets for everyone already in raw_sets.csv
python src/regional_backfill.py

# Compute and print the Elo leaderboard (also saves a snapshot to data/elo_history/)
python src/elo.py
python src/elo.py --label post-region-game-filter

# Or the Glicko-2 leaderboard (saves to data/glicko_history/)
python src/glicko.py --label first-run
```
