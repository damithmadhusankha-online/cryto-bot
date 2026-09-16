"""Technical indicators, pure pandas implementations (no TA-Lib needed)."""
import numpy as np
import pandas as pd

import config as cfg


def ema(series, n):
    return series.ewm(span=n, adjust=False).mean()


def rsi(series, n=None):
    n = n or cfg.RSI_PERIOD
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    return out.fillna(50.0)


def macd(series, fast=None, slow=None, signal=None):
    fast = fast or cfg.MACD_FAST
    slow = slow or cfg.MACD_SLOW
    signal = signal or cfg.MACD_SIGNAL
    line = ema(series, fast) - ema(series, slow)
    sig = line.ewm(span=signal, adjust=False).mean()
    hist = line - sig
    return line, sig, hist


def bollinger(series, n=None, k=None):
    n = n or cfg.BB_PERIOD
    k = k or cfg.BB_STD
    mid = series.rolling(n).mean()
    std = series.rolling(n).std(ddof=0)
    return mid + k * std, mid, mid - k * std


def atr(df, n=None):
    n = n or cfg.ATR_PERIOD
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx(df, n=None):
    """Wilder Average Directional Index (14). 0-100 trend strength."""
    n = n or cfg.ADX_PERIOD
    h, l, c = df["High"], df["Low"], df["Close"]
    up = h.diff()
    dn = -l.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr_w = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100.0 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr_w.replace(0, np.nan)
    mdi = 100.0 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr_w.replace(0, np.nan)
    dx = 100.0 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean().fillna(0.0)


def donchian(series, n=None):
    """Donchian channels: rolling max/min of closes over the last n bars."""
    n = n or cfg.DONCH_PERIOD
    high = series.rolling(n).max()
    low = series.rolling(n).min()
    return high, low


def volume_sma(df, n=None):
    n = n or cfg.VOL_SMA
    return df["Volume"].rolling(n).mean().fillna(df["Volume"])


def add_all(df):
    """Add every indicator column used by the strategies."""
    df = df.copy()
    c = df["Close"]
    df["ema_fast"] = ema(c, cfg.EMA_FAST)
    df["ema_mid"] = ema(c, cfg.EMA_MID)
    df["ema_slow"] = ema(c, cfg.EMA_SLOW)
    df["rsi"] = rsi(c)
    df["macd_line"], df["macd_sig"], df["macd_hist"] = macd(c)
    df["bb_up"], df["bb_mid"], df["bb_low"] = bollinger(c)
    df["atr"] = atr(df)
    df["atr_pct"] = df["atr"] / c * 100.0
    df["adx"] = adx(df)
    df["dc_high"], df["dc_low"] = donchian(c)
    df["vol_sma"] = volume_sma(df)
    df["vol_ratio"] = df["Volume"] / df["vol_sma"].replace(0.0, np.nan)
    return df