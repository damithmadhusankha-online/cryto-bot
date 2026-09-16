"""Binary-options style backtester (v3) with honest validation.

A signal taken at the close of bar t wins if price closes in the chosen
direction `expiry` candles later (mirrors a fixed-expiry binary bet).
Results are in units of one stake; a win pays PAYOUT, a loss costs the stake.

Validation tooling:
  bootstrap_ci  - resample the realised trade outcomes to bound the win rate
                  and expectancy (5th/95th percentile). Guards the scanner
                  against ranking pure noise.
  walk_forward  - split the window; stats on the untouched out-of-sample tail
                  are the numbers that matter, not the recent-window fit.
"""
import numpy as np
import pandas as pd

import config as cfg
from strategy import compute_signals


def run_backtest(df, asset_name, variant="momentum", tf=None,
                 payout=None, expiry=None, threshold=None):
    """Backtest `variant` on OHLCV `df` for `asset_name` on timeframe `tf`.

    tf defaults to a heuristic only when omitted (scanner always passes it);
    expiry = config expiry in candles of tf. A win must clear the entry by
    the category spread buffer (same rule as the live track record).
    """
    tf = tf or "M5"
    payout = payout if payout is not None else cfg.PAYOUT
    expiry = expiry if expiry is not None else cfg.expiry_candles(tf)
    threshold = threshold if threshold is not None else cfg.VAR_THRESHOLDS.get(variant, 4.0)
    cat = cfg.ASSET_CATEGORY.get(asset_name, "Forex")
    spread = cfg.SPREAD_BPS.get(cat, 0.0) / 10000.0

    df = compute_signals(df, asset_name=asset_name, variant=variant, threshold=threshold)
    sig = df["signal"].to_numpy(int)
    close = df["Close"].to_numpy(float)
    score = df["score"].to_numpy(float)
    idx = df.index

    n = len(df)
    rows = []
    for i in range(n - expiry):
        s = sig[i]
        if s == 0:
            continue
        entry = close[i]
        exit_ = close[i + expiry]
        win = (s > 0 and exit_ > entry * (1 + spread)) or \
              (s < 0 and exit_ < entry * (1 - spread))
        rows.append({
            "t": idx[i], "signal": s, "score": score[i],
            "entry": entry, "exit": exit_, "win": bool(win),
        })

    if not rows:
        return {
            "n_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "profit_factor": 0.0, "expectancy": 0.0, "net_pl": 0.0,
            "max_drawdown": 0.0, "avg_score": 0.0, "signals": pd.DataFrame(),
        }

    r = pd.DataFrame(rows)
    n_trades = len(r)
    wins = int(r["win"].sum())
    losses = n_trades - wins
    win_rate = wins / n_trades

    pl = np.where(r["win"].to_numpy(bool), payout, -1.0)
    equity = np.cumsum(pl)
    peak = np.maximum.accumulate(equity)
    max_dd = float(np.min(equity - peak))  # <= 0

    gross_win = float(pl[pl > 0].sum())
    gross_loss = float(-pl[pl < 0].sum())
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")

    return {
        "n_trades": n_trades,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "profit_factor": pf,
        "expectancy": win_rate * payout - (1.0 - win_rate),  # per trade, in stakes
        "net_pl": float(equity[-1]),
        "max_drawdown": float(-max_dd),
        "avg_score": float(r["score"].mean()),
        "signals": r,
    }


def bootstrap_ci(signals_df, payout=None, iters=None, seed=7):
    """Bootstrap 5th/95th percentiles of win rate & EV from realised trades."""
    if signals_df is None or signals_df.empty:
        return {"n": 0, "win_lo": 0.0, "win_hi": 0.0, "ev_lo": 0.0, "ev_hi": 0.0}
    payout = payout if payout is not None else cfg.PAYOUT
    iters = iters or cfg.MC_ITERATIONS
    wins = signals_df["win"].to_numpy(bool)
    n = len(wins)
    rng = np.random.default_rng(seed)
    wr = np.empty(iters)
    ev = np.empty(iters)
    for k in range(iters):
        w = wins[rng.integers(0, n, size=n)].mean()
        wr[k] = w
        ev[k] = w * payout - (1.0 - w)
    return {
        "n": n,
        "win_lo": float(np.percentile(wr, 5)),
        "win_hi": float(np.percentile(wr, 95)),
        "ev_lo": float(np.percentile(ev, 5)),
        "ev_hi": float(np.percentile(ev, 95)),
    }


def walk_forward(df, asset_name, variant, tf, train_frac=None,
                 min_oos_trades=6, payout=None):
    """Split window into train (context) + out-of-sample tail.

    Returns full backtest stats for both parts plus the OOS bootstrap CI.
    OOS numbers are what the scanner ranks on.
    """
    train_frac = train_frac if train_frac is not None else cfg.WF_TRAIN_FRAC
    split = max(40, int(len(df) * train_frac))
    train = run_backtest(df.iloc[:split], asset_name, variant, tf, payout=payout)
    oos = run_backtest(df.iloc[split:], asset_name, variant, tf, payout=payout)
    oos_ci = bootstrap_ci(oos["signals"], payout=payout)
    return {
        "train": train,
        "oos": oos,
        "oos_ci": oos_ci,
        "split_idx": split,
        "oos_ok": oos["n_trades"] >= min_oos_trades,
    }


def consistency_check(df, asset_name, variant, tf, min_trades_each=6,
                      payout=None):
    """Split the window in half; a pair is 'consistent' only if BOTH halves
    show positive expectancy. Guards against one lucky streak driving the
    ranking."""
    payout = payout if payout is not None else cfg.PAYOUT
    half = len(df) // 2
    if half < 40:
        return {"first": None, "second": None, "consistent": False}
    s1 = run_backtest(df.iloc[:half], asset_name, variant, tf, payout=payout)
    s2 = run_backtest(df.iloc[half:], asset_name, variant, tf, payout=payout)
    ok1 = s1["n_trades"] >= min_trades_each and s1["expectancy"] > 0
    ok2 = s2["n_trades"] >= min_trades_each and s2["expectancy"] > 0
    return {
        "first": s1["expectancy"], "second": s2["expectancy"],
        "consistent": bool(ok1 and ok2),
    }


def summarize(stats, name="", tf="", variant="", ci=None):
    """Human-readable one-liner for a backtest result (optionally with CI)."""
    if stats["n_trades"] == 0:
        return f"{name} {tf} [{variant}]: no signals in window"
    s = (f"{name} {tf} [{variant}]: {stats['n_trades']} trades | "
         f"win {stats['win_rate']:.0%} | PF {stats['profit_factor']:.2f} | "
         f"EV {stats['expectancy']:+.3f} stake/trade | "
         f"DD {stats['max_drawdown']:.1f} stakes")
    if ci:
        s += (f" | win 5-95% [{ci['win_lo']:.0%}, {ci['win_hi']:.0%}] "
              f"EV [{ci['ev_lo']:+.3f}, {ci['ev_hi']:+.3f}]")
    return s