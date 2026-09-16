"""Event-driven signal engine (v3).

Signals fire ONLY on fresh events - crossovers, fades and breakouts that
happen on the current bar - never on persistent states.

Variants:
  momentum : trend continuation. EMA9/21 cross (2) + RSI push (1.5) +
             MACD line/signal cross (1) + fresh Bollinger break (1).
             Must agree with the EMA trend stack; a strong ADX adds +1.
  reversal : mean reversion. Fade fresh Bollinger extremes (2) + RSI turns
             off overbought/oversold (1.5). Suppressed while ADX says a
             strong trend is running (mean reversion dies in trends).
  breakout : range expansion. Fresh Donchian(20) break (2.5) + volume surge
             (1) + EMA alignment (1.5) + agreeing RSI push (1).
             Gated on ADX >= ADX_MIN so it only fires in real moves.

Session filter: bars outside the asset category's UTC session are muted.
HTF confirmation: a higher-timeframe trend in the same direction adds +2
to the LAST bar only (live-signal confirmation, no lookahead in backtests).
"""
import numpy as np
import pandas as pd

import config as cfg
from indicators import add_all


def _events(df):
    """Return event arrays (+/-/0) for each indicator trigger."""
    f, m, s = df["ema_fast"], df["ema_mid"], df["ema_slow"]
    prev_up = (f.shift(1) > m.shift(1)).fillna(False).to_numpy(bool)
    cur_up = (f > m).to_numpy(bool)
    ema_cross = np.where(cur_up & ~prev_up, 1.0,
                 np.where(~cur_up & prev_up, -1.0, 0.0))

    trend = np.where((f > m) & (m > s), 1.0,
             np.where((f < m) & (m < s), -1.0, 0.0)).astype(float)

    r = df["rsi"]
    prev_r = r.shift(1)
    rsi_up = (r > 55) & (prev_r <= 55)
    rsi_dn = (r < 45) & (prev_r >= 45)
    rsi_ext_up = (r > 70) & (r > prev_r)
    rsi_ext_dn = (r < 30) & (r < prev_r)
    rsi_s = np.where(rsi_up | rsi_ext_up, 1.5,
             np.where(rsi_dn | rsi_ext_dn, -1.5, 0.0)).astype(float)

    line, sig = df["macd_line"], df["macd_sig"]
    prev_diff = (line.shift(1) - sig.shift(1))
    diff = line - sig
    macd_x = np.where((diff > 0) & (prev_diff <= 0), 1.0,
              np.where((diff < 0) & (prev_diff >= 0), -1.0, 0.0)).astype(float)

    c = df["Close"]
    prev_c = c.shift(1)
    up, low = df["bb_up"], df["bb_low"]
    bb_break = np.where((c > up) & (prev_c <= up.shift(1)), 1.0,
                np.where((c < low) & (prev_c >= low.shift(1)), -1.0, 0.0)).astype(float)
    bb_ext = np.where(c > up, 1.0, np.where(c < low, -1.0, 0.0)).astype(float)

    # RSI turn off extremes (mean-reversion events)
    fade_up = (r > 35) & (prev_r <= 30)
    fade_dn = (r < 65) & (prev_r >= 70)
    rsi_fade = np.where(fade_up, 1.5, np.where(fade_dn, -1.5, 0.0)).astype(float)

    # Fresh Donchian breakout: close beyond the PREVIOUS bar's channel edge
    dh, dl = df["dc_high"], df["dc_low"]
    dc_break = np.where((c > dh.shift(1)) & (prev_c <= dh.shift(2)), 1.0,
                 np.where((c < dl.shift(1)) & (prev_c >= dl.shift(2)), -1.0, 0.0)).astype(float)

    # Volume surge / dry-up vs its own 20-bar average
    vr = df["vol_ratio"].fillna(0.0).to_numpy(float)
    vol = np.where(vr >= cfg.VOL_SURGE, 1.0,
          np.where(vr <= 1.0 / cfg.VOL_SURGE, -1.0, 0.0))

    adx = df["adx"].to_numpy(float)
    adx_ok = adx >= cfg.ADX_MIN
    adx_strong = adx >= cfg.ADX_STRONG

    return {
        "ema_cross": ema_cross, "trend": trend, "rsi": rsi_s,
        "macd": macd_x, "bb_break": bb_break, "bb_ext": bb_ext,
        "rsi_fade": rsi_fade, "dc_break": dc_break, "vol": vol,
        "adx_ok": adx_ok, "adx_strong": adx_strong,
    }


def _utc_hours(df):
    idx = df.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("UTC")
    else:
        idx = idx.tz_localize("UTC")
    return idx.hour


def _run_age(series):
    """Age of the current non-zero run: 1..N while the state persists."""
    age = np.zeros(len(series))
    cnt = 0
    prev = 0.0
    for i, v in enumerate(series):
        if v == prev and v != 0:
            cnt += 1
        else:
            cnt = 1 if v != 0 else 0
        age[i] = cnt
        prev = v
    return age


def _apply_session(df, asset_name):
    cat = cfg.ASSET_CATEGORY.get(asset_name, "Forex")
    span = cfg.SESSION_UTC.get(cat)
    if span is None:
        return np.ones(len(df), dtype=bool)
    h = _utc_hours(df).to_numpy(int)
    lo, hi = span
    return (h >= lo) & (h < hi)


def _htf_trend(htf_df):
    """-1/0/+1 trend of the last bar of a higher-TF frame."""
    htf = add_all(htf_df)
    f, m, s = htf["ema_fast"].iloc[-1], htf["ema_mid"].iloc[-1], htf["ema_slow"].iloc[-1]
    return 1.0 if f > m > s else (-1.0 if f < m < s else 0.0)


def _driver_masks(df, ev, variant, direction):
    """Per-component contribution masks for the signal card (last bar)."""
    d = direction  # array of +1/-1/0
    agree = lambda arr: (np.sign(arr) == d) & (d != 0)
    if variant == "momentum":
        return {
            "EMA cross": agree(ev["ema_cross"]),
            "RSI push": agree(ev["rsi"]),
            "MACD cross": agree(ev["macd"]),
            "Band break": agree(ev["bb_break"]),
            "Strong ADX": ev["adx_strong"] & (d != 0) & (np.sign(ev["trend"]) == d),
        }
    if variant == "reversal":
        return {
            "Band fade": agree(-ev["bb_ext"]),
            "RSI turn": agree(ev["rsi_fade"]),
        }
    if variant == "breakout":
        return {
            "Donchian break": agree(ev["dc_break"]),
            "Volume surge": agree(ev["vol"]),
            "EMA alignment": agree(np.where(df["ema_fast"] > df["ema_mid"], 1.0, -1.0)),
            "RSI push": agree(ev["rsi"]),
            "ADX gate": ev["adx_ok"] & (d != 0),
        }
    return {}


def _tier(conf):
    if conf >= 70:
        return "A"
    if conf >= 60:
        return "B"
    return "C"


def compute_signals(df, asset_name="EURUSD", variant="momentum",
                    threshold=None, df_htf=None):
    """Attach score/signal/confidence columns.

    asset_name drives the session filter; variant selects the scoring.
    df_htf: optional higher-timeframe DataFrame; its trend adds +-2 to the
    LAST bar only (live-signal confirmation, no lookahead in backtests).
    """
    df = add_all(df)
    ev = _events(df)
    threshold = threshold if threshold is not None else cfg.VAR_THRESHOLDS.get(variant, 4.0)

    if variant == "momentum":
        score = (ev["ema_cross"] * 2.0 + ev["rsi"] + ev["macd"] + ev["bb_break"])
        # trend gate: momentum against the established trend is dropped;
        # with no clear trend, require extra conviction
        gate = np.where(ev["trend"] != 0,
                        np.sign(ev["trend"]) == np.sign(score),
                        np.abs(score) >= threshold + 1.0)
        score = np.where(gate, score, 0.0)
        # strong ADX adds conviction only when it agrees with the move
        boost = (ev["adx_strong"] & (np.sign(score) != 0)
                 & (np.sign(score) == np.sign(ev["trend"])))
        score = np.where(boost, score + 1.0, score)
    elif variant == "reversal":
        # fade a band extension while it is still fresh (age <= 3 bars)
        decay = (_run_age(ev["bb_ext"]) <= 3).astype(float)
        score = (-ev["bb_ext"] * 2.0 * decay + ev["rsi_fade"])
        # mean reversion is suppressed in strong trends
        score = np.where(ev["adx_ok"], 0.0, score)
    elif variant == "breakout":
        ema_dir = np.where(df["ema_fast"] > df["ema_mid"], 1.0, -1.0)
        align = np.where((np.sign(ev["dc_break"]) == np.sign(ema_dir))
                         & (np.sign(ev["dc_break"]) != 0), 1.5, 0.0)
        score = (ev["dc_break"] * 2.5 + ev["vol"] * 1.0 + align)
        agree_rsi = (np.sign(ev["dc_break"]) == np.sign(ev["rsi"])) & (np.sign(ev["dc_break"]) != 0)
        score = score + np.where(agree_rsi, 1.0, 0.0)
        score = np.where(ev["adx_ok"], score, 0.0)  # ADX gate
    else:
        raise ValueError(f"unknown variant {variant!r}")

    if df_htf is not None and len(df_htf) > 0:
        htrend = _htf_trend(df_htf)
        if htrend != 0 and score[-1] != 0 and np.sign(htrend) == np.sign(score[-1]):
            score[-1] += 2.0

    df["score"] = score

    # relative volatility filter: skip dead bars
    med = df["atr_pct"].rolling(50, min_periods=10).median()
    vol_ok = df["atr_pct"].fillna(0.0) >= 0.5 * med.fillna(0.0)

    session_ok = _apply_session(df, asset_name)
    mask = (vol_ok.to_numpy(bool)) & session_ok

    df["signal"] = np.where(mask & (score >= threshold), 1,
                    np.where(mask & (score <= -threshold), -1, 0))
    df["conf"] = np.clip(50.0 + np.abs(score) * 8.0, None, 96.0)
    df["tier"] = [_tier(c) for c in df["conf"]]
    return df


def live_signal(df, asset_name="EURUSD", variant="momentum",
                threshold=None, df_htf=None):
    """Latest-bar signal as a dict, ready for broadcast (None if no signal)."""
    df = compute_signals(df, asset_name=asset_name, variant=variant,
                         threshold=threshold, df_htf=df_htf)
    last = df.iloc[-1]
    sig = int(last["signal"])
    if sig == 0:
        return None

    direction = 1.0 if sig > 0 else -1.0
    ev = _events(df)
    masks = _driver_masks(df, ev, variant, df["signal"].to_numpy(int))
    drivers = [name for name, ok in masks.items() if bool(ok[-1])]
    if not drivers:
        drivers = [variant]

    return {
        "direction": "CALL" if sig > 0 else "PUT",
        "score": float(last["score"]),
        "confidence": float(last["conf"]),
        "tier": str(last["tier"]),
        "entry": float(last["Close"]),
        "at": df.index[-1],
        "drivers": drivers,
    }


def quick_read(df, asset_name="EURUSD", df_htf=None):
    """State-based fallback for quick trading when NO variant has a fresh
    event on the current bar (see `_events` - momentum/reversal/breakout
    only fire on a brand-new crossover/fade/break, so a flat stretch can
    legitimately produce nothing for several bars in a row).

    Instead of leaving a request empty, this reads the CURRENT STATE -
    EMA stack, RSI side, MACD side, price vs mid-Bollinger-band, plus an
    ADX/HTF-trend boost - and turns it into a directional lean. It is
    always weaker evidence than a real event signal, so confidence is
    capped below `cfg.BROADCAST_MIN_CONFIDENCE` and the caller MUST label
    it as an unconfirmed quick read, never a validated signal.

    Returns None only when every one of those state reads is genuinely
    tied at zero (e.g. exactly flat EMA stack, RSI at 50, MACD at 0) -
    there is nothing left to lean on even for a quick read.
    """
    df = add_all(df)
    last = df.iloc[-1]

    f, m, s = last["ema_fast"], last["ema_mid"], last["ema_slow"]
    if f > m and m > s:
        trend = 1.0
    elif f < m and m < s:
        trend = -1.0
    elif f > m:
        trend = 0.5
    elif f < m:
        trend = -0.5
    else:
        trend = 0.0

    rsi_bias = 1.0 if last["rsi"] > 50 else (-1.0 if last["rsi"] < 50 else 0.0)
    macd_bias = (1.0 if last["macd_line"] > last["macd_sig"]
                 else (-1.0 if last["macd_line"] < last["macd_sig"] else 0.0))
    bb_bias = (1.0 if last["Close"] > last["bb_mid"]
               else (-1.0 if last["Close"] < last["bb_mid"] else 0.0))

    raw = trend * 1.5 + rsi_bias * 1.0 + macd_bias * 1.0 + bb_bias * 0.5
    direction_sign = 1.0 if raw > 0 else (-1.0 if raw < 0 else 0.0)
    if direction_sign == 0.0:
        return None

    weight = abs(raw)
    if last["adx"] >= cfg.ADX_MIN and np.sign(trend) == direction_sign:
        weight += 1.0
    if df_htf is not None and len(df_htf) > 0:
        htrend = _htf_trend(df_htf)
        if htrend != 0 and np.sign(htrend) == direction_sign:
            weight += 1.0

    cap = max(50.0, cfg.BROADCAST_MIN_CONFIDENCE - 1.0)
    confidence = min(cap, 50.0 + weight * 6.0)

    drivers = []
    if np.sign(trend) == direction_sign and trend != 0:
        drivers.append("Trend state")
    if rsi_bias == direction_sign:
        drivers.append("RSI side")
    if macd_bias == direction_sign:
        drivers.append("MACD side")
    if bb_bias == direction_sign:
        drivers.append("Price vs mid-band")
    if not drivers:
        drivers = ["quick read"]

    return {
        "direction": "CALL" if direction_sign > 0 else "PUT",
        "score": float(weight),
        "confidence": float(confidence),
        "tier": _tier(confidence),
        "entry": float(last["Close"]),
        "at": df.index[-1],
        "drivers": drivers,
        "quick_read": True,
    }


def confluence(df, asset_name="EURUSD", variants=None, df_htf=None):
    """Aggregate latest-bar votes across variants on the same chart.

    Returns the combined read: direction wins if >=2 variants agree (or one
    variant scores tier A), confidence is the mean of the agreeing votes.
    """
    variants = variants or cfg.VARIANTS
    votes = {}
    for v in variants:
        try:
            d = compute_signals(df, asset_name=asset_name, variant=v,
                                df_htf=df_htf)
            last = d.iloc[-1]
            votes[v] = {
                "dir": int(last["signal"]),
                "score": float(last["score"]),
                "conf": float(last["conf"]),
                "tier": str(last["tier"]),
            }
        except Exception:
            votes[v] = {"dir": 0, "score": 0.0, "conf": 0.0, "tier": "C"}
    nonzero = {v: o for v, o in votes.items() if o["dir"] != 0}
    if not nonzero:
        return {"direction": None, "confidence": 0.0, "tier": "C", "votes": votes}

    calls = sum(1 for o in nonzero.values() if o["dir"] > 0)
    puts = sum(1 for o in nonzero.values() if o["dir"] < 0)
    if calls == puts:
        best = max(nonzero.items(), key=lambda kv: abs(kv[1]["score"]))[1]
        if best["conf"] < 70:
            return {"direction": None, "confidence": 0.0, "tier": "C", "votes": votes}
        direction = "CALL" if best["dir"] > 0 else "PUT"
        agreeing = [best]
    else:
        direction = "CALL" if calls > puts else "PUT"
        agreeing = [o for o in nonzero.values() if (o["dir"] > 0) == (direction == "CALL")]

    conf = float(np.mean([o["conf"] for o in agreeing]))
    return {
        "direction": direction,
        "confidence": conf,
        "tier": _tier(conf),
        "votes": votes,
    }