"""Experiment: reversal fade-edge vs band-extension age window."""
import sys
sys.path.insert(0, ".")

import numpy as np

import config as cfg
from data_fetcher import fetch
from strategy import _events, _run_age, compute_signals


def stat(name, tf, age_max):
    df = fetch(name, tf)
    df = compute_signals(df, asset_name=name, variant="reversal",
                         threshold=2.0)
    ev = _events(df)
    age = _run_age(ev["bb_ext"])

    exp = cfg.expiry_candles(tf)
    sig = df["signal"].to_numpy(int)
    close = df["Close"].to_numpy(float)
    n = len(df)
    wins = losses = 0
    for i in range(n - exp):
        s = sig[i]
        if s == 0:
            continue
        # keep only signals driven by fresh-enough bb_ext
        if not (ev["bb_ext"][i] != 0 and age[i] <= age_max):
            continue
        win = (s > 0 and close[i + exp] > close[i]) or (s < 0 and close[i + exp] < close[i])
        wins += win
        losses += (not win)
    tot = wins + losses
    wr = wins / tot if tot else float("nan")
    print(f"  {name} {tf} age<={age_max}: n={tot} win={wr:.3f}")


if __name__ == "__main__":
    pairs = [("EURUSD", "M5"), ("XAUUSD", "M5"), ("GBPUSD", "M5"),
             ("USDJPY", "M5"), ("EURUSD", "M15"), ("BTCUSD", "M15")]
    for name, tf in pairs:
        print(f"--- {name} {tf} ---")
        for age in (1, 3, 5, 10, 20):
            try:
                stat(name, tf, age)
            except Exception as exc:
                print("ERR", repr(exc))