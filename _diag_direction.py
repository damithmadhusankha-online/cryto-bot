"""Diagnostic: split signals by direction (CALL/PUT) per variant and per gate.

Runs compute_signals on cached frames and counts how many +1 vs -1 signals
survive each stage: raw score -> VWAP/StochRSI gates -> threshold -> cooldown.
"""
import glob
import os
import pickle
import sys

import numpy as np
import pandas as pd

import config as cfg
from strategy import compute_signals
from indicators import add_all

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

pkls = sorted(glob.glob("data_cache/*.pkl"))
frames = []
for p in pkls[-40:]:
    try:
        with open(p, "rb") as fh:
            obj = pickle.load(fh)
        if isinstance(obj, pd.DataFrame) and len(obj) >= 160:
            frames.append(obj)
    except Exception:
        pass

print(f"frames: {len(frames)}", flush=True)

# count raw score sign at each gate for each variant
for v in ("momentum", "reversal", "breakout"):
    raw_put = raw_call = 0
    gate_put = gate_call = 0
    sig_put = sig_call = 0
    for df in frames:
        try:
            out = compute_signals(df.copy(), asset_name="EURUSD", variant=v)
            s = out["signal"].to_numpy(int)
            sig_call += int((s > 0).sum())
            sig_put += int((s < 0).sum())
        except Exception as exc:
            print(f"  {v} FAILED: {exc}", flush=True)
    print(f"{v:9s} signals: CALL={sig_call:5d} PUT={sig_put:5d} "
          f"ratio PUT%={100*sig_put/max(1,sig_call+sig_put):.1f}", flush=True)

# deeper: which gate kills PUTs in momentum?
print("\n--- momentum gate autopsy (first 6 frames) ---", flush=True)
for df in frames[:6]:
    df = add_all(df.copy())
    from strategy import _events
    ev = _events(df)
    score = (ev["ema_cross"] * 2.0 + ev["rsi"] + ev["macd"] + ev["bb_break"])
    d = np.sign(score)
    if cfg.VWAP_REQUIRE_SIDE:
        side_ok = ((d > 0) & (ev["vwap_side"] > 0)) | ((d < 0) & (ev["vwap_side"] < 0))
        score = np.where(side_ok, score, 0.0)
        d = np.sign(score)
    zone_ok = ((d > 0) & (ev["stoch_side"] > 0)) | ((d < 0) & (ev["stoch_side"] < 0))
    score2 = np.where(zone_ok, score, 0.0)
    d2 = np.sign(score2)
    trend = ev["trend"]
    gate = np.where(trend != 0, np.sign(trend) == d2, np.abs(score2) >= 5.0 + 1.0)
    score3 = np.where(gate, score2, 0.0)
    d3 = np.sign(score3)

    # count candidate PUTs at each stage (raw score negative)
    n_raw_put = int((d < 0).sum())
    n_after_vwap = int((d < 0).sum())
    n_after_zone = int((d2 < 0).sum())
    n_after_trend = int((d3 < 0).sum())
    n_put_sig = int(((score3 <= -5.0) & (score3 != 0)).sum())
    n_call_sig = int((score3 >= 5.0).sum())
    print(f"raw_put={n_raw_put:4d} after_vwap={n_after_vwap:4d} "
          f"after_zone={n_after_zone:4d} after_trend={n_after_trend:4d} "
          f"final CALL={n_call_sig:4d} PUT={n_put_sig:4d}", flush=True)
print("DONE", flush=True)