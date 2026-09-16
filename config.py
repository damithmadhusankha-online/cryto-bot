"""Configuration for the Expert-Signal-Bot (v3).

All settings can be overridden via environment variables where noted.
"""
import os


def _load_dotenv():
    """Load KEY=VALUE pairs from .env next to this file (no external dep).
    Existing real environment variables always win."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError:
        pass


_load_dotenv()

# ---------------- Telegram ----------------
# Create a bot with @BotFather, then set:
#   TG_BOT_TOKEN -> the HTTP API token
#   TG_CHAT_ID   -> your admin channel/group/user numeric id
# Optional: TG_SUBSCRIBERS -> comma-separated extra chat ids that receive
# broadcasts (sellable access list). Managed automatically in
# subscribers.json once the bot is running.
TELEGRAM_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
TG_SUBSCRIBERS = os.environ.get("TG_SUBSCRIBERS", "")

# ---------------- Branding -------------
# Product identity shown on every card, menu and broadcast. Change BOT_NAME
# to rebrand the whole product in one place.
BOT_NAME = "CRYTO"                     # product short name
BOT_DISPLAY = "Cryto Signals"          # friendly product name
BOT_TAGLINE = "Real-time Pocket Option quick-trade signals on all markets"
BOT_URL = os.environ.get("TG_BOT_URL", "t.me/cryto_trader_bot")  # promo link
CARD_FOOTER = f"_Powered by {BOT_DISPLAY} \u00b7 verified live track record \u00b7 not financial advice_"

# Pocket Option deep link shown as a button on every signal card + the main
# menu, so a user can jump straight to placing the trade instead of hunting
# for the asset themselves. Set POCKET_OPTION_URL in .env to your own
# affiliate/referral link if you have one - defaults to the plain signup
# page. Never claim this link guarantees any result.
POCKET_OPTION_URL = os.environ.get("POCKET_OPTION_URL", "https://pocketoption.com/en/")

# ---------------- Profitability gates -------------
# Broadcast-time filters on top of the scan validation. A watchlist row only
# enters the live loop after the walk-forward scan proved it out-of-sample;
# these gates make the broadcast even more selective (fewer, stronger trades).
# Raised vs earlier build: breakeven at 85% payout is 54.05%, so a 55% floor
# left almost no margin for error once live spreads/slippage bite. 58%+
# and a bigger EV cushion means fewer pairs qualify, but the ones that do
# have real room above breakeven.
BROADCAST_MIN_WATCHLIST_EV = 0.15      # skip pairs with validated OOS EV below this
BROADCAST_MIN_WATCHLIST_WINRATE = 0.58 # and with a validated win rate below this
                                       # (breakeven at 85% payout = 54.05%)

# ---------------- Data ----------------
DATA_PERIOD_DAYS = 14

# Disk cache for fetched OHLCV. Big win for scans and the live loop.
CACHE_DIR = "data_cache"
CACHE_TTL_SECONDS = 60         # 1 min - so the live loop sees fresh M1 bars
CACHE_TTL_STALE = 86400 * 2    # fall back to a 2-day-old copy if fetch fails

# ---------------- Binary-option style expiry ----------------
# Expiry used for outcome resolution (in minutes of wall-clock time).
# One candle per timeframe: M1 = 1-min quick trade ... H1 = 60-min trade.
# This matches Pocket Option style quick-trade expiries (1m/5m/15m/30m/60m)
# and keeps the card label, the resolution and the backtest in sync.
EXPIRY_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60}

# User-facing expiry labels (broker style). Header short form + card words.
EXPIRY_SHORT = {"M1": "1 MIN", "M5": "5 MIN", "M15": "15 MIN",
                "M30": "30 MIN", "H1": "1 HOUR"}
EXPIRY_WORDS = {"M1": "1 minute", "M5": "5 minutes", "M15": "15 minutes",
                "M30": "30 minutes", "H1": "1 hour"}

# Assumed broker payout on a winning trade (0.85 = 85%).
# Breakeven win rate = 1 / (1 + PAYOUT) = 54.05% at 0.85.
PAYOUT = 0.85

# Spread model: a win is only credited when price clears the entry by more
# than half a typical spread (bps), both in the backtest AND in the live
# track record. Keeps stats honest vs broker quotes.
SPREAD_BPS = {"Forex": 0.8, "Commodities": 1.5, "Indices": 1.0,
               "Crypto": 2.0, "Stocks": 1.0}


def is_session_open(asset, now=None):
    """Heuristic 'is this asset currently tradeable' check.

    This is NOT a live feed from Pocket Option - it's an approximation
    from SESSION_UTC (already used per-bar in strategy.py) plus a weekday
    check: Forex/Commodities/Indices/Stocks are treated as closed on
    Saturday/Sunday UTC and outside their SESSION_UTC window; Crypto
    (SESSION_UTC = None) is always considered open, matching Pocket
    Option's 24/7 crypto + weekend-OTC-only offering for everything else.
    If you get access to Pocket Option's real live asset-availability
    endpoint, swap the body of this function to call it instead - that
    will always be more accurate than a fixed-hours heuristic (holidays,
    early closes, and broker-side OTC substitutions aren't modeled here).
    """
    import datetime as _dt
    now = now or _dt.datetime.now(_dt.timezone.utc)
    cat = ASSET_CATEGORY.get(asset)
    span = SESSION_UTC.get(cat)
    if span is None:
        return True  # crypto, or an unlisted category: treat as always open
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    lo, hi = span
    return lo <= now.hour < hi


def open_assets(now=None):
    """All configured assets currently open per is_session_open()."""
    return [a for a in ASSETS if is_session_open(a, now=now)]


def expiry_candles(tf):
    """Expiry expressed in candles of the timeframe."""
    return max(1, EXPIRY_MINUTES[tf] // TF_MINUTES[tf])


# ---------------- Strategy ----------------
EMA_FAST, EMA_MID, EMA_SLOW = 9, 21, 50
RSI_PERIOD = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
BB_PERIOD, BB_STD = 20, 2.0
ATR_PERIOD = 14

# ADX trend-strength filter (Wilder 14). momentum/breakout need enough trend.
ADX_PERIOD = 14
ADX_MIN = 18          # minimum ADX for breakout; momentum gets a boost >= 25
ADX_STRONG = 25

# Donchian breakout channel + volume confirmation.
DONCH_PERIOD = 20
VOL_SMA = 20
VOL_SURGE = 1.2       # breakout needs volume >= VOL_SURGE x its 20-bar average

# Strategy variants. Each emits event-driven signals (fresh crossovers /
# breakouts on the current bar), never persistent states.
#   momentum : trend continuation - EMA9/21 cross + MACD cross + RSI push
#              + fresh Bollinger break; must agree with EMA trend stack.
#   reversal : mean reversion - fade fresh Bollinger extremes + RSI turns.
#   breakout : range expansion - fresh Donchian break + volume surge +
#              EMA alignment + ADX gate.
VARIANTS = ["momentum", "reversal", "breakout"]

# |score| needed to fire, per variant (raised vs v3: marginal 2.0-reversal
# signals were broadcasting at the exact threshold - weak setups).
VAR_THRESHOLDS = {"momentum": 4.5, "reversal": 2.5, "breakout": 3.5}

# ---------------- Broadcast quality gates ----------------
# The live loop only broadcasts when the signal clears these bars, so
# subscribers get FEWER but STRONGER signals instead of threshold noise.
# Tightened: fewer signals/day, but each one clears a higher bar. This
# trades quantity for quality - the honest way to raise a live win rate.
BROADCAST_MIN_CONFIDENCE = 74.0   # absolute floor for any broadcast (was 68)
REQUIRE_CONFLUENCE = True         # require agreement across variants
CONFLUENCE_MIN_CONF = 65.0        # per-vote confidence must be >= this (was 60)
CONFLUENCE_MIN_AGREE = 2          # at least N variants must agree
CONFLUENCE_SINGLE_TIER_A = 82.0   # ...or one variant this confident (was 78)

# ---------------- Risk management rails ----------------
# Minimum gap between two broadcasts of the same pair (minutes). Prevents
# re-entry spam on the same move; also gives the price time to play out.
PAIR_COOLDOWN_MINUTES = 15

# Circuit breaker: pause a pair for CIRCUIT_PAUSE_HOURS after
# CIRCUIT_STREAK_LOSSES consecutive verified losses on it.
CIRCUIT_STREAK_LOSSES = 3
CIRCUIT_PAUSE_HOURS = 24

# Staleness guard: skip a pair when its last data bar is older than this
# (minutes). Yahoo 1m/5m feeds can lag - signals on stale bars are noise.
MAX_BAR_AGE_MINUTES = {"M1": 4, "M5": 8, "M15": 20, "M30": 40, "H1": 70}

# UTC trading-session filter per asset category: signals outside these
# hours are dropped. `None` = trade all day.
SESSION_UTC = {
    "Forex": (7, 21),        # London + NY overlap
    "Commodities": (13, 21), # US session
    "Indices": (13, 21),
    "Stocks": (13, 20),      # regular NYSE hours (9:30-16:00 ET)
    "Crypto": None,          # 24/7
}

# ---------------- Markets / assets ----------------
# Display name -> Yahoo Finance symbol.
# Pocket Option quick-trade coverage - majors + crosses + majors crypto +
# the most liquid commodity/equity index contracts.
FOREX = {
    "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X", "USDCAD": "USDCAD=X", "USDCHF": "USDCHF=X",
    "NZDUSD": "NZDUSD=X", "EURGBP": "EURGBP=X", "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X", "AUDJPY": "AUDJPY=X", "EURAUD": "EURAUD=X",
    "EURCHF": "EURCHF=X", "NZDJPY": "NZDJPY=X", "GBPCHF": "GBPCHF=X",
    "EURNZD": "EURNZD=X", "CADJPY": "CADJPY=X", "CHFJPY": "CHFJPY=X",
}
COMMODITIES = {
    "XAUUSD": "GC=F",   # gold futures (proxy for XAU/USD)
    "XAGUSD": "SI=F",   # silver
    "WTIOIL": "CL=F",   # crude oil (WTI) - no underscore: breaks Telegram's
                        # legacy Markdown parser if inserted unescaped
    "BRENT": "BZ=F",
    "NATGAS": "NG=F",   # natural gas
    "COPPER": "HG=F",   # copper
}
CRYPTO = {
    "BTCUSD": "BTC-USD", "ETHUSD": "ETH-USD",
    "SOLUSD": "SOL-USD", "XRPUSD": "XRP-USD",
    "DOGEUSD": "DOGE-USD", "ADAUSD": "ADA-USD",
    "LTCUSD": "LTC-USD", "BNBUSD": "BNB-USD",
    "PEPEUSD": "PEPE-USD", "SHIBUSD": "SHIB-USD",
    "AVAXUSD": "AVAX-USD", "LINKUSD": "LINK-USD",
    "DOTUSD": "DOT-USD", "TRXUSD": "TRX-USD",
}
INDICES = {
    "SPX500": "^GSPC", "NAS100": "^IXIC",
    "DJI30": "^DJI", "DAX40": "^GDAXI",
    "FTSE100": "^FTSE", "NIKKEI225": "^N225",
}

# Stock CFDs - the biggest Pocket Option category besides forex/crypto.
# Yahoo symbols track the underlying equity; the session filter keeps signals
# inside regular US market hours where spreads and liquidity are best.
STOCKS = {
    "AAPL": "AAPL", "TSLA": "TSLA", "NVDA": "NVDA",
    "AMZN": "AMZN", "META": "META", "GOOGL": "GOOGL",
    "MSFT": "MSFT", "AMD": "AMD", "NFLX": "NFLX",
    "JPM": "JPM", "COIN": "COIN", "RIVN": "RIVN",
}

ASSETS = {**FOREX, **COMMODITIES, **CRYPTO, **INDICES, **STOCKS}
ASSET_CATEGORY = {}
for _cat, _map in (("Forex", FOREX), ("Commodities", COMMODITIES),
                   ("Crypto", CRYPTO), ("Indices", INDICES),
                   ("Stocks", STOCKS)):
    for _k in _map:
        ASSET_CATEGORY[_k] = _cat

TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1"]
DATA_INTERVAL_MAP = {"M1": "1m", "M5": "5m", "M15": "15m", "M30": "30m", "H1": "1h"}
TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60}

# ---------------- Scanner / validation ----------------
SCAN_WINDOW_DAYS = 21      # lookback for the market ranking scan
SCAN_MIN_TRADES = 15       # minimum signals (train + OOS) to even consider
SCAN_MIN_OOS_TRADES = 10   # minimum OUT-OF-SAMPLE signals to rank a pair (was 8;
                           # more OOS trades = a less noisy win-rate estimate)
SCAN_REQUIRE_CONSISTENT = True   # both halves of the window must be +EV
SCAN_REQUIRE_CI_LO = 0.03        # OOS EV bootstrap lower bound must be > this (was 0.02)
SCAN_TOP_N = 15           # how many top pairs the scan reports by default
                          # (broad all-market coverage for quick trading)
WATCHLIST_FILE = "watchlist.csv"

# Walk-forward validation: the first WF_TRAIN_FRAC of the window is treated as
# context; ranking uses ONLY the out-of-sample tail, so a lucky streak in the
# recent window cannot drive selection.
WF_TRAIN_FRAC = 0.6

# Monte Carlo bootstrap iterations for win-rate/EV confidence intervals.
MC_ITERATIONS = 2000

# Blended rank score used to order scan results:
#   oos_ev * RANK_W_OOS + full_ev * RANK_W_EV + ci_lower * RANK_W_CI
#   + consistency * RANK_W_CONSISTENT
RANK_W_OOS = 1.0
RANK_W_EV = 0.4
RANK_W_CI = 0.6
RANK_W_CONSISTENT = 0.5

# ---------------- Signal tracking / subscriptions ----------------
SIGNAL_LOG = "signal_log.jsonl"   # every broadcast signal, append-only
SUBSCRIBERS_FILE = "subscribers.json"

# Subscription system. FREE users get FREE_DAILY_SIGNALS *winning* signals
# per day - every qualifying signal is still sent to them, but only a
# verified WIN (once the trade resolves against live market data) counts
# against the quota, so a losing trade never eats into their daily
# allowance. PRO users get unlimited signals + advanced indicator cards,
# portfolio tracking and AI analysis. Plans are stored in SUBSCRIPTIONS_FILE
# and are granted by the admin (TG_CHAT_ID) via /grantpro.
FREE_DAILY_SIGNALS = 5
SUBSCRIPTIONS_FILE = "subscriptions.json"

# Contact shown to FREE users once their daily win quota is used up, and
# anywhere else the bot points someone at the admin to upgrade to PRO.
ADMIN_CONTACT = os.environ.get("ADMIN_CONTACT", "@madhux")

# /signal with no asset given scans every validated market at once ("burst"
# mode) and returns whichever currently clear the SAME quality gates as the
# broadcast loop (see signal_scan.py) - never more than actually qualify.
# FREE is capped by the shared daily quota above (so a burst still counts
# against FREE_DAILY_SIGNALS); PRO/admin get a larger single-call cap purely
# to keep one Telegram reply from flooding the chat, not a quality relaxation.
PRO_SIGNAL_BURST = 20

# ⚡ Instant Signal (on-demand, all-assets scan triggered from the menu):
# scans every configured Pocket Option asset - not just the narrow
# pre-ranked watchlist - across these timeframes, in parallel. Kept short
# (M1/M5) so a user tapping the button gets a real answer in a few
# seconds instead of a long wait; widen this list if you'd rather trade
# scan speed for broader timeframe coverage.
INSTANT_SCAN_TIMEFRAMES = ["M1", "M5"]
INSTANT_SCAN_MAX_WORKERS = 16

# If NOTHING clears the quality bar on a burst /signal scan, show this many
# of the closest non-qualifying readings instead of an empty result - always
# clearly labeled "unconfirmed reading", never presented as a validated
# signal, and excluded from the daily quota / verified track record. Set to
# 0 to go back to returning nothing when no market qualifies.
INSTANT_SCAN_NEAR_MISS_FALLBACK = 3

# News & Sentiment Engine
SENTIMENT_TTL_SECONDS = 15 * 60   # refresh news per asset at most this often
SENTIMENT_MAX_HEADLINES = 8       # headlines kept per asset
SENTIMENT_TIMEOUT = 8             # seconds per news fetch

# ---------------- Serve mode (product runner) ----------------
LOOP_INTERVAL_MINUTES = 1   # default sleep between loop scans (matches the
                            # 60s OHLCV cache TTL, so automatic broadcasts
                            # go out as soon as a fresh bar qualifies)
UPDATES_POLL_SECONDS = 2    # telegram command poll interval
DAILY_DIGEST_HOUR_UTC = 20  # broadcast live-performance digest at this hour
MAX_DAILY_SIGNALS_PER_PAIR = 8  # safety cap so loop mode can't spam

# ---------------- Admin web panel ----------------
# The admin dashboard (admin_web.py) runs on this host/port. Keep it bound
# to localhost unless you know what you are doing - it controls the product.
PANEL_HOST = os.environ.get("PANEL_HOST", "127.0.0.1")
PANEL_PORT = int(os.environ.get("PANEL_PORT", "8080"))
# Password for the admin panel. CHANGE THIS. If empty, the panel refuses to
# start so an accidentally exposed dashboard cannot run unauthenticated.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
# Public URL shown by /panel (set when you tunnel/port-forward, e.g. ngrok).
PANEL_PUBLIC_URL = os.environ.get("PANEL_PUBLIC_URL", "")

# ---------------- Monetization / plans ----------------
# Shown on the /plans and /upgrade cards (placeholder prices - set real ones).
PRO_PRICE_WEEK_USD = float(os.environ.get("PRO_PRICE_WEEK_USD", "9.99"))
PRO_PRICE_MONTH_USD = float(os.environ.get("PRO_PRICE_MONTH_USD", "24.99"))
PRO_PRICE_LIFETIME_USD = float(os.environ.get("PRO_PRICE_LIFETIME_USD", "99.00"))
# Link the user sees in /upgrade to actually pay you (PayPal, CoinsPaid,
# crypto address, ...). Leave empty to hide the payment section.
PAYMENT_LINK = os.environ.get("PAYMENT_LINK", "")
# Referral reward: when a referred user goes PRO, the referrer receives this
# many PRO days (capped at the value below).
REFERRAL_REWARD_DAYS = int(os.environ.get("REFERRAL_REWARD_DAYS", "7"))
REFERRAL_REWARD_MAX_DAYS = int(os.environ.get("REFERRAL_REWARD_MAX_DAYS", "30"))
REFERRALS_FILE = "referrals.json"

# ---------------- Scanner: all-market coverage + anti-overfit ----------------
# Win-rate sanity cap: a validated row with a PERFECT (or near-perfect) record
# on a small sample is almost certainly overfit noise, not an edge. Rows above
# this cap are dropped from the ranking/watchlist.
SCAN_MAX_WINRATE = 0.96
# Per-category coverage: save_watchlist picks the top N validated pairs from
# EACH market category (Forex/Crypto/Commodities/Indices/Stocks) so users
# receive profitable signals on ALL markets, not just one lucky asset.
SCAN_PER_CATEGORY_TOP = 4

# ---------------- Runtime state hygiene ----------------
# prune_state() keeps serve_state.json small: drop daily counters older than
# this and stale per-pair keys after this many days.
STATE_DAILY_RETENTION_DAYS = 7
STATE_PAIR_RETENTION_DAYS = 14