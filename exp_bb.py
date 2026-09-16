"""Experiment: fresh Bollinger-break continuation vs fade win rates."""
import sys
sys.path.insert(0, ".")

import numpy as np

import config as cfg
from data_fetcher import fetch
from indicators import add_all


def stat(name, tf):
    df = add_all(fetch(name, tf))
    exp = cfg.expiry_candles(tf)
    c = df["Close"].to_numpy(float)
    bb_up = df["bb_up"].to_numpy(float)
    bb_low = df["bb_low"].to_numpy(float)

    pc = c[:-exp]
    ex = c[exp:]
    up = ex > pc

    prev_up = np.roll(bb_up, 1)
    prev_low = np.roll(bb_low, 1)
    prev_up[0] = prev_low[0] = np.nan

    fu = (c[:-exp] > bb_up[:-exp]) & (pc <= prev_up[:-exp])
    fd = (c[:-exp] < bb_low[:-exp]) & (pc >= prev_low[:-exp])

    print(f"{name} {tf}: fresh-up n={fu.sum()} cont={up[fu].mean():.3f} fade={1 - up[fu].mean():.3f}"
          f" | fresh-dn n={fd.sum()} cont={(~up[fd]).mean():.3f} fade={up[fd].mean():.3f}")


if __name__ == "__main__":
    for pair in [("EURUSD", "M5"), ("XAUUSD", "M5"), ("GBPUSD", "M5"),
                 ("BTCUSD", "M15"), ("EURUSD", "M15"), ("XAUUSD", "M15")]:
        try:
            stat(*pair)
        except Exception as exc:
            print(pair, "ERR", repr(exc))