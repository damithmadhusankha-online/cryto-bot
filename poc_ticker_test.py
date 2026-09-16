"""Task-unique temp script: probe data availability for candidate internal keys.
Usage: python poc_ticker_test.py
"""
import sys
import data_fetcher as df

TICKERS = [
    "PEPEUSD", "SHIBUSD", "AVAXUSD", "LINKUSD", "DOTUSD", "TRXUSD",
    "AAPL", "TSLA", "NVDA", "AMZN", "META", "GOOGL", "MSFT", "AMD", "NFLX",
    "GBPCHF", "EURNZD", "AUDJPY",
]

def main():
    ok = 0
    for t in TICKERS:
        results = []
        for tf in ("M30", "H1"):
            try:
                d = df.fetch(t, tf, days=14)
                n = 0 if d is None else len(d)
                results.append(f"{tf}:{n} rows")
            except Exception as exc:
                results.append(f"{tf}:ERR {str(exc)[:80]}")
        status = "OK" if "ERR" not in " ".join(results) and "0 rows" not in " ".join(results) else "FAIL"
        if status == "OK":
            ok += 1
        print(f"{status:4s} {t:10s} " + " | ".join(results))
    print(f"\n{ok}/{len(TICKERS)} usable (M30+H1 both non-empty)")

if __name__ == "__main__":
    sys.exit(main())