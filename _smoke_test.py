"""Temporary smoke test: run the pro strategy on cached data and backtest."""
import glob
import os
import pickle

import pandas as pd

import config as cfg
from strategy import compute_signals, live_signal, confluence
from backtester import run_backtest

os.chdir(os.path.dirname(os.path.abspath(__file__)))

pkls = sorted(glob.glob("data_cache/*.pkl"))
print(f"cached frames: {len(pkls)}", flush=True)

tested = 0
signals_total = {"momentum": 0, "reversal": 0, "breakout": 0}
trades_total = {"momentum": 0, "reversal": 0, "breakout": 0}
wins_total = {"momentum": 0, "reversal": 0, "breakout": 0}
for p in pkls[-24:]:
    with open(p, "rb") as fh:
        obj = pickle.load(fh)
    if not isinstance(obj, pd.DataFrame) or len(obj) < 160:
        continue
    name = "EURUSD"   # session filter: Forex (crypto frames: "BTCUSD")
    tf = "M5"
    for v in ("momentum", "reversal", "breakout"):
        try:
            out = compute_signals(obj.copy(), asset_name=name, variant=v)
            n_sig = int((out["signal"] != 0).sum())
            lt = live_signal(obj.copy(), asset_name=name, variant=v)
            bt = run_backtest(obj.copy(), name, variant=v, tf=tf)
            signals_total[v] += n_sig
            trades_total[v] += bt["n_trades"]
            wins_total[v] += bt["wins"]
            tested += 1
            live_txt = f"live={lt['direction']}@{lt['confidence']:.0f}" if lt else "live=None"
            if tested <= 6:
                print(f"[{name} {v}] sig={n_sig} bt={bt['n_trades']}t "
                      f"wr={bt['win_rate']:.1%} {live_txt}", flush=True)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"FAILED {v}: {e}", flush=True)

print(f"\nframes tested: {tested}", flush=True)
for v in ("momentum", "reversal", "breakout"):
    wr = wins_total[v] / trades_total[v] if trades_total[v] else 0.0
    print(f"{v:9s} signals={signals_total[v]:5d} trades={trades_total[v]:4d} "
          f"wins={wins_total[v]:4d} wr={wr:.1%}", flush=True)
print("SMOKE OK", flush=True)