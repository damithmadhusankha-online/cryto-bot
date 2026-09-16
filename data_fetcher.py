"""Market data layer (Yahoo Finance via yfinance) with disk caching.

Every fetch is cached to data_cache/ as a pickle. A fresh fetch is attempted
only when the cache is older than CACHE_TTL_SECONDS; if the refresh fails the
stale copy is still usable for up to CACHE_TTL_STALE seconds, so the live bot
keeps running through brief outages.
"""
import datetime as dt
import hashlib
import os
import pickle
import time

import pandas as pd
import yfinance as yf

import config as cfg

# in-memory cache: mem_key -> (cached_at_ts, df); expires like the disk cache
_memory = {}
_memory_lock = __import__("threading").Lock()


def _period_for(days, interval):
    """yfinance 'period' string; 1m bars are capped at 7 days upstream."""
    if interval == "1m":
        return "7d"
    if days <= 7:
        return "7d"
    if days <= 31:
        return "1mo"
    if days <= 93:
        return "3mo"
    return "6mo"


def _cache_path(symbol_name, timeframe, days):
    key = f"{symbol_name}|{timeframe}|{days}"
    digest = hashlib.sha1(key.encode()).hexdigest()[:16]
    return os.path.join(cfg.CACHE_DIR, f"{digest}.pkl")


def _read_cache(path):
    try:
        with open(path, "rb") as fh:
            return pickle.load(fh), os.path.getmtime(path)
    except (OSError, pickle.PickleError, EOFError):
        return None, 0.0


def _write_cache(path, df):
    os.makedirs(cfg.CACHE_DIR, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as fh:
            pickle.dump(df, fh, protocol=4)
        os.replace(tmp, path)
    except OSError:
        pass


def _clean(df):
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    if len(df) < 60:
        raise RuntimeError(f"too little data returned: {len(df)} bars")
    return df


def _download(yf_sym, interval, period):
    """One yfinance download with retries."""
    last = None
    for attempt in range(3):
        try:
            df = yf.download(
                yf_sym, interval=interval, period=period,
                auto_adjust=True, progress=False, threads=False,
            )
            if df is not None and not df.empty:
                return df
            last = RuntimeError(f"empty response for {yf_sym} {interval}")
        except Exception as exc:  # network hiccup, rate limit, ...
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"download failed for {yf_sym}: {last}")


def bar_age_minutes(df):
    """Minutes since the last closed bar (UTC). -1 if the index is unusable."""
    idx = df.index
    if getattr(idx, "tz", None) is None:
        idx = idx.tz_localize("UTC")
    else:
        idx = idx.tz_convert("UTC")
    try:
        last = idx[-1].to_pydatetime()
    except (IndexError, AttributeError):
        return -1.0
    now = dt.datetime.now(dt.timezone.utc)
    return (now - last.replace(tzinfo=dt.timezone.utc)).total_seconds() / 60.0


def fetch(symbol_name, timeframe, days=None):
    """Return a clean OHLCV DataFrame for a display-name asset + timeframe."""
    if symbol_name not in cfg.ASSETS:
        raise KeyError(f"unknown asset {symbol_name!r}; valid: {sorted(cfg.ASSETS)}")
    if timeframe not in cfg.DATA_INTERVAL_MAP:
        raise KeyError(f"unknown timeframe {timeframe!r}; valid: {cfg.TIMEFRAMES}")

    days = days or cfg.DATA_PERIOD_DAYS
    mem_key = (symbol_name, timeframe, days)
    with _memory_lock:
        mem_hit = _memory.get(mem_key)
    if mem_hit is not None and (time.time() - mem_hit[0]) < cfg.CACHE_TTL_SECONDS:
        return mem_hit[1].copy()

    yf_sym = cfg.ASSETS[symbol_name]
    interval = cfg.DATA_INTERVAL_MAP[timeframe]
    period = _period_for(days, interval)
    path = _cache_path(symbol_name, timeframe, days)

    cached, mtime = _read_cache(path)
    age = time.time() - mtime if mtime else float("inf")

    if cached is not None and age < cfg.CACHE_TTL_SECONDS:
        try:
            df = _clean(cached)
            with _memory_lock:
                _memory[mem_key] = (time.time(), df)
            return df.copy()
        except (KeyError, RuntimeError):
            pass  # corrupt cache entry; fall through to a refetch

    try:
        raw = _download(yf_sym, interval, period)
        df = _clean(raw)
    except Exception:
        if cached is not None and age < cfg.CACHE_TTL_STALE:
            try:
                df = _clean(cached)
                with _memory_lock:
                    _memory[mem_key] = (time.time(), df)
                return df.copy()
            except (KeyError, RuntimeError):
                pass
        raise

    _write_cache(path, df)
    with _memory_lock:
        _memory[mem_key] = (time.time(), df)
    return df.copy()