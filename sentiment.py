"""News & Sentiment Engine.

Pulls relevant market headlines for an asset (Yahoo Finance news feed with a
Google News RSS fallback - no API keys needed), scores them with a financial
sentiment lexicon (-100 bearish .. +100 bullish), and caches the result so the
signal loop never blocks on news fetches.

Usage:
    from sentiment import sentiment_for
    s = sentiment_for("EURUSD")
    # -> {"asset": "EURUSD", "score": 62, "label": "Bullish",
    #     "headlines": [...], "updated": "...", "source": "yahoo|google|none"}
"""
import datetime as dt
import html
import json
import threading
import time
import xml.etree.ElementTree as ET

import requests

import config as cfg

# Google News RSS search per asset (group + pair words). No API key.
_QUERY_MAP = {
    "EURUSD": 'EUR/USD forex', "GBPUSD": 'GBP/USD forex', "USDJPY": 'USD/JPY forex',
    "AUDUSD": 'AUD/USD forex', "USDCAD": 'USD/CAD forex', "USDCHF": 'USD/CHF forex',
    "NZDUSD": 'NZD/USD forex', "EURGBP": 'EUR/GBP forex', "EURJPY": 'EUR/JPY forex',
    "GBPJPY": 'GBP/JPY forex', "AUDJPY": 'AUD/JPY forex', "EURAUD": 'EUR/AUD forex',
    "EURCHF": 'EUR/CHF forex', "NZDJPY": 'NZD/JPY forex',
    "XAUUSD": 'gold price', "XAGUSD": 'silver price',
    "WTIOIL": 'WTI crude oil', "BRENT": 'Brent crude oil',
    "NATGAS": 'natural gas', "COPPER": 'copper price',
    "BTCUSD": 'Bitcoin', "ETHUSD": 'Ethereum', "SOLUSD": 'Solana crypto',
    "XRPUSD": 'XRP crypto', "DOGEUSD": 'Dogecoin', "ADAUSD": 'Cardano crypto',
    "LTCUSD": 'Litecoin', "BNBUSD": 'BNB crypto',
    "SPX500": 'S&P 500', "NAS100": 'Nasdaq',
    "DJI30": 'Dow Jones', "DAX40": 'DAX Germany',
}

POS_WORDS = {
    "surge", "surges", "rally", "rallies", "gain", "gains", "gained", "jump",
    "jumps", "soar", "soars", "soared", "climb", "climbs", "climbed", "rise",
    "rises", "rose", "high", "higher", "bullish", "breakout", "beats", "beat",
    "strong", "stronger", "strengthens", "strengthened", "upgrade", "upgrades",
    "outperform", "growth", "expansion", "rebound", "recovery", "optimistic",
    "positive", "support", "buy", "buys", "demand", "hike", "hikes", "hawkish",
    "boost", "momentum", "record", "best", "win", "wins", "hope", "hopes",
    "stable", "stabilizes", "rebounds", "firm", "firms",
}
NEG_WORDS = {
    "drop", "drops", "dropped", "fall", "falls", "fell", "slide", "slides",
    "slump", "slumps", "decline", "declines", "declined", "lose", "loses",
    "lost", "lower", "weaker", "weakens", "weakened", "bearish", "breakdown",
    "miss", "misses", "weak", "downgrade", "downgrades", "underperform",
    "contraction", "recession", "selloff", "sell-off", "sell", "sells",
    "pressure", "cut", "cuts", "tumble", "tumbles", "plunge", "plunges",
    "crash", "warning", "warnings", "uncertainty", "fears", "fear", "worry",
    "worries", "negative", "resistance", "selloff", "risk", "risks", "dovish",
    "slows", "slowdown", "weakness", "deficit", "debt", "default", "layoffs",
    "inflation", "hot", "overheated", "correction",
}
# context words that flip a word's meaning
_NEGATORS = {"not", "no", "below", "under", "fails", "lack", "despite"}

_cache = {}
_cache_lock = threading.Lock()


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _yf_sym(asset):
    return cfg.ASSETS.get(asset, "")


def _fetch_yahoo(asset):
    """Headlines via yfinance's built-in news feed (no key)."""
    try:
        import yfinance as yf
        t = yf.Ticker(_yf_sym(asset))
        items = t.news or []
        out = []
        for it in items[:cfg.SENTIMENT_MAX_HEADLINES]:
            title = str(it.get("title", "")).strip()
            if not title:
                continue
            ts = it.get("providerPublishTime") or it.get("publishTime")
            out.append({
                "title": title,
                "source": str(it.get("publisher", "Yahoo") or "Yahoo"),
                "ts": dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()
                     if isinstance(ts, (int, float)) else _now().isoformat(),
            })
        return out
    except Exception:
        return []


def _fetch_google(asset):
    """Headlines via Google News RSS search (no key)."""
    q = _QUERY_MAP.get(asset, asset)
    url = ("https://news.google.com/rss/search?q="
           + requests.utils.quote(f"{q} when:7d")
           + "&hl=en-US&gl=US&ceid=US:en")
    try:
        resp = requests.get(url, timeout=cfg.SENTIMENT_TIMEOUT,
                            headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        out = []
        for item in root.iter("item"):
            title = html.unescape(item.findtext("title") or "").strip()
            if not title:
                continue
            pub = item.findtext("pubDate") or ""
            try:
                ts = dt.datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %Z")
                ts = ts.replace(tzinfo=dt.timezone.utc)
            except (ValueError, TypeError):
                ts = _now()
            out.append({
                "title": title,
                "source": "Google News",
                "ts": ts.isoformat(),
            })
        return out[:cfg.SENTIMENT_MAX_HEADLINES]
    except Exception:
        return []


def _score_news(headlines):
    """Aggregate sentiment in [-100, +100]; 0 when there is no news."""
    if not headlines:
        return 0.0, "No news"
    total = 0.0
    n = 0
    for h in headlines:
        words = set(h["title"].lower().split())
        pos = sum(1 for w in words if w in POS_WORDS)
        neg = sum(1 for w in words if w in NEG_WORDS)
        # crude negation flip: "no drop" => positive
        if words & _NEGATORS and neg:
            pos, neg = neg, pos
        total += (pos - neg)
        n += 1
    score = max(-100.0, min(100.0, total / max(n, 1) * 45.0))
    return score, _label(score)


def _label(score):
    if score >= 15:
        return "Bullish"
    if score <= -15:
        return "Bearish"
    return "Neutral"


def sentiment_for(asset, force=False):
    """Cached sentiment snapshot for an asset. Never raises.

    On total news failure returns a neutral snapshot so the signal card still
    renders (with the failure noted in `source`).
    """
    if asset not in cfg.ASSETS:
        return {"asset": asset, "score": 0.0, "label": "Neutral",
                "headlines": [], "updated": _now().isoformat(),
                "source": "none", "error": "unknown asset"}
    with _cache_lock:
        hit = _cache.get(asset)
    if hit and not force and (time.time() - hit[1]) < cfg.SENTIMENT_TTL_SECONDS:
        return hit[0]

    headlines = _fetch_yahoo(asset) or _fetch_google(asset)
    score, label = _score_news(headlines)
    snap = {
        "asset": asset,
        "score": score,
        "label": label,
        "headlines": headlines[:cfg.SENTIMENT_MAX_HEADLINES],
        "updated": _now().isoformat(),
        "source": "yahoo" if headlines and "Yahoo" in [h["source"] for h in headlines]
                  else ("google" if headlines else "none"),
    }
    if not headlines:
        snap["error"] = "news feed unavailable"
    with _cache_lock:
        _cache[asset] = (snap, time.time())
    return snap


def sentiment_line(snap):
    """One-line card snippet: '📰 Sentiment: Bullish +62'."""
    if not snap:
        return "📰 Sentiment: --"
    arrow = "🟢" if snap["score"] >= 15 else ("🔴" if snap["score"] <= -15 else "⚪")
    return (f"📰 Sentiment: {snap['label']} {arrow} "
            f"{snap['score']:+.0f}")


def top_headline(snap, limit=1):
    if not snap or not snap.get("headlines"):
        return None
    return snap["headlines"][0]["title"]


def briefing_text(assets=None, max_assets=6):
    """Telegram briefing of current sentiment across markets."""
    assets = assets or list(cfg.ASSETS)
    lines = ["*📰 Market News & Sentiment*", ""]
    for asset in assets[:max_assets]:
        snap = sentiment_for(asset)
        lines.append(f"*{asset}*  {sentiment_line(snap)}")
        h = top_headline(snap)
        if h:
            lines.append(f"   - {h[:110]}")
        elif snap.get("error"):
            lines.append("   - (no news feed available)")
    lines += ["", "_News auto-scored by keyword sentiment. For context only._"]
    return "\n".join(lines)


def refresh_all(assets=None):
    """Force-refresh the cache for a set of assets (used by the digest)."""
    assets = assets or list(cfg.ASSETS)
    for a in assets:
        try:
            sentiment_for(a, force=True)
        except Exception:
            pass