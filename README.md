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
- `src/features.py` builds a leakage-free modeling dataset from
  `raw_sets.csv`: for every set, each player's Elo/Glicko-2 rating *as of
  right before that set* (not the final leaderboard - see below), seed,
  and character matchup (from `data/character-matchup-matrix.csv`, when
  both entrants' characters are known).
- `src/upset_model.py` trains a logistic regression on those features to
  predict P(win) for any matchup, and backtests it against two
  zero-training baselines - the Elo expected-score formula and an
  analogous Glicko-2 formula - on a chronological holdout (train on the
  earliest sets, test on the most recent). Saves the model to
  `models/upset_model.joblib`, a calibration plot to
  `data/model_calibration.png`, and metrics to `data/model_metrics.json`.
- `src/dashboard.py` is a Streamlit app to browse the leaderboards and get
  a live win/upset probability for any two players (optionally with
  characters), backed by the trained model.

## Upset prediction model

Elo and Glicko-2 already produce implicit win probabilities (that's what
their update formulas are built from), but "predict the winner of the next
set" was never actually validated as a real predictive task on this data -
it was just a leaderboard. `upset_model.py` turns it into one: given two
players' pre-match ratings (and optionally seed/character), predict who
wins, and evaluate that prediction the same way any ML model gets
evaluated - on data the model never trained on, in time order.

A logistic regression on `[elo_diff, glicko_diff, glicko_rd_sum,
games_min_pre, seed_diff, matchup_delta]` beat both rating systems' own
formulas on a chronological 80/20 holdout (~14.5k of the most recent sets,
unseen during training):

| Predictor                         | Accuracy | Log loss | Brier |
|------------------------------------|:--------:|:--------:|:-----:|
| Always-favorite (by Elo sign)      |  72.6%   |   1.085  | 0.263 |
| Elo formula (no training)          |  72.6%   |   0.552  | 0.186 |
| Glicko-2 formula (no training)     |  74.2%   |   0.521  | 0.173 |
| **Logistic regression (trained)**  | **76.0%**| **0.503**| **0.165** |

A few things worth calling out:

- **Log loss/Brier matter more than accuracy here.** "Always-favorite" gets
  the same accuracy as the raw Elo formula but is badly overconfident
  (it's implicitly claiming ~98%+ certainty on every set), which is why
  its log loss is nearly double everyone else's - a model that's *right
  about how sure it should be* is worth more than one that's just right.
- **Seed and Glicko rating gap turned out to be the two strongest
  predictors** (by standardized coefficient), ahead of raw Elo -
  consistent with Glicko-2's whole premise: knowing how *uncertain* a
  rating is adds real signal beyond the rating alone.
- **The model is sanity-checked against actual upsets, not just
  aggregate metrics**: on sets that were real upsets, its mean predicted
  upset probability was meaningfully higher than on sets that weren't -
  i.e. it's assigning real risk to the matchups that actually flipped, not
  just improving the topline numbers some other way.
- **Character matchup data only covers ~26% of sets** (start.gg character
  selection isn't always reported) and turned out to add little on top of
  the rating features - unsurprising, since a strong rating gap already
  implies *something* about how the matchup played out.

This also finally gives a rough, principled answer to the README's oldest
open question about attendance bias (point 4 below): the model's own
`games_min_pre` feature - how many sets the *less experienced* of the two
players had played coming in - carries almost no weight once Glicko's RD
is already in the feature set. Uncertainty (RD) turned out to be the
better-calibrated way to represent "this player hasn't been tested much"
than raw game count.

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

# Build the modeling dataset (data/model_features.csv)
python src/features.py

# Train + backtest the upset/win-probability model
python src/upset_model.py
python src/upset_model.py --train-frac 0.85   # larger train split

# Launch the leaderboard + matchup-predictor dashboard
streamlit run src/dashboard.py
```
