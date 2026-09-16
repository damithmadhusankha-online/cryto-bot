"""Diagnostic: CALL/PUT signal counts via the PRODUCTION compute_signals path.

Loads every cached frame, runs each variant through strategy.compute_signals,
and reports direction balance (the sell-signal bug showed as CALL-heavy or
PUT-zero output). Also dumps PUT score stats on the last frame.
"""
import glob
import os
import pickle
import sys

import numpy as np
import pandas as pd

import config as cfg
from strategy import compute_signals

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

frames = []
for p in sorted(glob.glob("data_cache/*.pkl")):
    with open(p, "rb") as fh:
        obj = pickle.load(fh)
    if isinstance(obj, pd.DataFrame) and len(obj) > 100:
        frames.append((os.path.basename(p), obj))
print("frames:", len(frames), flush=True)
print("volume zero-frac on last frame:", flush=True)
print("  ", float((frames[-1][1]["Volume"] == 0).mean()), flush=True)

TH = cfg.VAR_THRESHOLDS["momentum"]  # threshold sanity reference
for variant in ("momentum", "breakout", "reversal"):
    calls = puts = 0
    last_put_scores = []
    last_sig_col = None
    for name, df in frames:
        d = compute_signals(df.copy(), asset_name=name.replace(".pkl", "").upper(),
                            variant=variant)
        sig = d["signal"].to_numpy(int)
        calls += int((sig > 0).sum())
        puts += int((sig < 0).sum())
        if name == frames[-1][0]:
            last_sig_col = d["signal"].to_numpy(int)
            last_put_scores = d.loc[d["signal"] < 0, "score"].to_numpy(float)
    print(f"--- {variant}: CALL={calls} PUT={puts} "
          f"(ratio {calls/max(puts,1):.1f}:1) ---", flush=True)
    if len(last_put_scores):
        print("  last-frame PUT scores: min", round(float(np.min(last_put_scores)), 2),
              "median", round(float(np.median(last_put_scores)), 2),
              "max", round(float(np.max(last_put_scores)), 2), flush=True)
    else:
        print("  no PUT signals on last frame", flush=True)
print("DONE", flush=True)