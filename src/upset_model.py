"""
upset_model.py
Trains and backtests a win-probability model on top of the Elo/Glicko-2
ratings this repo already computes, then compares it against two
zero-training baselines: the Elo logistic-curve formula, and an analogous
Glicko-2 formula. All three predict P(entrant1 wins) for every set in
data/raw_sets.csv; "upset probability" for a given set is just that
probability evaluated on whichever side the pre-match ratings made the
underdog.

Evaluation is a *chronological* holdout - the model trains on the earlier
X% of sets (by completed_at) and is scored on the later (1-X)%, so a
result here reflects "how well would this have predicted upcoming sets",
not just how well it fits data it already memorized.

Usage:
    python src/upset_model.py                     # train + backtest + save
    python src/upset_model.py --train-frac 0.85
"""

import argparse
import json
import os

import joblib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from sklearn.calibration import calibration_curve
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss
from sklearn.preprocessing import StandardScaler

from features import build_features
from glicko import E as glicko_E
from glicko import SCALE as GLICKO_SCALE

MODEL_DIR = os.path.join(os.path.dirname(__file__), "..", "models")
MODEL_PATH = os.path.join(MODEL_DIR, "upset_model.joblib")
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
CALIBRATION_PLOT_PATH = os.path.join(DATA_DIR, "model_calibration.png")
METRICS_PATH = os.path.join(DATA_DIR, "model_metrics.json")

FEATURE_NAMES = [
    "elo_diff",
    "glicko_diff",
    "glicko_rd_sum",
    "games_min_pre",
    "seed_diff",
    "seed_known",
    "matchup_delta",
    "matchup_known",
]


def elo_formula_proba(elo_diff):
    """The same logistic curve elo.py's expected_score() computes, applied
    directly as a win-probability baseline (no training)."""
    return 1.0 / (1.0 + 10 ** (-elo_diff / 400))


def glicko_formula_proba(glicko_diff, glicko_rd_sum):
    """Analogous zero-training baseline from the Glicko-2 formula: scale
    the rating gap and combined uncertainty onto the Glicko-2 internal
    scale, then plug into Glickman's own E() function (same g()-damped
    logistic form the update step uses)."""
    mu_diff = np.asarray(glicko_diff) / GLICKO_SCALE
    phi_combined = np.asarray(glicko_rd_sum) / GLICKO_SCALE  # conservative: sum, not RSS
    # glicko.E()/g() are plain-math scalar functions (shared with the
    # sequential Glicko-2 update loop), so vectorize rather than duplicate them.
    return np.vectorize(lambda md, pc: glicko_E(md, 0.0, pc))(mu_diff, phi_combined)


def underdog_win_proba(p_entrant1, elo_diff):
    """P(the pre-match underdog wins), i.e. "upset probability", given
    P(entrant1 wins) and which side elo_diff makes the favorite. Works on
    both plain floats and numpy arrays."""
    return np.where(np.asarray(elo_diff) < 0, p_entrant1, 1 - np.asarray(p_entrant1))


def report_metrics(name, y_true, p_pred):
    acc = accuracy_score(y_true, np.array(p_pred) > 0.5)
    ll = log_loss(y_true, p_pred, labels=[0, 1])
    brier = brier_score_loss(y_true, p_pred)
    print(f"{name:<32}accuracy={acc:.3f}  log_loss={ll:.3f}  brier={brier:.3f}")
    return {"name": name, "accuracy": acc, "log_loss": ll, "brier": brier}


def train(train_frac=0.8):
    df = build_features()
    n = len(df)
    split = int(n * train_frac)
    train_df, test_df = df.iloc[:split], df.iloc[split:]
    print(f"{n} sets total - training on {len(train_df)} (earliest), "
          f"backtesting on {len(test_df)} (most recent)\n")

    X_train = train_df[FEATURE_NAMES].to_numpy(dtype=float)
    X_test = test_df[FEATURE_NAMES].to_numpy(dtype=float)
    y_train = train_df["entrant1_wins"].to_numpy()
    y_test = test_df["entrant1_wins"].to_numpy()

    scaler = StandardScaler().fit(X_train)
    clf = LogisticRegression(max_iter=1000)
    clf.fit(scaler.transform(X_train), y_train)

    # --- Baselines, evaluated on the same holdout ---
    print("Backtest on the holdout set (higher accuracy / lower log_loss & brier is better):")
    always_favorite = (test_df["elo_diff"] > 0).astype(int).clip(0.02, 0.98)
    # "always favorite" has no real probability - score it as a near-certain
    # (not exactly 0/1, to keep log_loss finite) prediction for comparability.
    results = [report_metrics("Always-favorite (Elo sign)", y_test, always_favorite)]

    elo_p = elo_formula_proba(test_df["elo_diff"].to_numpy())
    results.append(report_metrics("Elo formula (no training)", y_test, elo_p))

    glicko_p = glicko_formula_proba(
        test_df["glicko_diff"].to_numpy(), test_df["glicko_rd_sum"].to_numpy()
    )
    results.append(report_metrics("Glicko-2 formula (no training)", y_test, glicko_p))

    model_p = clf.predict_proba(scaler.transform(X_test))[:, 1]
    results.append(report_metrics("Logistic regression (trained)", y_test, model_p))

    # --- Feature importances (standardized coefficients) ---
    print("\nTrained model coefficients (standardized - larger |coef| = more predictive):")
    for name, coef in sorted(zip(FEATURE_NAMES, clf.coef_[0]), key=lambda kv: -abs(kv[1])):
        print(f"  {name:<16}{coef:+.3f}")

    # --- Sanity check: does the model actually flag real upsets as riskier? ---
    upset_mask = (
        (test_df["elo_diff"] > 0) & (y_test == 0)
    ) | ((test_df["elo_diff"] < 0) & (y_test == 1))
    underdog_p = underdog_win_proba(model_p, test_df["elo_diff"].to_numpy())
    upset_mean = float(underdog_p[upset_mask.to_numpy()].mean())
    non_upset_mean = float(underdog_p[~upset_mask.to_numpy()].mean())
    print(f"\nMean predicted upset probability on sets that WERE upsets:     {upset_mean:.3f}")
    print(f"Mean predicted upset probability on sets that WEREN'T upsets:   {non_upset_mean:.3f}")

    _save_calibration_plot(y_test, model_p, elo_p)

    os.makedirs(MODEL_DIR, exist_ok=True)
    joblib.dump({"model": clf, "scaler": scaler, "feature_names": FEATURE_NAMES}, MODEL_PATH)

    with open(METRICS_PATH, "w") as f:
        json.dump({
            "n_sets": n,
            "n_train": len(train_df),
            "n_test": len(test_df),
            "train_frac": train_frac,
            "results": results,
            "coefficients": {name: float(coef) for name, coef in zip(FEATURE_NAMES, clf.coef_[0])},
            "mean_upset_probability_on_upsets": upset_mean,
            "mean_upset_probability_on_non_upsets": non_upset_mean,
        }, f, indent=2)

    print(f"\nModel saved to {MODEL_PATH}")
    print(f"Calibration plot saved to {CALIBRATION_PLOT_PATH}")
    print(f"Metrics saved to {METRICS_PATH}")

    return clf, scaler


def _save_calibration_plot(y_test, model_p, elo_p):
    fig, ax = plt.subplots(figsize=(5, 5))
    for label, p in [("Logistic regression", model_p), ("Elo formula", elo_p)]:
        frac_pos, mean_pred = calibration_curve(y_test, p, n_bins=10, strategy="quantile")
        ax.plot(mean_pred, frac_pos, marker="o", label=label)
    ax.plot([0, 1], [0, 1], linestyle="--", color="gray", label="Perfectly calibrated")
    ax.set_xlabel("Predicted P(entrant1 wins)")
    ax.set_ylabel("Actual win rate")
    ax.set_title("Calibration on holdout (most recent sets)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(CALIBRATION_PLOT_PATH, dpi=150)
    plt.close(fig)


def load_model(path=MODEL_PATH):
    return joblib.load(path)


def predict_proba(bundle, elo_diff, glicko_diff, glicko_rd_sum, games_min_pre,
                   seed_diff=0, seed_known=0, matchup_delta=0.0, matchup_known=0):
    """P(entrant1/player-A wins) for a hypothetical matchup, given current
    (not necessarily historical) ratings - used by the dashboard to score a
    live "what if these two played" query."""
    x = np.array([[elo_diff, glicko_diff, glicko_rd_sum, games_min_pre,
                    seed_diff, seed_known, matchup_delta, matchup_known]], dtype=float)
    x_scaled = bundle["scaler"].transform(x)
    return float(bundle["model"].predict_proba(x_scaled)[0, 1])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--train-frac", type=float, default=0.8,
                         help="fraction of sets (chronologically earliest) to train on")
    args = parser.parse_args()
    train(train_frac=args.train_frac)
