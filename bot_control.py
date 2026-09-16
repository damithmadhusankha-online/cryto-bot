"""Runtime control plane for the product runner (serve.py + telegram commands).

Single source of truth for serve_state.json so the signal loop and the
Telegram command responder (/pausepair /resumepair) never fight over
stale copies.

State layout (besides the per-pair sent bookkeeping already used by serve.py):

    {
      "paused": {"EURUSD|M1|momentum": "2026-09-10T12:00:00+00:00", ...},
      "cooldown": {"EURUSD|M1|momentum": "2026-09-09T14:41:27+00:00", ...},
      ...
    }
"""
import datetime as dt
import json
import os
import threading

import config as cfg

_lock = threading.Lock()
_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "serve_state.json")


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _parse(iso):
    """Parse an ISO timestamp to a tz-aware datetime; None on garbage."""
    if not iso:
        return None
    try:
        d = dt.datetime.fromisoformat(str(iso))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d
    except (TypeError, ValueError):
        return None


def pair_key(asset, tf, variant):
    return f"{asset}|{tf}|{variant}"


def load():
    with _lock:
        try:
            with open(_PATH, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}


def save(data):
    with _lock:
        with open(_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)


# ---------------- auto-broadcast global switch ----------------

def auto_broadcast_enabled(state):
    """False unless the admin explicitly turns the background loop on with
    /autosignal all on. Default False: signals are generated strictly on
    demand (/signal, /scan) unless the admin opts into automatic pushes."""
    return bool(state.get("auto_broadcast", False))


def set_auto_broadcast(state, enabled):
    state["auto_broadcast"] = bool(enabled)
    save(state)
    return bool(enabled)


# ---------------- pause / resume ----------------

def is_paused(state, key):
    """True if the pair is paused until some time."""
    d = _parse(state.get("paused", {}).get(key))
    return d is not None and d > _now()


def paused_text(state, key):
    """Human-readable reason a pair is paused ('' if not paused)."""
    d = _parse(state.get("paused", {}).get(key))
    if d is not None and d > _now():
        return f"pair circuit breaker until {d:%Y-%m-%d %H:%M} UTC"
    return ""


def pause_pair(state, key, hours=None):
    hours = hours if hours is not None else cfg.CIRCUIT_PAUSE_HOURS
    state.setdefault("paused", {})[key] = (_now() + dt.timedelta(hours=hours)).isoformat()


def resume_pair(state, key):
    state.setdefault("paused", {}).pop(key, None)


# ---------------- cooldown ----------------

def cooldown_ok(state, key, minutes=None):
    """True if PAIR_COOLDOWN_MINUTES (or given minutes) passed since the
    last broadcast of this pair."""
    minutes = minutes if minutes is not None else cfg.PAIR_COOLDOWN_MINUTES
    sent = state.get(key, {}).get("sent_at")
    last = _parse(sent)
    if last is None:
        return True
    return (_now() - last).total_seconds() >= minutes * 60


def last_sent(state, key):
    return _parse(state.get(key, {}).get("sent_at"))


# ---------------- hygiene ----------------

def prune_state(state, watch_keys=None):
    """Drop stale daily counters and per-pair keys so serve_state.json
    stays small on a long-running install."""
    today = _now().date()
    daily = state.get("daily", {})
    stale_days = [
        d for d in daily
        if _parse(d) is not None
        and (_now().date() - _parse(d).date()).days > cfg.STATE_DAILY_RETENTION_DAYS
    ]
    for d in stale_days:
        daily.pop(d, None)

    if watch_keys:
        keep = set(watch_keys) | {"paused", "daily"}
        for k in list(state.keys()):
            if k not in keep and k not in ("paused", "daily"):
                state.pop(k, None)
    return state