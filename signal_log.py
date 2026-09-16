"""Append-only log of every broadcast signal + live outcome verification.

This is the track-record engine: every signal that goes out is logged with
its entry price and expiry time. After the expiry passes, verify_expired()
resolves the outcome against real market data and the live win rate is
computed from those resolved trades - no editing, no cherry-picking.

A "win" mirrors the backtest definition: at the expiry time the price is on
the CALL/PUT side of the entry price.
"""
import datetime as dt
import json
import os
import threading

import pandas as pd

import config as cfg
from data_fetcher import fetch

_lock = threading.Lock()


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _log_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), cfg.SIGNAL_LOG)


def log_signal(asset, tf, variant, sig, entry_bar=None):
    """Record a broadcast signal. sig: dict from strategy.live_signal."""
    entry_bar = entry_bar or sig["at"]
    if getattr(entry_bar, "tzinfo", None) is None:
        entry_bar = entry_bar.replace(tzinfo=dt.timezone.utc)
    expiry_min = cfg.EXPIRY_MINUTES[tf]
    rec = {
        "ts": _now().isoformat(),
        "entry_bar": entry_bar.isoformat(),
        "expiry_ts": (entry_bar + dt.timedelta(minutes=expiry_min)).isoformat(),
        "asset": asset,
        "tf": tf,
        "variant": variant,
        "direction": sig["direction"],
        "entry": round(float(sig["entry"]), 8),
        "score": float(sig["score"]),
        "confidence": float(sig["confidence"]),
        "status": "open",
    }
    with _lock:
        with open(_log_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    return rec


def load_records():
    path = _log_path()
    if not os.path.exists(path):
        return []
    recs = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return recs


def _spread_for(asset):
    """Half-spread buffer (decimal) used to credit a win only when price
    clearly crosses the entry - mirrors cfg.SPREAD_BPS per category."""
    cat = cfg.ASSET_CATEGORY.get(asset, "Forex")
    return cfg.SPREAD_BPS.get(cat, 0.0) / 10000.0


def _resolve(rec, df):
    """Exit price at the bar closest to/after expiry; win/loss vs entry.
    A win must clear the entry by the spread buffer (honest vs broker quote)."""
    idx = df.index
    if getattr(idx, "tz", None) is None:
        idx = idx.tz_localize("UTC")
    else:
        idx = idx.tz_convert("UTC")
    exp = pd.Timestamp(rec["expiry_ts"])
    future = idx[idx >= exp]
    if len(future) == 0:
        return None
    bar = future[0]
    exit_price = float(df.loc[bar, "Close"])
    entry = float(rec["entry"])
    spread = _spread_for(rec["asset"])
    win = (rec["direction"] == "CALL" and exit_price > entry * (1 + spread)) or \
          (rec["direction"] == "PUT" and exit_price < entry * (1 - spread))
    return {"win": bool(win), "exit": exit_price, "resolved_at": _now().isoformat()}


def verify_expired(progress=False):
    """Resolve every 'open' record whose expiry has passed. Returns (resolved, total)."""
    recs = load_records()
    resolved = 0
    now = _now()
    by_key = {}
    for rec in recs:
        if rec["status"] != "open":
            continue
        if pd.Timestamp(rec["expiry_ts"]) > now:
            continue
        key = (rec["asset"], rec["tf"])
        if key not in by_key:
            # fetch enough bars to cover entry -> expiry (+ margin)
            days = max(1, int(cfg.EXPIRY_MINUTES[rec["tf"]] / 1440) + 2)
            try:
                by_key[key] = fetch(rec["asset"], rec["tf"], days=days)
            except Exception as exc:
                if progress:
                    print(f"  verify {key}: fetch failed ({exc})")
                continue
        outcome = _resolve(rec, by_key[key])
        if outcome is None:
            continue
        rec["status"] = "won" if outcome["win"] else "lost"
        rec["exit"] = outcome["exit"]
        rec["resolved_at"] = outcome["resolved_at"]
        resolved += 1

    if resolved:
        with _lock:
            lines = []
            for rec in recs:
                lines.append(json.dumps(rec) + "\n")
            with open(_log_path(), "w", encoding="utf-8") as fh:
                fh.writelines(lines)
    return resolved, len(recs)


def last_outcomes(asset, tf, n=3):
    """The last n verified outcomes ('won'/'lost') for one pair."""
    recs = [r for r in load_records()
            if r["asset"] == asset and r["tf"] == tf
            and r["status"] in ("won", "lost")]
    return [r["status"] for r in recs[-n:]]


def recent_resolved(n=20):
    """The last n verified outcomes across all pairs."""
    recs = [r for r in load_records() if r["status"] in ("won", "lost")]
    return recs[-n:]


def streaks():
    """Current + best win/loss streaks from the verified log.

    Returns:
      {"current": "won"|"lost"|None, "current_len": int,
       "best_won": int, "best_lost": int, "total": int}
    """
    seq = [r["status"] for r in load_records() if r["status"] in ("won", "lost")]
    out = {"current": None, "current_len": 0,
           "best_won": 0, "best_lost": 0, "total": len(seq)}
    if not seq:
        return out
    cur = seq[-1]
    out["current"] = cur
    n = 0
    for s in reversed(seq):
        if s != cur:
            break
        n += 1
    out["current_len"] = n
    best = 1
    for i in range(1, len(seq)):
        best = best + 1 if seq[i] == seq[i - 1] else 1
        if seq[i] == "won":
            out["best_won"] = max(out["best_won"], best)
        else:
            out["best_lost"] = max(out["best_lost"], best)
    if seq[0] == "won":
        out["best_won"] = max(out["best_won"], 1)
    else:
        out["best_lost"] = max(out["best_lost"], 1)
    return out


def market_leaderboard(limit=8):
    """Verified markets ranked by win rate (min 3 resolved signals each).
    Returns a list of dicts: {market, wins, n, win_rate, status}."""
    s = stats()
    rows = []
    for key, v in s["by_market"].items():
        if v["n"] < 3:
            continue
        rows.append({"market": key, "wins": v["wins"], "n": v["n"],
                     "win_rate": v["win_rate"]})
    rows.sort(key=lambda r: (-r["win_rate"], -r["n"]))
    return rows[:limit]


def stats():
    """Live performance stats from the log (all time + last 30 + by market)."""
    recs = load_records()
    if not recs:
        return {
            "total": 0, "resolved": 0, "wins": 0, "win_rate": 0.0,
            "open": 0, "last30": [], "by_market": {}, "by_variant": {},
        }
    resolved = [r for r in recs if r["status"] in ("won", "lost")]
    open_n = sum(1 for r in recs if r["status"] == "open")
    wins = sum(1 for r in resolved if r["status"] == "won")
    total = len(recs)

    def agg(subset):
        if not subset:
            return {"n": 0, "wins": 0, "win_rate": 0.0}
        w = sum(1 for r in subset if r["status"] == "won")
        return {"n": len(subset), "wins": w, "win_rate": w / len(subset)}

    by_market = {}
    for r in resolved:
        key = f"{r['asset']} {r['tf']} [{r['variant']}]"
        by_market.setdefault(key, []).append(r)
    by_market = {k: agg(v) for k, v in by_market.items()}

    by_variant = {}
    for r in resolved:
        by_variant.setdefault(r["variant"], []).append(r)
    by_variant = {k: agg(v) for k, v in by_variant.items()}

    return {
        "total": total,
        "resolved": len(resolved),
        "open": open_n,
        "wins": wins,
        "win_rate": wins / len(resolved) if resolved else 0.0,
        "last30": agg(resolved[-30:]),
        "by_market": dict(sorted(by_market.items(), key=lambda kv: -kv[1]["n"])),
        "by_variant": by_variant,
    }


def report_text():
    """Telegram-friendly daily/on-demand performance report."""
    s = stats()
    lines = ["*Live signal track record*", ""]
    if s["total"] == 0:
        lines.append("No signals broadcast yet.")
        return "\n".join(lines)
    lines.append(f"Signals sent: {s['total']}  |  open: {s['open']}")
    if s["resolved"]:
        lines.append(
            f"Resolved: {s['resolved']}  |  wins: {s['wins']}  |  "
            f"live win rate: *{s['win_rate']:.0%}*"
        )
        l30 = s["last30"]
        if l30["n"]:
            lines.append(f"Last 30: {l30['wins']}/{l30['n']} ({l30['win_rate']:.0%})")
        lines += ["", "*By market:*"]
        for k, v in list(s["by_market"].items())[:10]:
            lines.append(f"  {k}: {v['wins']}/{v['n']} ({v['win_rate']:.0%})")
        lines += ["", "_Track record is verified from live market data after each "
                  "expiry - not a backtest._"]
    else:
        lines.append("No resolved signals yet (signals still open).")
    return "\n".join(lines)