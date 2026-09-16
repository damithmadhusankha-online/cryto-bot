"""Shared live-signal scanning.

Scans the *validated* watchlist right now and returns every pair that
currently clears the SAME quality gates the background broadcast loop
(serve.py) uses: a proven out-of-sample edge (EV/win-rate from the
walk-forward scan), multi-strategy confluence, the confidence floor, fresh
data, and the pause / circuit-breaker state. Used by:

  - serve.py's signal_loop (background broadcast)
  - telegram_bot.py's /signal command in "burst" mode (on-demand, all
    markets at once, plan-aware count)

Keeping this in one module means the on-demand pull can never be a looser,
"easier to hit 100%" version of the loop - it is the exact same bar. If
nothing currently qualifies, `scan_live` returns an empty list; it never
pads results with a weaker signal just to fill a quota.
"""
import config as cfg
import bot_control as bc
import signal_log
from data_fetcher import bar_age_minutes, fetch
from scanner import load_watchlist, watchlist_rows
from strategy import confluence, quick_read

import concurrent.futures
import threading

HTF_MAP = {"M1": "M5", "M5": "M15", "M15": "M30", "M30": "H1"}


def fresh_enough(df, tf):
    """True if the latest closed bar is fresh enough for this timeframe."""
    age = bar_age_minutes(df)
    limit = cfg.MAX_BAR_AGE_MINUTES.get(tf, 60)
    return 0 <= age <= limit


def confluence_gate(c):
    """Same gate serve.py applies: enough variants must agree AND the
    combined confidence must clear the floor (a lone variant needs to be
    very strong)."""
    direction = c["direction"]
    agree = [v for v, o in c["votes"].items()
             if o["dir"] != 0 and (o["dir"] > 0) == (direction == "CALL")]
    if len(agree) >= cfg.CONFLUENCE_MIN_AGREE:
        min_conf = cfg.BROADCAST_MIN_CONFIDENCE
    else:
        min_conf = max(cfg.BROADCAST_MIN_CONFIDENCE, cfg.CONFLUENCE_SINGLE_TIER_A)
    ok = bool(agree) and c["confidence"] >= min_conf
    return agree, min_conf, ok


def build_signal(c, df, agree_votes):
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


def _pair_paused(state, asset, tf):
    key = bc.pair_key(asset, tf, "confluence")
    return bc.is_paused(state, key)


def _circuit_broken(asset, tf):
    outcomes = signal_log.last_outcomes(asset, tf, n=cfg.CIRCUIT_STREAK_LOSSES)
    return (len(outcomes) >= cfg.CIRCUIT_STREAK_LOSSES
            and all(o == "lost" for o in outcomes))


def scan_live(limit=None, state=None, exclude=None):
    """Scan the validated watchlist right now; return qualifying signals.

    Returns a list of {"asset", "tf", "sig", "row"} dicts sorted by
    confidence (highest first), already passed through the EV/win-rate,
    confluence, confidence-floor, freshness, pause and circuit-breaker
    gates. `limit` caps the number returned (None = all that qualify).
    Never invents extra rows to reach `limit` - if only 3 markets clear
    the bar, only 3 come back.
    """
    exclude = exclude or set()
    state = bc.load() if state is None else state
    watch = load_watchlist()
    rows = watchlist_rows()
    out = []

    for asset, tf, _variant in watch:
        if (asset, tf) in exclude:
            continue
        if not cfg.is_session_open(asset):
            continue  # not currently tradeable on Pocket Option
        row = rows.get((asset, tf))
        if row is None:
            continue
        if row["oos_ev"] < cfg.BROADCAST_MIN_WATCHLIST_EV:
            continue
        if row["win_rate"] < cfg.BROADCAST_MIN_WATCHLIST_WINRATE:
            continue
        if _pair_paused(state, asset, tf) or _circuit_broken(asset, tf):
            continue
        try:
            df = fetch(asset, tf)
        except Exception:
            continue
        if not fresh_enough(df, tf):
            continue
        df_htf = None
        if tf in HTF_MAP:
            try:
                df_htf = fetch(asset, HTF_MAP[tf])
            except Exception:
                df_htf = None
        try:
            c = confluence(df, asset_name=asset, df_htf=df_htf)
        except Exception:
            continue
        if c["direction"] is None:
            continue
        agree, _min_conf, ok = confluence_gate(c)
        if not ok:
            continue
        out.append({"asset": asset, "tf": tf,
                     "sig": build_signal(c, df, agree), "row": row})

    out.sort(key=lambda r: r["sig"]["confidence"], reverse=True)
    return out[:limit] if limit is not None else out


def _live_check(asset, tf, state, rows, require_gate=True):
    """One asset/timeframe: the exact same real-time gates as scan_live()
    (confluence direction, agree-votes, confidence floor, freshness, pause,
    circuit-breaker). If the pair happens to be on the validated watchlist
    it also needs to clear the OOS EV/win-rate gate; if not, it's still a
    real, freshly computed technical signal - it just doesn't carry a
    historical edge stat yet. Returns None on no-signal/any hard gate
    failure (session closed, paused, circuit-broken, stale data, no
    direction), never a fabricated result.

    `require_gate=False` still returns None for all of the above hard
    gates, but no longer requires the confluence confidence bar to be
    cleared - the result carries `qualified=False` so the caller can be
    honest that this is a real, freshly computed reading that just hasn't
    earned the "validated signal" bar yet, rather than silently upgrading
    it into one."""
    if not cfg.is_session_open(asset):
        return None  # not currently tradeable on Pocket Option
    row = rows.get((asset, tf))
    if row is not None:
        if row["oos_ev"] < cfg.BROADCAST_MIN_WATCHLIST_EV:
            return None
        if row["win_rate"] < cfg.BROADCAST_MIN_WATCHLIST_WINRATE:
            return None
    if _pair_paused(state, asset, tf) or _circuit_broken(asset, tf):
        return None
    try:
        df = fetch(asset, tf)
    except Exception:
        return None
    if not fresh_enough(df, tf):
        return None
    df_htf = None
    if tf in HTF_MAP:
        try:
            df_htf = fetch(asset, HTF_MAP[tf])
        except Exception:
            df_htf = None
    try:
        c = confluence(df, asset_name=asset, df_htf=df_htf)
    except Exception:
        return None
    if c["direction"] is None:
        # No variant fired a fresh event on this bar at all (event-driven
        # strategies can legitimately go quiet for a stretch). Fall back to
        # a state-based quick read rather than reporting nothing - it is
        # always weaker evidence, so it always comes back `qualified=False`
        # regardless of `require_gate`.
        try:
            qr = quick_read(df, asset_name=asset, df_htf=df_htf)
        except Exception:
            qr = None
        if qr is None:
            return None
        qr["_df"] = df
        return {"asset": asset, "tf": tf, "sig": qr, "row": row,
                "qualified": False, "min_conf": cfg.BROADCAST_MIN_CONFIDENCE,
                "quick_read": True}
    agree, min_conf, ok = confluence_gate(c)
    if not ok and require_gate:
        return None
    return {"asset": asset, "tf": tf, "sig": build_signal(c, df, agree),
            "row": row, "qualified": ok, "min_conf": min_conf}


def scan_live_all(limit=None, timeframes=None, state=None, exclude=None,
                  max_workers=None, progress=None, near_miss_fallback=0):
    """Scan EVERY Pocket Option asset in cfg.ASSETS - not just the narrow,
    pre-ranked watchlist - across `timeframes` (default
    cfg.INSTANT_SCAN_TIMEFRAMES) in parallel, applying the exact same
    real-time confluence + confidence gates scan_live() uses. This is what
    powers the on-demand "find me something right now, anywhere" pull
    (⚡ Instant Signal); the unattended background broadcast loop keeps
    using scan_live()'s narrower, historically-validated-only watchlist.

    Markets that also happen to be on the validated watchlist get an
    additional OOS EV/win-rate gate and carry `row` in the result (so the
    card can show the "validated edge" line); markets outside the
    watchlist still have to clear the live confluence + confidence bar to
    appear at all - they just don't have a historical edge stat yet, and
    `row` is None so the caller can label that honestly instead of
    implying a track record that doesn't exist for that pair.

    `progress(done, total)` fires after each asset/timeframe finishes, so
    a caller can show REAL scan progress (e.g. "34/112 markets checked")
    instead of a fake countdown timer. Never pads results to hit `limit` -
    if only 2 markets clear the bar, only 2 come back.

    `near_miss_fallback` (0 = off, matches previous behavior exactly): if
    NOTHING clears the quality bar, return up to this many of the closest
    non-qualifying readings instead of an empty list, each tagged
    `qualified: False`. These are real, freshly computed technical reads
    that just fell short of the confidence/confluence bar - the caller
    MUST label them as unconfirmed rather than presenting them as a
    validated signal, or this stops being an honest quality gate.
    """
    exclude = exclude or set()
    timeframes = timeframes or cfg.INSTANT_SCAN_TIMEFRAMES
    max_workers = max_workers or cfg.INSTANT_SCAN_MAX_WORKERS
    state = bc.load() if state is None else state
    rows = watchlist_rows()

    pairs = [(a, tf) for a in cfg.ASSETS for tf in timeframes
             if (a, tf) not in exclude and cfg.is_session_open(a)]
    total = len(pairs)
    done = 0
    lock = threading.Lock()
    out = []

    def _run(pair):
        nonlocal done
        asset, tf = pair
        result = _live_check(asset, tf, state, rows, require_gate=False)
        with lock:
            done += 1
            n = done
        if progress:
            try:
                progress(n, total)
            except Exception:
                pass
        return result

    if total == 0:
        return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        for result in ex.map(_run, pairs):
            if result is not None:
                out.append(result)

    qualified = [r for r in out if r["qualified"]]
    qualified.sort(key=lambda r: r["sig"]["confidence"], reverse=True)
    if qualified or not near_miss_fallback:
        return qualified[:limit] if limit is not None else qualified

    near = sorted(out, key=lambda r: r["sig"]["confidence"], reverse=True)
    return near[:near_miss_fallback]
