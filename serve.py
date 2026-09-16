"""Product runner: the bot as a service.

Runs three things concurrently:
  1. Signal loop  - every LOOP_INTERVAL_MINUTES scan the watchlist, broadcast
                    new qualifying signals to the admin chat + subscribers,
                    and log every signal for the verified track record.
  2. Command bot  - long-poll Telegram updates and answer /signal /scan
                    /status /mystats /subscribe /pause /resume etc.
  3. Daily digest - at DAILY_DIGEST_HOUR_UTC, resolve expired signals and
                    broadcast the verified live performance report.

Quality gates (config.py) applied to every broadcast:
  - data staleness : the last closed bar must be fresh enough for its TF
                     (MAX_BAR_AGE_MINUTES), so Yahoo M1 delay can't fake a signal
  - confluence     : >= CONFLUENCE_MIN_AGREE variants must agree on direction,
                     or a single variant must be very strong (>= 78%)
                     (BROADCAST_MIN_CONFIDENCE floor always applies)
  - profitability  : only pairs with a validated OUT-OF-SAMPLE edge are
                     broadcast - OOS EV >= BROADCAST_MIN_WATCHLIST_EV and
                     OOS win rate >= BROADCAST_MIN_WATCHLIST_WINRATE
                     (walk-forward scan stats, so weak rows never reach users)
  - cooldown       : no more than one signal per pair per PAIR_COOLDOWN_MINUTES
  - circuit break  : a pair pauses CIRCUIT_PAUSE_HOURS after
                     CIRCUIT_STREAK_LOSSES consecutive verified losses
  - daily cap      : MAX_DAILY_SIGNALS_PER_PAIR per pair per day

All runtime state lives in serve_state.json (via bot_control), so admin
Telegram commands (/pause /resume /pausepair) take effect within one cycle.

Usage:
  set TG_BOT_TOKEN (and TG_CHAT_ID) first
  python serve.py [--minutes N]
"""
import argparse
import datetime as dt
import os
import sys
import threading
import time

import bot_control as bc
import config as cfg
import signal_log
import telegram_bot as tg
from health_server import start_health_server
from data_fetcher import bar_age_minutes, fetch
from scanner import load_watchlist, watchlist_rows
from sentiment import briefing_text, sentiment_for
from signal_log import report_text, verify_expired
from strategy import confluence

# Windows consoles default to cp1252 and choke on Unicode arrows.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

LOCK_FILE = "serve.lock"
ctx = {"started_at": None, "scan_lock": False}


# ---------------- signal emission ----------------

def _fresh_enough(df, tf):
    """True if the latest closed bar is fresh for this timeframe."""
    age = bar_age_minutes(df)
    limit = cfg.MAX_BAR_AGE_MINUTES.get(tf, 60)
    if age < 0:
        print(f"    [staleness] {tf}: could not read bar timestamp - skip")
        return False
    if age > limit:
        print(f"    [staleness] last {tf} bar {age:.0f} min old "
              f"(limit {limit}) - skip")
        return False
    return True


def _build_signal(c, df, agree_votes):
    """Full signal dict (broadcast/log compatible) from a confluence read."""
    return {
        "direction": c["direction"],
        "confidence": c["confidence"],
        "tier": c["tier"],
        "score": max((abs(o["score"]) for v, o in c["votes"].items()
                      if v in agree_votes), default=0.0),
        "entry": float(df.iloc[-1]["Close"]),
        "at": df.index[-1],
        "drivers": agree_votes,
        "confluence": True,
        "_df": df,
    }


def _confluence_gate(c):
    """Apply the quality gate to a confluence read.

    Returns (agree_votes, min_conf, ok). A signal passes only if enough
    variants agree on the direction AND the combined confidence clears the
    floor (a lone variant needs to be very strong).
    """
    direction = c["direction"]
    agree = [v for v, o in c["votes"].items()
             if o["dir"] != 0 and (o["dir"] > 0) == (direction == "CALL")]
    if len(agree) >= cfg.CONFLUENCE_MIN_AGREE:
        min_conf = cfg.BROADCAST_MIN_CONFIDENCE
    else:
        min_conf = max(cfg.BROADCAST_MIN_CONFIDENCE, 78.0)
    ok = bool(agree) and c["confidence"] >= min_conf
    return agree, min_conf, ok


def emit_signal(asset, tf, variant, state):
    """Compute the confluence signal for one watchlist pair; broadcast + log.

    `variant` is the watchlist row that triggered the scan; the actual
    direction comes from the confluence of all variants on the chart.
    """
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    key = bc.pair_key(asset, tf, "confluence")

    if not cfg.is_session_open(asset):
        print(f"  [closed] {asset}: not currently tradeable on Pocket "
              f"Option (outside session hours / weekend) - skip")
        return 0
    if bc.is_paused(state, key):
        print(f"  [paused] {asset} {tf}: {bc.paused_text(state, key)}")
        return 0
    if not bc.cooldown_ok(state, key):
        print(f"  [cooldown] {asset} {tf}: "
              f"last sent {bc.last_sent(state, key):%H:%M} UTC")
        return 0

    day = state.setdefault("daily", {}).setdefault(today, {})
    if day.get(key, 0) >= cfg.MAX_DAILY_SIGNALS_PER_PAIR:
        print(f"  [daily cap] {asset} {tf}: already sent "
              f"{cfg.MAX_DAILY_SIGNALS_PER_PAIR} today")
        return 0

    # Per-pair circuit breaker: pause after N consecutive verified losses.
    outcomes = signal_log.last_outcomes(asset, tf, n=cfg.CIRCUIT_STREAK_LOSSES)
    if (len(outcomes) >= cfg.CIRCUIT_STREAK_LOSSES
            and all(o == "lost" for o in outcomes)):
        bc.pause_pair(state, key)
        print(f"  [circuit] {asset} {tf}: {len(outcomes)} consecutive "
              f"verified losses -> paused {cfg.CIRCUIT_PAUSE_HOURS}h")
        return 0

    # Profitability gate: only broadcast pairs with a validated out-of-sample
    # edge from the walk-forward scan, so users only ever receive pairs that
    # clear the payout break-even (EV >= min, win rate >= min).
    row = watchlist_rows().get((asset, tf))
    if row is None:
        print(f"  [EV gate] {asset} {tf}: no validated OOS stats - skip")
        return 0
    if row["oos_ev"] < cfg.BROADCAST_MIN_WATCHLIST_EV:
        print(f"  [EV gate] {asset} {tf}: OOS EV {row['oos_ev']:+.3f} < "
              f"{cfg.BROADCAST_MIN_WATCHLIST_EV:+.2f} - skip")
        return 0
    if row["win_rate"] < cfg.BROADCAST_MIN_WATCHLIST_WINRATE:
        print(f"  [EV gate] {asset} {tf}: OOS win {row['win_rate']:.0%} < "
              f"{cfg.BROADCAST_MIN_WATCHLIST_WINRATE:.0%} - skip")
        return 0

    df = fetch(asset, tf)
    if not _fresh_enough(df, tf):
        return 0

    df_htf = None
    htf_map = {"M1": "M5", "M5": "M15", "M15": "M30", "M30": "H1"}
    if tf in htf_map:
        try:
            df_htf = fetch(asset, htf_map[tf])
        except Exception:
            df_htf = None

    c = confluence(df, asset_name=asset, df_htf=df_htf)
    if c["direction"] is None:
        votes_str = ", ".join(f"{v}:{o['dir']}" for v, o in c["votes"].items())
        print(f"  [no confluence] {asset} {tf}: no agreed direction "
              f"(votes: {votes_str})")
        return 0

    agree, min_conf, ok = _confluence_gate(c)
    if not ok:
        print(f"  [quality gate] {asset} {tf}: {c['direction']} "
              f"conf {c['confidence']:.0f}% < required {min_conf:.0f}% "
              f"({len(agree)}/{len(c['votes'])} variants agree)")
        return 0

    sig = _build_signal(c, df, agree)
    sentiment = sentiment_for(asset)  # cached 15 min; never blocks the loop
    sent_to, blocked = tg.broadcast_signal(sig, asset, tf,
                                           variant="confluence",
                                           sentiment=sentiment)
    if sent_to:
        state[key] = {
            "sent_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "signal": sig["direction"],
            "bar": str(sig["at"]),
        }
        day[key] = day.get(key, 0) + 1
        print(f"  [broadcast] {asset} {tf} [{variant}] -> {sig['direction']} "
              f"(conf {sig['confidence']:.0f}%, {len(agree)} variants) "
              f"-> {sent_to} chats")
        if blocked:
            print(f"    (quota-blocked: {', '.join(c for c, _, _ in blocked)})")
    else:
        print(f"  [broadcast skipped] {asset} {tf}: no recipients "
              f"(blocked={len(blocked)})")
    return sent_to


def signal_loop(state, interval_min):
    print(f"Signal loop: watchlist scan every {interval_min} min "
          f"(only broadcasts when the admin has enabled auto-broadcast "
          f"with /autosignal all on \u2014 otherwise signals are on-demand "
          f"only, via /signal or /scan). Ctrl+C to stop.")
    cycle = 0
    while True:
        cycle += 1
        now = dt.datetime.now(dt.timezone.utc)
        print(f"\n[{now:%Y-%m-%d %H:%M} UTC] scanning watchlist...")
        # Resolve expired signals against live market data every ~5 cycles so
        # the verified track record (and the circuit breakers that depend on
        # it) update continuously instead of waiting for the daily digest.
        if cycle % 5 == 0:
            try:
                resolved, _ = signal_log.verify_expired(progress=False)
                if resolved:
                    print(f"  [verify] resolved {resolved} expired signal(s) "
                          f"against live market data")
            except Exception as exc:
                print(f"  [verify] failed: {exc}")
        # Reload state each cycle so admin /pausepair commands apply immediately.
        state = bc.load()
        if not bc.auto_broadcast_enabled(state):
            print("  [auto-broadcast off] admin disabled the loop with "
                  "/autosignal all off - skipping this cycle")
            watch = []
        else:
            watch = load_watchlist()
        if not watch:
            if bc.auto_broadcast_enabled(state):
                print("  no watchlist - run `python bot.py scan` first")
        else:
            seen = set()
            for asset, tf, variant in watch:
                key = bc.pair_key(asset, tf, "confluence")
                if key in seen:
                    continue  # same pair listed under several variants
                seen.add(key)
                try:
                    emit_signal(asset, tf, variant, state)
                except Exception as exc:
                    print(f"  {asset} {tf} [{variant}]: skipped ({exc})")
        watch_keys = [bc.pair_key(a, t, "confluence") for a, t, _ in watch]
        bc.prune_state(state, watch_keys=watch_keys)
        bc.save(state)
        try:
            time.sleep(interval_min * 60)
        except KeyboardInterrupt:
            raise


def command_loop(ctx):
    offset = None
    while True:
        updates = tg.get_updates(offset=offset, timeout=25)
        if updates:
            offset = tg.process_updates(updates, ctx)
        try:
            time.sleep(cfg.UPDATES_POLL_SECONDS)
        except KeyboardInterrupt:
            raise


def digest_loop(state):
    last_date = None
    while True:
        now = dt.datetime.now(dt.timezone.utc)
        if now.hour >= cfg.DAILY_DIGEST_HOUR_UTC and now.date().isoformat() != last_date:
            print(f"[digest] resolving expired signals...")
            try:
                resolved, _ = verify_expired(progress=False)
                text = report_text()
                if resolved:
                    # News & Sentiment Engine: append the morning briefing to
                    # the verified-performance digest.
                    try:
                        text += "\n\n" + briefing_text(max_assets=4)
                    except Exception as exc:
                        print(f"[digest] briefing failed: {exc}")
                    # Hot markets: best validated edges + live leaderboard.
                    try:
                        from telegram_bot import _hot_markets_text
                        text += "\n\n" + _hot_markets_text()
                    except Exception as exc:
                        print(f"[digest] hot markets failed: {exc}")
                results = tg.broadcast(text)
                ok = any(r.get("ok") for r in results)
                print(f"[digest] resolved {resolved}; broadcast "
                      f"{'OK' if ok else 'FAILED'}")
                last_date = now.date().isoformat()
                # PRO subscriptions expiring within 3 days: notify the admin
                # (for renewal follow-up) AND the affected user directly, so
                # they can renew before access lapses. Each user is reminded
                # once per distinct expiry date (a renewal resets the flag).
                try:
                    from subscriptions import (expiring_pro,
                                               mark_pro_expiry_notified,
                                               pro_expiry_already_notified)
                    exp = expiring_pro(days=3)
                    if exp:
                        who = ", ".join(
                            ("@" + e["username"] if e.get("username")
                             else str(e["chat_id"]))
                            + f" ({e['pro_until']:%Y-%m-%d})" for e in exp)
                        tg.send_message(
                            f"⚠️ *PRO expiring within 3 days:* {who}\n"
                            f"Renew: /grantpro <username> 30",
                            chat_id=cfg.TELEGRAM_CHAT_ID)
                        for e in exp:
                            until_iso = e["pro_until"].isoformat()
                            if pro_expiry_already_notified(e["chat_id"], until_iso):
                                continue
                            try:
                                tg.send_message(
                                    f"⏳ *Your PRO access expires on "
                                    f"{e['pro_until']:%Y-%m-%d}.*\n\n"
                                    f"Renew to keep unlimited signals, "
                                    f"advanced indicators, AI analysis and "
                                    f"portfolio tracking \u2014 contact "
                                    f"{cfg.ADMIN_CONTACT} to extend your "
                                    f"plan.",
                                    chat_id=e["chat_id"])
                                mark_pro_expiry_notified(e["chat_id"], until_iso)
                            except Exception as exc:
                                print(f"[digest] expiry reminder to "
                                      f"{e['chat_id']} failed: {exc}")
                except Exception as exc:
                    print(f"[digest] expiring-pro notice failed: {exc}")
            except Exception as exc:
                print(f"[digest] failed: {exc}")
        try:
            time.sleep(300)
        except KeyboardInterrupt:
            raise


def _single_instance():
    """Windows file-lock: refuse to start if another serve.py holds the lock.
    Returns the locked handle; the caller must keep it referenced for the
    lifetime of the process (dropping it would release the lock)."""
    import msvcrt
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), LOCK_FILE)
    try:
        fh = open(lock_path, "w+")
        fh.write("x")
        fh.flush()
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        return fh
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser(description=f"{cfg.BOT_DISPLAY} product runner")
    ap.add_argument("--minutes", type=int, default=cfg.LOOP_INTERVAL_MINUTES)
    args = ap.parse_args()

    if not tg.configured():
        print("ERROR: TG_BOT_TOKEN not set. Create a bot with @BotFather and "
              "set the environment variable (see README).")
        return 1

    lock_handle = _single_instance()
    if lock_handle is None:
        print("serve.py is already running (lock held). Exiting.")
        return 1

    state = bc.load()
    ctx["started_at"] = dt.datetime.now(dt.timezone.utc)

    start_health_server()  # no-op unless PORT env var is set (Render)

    threads = [
        threading.Thread(target=signal_loop, args=(state, args.minutes), daemon=True),
        threading.Thread(target=command_loop, args=(ctx,), daemon=True),
        threading.Thread(target=digest_loop, args=(state,), daemon=True),
    ]
    for t in threads:
        t.start()
    print("serve.py running. Ctrl+C to stop.")
    if cfg.TELEGRAM_CHAT_ID:
        try:
            tg.send_message(f"🟢 *{cfg.BOT_DISPLAY} is online* - verified "
                            f"signals on all markets are live. "
                            f"Use /help for commands.",
                            chat_id=cfg.TELEGRAM_CHAT_ID)
        except Exception as exc:
            print(f"startup notice failed: {exc}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    sys.exit(main())