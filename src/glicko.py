"""
glicko.py
Computes Glicko-2 ratings (Glickman, http://www.glicko.net/glicko/glicko2.pdf)
for players from data/raw_sets.csv.

Why this instead of plain Elo (see src/elo.py): Elo has no notion of how
*certain* a rating is. A player who mostly beats a small, weakly-connected
pool of opponents can climb just as fast as one who's been tested against
the full population - Elo can't tell the difference. Glicko-2 tracks a
rating deviation (RD) per player: sparse/isolated results keep RD high
(uncertain), and the leaderboard here ranks by a conservative estimate
(rating - 2*RD) rather than the raw rating, so an inflated point estimate
built on thin competition doesn't outrank a well-tested one.

Games are grouped into weekly rating periods and applied as a single batch
update per player per period (per spec - this is not the sequential
game-by-game update Elo uses).

Ratings are computed from every set in raw_sets.csv, but the leaderboard
only ranks players with at least one home-series (Encore/Undiscovered/
BFTD) appearance - otherwise a strong outsider who drove in once for a
single regional invitational can top the board off a handful of sets
without ever being part of the scene (their results still count toward
calibrating the locals who actually played them).

Usage:
    python src/glicko.py
    python src/glicko.py --label <tag>
"""

import csv
import datetime
import math
import os
import sys

from set_parsing import home_series_player_ids

IN_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "raw_sets.csv")
OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "glicko_ratings.csv")
HISTORY_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "glicko_history")

DEFAULT_RATING = 1500.0
DEFAULT_RD = 350.0
DEFAULT_VOLATILITY = 0.06
TAU = 0.5  # system constant - constrains how fast volatility can change
SCALE = 173.7178
EPSILON = 0.000001


def to_glicko2_scale(rating, rd):
    return (rating - DEFAULT_RATING) / SCALE, rd / SCALE


def from_glicko2_scale(mu, phi):
    return SCALE * mu + DEFAULT_RATING, SCALE * phi


def g(phi):
    return 1 / math.sqrt(1 + 3 * phi ** 2 / math.pi ** 2)


def E(mu, mu_j, phi_j):
    return 1 / (1 + math.exp(-g(phi_j) * (mu - mu_j)))


def _new_volatility(phi, v, delta, sigma):
    """Step 5 of the Glicko-2 spec: iteratively solve for sigma' via the
    Illinois algorithm (a regula-falsi variant)."""
    a = math.log(sigma ** 2)

    def f(x):
        ex = math.exp(x)
        num = ex * (delta ** 2 - phi ** 2 - v - ex)
        den = 2 * (phi ** 2 + v + ex) ** 2
        return num / den - (x - a) / TAU ** 2

    A = a
    if delta ** 2 > phi ** 2 + v:
        B = math.log(delta ** 2 - phi ** 2 - v)
    else:
        k = 1
        while f(a - k * TAU) < 0:
            k += 1
        B = a - k * TAU

    fA, fB = f(A), f(B)
    while abs(B - A) > EPSILON:
        C = A + (A - B) * fA / (fB - fA)
        fC = f(C)
        if fC * fB < 0:
            A, fA = B, fB
        else:
            fA = fA / 2
        B, fB = C, fC

    return math.exp(A / 2)


def update_player(mu, phi, sigma, opponents):
    """opponents: list of (mu_j, phi_j, score) - every game this player
    played in one rating period, all applied as a single batch update.
    Returns (new_mu, new_phi, new_sigma)."""
    if not opponents:
        # Step 6: no games this period - RD still widens (less certain
        # over time), rating and volatility are otherwise unchanged.
        return mu, math.sqrt(phi ** 2 + sigma ** 2), sigma

    v_inv = 0.0
    delta_sum = 0.0
    for mu_j, phi_j, s_j in opponents:
        gj = g(phi_j)
        Ej = E(mu, mu_j, phi_j)
        v_inv += gj ** 2 * Ej * (1 - Ej)
        delta_sum += gj * (s_j - Ej)
    v = 1 / v_inv
    delta = v * delta_sum

    sigma_new = _new_volatility(phi, v, delta, sigma)
    phi_star = math.sqrt(phi ** 2 + sigma_new ** 2)
    phi_new = 1 / math.sqrt(1 / phi_star ** 2 + 1 / v)
    mu_new = mu + phi_new ** 2 * delta_sum

    return mu_new, phi_new, sigma_new


def _self_test():
    """Validates the implementation against Glickman's own worked example
    from the Glicko-2 paper (section on example calculation): a player
    rated 1500/RD200/sigma0.06 plays 3 games in one period against
    opponents (1400,30,win) (1550,100,loss) (1700,300,loss), and should
    land at approximately rating=1464.06, RD=151.52, sigma=0.05999."""
    mu, phi = to_glicko2_scale(1500, 200)
    sigma = 0.06
    opponents = []
    for opp_rating, opp_rd, score in [(1400, 30, 1), (1550, 100, 0), (1700, 300, 0)]:
        mu_j, phi_j = to_glicko2_scale(opp_rating, opp_rd)
        opponents.append((mu_j, phi_j, score))

    mu2, phi2, sigma2 = update_player(mu, phi, sigma, opponents)
    rating2, rd2 = from_glicko2_scale(mu2, phi2)

    assert abs(rating2 - 1464.06) < 0.01, f"rating off: {rating2}"
    assert abs(rd2 - 151.52) < 0.01, f"RD off: {rd2}"
    assert abs(sigma2 - 0.05999) < 0.0001, f"sigma off: {sigma2}"


_self_test()  # run on import - fail loudly if the math is wrong, not silently


def period_key(completed_at):
    dt = datetime.datetime.fromtimestamp(int(completed_at))
    iso = dt.isocalendar()
    return (iso[0], iso[1])  # (ISO year, ISO week) - groups Mon-Sun


def load_sets(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("completed_at")]
    rows.sort(key=lambda r: int(r["completed_at"]))
    return rows


def compute_glicko(rows, record_history=False):
    """record_history=True additionally returns pre_match: dict of
    set_id -> (rating1_pre, rd1_pre, rating2_pre, rd2_pre, games1_pre,
    games2_pre) - each player's rating/RD/experience as of the *start of
    that set's rating period*, i.e. what was actually knowable before the
    set was played. See compute_elo's docstring for why this matters."""
    # Per-player state, keyed by player_id
    mu = {}
    phi = {}
    sigma = {}
    names = {}
    games_played = {}
    last_period_index = {}  # last period a player had state updated through
    pre_match = {} if record_history else None

    def ensure(pid, name, period_idx):
        if pid not in mu:
            mu[pid], phi[pid] = to_glicko2_scale(DEFAULT_RATING, DEFAULT_RD)
            sigma[pid] = DEFAULT_VOLATILITY
            games_played[pid] = 0
            last_period_index[pid] = period_idx
        names[pid] = name

    def catch_up(pid, period_idx):
        """Apply pure-RD-growth for any fully-inactive periods between a
        player's last update and now, per the Glicko-2 spec (step 6)."""
        gap = period_idx - last_period_index[pid]
        if gap > 0:
            phi[pid] = math.sqrt(phi[pid] ** 2 + gap * sigma[pid] ** 2)
            last_period_index[pid] = period_idx

    # Group rows by period, in chronological order
    periods = {}
    for row in rows:
        key = period_key(row["completed_at"])
        periods.setdefault(key, []).append(row)
    ordered_keys = sorted(periods.keys())
    period_index = {key: i for i, key in enumerate(ordered_keys)}

    for key in ordered_keys:
        idx = period_index[key]
        period_rows = periods[key]

        # Snapshot pre-period ratings for every player active this period,
        # since Glicko-2 batches all of a period's games against the
        # *start-of-period* rating, not updated mid-period like Elo.
        active_players = set()
        for row in period_rows:
            p1, p2 = row["entrant1_player_id"], row["entrant2_player_id"]
            ensure(p1, row["entrant1_name"], idx)
            ensure(p2, row["entrant2_name"], idx)
            catch_up(p1, idx)
            catch_up(p2, idx)
            active_players.add(p1)
            active_players.add(p2)

        pre_mu = {pid: mu[pid] for pid in active_players}
        pre_phi = {pid: phi[pid] for pid in active_players}
        pre_games = {pid: games_played.get(pid, 0) for pid in active_players}

        opponents_this_period = {pid: [] for pid in active_players}
        for row in period_rows:
            p1, p2 = row["entrant1_player_id"], row["entrant2_player_id"]
            winner = row["winner_player_id"]
            s1 = 1.0 if winner == p1 else 0.0
            s2 = 1.0 - s1
            opponents_this_period[p1].append((pre_mu[p2], pre_phi[p2], s1))
            opponents_this_period[p2].append((pre_mu[p1], pre_phi[p1], s2))
            games_played[p1] = games_played.get(p1, 0) + 1
            games_played[p2] = games_played.get(p2, 0) + 1

            if record_history:
                # All sets in a rating period share the same pre-period
                # snapshot (Glicko-2 batches within a period), so games
                # played within-period aren't reflected here - only games
                # from prior periods are "pre-match knowable".
                r1_pre, rd1_pre = from_glicko2_scale(pre_mu[p1], pre_phi[p1])
                r2_pre, rd2_pre = from_glicko2_scale(pre_mu[p2], pre_phi[p2])
                pre_match[row["set_id"]] = (
                    r1_pre, rd1_pre, r2_pre, rd2_pre, pre_games[p1], pre_games[p2]
                )

        for pid in active_players:
            new_mu, new_phi, new_sigma = update_player(
                pre_mu[pid], pre_phi[pid], sigma[pid], opponents_this_period[pid]
            )
            mu[pid], phi[pid], sigma[pid] = new_mu, new_phi, new_sigma
            last_period_index[pid] = idx

    # Final catch-up to "now" so a long-inactive player's uncertainty
    # reflects that inactivity in the reported leaderboard.
    if ordered_keys:
        final_idx = len(ordered_keys) - 1
        for pid in mu:
            catch_up(pid, final_idx)

    ratings = {}
    rds = {}
    for pid in mu:
        r, rd = from_glicko2_scale(mu[pid], phi[pid])
        ratings[pid] = r
        rds[pid] = rd

    if record_history:
        return ratings, rds, names, games_played, pre_match
    return ratings, rds, names, games_played


def write_leaderboard(ratings, rds, names, games_played, path, eligible_ids=None):
    # Rank by conservative rating (rating - 2*RD), not raw rating - this is
    # the whole point: a high point-estimate propped up by a small,
    # uncertain sample shouldn't outrank a well-tested, well-calibrated one.
    conservative = {pid: ratings[pid] - 2 * rds[pid] for pid in ratings}
    pool = conservative.items() if eligible_ids is None else (
        (pid, c) for pid, c in conservative.items() if pid in eligible_ids
    )
    leaderboard = sorted(pool, key=lambda kv: kv[1], reverse=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["rank", "player_id", "name", "conservative_rating", "rating", "rd", "sets_played"])
        for rank, (pid, cons) in enumerate(leaderboard, start=1):
            writer.writerow(
                [rank, pid, names[pid], round(cons, 1), round(ratings[pid], 1), round(rds[pid], 1), games_played[pid]]
            )
    return leaderboard


def snapshot_path(label=None):
    os.makedirs(HISTORY_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"glicko_ratings_{stamp}" + (f"_{label}" if label else "") + ".csv"
    return os.path.join(HISTORY_DIR, name)


def main(label=None):
    rows = load_sets(IN_PATH)
    print(f"Processing {len(rows)} sets across weekly rating periods...")
    ratings, rds, names, games_played = compute_glicko(rows)
    eligible = home_series_player_ids(rows)
    print(f"{len(eligible)}/{len(ratings)} players have a home-series appearance (leaderboard-eligible)")
    leaderboard = write_leaderboard(ratings, rds, names, games_played, OUT_PATH, eligible_ids=eligible)

    print(f"\n{'Rank':<5}{'Name':<20}{'Cons.':<8}{'Rating':<8}{'RD':<7}{'Sets':<6}")
    for rank, (pid, cons) in enumerate(leaderboard[:25], start=1):
        print(f"{rank:<5}{names[pid]:<20}{round(cons):<8}{round(ratings[pid]):<8}{round(rds[pid]):<7}{games_played[pid]:<6}")
    print(f"\nFull leaderboard written to {OUT_PATH}")

    snap_path = snapshot_path(label)
    write_leaderboard(ratings, rds, names, games_played, snap_path, eligible_ids=eligible)
    print(f"Snapshot saved to {snap_path}")


if __name__ == "__main__":
    args = sys.argv[1:]
    label = None
    if "--label" in args:
        i = args.index("--label")
        label = args[i + 1]
    main(label=label)
