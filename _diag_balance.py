"""Diagnostic: all-bars confluence direction balance on cached data.

Replicates confluence()'s per-bar decision (majority of agreeing variants,
tie -> best score if conf>=70) WITHOUT HTF (HTF only affects the last bar
live) and counts CALL/PUT bars across the full window per asset/timeframe,
plus the gate pass (>=2 agree or single tier-A conf>=82, floor 74).
"""
import glob
import hashlib
import os
import pickle
import sys

import numpy as np
import pandas as pd

import config as cfg
from strategy import compute_signals

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

digest_map = {}
for asset in cfg.ASSETS:
    for tf in cfg.TIMEFRAMES:
        key = f"{asset}|{tf}|{cfg.DATA_PERIOD_DAYS}"
        digest_map[hashlib.sha1(key.encode()).hexdigest()[:16]] = (asset, tf)

frames = {}
for p in sorted(glob.glob("data_cache/*.pkl")):
    name = os.path.basename(p)[:-4]
    if name not in digest_map:
        continue
    asset, tf = digest_map[name]
    try:
        with open(p, "rb") as fh:
            obj = pickle.load(fh)
    except Exception:
        continue
    if isinstance(obj, pd.DataFrame) and len(obj) > 100:
        frames[(asset, tf)] = obj

print(f"mapped frames: {len(frames)}", flush=True)

def conf_of(score):
    return float(np.clip(50.0 + abs(score) * 8.0, None, 96.0))

def tier_of(c):
    return "A" if c >= 70 else ("B" if c >= 60 else "C")

agg = {"CALL": 0, "PUT": 0, "NONE": 0}
gate = {"CALL": 0, "PUT": 0}
per_tf = {}
by_variant = {v: {"CALL": 0, "PUT": 0} for v in cfg.VARIANTS}

for (asset, tf), df in frames.items():
    sigs = {}
    for v in cfg.VARIANTS:
        try:
            d = compute_signals(df.copy(), asset_name=asset, variant=v)
            sigs[v] = (d["signal"].to_numpy(int), d["score"].to_numpy(float))
        except Exception:
            sigs[v] = (np.zeros(len(df), dtype=int), np.zeros(len(df)))
    n = len(df)
    for i in range(n):
        nonzero = {}
        for v in cfg.VARIANTS:
            s, sc = sigs[v]
            if s[i] != 0:
                nonzero[v] = (s[i], float(sc[i]))
                by_variant[v]["CALL" if s[i] > 0 else "PUT"] += 1
        if not nonzero:
            agg["NONE"] += 1
            continue
        calls = sum(1 for o in nonzero.values() if o[0] > 0)
        puts = sum(1 for o in nonzero.values() if o[0] < 0)
        if calls == puts:
            best = max(nonzero.items(), key=lambda kv: abs(kv[1][1]))[1]
            if conf_of(best[1]) < 70:
                agg["NONE"] += 1
                continue
            direction = "CALL" if best[0] > 0 else "PUT"
        else:
            direction = "CALL" if calls > puts else "PUT"
        agreeing = [o for o in nonzero.values() if (o[0] > 0) == (direction == "CALL")]
        conf = float(np.mean([conf_of(o[1]) for o in agreeing]))
        agg[direction] += 1
        # gate: >=2 agree, or single with tier A (>=82), floor 74
        if len(agreeing) >= 2:
            min_conf = cfg.BROADCAST_MIN_CONFIDENCE
        else:
            min_conf = max(cfg.BROADCAST_MIN_CONFIDENCE, cfg.CONFLUENCE_SINGLE_TIER_A)
        if conf >= min_conf:
            gate[direction] += 1
        per_tf.setdefault(tf, {"CALL": 0, "PUT": 0})[direction] += 1

tot = agg["CALL"] + agg["PUT"]
print(f"\n--- confluence direction, all bars, no HTF ---")
print(f"  CALL={agg['CALL']} PUT={agg['PUT']} "
      f"(PUT {100*agg['PUT']/max(tot,1):.1f}%)  NONE={agg['NONE']}")
gt = gate["CALL"] + gate["PUT"]
print(f"  after gate: CALL={gate['CALL']} PUT={gate['PUT']} "
      f"(PUT {100*gate['PUT']/max(gt,1):.1f}%)")
print("\n--- by timeframe ---")
for tf, d in per_tf.items():
    t = d["CALL"] + d["PUT"]
    print(f"  {tf:4s}: CALL={d['CALL']:6d} PUT={d['PUT']:6d} "
          f"(PUT {100*d['PUT']/max(t,1):.0f}%)")
print("\n--- per-variant bars ---")
for v, d in by_variant.items():
    t = d["CALL"] + d["PUT"]
    print(f"  {v:9s}: CALL={d['CALL']:6d} PUT={d['PUT']:6d} "
          f"(PUT {100*d['PUT']/max(t,1):.0f}%)")
print("DONE", flush=True)