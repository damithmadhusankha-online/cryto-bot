"""Market scanner (v3): empirically ranks asset x timeframe x variant.

Each combination is validated walk-forward: the first 60% of the window is
context, and ONLY the untouched out-of-sample tail counts toward the rank.
A blended score rewards OOS expectancy, the OOS bootstrap confidence-interval
lower bound, full-window EV, and two-half consistency, so a single lucky
streak cannot drive selection.
"""
import itertools
import os

import pandas as pd

import config as cfg
from backtester import bootstrap_ci, consistency_check, run_backtest, walk_forward
from data_fetcher import fetch

_df_cache = {}


def _get_df(name, tf, window_days):
    key = (name, tf, window_days)
    if key not in _df_cache:
        _df_cache[key] = fetch(name, tf, days=window_days)
    return _df_cache[key]


def _rank_score(row):
    return (row["oos_ev"] * cfg.RANK_W_OOS
            + row["expectancy"] * cfg.RANK_W_EV
            + max(row["ci_lower"], 0.0) * cfg.RANK_W_CI
            + (cfg.RANK_W_CONSISTENT if row["consistent"] else 0.0))


def scan(assets=None, timeframes=None, variants=None, window_days=None,
         min_trades=None, min_oos_trades=None, require_consistent=None,
         require_ci_lo=None, progress=False, on_error="skip"):
    assets = assets or list(cfg.ASSETS)
    timeframes = timeframes or cfg.TIMEFRAMES
    variants = variants or cfg.VARIANTS
    window_days = window_days or cfg.SCAN_WINDOW_DAYS
    min_trades = min_trades or cfg.SCAN_MIN_TRADES
    min_oos_trades = min_oos_trades if min_oos_trades is not None else cfg.SCAN_MIN_OOS_TRADES
    require_consistent = cfg.SCAN_REQUIRE_CONSISTENT if require_consistent is None else require_consistent
    require_ci_lo = cfg.SCAN_REQUIRE_CI_LO if require_ci_lo is None else require_ci_lo
    max_winrate = getattr(cfg, "SCAN_MAX_WINRATE", 0.96)

    rows = []
    total = len(assets) * len(timeframes) * len(variants)
    k = 0
    for name, tf, variant in itertools.product(assets, timeframes, variants):
        k += 1
        try:
            df = _get_df(name, tf, window_days)
            wf = walk_forward(df, name, variant, tf, min_oos_trades=min_oos_trades)
            oos = wf["oos"]
            train = wf["train"]
            if train["n_trades"] + oos["n_trades"] < min_trades:
                continue
            if oos["n_trades"] < min_oos_trades:
                continue
            cc = consistency_check(df, name, variant, tf)
            ci_lower = wf["oos_ci"]["ev_lo"]
            # Professional filters: both window halves positive, and the OOS
            # EV lower bound above zero - a lucky streak cannot make the cut.
            if require_consistent and not cc["consistent"]:
                continue
            if ci_lower <= require_ci_lo:
                continue
            # Anti-overfit: a (near-)perfect record on a small sample is almost
            # certainly noise, not an edge - it would never survive live.
            if oos["win_rate"] > max_winrate:
                if progress:
                    print(f"  [{k}/{total}] {name} {tf} {variant}: "
                          f"win {oos['win_rate']:.0%} > cap {max_winrate:.0%} "
                          f"(overfit) - skip")
                continue
            row = {
                "asset": name,
                "category": cfg.ASSET_CATEGORY.get(name, ""),
                "timeframe": tf,
                "variant": variant,
                "n_trades": oos["n_trades"],          # out-of-sample trades
                "win_rate": round(oos["win_rate"], 4),
                "profit_factor": round(oos["profit_factor"], 2),
                "expectancy": round(oos["expectancy"], 4),
                "net_pl": round(oos["net_pl"], 2),
                "max_drawdown": round(oos["max_drawdown"], 2),
                "oos_ev": round(oos["expectancy"], 4),
                "ci_lower": round(ci_lower, 4),
                "ci_upper": round(wf["oos_ci"]["ev_hi"], 4),
                "win_lo": round(wf["oos_ci"]["win_lo"], 4),
                "win_hi": round(wf["oos_ci"]["win_hi"], 4),
                "consistent": cc["consistent"],
                "ev_1st_half": (round(cc["first"], 4) if cc["first"] is not None else None),
                "ev_2nd_half": (round(cc["second"], 4) if cc["second"] is not None else None),
                "rank_score": 0.0,
            }
            row["rank_score"] = round(_rank_score(row), 4)
            rows.append(row)
            if progress:
                print(f"  [{k}/{total}] {name} {tf} {variant}: "
                      f"{oos['n_trades']} OOS trades, win {oos['win_rate']:.0%}, "
                      f"EV {oos['expectancy']:+.3f}, CI-lo {ci_lower:+.3f}"
                      f"{'  [CONSISTENT]' if cc['consistent'] else ''}")
        except Exception as exc:
            if progress:
                print(f"  [{k}/{total}] {name} {tf} {variant}: skipped ({exc})")
            if on_error == "raise":
                raise

    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(["rank_score", "oos_ev", "win_rate"],
                              ascending=False).reset_index(drop=True)
    return out


def save_watchlist(df, path=None, top=None, per_category=None):
    """Persist a validated watchlist for loop mode.

    With `per_category=N` (default cfg.SCAN_PER_CATEGORY_TOP) the top-N rows
    are taken from EVERY market category, so the live bot broadcasts on all
    markets (forex, crypto, commodities, indices, stocks) instead of only the
    single hottest asset. Junk rows (0 trades / unvalidated / overfit) are
    dropped before saving.
    """
    path = path or cfg.WATCHLIST_FILE
    top = top or cfg.SCAN_TOP_N
    per_category = cfg.SCAN_PER_CATEGORY_TOP if per_category is None else per_category
    if df.empty:
        return []
    clean = df[df["n_trades"] >= cfg.SCAN_MIN_OOS_TRADES].copy()
    if "win_rate" in clean.columns:
        cap = getattr(cfg, "SCAN_MAX_WINRATE", 0.96)
        clean = clean[clean["win_rate"] <= cap]
    if clean.empty:
        return []

    if per_category > 0 and "category" in clean.columns:
        picked = clean.groupby("category", sort=False).head(per_category)
    else:
        picked = clean
    picked = picked.head(top) if top else picked
    picked.to_csv(path, index=False)
    return list(picked[["asset", "timeframe", "variant"]].itertuples(index=False, name=None))


def watchlist_rows():
    """Validated watchlist stats keyed by (asset, timeframe).

    Only rows that cleared the scan's out-of-sample filters count; junk rows
    (e.g. a manual pair with 0 trades) are ignored. Used by the broadcast
    profitability gate, the signal card edge line and /top.
    """
    path = cfg.WATCHLIST_FILE
    if not os.path.exists(path):
        return {}
    try:
        df = pd.read_csv(path)
    except Exception:
        return {}
    need = {"asset", "timeframe", "win_rate", "oos_ev", "n_trades"}
    if not need.issubset(df.columns):
        return {}
    out = {}
    max_winrate = getattr(cfg, "SCAN_MAX_WINRATE", 0.96)
    for r in df.itertuples(index=False):
        n = int(getattr(r, "n_trades", 0) or 0)
        if n < cfg.SCAN_MIN_OOS_TRADES:
            continue
        wr = float(getattr(r, "win_rate", 0.0) or 0.0)
        # Defense in depth: a stale watchlist row with a (near-)perfect
        # record is overfit noise, not an edge - never let it through the
        # broadcast profitability gate even if the file predates the cap.
        if wr > max_winrate:
            continue
        out[(str(r.asset), str(r.timeframe))] = {
            "win_rate": float(getattr(r, "win_rate", 0.0) or 0.0),
            "oos_ev": float(getattr(r, "oos_ev", 0.0) or 0.0),
            "n_trades": n,
            "variant": str(getattr(r, "variant", "") or ""),
            "category": str(getattr(r, "category", "") or ""),
            "rank_score": float(getattr(r, "rank_score", 0.0) or 0.0),
        }
    return out


def load_watchlist(path=None):
    path = path or cfg.WATCHLIST_FILE
    if not os.path.exists(path):
        return []
    df = pd.read_csv(path)
    cols = [c for c in ("asset", "timeframe", "variant") if c in df.columns]
    if "asset" not in cols or "timeframe" not in cols:
        return []
    rows = df[cols].fillna("momentum").itertuples(index=False, name=None)
    return [(r[0], r[1], r[2] if len(r) > 2 else "momentum") for r in rows]