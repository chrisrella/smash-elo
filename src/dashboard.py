"""
dashboard.py
Streamlit app: browse the Elo/Glicko-2 leaderboards and get a live win
probability (and upset probability) for any hypothetical matchup, using
the model trained by upset_model.py.

Run:
    streamlit run src/dashboard.py

Needs data/elo_ratings.csv + data/glicko_ratings.csv (from elo.py/glicko.py)
and models/upset_model.joblib (from upset_model.py) to already exist.
"""

import json
import os

import pandas as pd
import streamlit as st

from features import load_matchup_matrix, matchup_delta
from upset_model import CALIBRATION_PLOT_PATH, METRICS_PATH, MODEL_PATH, load_model, predict_proba

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
ELO_PATH = os.path.join(DATA_DIR, "elo_ratings.csv")
GLICKO_PATH = os.path.join(DATA_DIR, "glicko_ratings.csv")

st.set_page_config(page_title="Smash Ult Upset Predictor", page_icon="🎮", layout="wide")


@st.cache_data
def load_leaderboard():
    elo = pd.read_csv(ELO_PATH).rename(columns={"entrant_id": "player_id", "elo": "elo_rating"})
    glicko = pd.read_csv(GLICKO_PATH)[["player_id", "conservative_rating", "rating", "rd"]]
    glicko = glicko.rename(columns={"rating": "glicko_rating", "rd": "glicko_rd"})
    merged = elo.merge(glicko, on="player_id", how="inner")
    merged["player_id"] = merged["player_id"].astype(str)
    return merged.sort_values("conservative_rating", ascending=False).reset_index(drop=True)


@st.cache_resource
def load_model_bundle():
    return load_model(MODEL_PATH)


@st.cache_data
def load_matrix():
    return load_matchup_matrix()


@st.cache_data
def load_metrics():
    if not os.path.exists(METRICS_PATH):
        return None
    with open(METRICS_PATH) as f:
        return json.load(f)


st.title("🎮 Smash Ult Power Rankings & Upset Predictor")
st.caption(
    "Elo and Glicko-2 ratings computed from real bracket results, plus a "
    "logistic regression trained to beat both formulas' own win-probability "
    "estimates on a chronological holdout."
)

if not (os.path.exists(ELO_PATH) and os.path.exists(GLICKO_PATH)):
    st.error("Leaderboards not found - run `python src/elo.py` and `python src/glicko.py` first.")
    st.stop()

leaderboard = load_leaderboard()

tab_leaderboard, tab_predict, tab_model = st.tabs(["Leaderboard", "Predict a Matchup", "About the Model"])

with tab_leaderboard:
    st.subheader("Leaderboard")
    st.caption("Ranked by conservative Glicko-2 rating (rating - 2*RD) - a high rating on a thin sample doesn't outrank a well-tested one.")
    top_n = st.slider("Show top N", 5, len(leaderboard), min(25, len(leaderboard)))
    display = leaderboard.head(top_n).copy()
    display.insert(0, "rank", range(1, len(display) + 1))
    st.dataframe(
        display[["rank", "name", "conservative_rating", "glicko_rating", "glicko_rd", "elo_rating", "sets_played"]],
        column_config={
            "conservative_rating": st.column_config.NumberColumn("Glicko (conservative)", format="%.0f"),
            "glicko_rating": st.column_config.NumberColumn("Glicko rating", format="%.0f"),
            "glicko_rd": st.column_config.NumberColumn("RD", format="%.0f"),
            "elo_rating": st.column_config.NumberColumn("Elo", format="%.0f"),
        },
        hide_index=True,
        use_container_width=True,
    )
    st.bar_chart(display.set_index("name")["conservative_rating"], horizontal=True)

with tab_predict:
    st.subheader("Predict a Matchup")
    if not os.path.exists(MODEL_PATH):
        st.error("No trained model found - run `python src/upset_model.py` first.")
        st.stop()

    bundle = load_model_bundle()
    matrix = load_matrix()
    char_options = ["(unknown / skip)"] + sorted(matrix.index.tolist())

    col_a, col_b = st.columns(2)
    with col_a:
        name_a = st.selectbox("Player A", leaderboard["name"], index=0)
        char_a = st.selectbox("Player A's character", char_options, key="char_a")
    with col_b:
        default_b = 1 if len(leaderboard) > 1 else 0
        name_b = st.selectbox("Player B", leaderboard["name"], index=default_b)
        char_b = st.selectbox("Player B's character", char_options, key="char_b")

    row_a = leaderboard[leaderboard["name"] == name_a].iloc[0]
    row_b = leaderboard[leaderboard["name"] == name_b].iloc[0]

    if name_a == name_b:
        st.warning("Pick two different players.")
    else:
        delta = None
        if char_a != "(unknown / skip)" and char_b != "(unknown / skip)":
            delta = matchup_delta(matrix, char_a, char_b)

        p_a_wins = predict_proba(
            bundle,
            elo_diff=row_a["elo_rating"] - row_b["elo_rating"],
            glicko_diff=row_a["glicko_rating"] - row_b["glicko_rating"],
            glicko_rd_sum=row_a["glicko_rd"] + row_b["glicko_rd"],
            games_min_pre=min(row_a["sets_played"], row_b["sets_played"]),
            matchup_delta=delta or 0.0,
            matchup_known=int(delta is not None),
        )

        favorite = name_a if p_a_wins >= 0.5 else name_b
        underdog = name_b if favorite == name_a else name_a
        upset_p = 1 - p_a_wins if favorite == name_a else p_a_wins

        col1, col2 = st.columns(2)
        col1.metric(f"{name_a} win probability", f"{p_a_wins:.0%}")
        col2.metric(f"{name_b} win probability", f"{1 - p_a_wins:.0%}")
        st.progress(p_a_wins, text=f"{name_a} ↔ {name_b}")

        st.info(
            f"**{favorite}** is favored. If **{underdog}** wins, that's an "
            f"upset the model currently puts at **{upset_p:.0%}**."
        )
        if delta is not None:
            st.caption(f"Character matchup ({char_a} vs {char_b}): {delta:+.1f} stocks, factored in above.")
        elif char_a != "(unknown / skip)" or char_b != "(unknown / skip)":
            st.caption("No matchup data for that character pairing - ignored.")

with tab_model:
    st.subheader("How the model was evaluated")
    metrics = load_metrics()
    if metrics is None:
        st.warning("No metrics file found - run `python src/upset_model.py` to generate one.")
    else:
        st.write(
            f"Trained on the chronologically earliest {metrics['n_train']:,} sets, "
            f"backtested on the most recent {metrics['n_test']:,} ({metrics['n_sets']:,} total)."
        )
        results_df = pd.DataFrame(metrics["results"]).set_index("name")
        results_df.columns = ["Accuracy", "Log loss", "Brier score"]
        st.dataframe(results_df.style.format("{:.3f}"), use_container_width=True)
        st.caption(
            "Lower log loss / Brier is better calibrated, not just more often right. "
            "\"Always-favorite\" has no real probability, so it's scored as a "
            "near-certain (98%) prediction for comparability."
        )

        st.write(
            f"On sets that were actual upsets, the model's mean predicted upset "
            f"probability was **{metrics['mean_upset_probability_on_upsets']:.0%}**, vs "
            f"**{metrics['mean_upset_probability_on_non_upsets']:.0%}** on sets that weren't - "
            "so it is assigning real signal to upset risk, not just baseline noise."
        )

        if os.path.exists(CALIBRATION_PLOT_PATH):
            st.image(CALIBRATION_PLOT_PATH, caption="Calibration on the holdout set")

        st.write("Standardized feature coefficients (larger |coef| = more predictive):")
        coef_df = pd.DataFrame(
            sorted(metrics["coefficients"].items(), key=lambda kv: -abs(kv[1])),
            columns=["feature", "coefficient"],
        ).set_index("feature")
        st.bar_chart(coef_df)
