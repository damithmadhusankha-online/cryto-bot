"""Diagnostic: replicate the EXACT live signal path on cached data.

For every cached (asset, tf) frame (with its HTF frame when cached), run
confluence() + confluence_gate() and count CALL/PUT at each stage so we can
see exactly where sell (PUT) signals die:

  per-variant last-bar votes
  -> confluence direction (majority + tier-A)
  -> after the quality gate (agree-count + confidence floor)
  -> with/without HTF (live uses HTF; backtest does not)
"""
import glob
import hashlib
import os
import pickle
import sys

import config as cfg
from strategy import confluence, compute_signals
from signal_scan import confluence_gate, HTF_MAP

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# map cache digests -> (asset, tf)
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
    if isinstance(obj, __import__("pandas").DataFrame) and len(obj) > 100:
        frames[(asset, tf)] = obj

print(f"mapped frames: {len(frames)}", flush=True)

# stage counters
votes = {"CALL": 0, "PUT": 0, "0": 0}
confluence_dir = {"CALL": 0, "PUT": 0, "None": 0}
confluence_htf_dir = {"CALL": 0, "PUT": 0, "None": 0}
after_gate = {"CALL": 0, "PUT": 0}
after_gate_htf = {"CALL": 0, "PUT": 0}
agree_counts = {"CALL": 0, "PUT": 0}

# per-variant PUT survival with/without HTF
var_stats = {}
var_htf_stats = {}

for (asset, tf), df in frames.items():
    htf_df = None
    if tf in HTF_MAP and (asset, HTF_MAP[tf]) in frames:
        htf_df = frames[(asset, HTF_MAP[tf])]

    for label, use_htf in (("nohtf", False), ("htf", True)):
        h = htf_df if use_htf else None
        try:
            c = confluence(df, asset_name=asset, df_htf=h)
        except Exception as exc:
            print(f"confluence FAILED {asset} {tf}: {exc}", flush=True)
            continue
        d = c["direction"]
        if use_htf:
            if d is None:
                confluence_htf_dir["None"] += 1
            else:
                confluence_htf_dir[d] += 1
            agree, _mc, ok = confluence_gate(c)
            if ok:
                after_gate_htf[d] += 1
                agree_counts[d] += len(agree)
        else:
            if d is None:
                confluence_dir["None"] += 1
            else:
                confluence_dir[d] += 1
            agree, _mc, ok = confluence_gate(c)
            if ok:
                after_gate[d] += 1

    # per-variant votes (no HTF, to mirror the backtest) - last bar only
    for v in cfg.VARIANTS:
        try:
            out = compute_signals(df.copy(), asset_name=asset, variant=v)
            s = int(out["signal"].iloc[-1])
        except Exception:
            continue
        if s > 0:
            votes["CALL"] += 1
            var_stats.setdefault(v, {"CALL": 0, "PUT": 0})["CALL"] += 1
        elif s < 0:
            votes["PUT"] += 1
            var_stats.setdefault(v, {"CALL": 0, "PUT": 0})["PUT"] += 1
        else:
            votes["0"] += 1
        # with HTF
        if htf_df is not None:
            try:
                out2 = compute_signals(df.copy(), asset_name=asset, variant=v,
                                       df_htf=htf_df)
                s2 = int(out2["signal"].iloc[-1])
            except Exception:
                continue
            if s2 > 0:
                var_htf_stats.setdefault(v, {"CALL": 0, "PUT": 0})["CALL"] += 1
            elif s2 < 0:
                var_htf_stats.setdefault(v, {"CALL": 0, "PUT": 0})["PUT"] += 1

print("\n--- per-variant last-bar votes (backtest path, no HTF) ---")
for v in cfg.VARIANTS:
    st = var_stats.get(v, {"CALL": 0, "PUT": 0})
    tot = st["CALL"] + st["PUT"]
    print(f"  {v:9s} CALL={st['CALL']:4d} PUT={st['PUT']:4d} "
          f"(PUT {100*st['PUT']/max(tot,1):.0f}%)", flush=True)
print("--- per-variant last-bar votes (LIVE path: HTF applied) ---")
for v in cfg.VARIANTS:
    st = var_htf_stats.get(v, {"CALL": 0, "PUT": 0})
    tot = st["CALL"] + st["PUT"]
    print(f"  {v:9s} CALL={st['CALL']:4d} PUT={st['PUT']:4d} "
          f"(PUT {100*st['PUT']/max(tot,1):.0f}%)", flush=True)

print("\n--- confluence direction ---")
tot = sum(confluence_dir.values())
for k, n in confluence_dir.items():
    print(f"  no-HTF {k:4s}: {n:4d} ({100*n/max(tot,1):.0f}%)", flush=True)
tot = sum(confluence_htf_dir.values())
for k, n in confluence_htf_dir.items():
    print(f"  HTF    {k:4s}: {n:4d} ({100*n/max(tot,1):.0f}%)", flush=True)

print("\n--- after confluence_gate (broadcastable) ---")
for label, d in (("no-HTF", after_gate), ("HTF   ", after_gate_htf)):
    t = d["CALL"] + d["PUT"]
    print(f"  {label}: CALL={d['CALL']:4d} PUT={d['PUT']:4d} "
          f"(PUT {100*d['PUT']/max(t,1):.0f}%)", flush=True)
print("DONE", flush=True)