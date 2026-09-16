"""AI-style signal analysis (PRO feature).

Rule-based natural-language summary built from the live indicators + news
sentiment. No external AI API is required, so it is fully offline and free;
the text explains *why* the engine picked this direction, which is what
matters for a signal product.
"""
import numpy as np

import config as cfg
from indicators import add_all


def _fmt_pct(x):
    return f"{x:.1f}%"


def ai_analysis(df, sig, asset="EURUSD", tf="M1", variant="momentum",
                sentiment=None, df_htf=None):
    """Return a 2-3 sentence analysis for a live signal (never raises)."""
    try:
        df = add_all(df)
        last = df.iloc[-1]
        close = float(last["Close"])
        rsi = float(last["rsi"])
        adx = float(last["adx"])
        atr_pct = float(last["atr_pct"])
        macd_hist = float(last["macd_hist"])
        ema9, ema21, ema50 = (float(last["ema_fast"]), float(last["ema_mid"]),
                              float(last["ema_slow"]))
        bb_up, bb_low = float(last["bb_up"]), float(last["bb_low"])
        vol_ratio = float(last["vol_ratio"]) if not np.isnan(last["vol_ratio"]) else 1.0

        up = sig["direction"] == "CALL"
        trend = "bullish" if ema9 > ema21 > ema50 else ("bearish" if ema9 < ema21 < ema50 else "mixed")

        parts = []
        # 1. trend & momentum
        if up:
            if trend == "bullish":
                parts.append(f"{asset} trades above a rising EMA stack "
                             f"({trend} trend structure).")
            else:
                parts.append(f"Price is pressing higher against a {trend} "
                             f"trend structure - a counter-trend push.")
            if rsi >= 60:
                parts.append(f"RSI at {rsi:.0f} confirms strong buying "
                             f"momentum.")
            elif rsi >= 45:
                parts.append(f"RSI at {rsi:.0f} leaves room for upside "
                             f"before overbought.")
            else:
                parts.append(f"RSI at {rsi:.0f} shows a washed-out market "
                             f"with recovery potential.")
            if macd_hist > 0:
                parts.append("MACD histogram is positive, backing the bullish case.")
            else:
                parts.append("MACD is lagging - the move relies on price action.")
        else:
            if trend == "bearish":
                parts.append(f"{asset} trades below a declining EMA stack "
                             f"({trend} trend structure).")
            else:
                parts.append(f"Price is selling off against a {trend} trend "
                             f"structure - a counter-trend decline.")
            if rsi <= 40:
                parts.append(f"RSI at {rsi:.0f} confirms strong selling "
                             f"pressure.")
            elif rsi <= 55:
                parts.append(f"RSI at {rsi:.0f} shows room to fall before "
                             f"oversold.")
            else:
                parts.append(f"RSI at {rsi:.0f} suggests distribution into "
                             f"weakness.")
            if macd_hist < 0:
                parts.append("MACD histogram is negative, backing the bearish case.")
            else:
                parts.append("MACD is lagging - the move relies on price action.")

        # 2. volatility context
        if atr_pct >= 0.1:
            parts.append(f"Volatility is elevated (ATR {_fmt_pct(atr_pct)}), "
                         f"so the move can extend quickly.")
        elif atr_pct >= 0.04:
            parts.append(f"Volatility is normal (ATR {_fmt_pct(atr_pct)}).")
        else:
            parts.append(f"Volatility is low (ATR {_fmt_pct(atr_pct)}) - "
                         f"expect a faster reaction if volume returns.")
        if vol_ratio >= 1.2:
            parts.append(f"Volume is {vol_ratio:.1f}x its average, confirming "
                         f"participation.")

        # 3. trend strength + bands
        if adx >= cfg.ADX_STRONG:
            parts.append(f"ADX {adx:.0f} signals a strong trend in play.")
        elif adx >= cfg.ADX_MIN:
            parts.append(f"ADX {adx:.0f} shows a developing trend.")
        if up and close >= bb_up:
            parts.append("Price broke above the upper Bollinger band - early momentum, not yet exhausted.")
        if (not up) and close <= bb_low:
            parts.append("Price broke below the lower Bollinger band - early downside momentum.")

        # 4. news sentiment tie-in
        if sentiment:
            lab = sentiment.get("label", "Neutral")
            sc = sentiment.get("score", 0)
            hl = None
            if sentiment.get("headlines"):
                hl = sentiment["headlines"][0]["title"][:90]
            if sc >= 15:
                parts.append(f"News flow is {lab.lower()} ({sc:+.0f}); "
                             + (f"e.g. \"{hl}\"." if hl else "supporting the move."))
            elif sc <= -15:
                parts.append(f"News flow is {lab.lower()} ({sc:+.0f}); "
                             + (f"e.g. \"{hl}\"." if hl else "pressuring the asset."))
            else:
                parts.append("News sentiment is neutral - the technical setup is the primary driver.")

        text = " ".join(parts).strip()
        return text or "Setup detected on the current bar; see indicator card for details."
    except Exception as exc:
        return f"Analysis unavailable ({exc})."


def advanced_indicators_text(df):
    """PRO indicator block: RSI / MACD / EMA stack / ADX / ATR / S/R."""
    try:
        df = add_all(df)
        last = df.iloc[-1]
        rsi = float(last["rsi"])
        macd_line = float(last["macd_line"])
        macd_sig = float(last["macd_sig"])
        ema9, ema21, ema50 = (float(last["ema_fast"]), float(last["ema_mid"]),
                              float(last["ema_slow"]))
        adx = float(last["adx"])
        atr = float(last["atr"])
        atr_pct = float(last["atr_pct"])
        high20 = float(df["High"].tail(20).max())
        low20 = float(df["Low"].tail(20).min())
        stack = "bullish" if ema9 > ema21 > ema50 else (
                "bearish" if ema9 < ema21 < ema50 else "mixed")
        lines = [
            f"• RSI(14): {rsi:.1f}  |  ADX: {adx:.1f}",
            f"• MACD: {macd_line:+.4f} vs signal {macd_sig:+.4f} "
            f"({'bullish' if macd_line > macd_sig else 'bearish'})",
            f"• EMA stack: 9={ema9:.5f} / 21={ema21:.5f} / 50={ema50:.5f} "
            f"→ {stack}",
            f"• ATR: {atr:.5f} ({atr_pct:.3f}%)",
            f"• Support: {low20:.5f}  |  Resistance: {high20:.5f}",
        ]
        return "\n".join(lines)
    except Exception as exc:
        return f"Indicator block unavailable ({exc})"