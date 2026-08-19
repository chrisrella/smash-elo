# smash-elo

Elo power rankings for a Super Smash Bros. Ultimate local scene, built from
real bracket results pulled via the [start.gg](https://start.gg) API.

## What it does today

- `src/collect.py` pulls completed sets (seeds, entrants, results, and
  character selections where the event tracked them) from one or more
  start.gg tournaments and writes them to `data/raw_sets.csv`.
- `src/elo.py` processes those sets in chronological order and computes an
  Elo rating per player, correctly tracked across events via start.gg's
  stable player ID (not the event-scoped entrant ID). Output goes to
  `data/elo_ratings.csv`.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # add your start.gg API token
```

Get a start.gg API token from your [start.gg developer settings](https://start.gg/admin/profile/developer).

## Usage

```bash
# Pull sets from a tournament (slug from the start.gg URL)
python src/collect.py tournament/<slug>

# Cap how many events to pull (useful for large recurring series)
python src/collect.py --max-events 8 tournament/<slug>

# Compute and print the Elo leaderboard
python src/elo.py
```

## Roadmap / someday

The original idea behind this project was an **upset predictor** — given a
set's seed difference, character matchup, and player history, estimate the
odds of the lower seed winning. That's still the long-term direction; the
Elo ratings here are a step toward a more principled label/feature than raw
tournament seeding. `data/character-matchup-matrix.csv` is a hand-built
matchup chart earmarked for that stage.

Known limitation: character-selection data on start.gg is inconsistently
tracked — usually present for later bracket rounds (top 8ish) and missing
for early pools/round 1.
