"""Convert synthetic crash episodes into compact JSON for the Risks game.

The upstream generator emits one CSV per episode with a row per ticker per day:

    Episode, Date, Ticker, SyntheticLogReturn, SyntheticClose, Beta,
    HistoricalBeta, PanicWindow, ReboundWindow, ShockGroup[, Sector]

The v7 generator (crash_recovery_simulator_realistic_v7.py) adds three things
this script picks up when they are present alongside the stock CSV:

    market_messages_<suffix>.csv   a commentary line and a confidence per day
    run_metadata_<suffix>.json     crash / stabilisation / recovery phases,
                                   which historical crashes were blended, and
                                   the dated macro events
    Sector column                  which sector each name belongs to

Those CSVs are large (the S&P universe is 493 tickers over 100 days) and carry
columns the game must not leak while a round is live. This script picks a
playable basket, keeps only what the game needs, and writes one small JSON per
episode into app/data/risk_episodes/.

    python scripts/build_risk_episodes.py --source ~/crash_data --tickers 12

Re-run it whenever the generator produces new episodes; the game picks up any
JSON in the output directory. Older episodes without the v7 extras keep
working — every added field is optional.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "app" / "data" / "risk_episodes"

# Directory-name prefix in the generator's output -> the universe shown in
# game. Matched as a prefix because v7 writes to "Synthetic_crash_SP500_".
UNIVERSES = {
    "Synthetic_crash_DWIA": ("dow", "Dow 30"),
    "Synthetic_crash_SPDefence": ("defence", "Aerospace & Defence"),
    "Synthetic_crash_SP500": ("sp500", "S&P 500"),
}


def universe_for(folder_name: str) -> tuple[str, str] | None:
    """Longest matching prefix wins, so SPDefence is not read as SP500."""
    for prefix in sorted(UNIVERSES, key=len, reverse=True):
        if folder_name.startswith(prefix):
            return UNIVERSES[prefix]
    return None


def sidecar(csv_path: Path, kind: str, suffix: str) -> Path | None:
    """Find the v7 file that belongs to this stock CSV.

    v7 names its outputs with a shared seed/length suffix, e.g.
    synthetic_stock_output_seed_329..._100d.csv and
    market_messages_seed_329..._100d.csv.
    """
    for tail in (f"{kind}_{suffix}", kind):
        for candidate in sorted(csv_path.parent.glob(f"{tail}*")):
            return candidate
    return None


def load_extras(csv_path: Path) -> dict:
    """Daily commentary, phases, blend and events, when the generator wrote them."""
    stem = csv_path.stem
    suffix = ""
    if "_seed_" in stem:
        suffix = "seed_" + stem.split("_seed_", 1)[1]

    extras: dict = {}

    msg_path = sidecar(csv_path, "market_messages", suffix)
    if msg_path and msg_path.exists():
        msgs = pd.read_csv(msg_path)
        if {"MarketMessage", "MarketConfidence"} <= set(msgs.columns):
            extras["messages"] = [
                {"text": str(r.MarketMessage), "confidence": str(r.MarketConfidence)}
                for r in msgs.itertuples()
            ]

    meta_path = sidecar(csv_path, "run_metadata", suffix)
    if meta_path and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            meta = {}
        ep = meta.get("episode", {}) or {}
        phases = {}
        for name, (a, b) in {
            "crash": ("crash_start", "crash_end"),
            "stabilisation": ("stabilization_start", "stabilization_end"),
            "recovery": ("recovery_start", "recovery_end"),
        }.items():
            if ep.get(a) is not None and ep.get(b) is not None:
                phases[name] = [int(ep[a]), int(ep[b])]
        if phases:
            extras["phases"] = phases
        if ep.get("blend_weights"):
            extras["blend"] = {str(k): round(float(v), 4)
                               for k, v in ep["blend_weights"].items()}
        if ep.get("severity") is not None:
            extras["severity"] = round(float(ep["severity"]), 3)

        events = meta.get("events") or ep.get("events") or []
        if events:
            extras["events"] = [{
                "day": int(e.get("day", 0)),
                "kind": str(e.get("event_type", "")),
                "label": str(e.get("signal_label", "") or e.get("message_key", "")),
                "value": e.get("signal_value"),
                "unit": str(e.get("signal_unit", "")),
                "hits": [str(x) for x in (e.get("affected_roles") or [])],
            } for e in events]

    return extras


def pick_basket(summary: pd.DataFrame, n: int) -> list[str]:
    """Choose a basket that spans sectors, the beta range and the shock groups.

    A basket of all-high-beta names makes the round a single directional bet.
    Spreading it is what gives the player something to actually manage, and
    spreading across sectors matters most: names in a sector move together, so
    a single-sector basket collapses to one position however you slice it.
    """
    chosen: list[str] = []

    def take(rows: pd.DataFrame) -> None:
        rows = rows[~rows.index.isin(chosen)]
        if not rows.empty:
            chosen.append(rows.sort_values("hist_beta").index[len(rows) // 2])

    # One from each sector first, when the generator tagged them.
    if "sector" in summary.columns:
        for sector in sorted(x for x in summary["sector"].dropna().unique() if x):
            if len(chosen) >= n:
                break
            take(summary[summary["sector"] == sector])

    # Then make sure every shock group is represented somewhere.
    for group in ("defensive", "normal", "vulnerable", "victim"):
        if len(chosen) >= n:
            break
        take(summary[summary["group"] == group])

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
    if "Sector" in df.columns:
        summary["sector"] = grouped["Sector"].first()
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
            "sector": (str(summary.at[ticker, "sector"])
                       if "sector" in summary.columns else ""),
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
    extras = load_extras(csv_path)
    # One commentary line per day; trim or pad so the game can index by day.
    if extras.get("messages"):
        msgs = extras["messages"][:n_days]
        while len(msgs) < n_days:
            msgs.append(msgs[-1] if msgs else {"text": "", "confidence": "Low"})
        extras["messages"] = msgs

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
        **extras,
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
    folders = sorted(p for p in source.iterdir() if p.is_dir() and universe_for(p.name))
    if not folders:
        print(f"No Synthetic_crash_* folders under {source}")
    for folder_path in folders:
        universe, label = universe_for(folder_path.name)
        out_root = folder_path / "output"
        if not out_root.is_dir():
            print(f"{folder_path.name}: no output/ directory, skipping")
            continue
        print(f"{folder_path.name} -> {universe}")
        for csv_path in sorted(out_root.glob("synthetic_*.csv")):
            episode = build_episode(csv_path, universe, label, args.tickers)
            if episode is None:
                continue
            dest = out_dir / f"{episode['episode_id']}.json"
            dest.write_text(json.dumps(episode, separators=(",", ":")))
            written += 1
            extra_bits = []
            if episode.get("messages"):
                extra_bits.append("wire")
            if episode.get("phases"):
                extra_bits.append("phases")
            if episode.get("blend"):
                extra_bits.append("blend")
            if any(n.get("sector") for n in episode["names"]):
                extra_bits.append("sectors")
            print(f"  {dest.name}: {episode['days']}d, {len(episode['names'])} names, "
                  f"index {episode['index_return_pct']:+.1f}% "
                  f"(trough {episode['index_drawdown_pct']:+.1f}%), "
                  f"{dest.stat().st_size // 1024}KB"
                  + (f"  [{', '.join(extra_bits)}]" if extra_bits else ""))

    print(f"\nwrote {written} episodes to {out_dir}")


if __name__ == "__main__":
    main()
