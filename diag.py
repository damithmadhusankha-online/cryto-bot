"""Diagnostic: why does the strategy fire so often and lose?"""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, ".")
import config as cfg
from data_fetcher import fetch
from strategy import compute_signals


def main():
    name, tf = sys.argv[1] if len(sys.argv) > 1 else "EURUSD", sys.argv[2] if len(sys.argv) > 2 else "M5"
    df = fetch(name, tf)
    df = compute_signals(df)
    close = df["Close"].to_numpy(float)
    expiry = cfg.EXPIRY_CANDLES

    up = close[expiry:] > close[:-expiry]
    print(f"baseline: P(price up over next {expiry} bars) = {up.mean():.3f}")

    sig = df["signal"].to_numpy(int)
    score = df["score"].to_numpy(float)
    valid = np.arange(len(df) - expiry)
    s = sig[valid]
    c = close[valid]
    e = close[valid + expiry]

    calls = s > 0
    puts = s < 0
    print(f"bars={len(df)} signals={calls.sum()+puts.sum()} "
          f"({(calls.sum()+puts.sum())/len(df):.0%} fire rate) "
          f"calls={calls.sum()} puts={puts.sum()}")

    for label, mask in (("CALL", calls), ("PUT", puts)):
        n = mask.sum()
        if n == 0:
            print(f"{label}: none"); continue
        wr = ((e > c)[mask]).mean()
        print(f"{label}: n={n} win={wr:.3f}")

    # win rate by score magnitude
    print("\nwin rate by |score| bucket:")
    mag = np.abs(score[valid])
    for lo, hi in [(4, 4.5), (4.5, 5.5), (5.5, 7), (7, 99)]:
        m = (mag >= lo) & (mag < hi) & (s != 0)
        n = m.sum()
        if n:
            wr = ((e > c)[m] == (s[m] > 0)).mean()
            print(f"  {lo}-{hi}: n={n} win={wr:.3f}")

    # per-component correlation with outcome
    print("\nper-component directional accuracy (component sign vs up-move):")
    comps = {
        "trend": np.where(df["ema_fast"] > df["ema_slow"], 1, -1)[valid],
        "rsi": np.where(df["rsi"] > 55, 1, -1)[valid],
        "macd_hist": np.where(df["macd_hist"] > 0, 1, -1)[valid],
        "bb": np.where(df["Close"] > df["bb_up"], 1, np.where(df["Close"] < df["bb_low"], -1, 0))[valid],
    }
    for comp, v in comps.items():
        m = v != 0
        acc = ((v[m] > 0) == (e[m] > c[m])).mean() if m.any() else float("nan")
        print(f"  {comp}: accuracy={acc:.3f} n={m.sum()}")

    # drift by hour
    if hasattr(df.index, "tz"):
        idx = df.index[valid].tz_localize(None) if df.index.tz else df.index[valid]
    else:
        idx = df.index[valid]
    hours = idx.hour
    print("\nP(up) by hour:")
    for h in sorted(set(hours)):
        m = hours == h
        print(f"  {h:02d}h: {up[m].mean():.3f} n={m.sum()}")


if __name__ == "__main__":
    main()