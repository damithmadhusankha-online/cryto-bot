"""Expert-Signal-Bot CLI (v3).

Commands:
  python bot.py scan [--top N] [--send]
      Rank asset x timeframe x strategy pairs by walk-forward, out-of-sample
      expectancy (with bootstrap CI + consistency). Saves the top-N watchlist;
      optionally broadcasts the ranking to Telegram.

  python bot.py signal ASSET TF [--variant V] [--all] [--htf] [--send] [--chart]
      Generate one live signal for an asset/timeframe and print it
      (or broadcast it with an optional chart image).

  python bot.py verify [--send]
      Resolve expired signals against live market data and print (or
      broadcast) the verified track record.

  python bot.py loop [--minutes N] [--send] [--chart]
      Scheduled mode for the watchlist. For the full product (Telegram
      commands + subscribers + daily digest) use `python serve.py` instead.

Flags:
  --send      actually post to Telegram (requires TG_BOT_TOKEN/TG_CHAT_ID)
  --chart     attach a chart image to broadcast signals
  --dry-run   default behavior when --send is absent: print, don't post
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

import config as cfg
from backtester import run_backtest, summarize
from data_fetcher import fetch
from scanner import load_watchlist, save_watchlist, scan
from signal_log import log_signal, report_text, verify_expired
from strategy import confluence, live_signal
from telegram_bot import configured, format_signal, send_message, signal_buttons

STATE_FILE = "sent_state.json"

# Windows consoles default to cp1252 and choke on the Unicode arrows in the
# signal card - force UTF-8 (the Telegram API itself is UTF-8, so this only
# affects local console printing).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_state(state):
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2)


def _daily_cap_ok(state, key, date_str, cap=None):
    cap = cap if cap is not None else cfg.MAX_DAILY_SIGNALS_PER_PAIR
    day = state.setdefault("daily", {}).setdefault(date_str, {})
    return day.get(key, 0) < cap


def _maybe_broadcast(sig, asset, tf, variant, send, chart, state, state_key):
    """Dedupe + optionally send. Returns True if (re)broadcast happened."""
    if sig is None:
        return False
    if state.get(state_key):
        return False  # already sent this bar

    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    if not _daily_cap_ok(state, state_key, today):
        return False

    note = None
    if not send and not configured():
        note = ("(Telegram not configured - run with --send after setting "
                "TG_BOT_TOKEN/TG_CHAT_ID)")
    elif not send:
        note = "(dry-run: set --send to broadcast)"
    elif not configured():
        note = "Telegram not configured - signal printed only"

    text = format_signal(sig, asset, tf, variant, extra_note=note)
    photo = None
    if chart:
        try:
            import chart as chart_mod
            df = fetch(asset, tf)
            photo = chart_mod.save_chart(df, asset, tf, variant=variant)
        except Exception as exc:
            print(f"chart failed for {asset} {tf}: {exc}")

    if send and configured():
        resp = send_message(text, photo_path=photo, chat_id=cfg.TELEGRAM_CHAT_ID,
                            buttons=signal_buttons(asset, tf, variant))
        ok = resp.get("ok", False)
        print(f"[send] {asset} {tf} {variant} {'OK' if ok else 'FAILED: ' + str(resp)}")
        if ok:
            log_signal(asset, tf, variant, sig)
    else:
        print(f"\n=== SIGNAL {asset} {tf} [{variant}] ===")
        print(text)

    state[state_key] = {
        "sent_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "signal": sig["direction"],
        "score": sig["score"],
        "bar": str(sig["at"]),
    }
    state.setdefault("daily", {}).setdefault(today, {})[state_key] = \
        state.get("daily", {}).get(today, {}).get(state_key, 0) + 1
    return True


# ---------------- commands ----------------

def cmd_scan(args):
    print(f"Scanning {len(cfg.ASSETS)} assets x {len(cfg.TIMEFRAMES)} timeframes "
          f"x {len(cfg.VARIANTS)} variants over {cfg.SCAN_WINDOW_DAYS}d "
          f"(min {cfg.SCAN_MIN_TRADES} trades, OOS-validated)...")
    df = scan(progress=True)
    if df.empty:
        print("No pairs passed validation. Widen SCAN_WINDOW_DAYS or lower "
              "SCAN_MIN_TRADES in config.py.")
        return 1

    print("\n===== TOP MARKETS (OOS-VALIDATED RANK) =====")
    show = df[["asset", "category", "timeframe", "variant", "n_trades",
               "win_rate", "profit_factor", "oos_ev", "ci_lower",
               "max_drawdown", "consistent", "rank_score"]].head(args.top)
    print(show.to_string(index=False))

    top = save_watchlist(df, top=args.top)
    print(f"\nWatchlist saved to {cfg.WATCHLIST_FILE}:")
    for name, tf, variant in top:
        print(f"  - {name} {tf} [{variant}]")

    if args.send:
        if not configured():
            print("--send given but Telegram not configured; skipping broadcast.")
        else:
            lines = ["*Top markets this window (OOS-validated)*", ""]
            for r in df.head(args.top).itertuples(index=False):
                lines.append(
                    f"{r.asset} {r.timeframe} [{r.variant}]: {r.n_trades} OOS trades, "
                    f"win {r.win_rate:.0%}, EV {r.oos_ev:+.3f} "
                    f"(CI-lo {r.ci_lower:+.3f})"
                    f"{'  (consistent)' if r.consistent else ''}"
                )
            resp = send_message("\n".join(lines), chat_id=cfg.TELEGRAM_CHAT_ID)
            print("[send] ranking", "OK" if resp.get("ok") else resp)
    return 0


def cmd_signal(args):
    df = fetch(args.asset, args.timeframe)
    df_htf = None
    if args.htf:
        htf_tf = _next_htf(args.timeframe)
        if htf_tf:
            try:
                df_htf = fetch(args.asset, htf_tf)
            except Exception as exc:
                print(f"HTF fetch failed ({exc}); using single timeframe")

    state = _load_state()
    variants = cfg.VARIANTS if args.all else [args.variant]
    for variant in variants:
        sig = live_signal(df, asset_name=args.asset, variant=variant,
                          df_htf=df_htf)
        key = f"{args.asset}|{args.timeframe}|{variant}"
        sent = _maybe_broadcast(sig, args.asset, args.timeframe, variant,
                                args.send, args.chart, state, key)
        if sent:
            _save_state(state)

    if args.all:
        c = confluence(df, asset_name=args.asset, df_htf=df_htf)
        if c["direction"]:
            print(f"\n[confluence] {c['direction']} ({c['confidence']:.0f}%, "
                  f"tier {c['tier']}) - votes: " +
                  ", ".join(f"{v}:{('CALL' if o['dir'] > 0 else ('PUT' if o['dir'] < 0 else '-'))}"
                            for v, o in c["votes"].items()))
        else:
            print("\n[confluence] no agreement across variants")

    st = run_backtest(df, args.asset, args.variant, args.timeframe)
    print("\n[context] " + summarize(st, args.asset, args.timeframe, args.variant))
    if st["n_trades"] > 0 and not st["signals"].empty:
        r = st["signals"]
        print(r.tail(5)[["t", "signal", "entry", "win"]].to_string(index=False))
    return 0


def _next_htf(tf):
    order = ["M1", "M5", "M15", "M30", "H1"]
    try:
        i = order.index(tf)
    except ValueError:
        return None
    return order[i + 1] if i + 1 < len(order) else None


def cmd_verify(args):
    resolved, total = verify_expired(progress=True)
    print(f"\nResolved {resolved} expired signal(s) from {total} logged.")
    print(report_text())
    if args.send and configured():
        resp = send_message(report_text(), chat_id=cfg.TELEGRAM_CHAT_ID)
        print("[send] track record", "OK" if resp.get("ok") else resp)
    return 0


def cmd_loop(args):
    watch = load_watchlist()
    if not watch:
        print(f"No watchlist found. Run `python bot.py scan --top {cfg.SCAN_TOP_N}` "
              f"first to generate {cfg.WATCHLIST_FILE}.")
        return 1

    state = _load_state()
    interval = args.minutes or cfg.LOOP_INTERVAL_MINUTES
    print(f"Loop mode: {len(watch)} watchlist pairs, every {interval} min. "
          f"Ctrl+C to stop. (For the full product run `python serve.py`.)")

    try:
        while True:
            now = dt.datetime.now(dt.timezone.utc)
            print(f"\n[{now:%Y-%m-%d %H:%M} UTC] scanning watchlist...")
            for asset, tf, variant in watch:
                try:
                    df = fetch(asset, tf)
                    sig = live_signal(df, asset_name=asset, variant=variant)
                    key = f"{asset}|{tf}|{variant}"
                    if _maybe_broadcast(sig, asset, tf, variant, args.send,
                                        args.chart, state, key):
                        _save_state(state)
                except Exception as exc:
                    print(f"  {asset} {tf} [{variant}]: skipped ({exc})")
            print(f"sleeping {interval} min...")
            time.sleep(interval * 60)
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


# ---------------- main ----------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="Expert-Signal-Bot: scanner + signals + Telegram")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="rank markets/timeframes by OOS-validated expectancy")
    p_scan.add_argument("--top", type=int, default=cfg.SCAN_TOP_N)
    p_scan.add_argument("--send", action="store_true")

    p_sig = sub.add_parser("signal", help="generate one live signal")
    p_sig.add_argument("asset", help="e.g. EURUSD, XAUUSD, BTCUSD")
    p_sig.add_argument("timeframe", help="M1 M5 M15 M30 H1")
    p_sig.add_argument("--variant", default="momentum",
                       choices=cfg.VARIANTS, help="strategy variant")
    p_sig.add_argument("--all", action="store_true",
                       help="evaluate every variant + report confluence")
    p_sig.add_argument("--htf", action="store_true", help="add higher-TF confirmation")
    p_sig.add_argument("--send", action="store_true")
    p_sig.add_argument("--chart", action="store_true")

    p_ver = sub.add_parser("verify", help="resolve expired signals + show live track record")
    p_ver.add_argument("--send", action="store_true")

    p_loop = sub.add_parser("loop", help="scheduled watchlist scanning")
    p_loop.add_argument("--minutes", type=int, default=cfg.LOOP_INTERVAL_MINUTES)
    p_loop.add_argument("--send", action="store_true")
    p_loop.add_argument("--chart", action="store_true")

    args = ap.parse_args(argv)
    if args.cmd == "scan":
        return cmd_scan(args)
    if args.cmd == "signal":
        return cmd_signal(args)
    if args.cmd == "verify":
        return cmd_verify(args)
    if args.cmd == "loop":
        return cmd_loop(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())