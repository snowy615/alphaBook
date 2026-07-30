"""Convert synthetic crash episodes into compact JSON for the Risks game.

The upstream generator (Data.py -> Betas.py -> output.py) emits one CSV per
episode with a row per ticker per day:

    Episode, Date, Ticker, SyntheticLogReturn, SyntheticClose, Beta,
    HistoricalBeta, PanicWindow, ReboundWindow, ShockGroup

Those CSVs are large (the S&P universe is 493 tickers) and carry columns the
game must not leak while a round is live. This script picks a playable basket,
keeps only what the game needs, and writes one small JSON per episode into
app/data/risk_episodes/.

    python scripts/build_risk_episodes.py --source ~/crash_data --tickers 12

Re-run it whenever the generator produces new episodes; the game picks up any
JSON in the output directory.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "app" / "data" / "risk_episodes"

# Directory name in the generator's output -> the universe shown in game.
UNIVERSES = {
    "Synthetic_crash_DWIA": ("dow", "Dow 30"),
    "Synthetic_crash_SP500": ("sp500", "S&P 500"),
    "Synthetic_crash_SPDefence": ("defence", "Aerospace & Defence"),
}


def pick_basket(summary: pd.DataFrame, n: int) -> list[str]:
    """Choose a basket that spans both the beta range and the shock groups.

    A basket of all-high-beta names makes the round a single directional bet;
    a spread is what gives the player something to actually manage.
    """
    chosen: list[str] = []

    # One of each shock group first, so every round has a defensive name and a
    # victim in it somewhere.
    for group in ("defensive", "normal", "vulnerable", "victim"):
        rows = summary[(summary["group"] == group) & (~summary.index.isin(chosen))]
        if not rows.empty:
            chosen.append(rows.sort_values("hist_beta").index[len(rows) // 2])

    # Fill the rest by walking the beta ranking, so the basket covers low to
    # high beta rather than clustering.
    remaining = summary[~summary.index.isin(chosen)].sort_values("hist_beta")
    if len(remaining) and len(chosen) < n:
        need = n - len(chosen)
        step = max(1, len(remaining) // need)
        for i in range(0, len(remaining), step):
            if len(chosen) >= n:
                break
            chosen.append(remaining.index[i])

    return sorted(chosen[:n])


def build_episode(csv_path: Path, universe: str, universe_label: str,
                  n_tickers: int) -> dict | None:
    df = pd.read_csv(csv_path)
    needed = {"Date", "Ticker", "SyntheticClose", "Beta", "HistoricalBeta",
              "PanicWindow", "ReboundWindow", "ShockGroup"}
    missing = needed - set(df.columns)
    if missing:
        print(f"  skip {csv_path.name}: missing {sorted(missing)}")
        return None

    df = df.sort_values(["Ticker", "Date"])
    grouped = df.groupby("Ticker")
    summary = pd.DataFrame({
        "hist_beta": grouped["HistoricalBeta"].first(),
        "episode_beta": grouped["Beta"].first(),
        "group": grouped["ShockGroup"].first(),
        "first": grouped["SyntheticClose"].first(),
        "last": grouped["SyntheticClose"].last(),
    })
    # A near-zero beta means the upstream fit had too little data to work with.
    summary = summary[summary["hist_beta"].abs() > 0.01]
    if summary.empty:
        print(f"  skip {csv_path.name}: no usable betas")
        return None

    basket = pick_basket(summary, n_tickers)
    dates = sorted(df["Date"].unique())

    names = []
    for ticker in basket:
        rows = df[df["Ticker"] == ticker].set_index("Date").reindex(dates)
        closes = rows["SyntheticClose"].ffill().bfill()
        base = float(closes.iloc[0])
        if not base or base <= 0:
            continue
        names.append({
            "ticker": str(ticker),
            # Rebased to 100 so every name starts level and the player is
            # trading relative moves, not share-price accidents.
            "closes": [round(float(c) / base * 100.0, 4) for c in closes],
            "published_beta": round(float(summary.at[ticker, "hist_beta"]), 3),
            "realised_beta": round(float(summary.at[ticker, "episode_beta"]), 3),
            "shock_group": str(summary.at[ticker, "group"]),
        })

    if len(names) < 4:
        print(f"  skip {csv_path.name}: only {len(names)} usable names")
        return None

    # Equal-weighted index over the basket, so the level the player sees is
    # consistent with the names they can actually trade.
    n_days = len(dates)
    index = [
        round(sum(n["closes"][d] for n in names) / len(names), 4)
        for d in range(n_days)
    ]

    per_day = df.groupby("Date")[["PanicWindow", "ReboundWindow"]].max().reindex(dates).fillna(0)
    panic = [int(i) for i, v in enumerate(per_day["PanicWindow"]) if v > 0]
    rebound = [int(i) for i, v in enumerate(per_day["ReboundWindow"]) if v > 0]

    episode_id = f"{universe}_{str(df['Episode'].iloc[0]) if 'Episode' in df.columns else csv_path.stem}"
    episode_id = episode_id.replace(" ", "_").lower()

    trough = min(index)
    return {
        "episode_id": episode_id,
        "universe": universe,
        "universe_label": universe_label,
        "days": n_days,
        "dates": [str(d)[:10] for d in dates],
        "index": index,
        "names": names,
        "panic_days": panic,
        "rebound_days": rebound,
        # Headline stats, used to describe the round after it finishes.
        "index_drawdown_pct": round((trough / index[0] - 1) * 100, 2),
        "index_return_pct": round((index[-1] / index[0] - 1) * 100, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", required=True,
                    help="Folder holding the generator's Synthetic_crash_* directories")
    ap.add_argument("--tickers", type=int, default=12,
                    help="Names per basket (default 12)")
    ap.add_argument("--out", default=str(OUT_DIR))
    args = ap.parse_args()

    source = Path(args.source).expanduser()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    for folder, (universe, label) in UNIVERSES.items():
        out_root = source / folder / "output"
        if not out_root.is_dir():
            print(f"{folder}: no output/ directory, skipping")
            continue
        print(f"{folder} -> {universe}")
        for csv_path in sorted(out_root.glob("synthetic_*.csv")):
            episode = build_episode(csv_path, universe, label, args.tickers)
            if episode is None:
                continue
            dest = out_dir / f"{episode['episode_id']}.json"
            dest.write_text(json.dumps(episode, separators=(",", ":")))
            written += 1
            print(f"  {dest.name}: {episode['days']}d, {len(episode['names'])} names, "
                  f"index {episode['index_return_pct']:+.1f}% "
                  f"(trough {episode['index_drawdown_pct']:+.1f}%), "
                  f"{dest.stat().st_size // 1024}KB")

    print(f"\nwrote {written} episodes to {out_dir}")


if __name__ == "__main__":
    main()
