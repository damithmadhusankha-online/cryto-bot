"""Task-unique temp script: probe walk_forward trade counts for candidate combos.
Usage: python poc_wf_probe.py
"""
import sys
import config as cfg
from data_fetcher import fetch
from backtester import walk_forward, consistency_check

COMBOS = [
    ("EURUSD", "M1", "momentum"), ("EURUSD", "M1", "reversal"),
    ("EURUSD", "M5", "momentum"), ("EURUSD", "M30", "momentum"),
    ("GBPUSD", "M5", "momentum"), ("BTCUSD", "M5", "momentum"),
    ("BTCUSD", "M30", "breakout"), ("SOLUSD", "M30", "momentum"),
    ("AAPL", "M30", "momentum"), ("AAPL", "M5", "momentum"),
    ("TSLA", "M30", "reversal"), ("XAUUSD", "M15", "momentum"),
    ("EURUSD", "H1", "breakout"), ("NVDA", "M30", "momentum"),
]

def main():
    for name, tf, variant in COMBOS:
        try:
            df = fetch(name, tf, days=cfg.SCAN_WINDOW_DAYS)
            wf = walk_forward(df, name, variant, tf, min_oos_trades=3)
            oos, train = wf["oos"], wf["train"]
            cc = consistency_check(df, name, variant, tf, min_trades_each=3)
            ci_lo = wf["oos_ci"]["ev_lo"]
            n = len(df) if df is not None else 0
            print(f"{name:8s} {tf:4s} {variant:9s} rows={n:5d} "
                  f"train={train['n_trades']:3d} oos={oos['n_trades']:3d} "
                  f"win={oos['win_rate']:.2f} ev={oos['expectancy']:+.3f} "
                  f"ci_lo={ci_lo:+.3f} consistent={cc['consistent']}")
        except Exception as exc:
            print(f"{name:8s} {tf:4s} {variant:9s} ERR {str(exc)[:60]}")

if __name__ == "__main__":
    sys.exit(main())