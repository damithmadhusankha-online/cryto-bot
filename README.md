# Expert Signal Bot (v3.3 - bugfixes: /markets, PRO quota display)

**Bugs fixed in this update:**
- **`/markets` returned nothing.** The asset code `WTI_OIL` had a single,
  unpaired underscore. Telegram's legacy Markdown parser rejects the
  *entire* message when it can't match a formatting character like `_`,
  `*`, `` ` `` or `[` - so the whole `/markets` reply (and any signal
  card for that asset) silently failed to send. Renamed it to `WTIOIL`
  (config.py, sentiment.py) and added a `md_escape()` helper, applied to
  Telegram usernames/names wherever they're interpolated into a message
  (`/user`, `/users`) since real usernames very commonly contain
  underscores too and would hit the exact same failure.
- **PRO/admin showed "8 signals left" instead of unlimited.**
  `remaining_today()` only special-cased `is_pro()`, not `is_admin()` -
  so the admin account (or anyone whose "PRO" badge came from admin
  status rather than an explicit `/grantpro`) displayed a FREE-plan quota
  next to a PRO badge. `remaining_today()` now returns "unlimited" (None)
  for admin too, and the `/pro` status line now keys off that return
  value directly instead of re-checking `is_pro()` separately, so the
  two can't disagree again.

**What changed in this update:**
- ⚡ **Instant Signal now scans every configured Pocket Option asset**
  (`config.ASSETS` - forex, crypto, commodities, indices, stocks), not
  just the narrow pre-ranked watchlist. It runs the same real-time
  confluence + confidence gates on all of them in parallel
  (`signal_scan.scan_live_all`, `INSTANT_SCAN_TIMEFRAMES` in `config.py`).
  Markets that also happen to be on the walk-forward-validated watchlist
  still show the historical edge/win-rate line; markets outside it are
  clearly labeled as a live-only signal with no track record yet - never
  presented as more proven than they are.
- The bot now replies **immediately** when you tap Instant Signal, then
  edits that same message into a **real progress countdown** ("47/112
  checked") as the scan actually runs, then sends whichever markets
  qualify as soon as they're found. The countdown reflects genuine work
  in progress - it's not an artificial delay or a manufactured "urgency"
  timer.
- Previous update (v3.1): tap-to-use main menu, one-tap "Trade on Pocket
  Option" button on every card, `LOOP_INTERVAL_MINUTES` lowered to 1.

**A note on the countdown:** it exists so you're not staring at a blank
chat while ~100+ markets get checked - it is not there to manufacture
suspense or pressure anyone into trading. If you want it faster, narrow
`INSTANT_SCAN_TIMEFRAMES` in `config.py` to just `["M1"]`.

Event-driven trading signal engine covering **Forex, Crypto, Commodities and
Indices** (22 assets x 5 timeframes x 3 strategies), delivered in real time
through a Telegram bot - with a **verified live track record** (not just
backtests).

## What's inside

| File | Purpose |
|---|---|
| `strategy.py` | 3 event-driven variants: `momentum`, `reversal`, `breakout` (+ HTF confirmation) |
| `backtester.py` | Fixed-expiry binary-style backtest + walk-forward + Monte Carlo CI |
| `scanner.py` | Ranks asset x timeframe x strategy by out-of-sample expectancy |
| `data_fetcher.py` | Yahoo Finance data with disk cache + retry/stale fallback |
| `signal_log.py` | Append-only log of every broadcast signal; resolves outcomes after expiry |
| `telegram_bot.py` | Broadcaster + command responder (/signal /scan /status /mystats /subscribe) |
| `serve.py` | **Product mode** - signal loop + command bot + daily digest in one process |
| `bot.py` | CLI: `scan`, `signal`, `verify`, `loop` |

## Setup

```bat
pip install -r requirements.txt
```

Create a bot with @BotFather, then set:

```bat
set TG_BOT_TOKEN=123456:ABC...
set TG_CHAT_ID=-100123456789   :: your channel/group/user numeric id
```

Windows: copy `.env.example` notes into a `setenv.bat` or set them in System
Properties. macOS/Linux: `export TG_BOT_TOKEN=...`.

## Commands

```bat
:: rank markets (walk-forward validated), saves watchlist.csv
python bot.py scan --top 5

:: one live signal
python bot.py signal EURUSD M5 --variant momentum
python bot.py signal XAUUSD M15 --variant breakout --htf --send --chart

:: evaluate ALL variants + confluence on one chart
python bot.py signal BTCUSD M15 --all

:: resolve expired signals and show the verified track record
python bot.py verify --send

:: product mode: watchlist loop + telegram commands + daily digest
python serve.py
```

In Telegram: `/signal EURUSD M5`, `/signal XAUUSD M15 breakout`, `/scan`,
`/markets`, `/status`, `/mystats`, `/subscribe`, `/help`. Every signal card
has a **Chart** button.

## Selling mode

1. Run `python bot.py scan --top 10` and keep the watchlist tight.
2. Run `python serve.py` - it broadcasts every qualifying signal to your
   admin chat **plus** everyone in `subscribers.json` (`/subscribe` adds
   people automatically, or pre-seed the file).
3. `/mystats` shows the *verified* live win rate. **Broadcast that**, not a
   backtest - that is the only honest marketing you can defend.

## Be honest about performance - read this before selling

- **No bot can guarantee a 90%+ win rate.** Anyone selling that is lying.
  Real market regimes change; every strategy has losing streaks.
- At an 85% payout the **breakeven win rate is 54%** - you need to be
  meaningfully above that to profit. Realistic good-strategy windows: 55-70%
  depending on market regime. Expect losing days and losing weeks.
- The scanner ranks markets by *out-of-sample* expectancy with confidence
  intervals precisely so a lucky recent window can't be sold as an edge.
- Signals are "CALL/PUT with a fixed expiry" in the style used by binary
  brokers. **Binary options are banned for retail in many jurisdictions**
  (EU, UK, Australia, Canada, US and others). Signal-selling may also be
  regulated as financial advice in your country. Check local law before
  monetizing; never fabricate track records.
- This tool is for research and personal use. Risk only what you can afford
  to lose. Nothing here is financial advice.

## How the math works

Per trade with payout P: win contributes `+P`, loss `-1` stake.
Expectancy per trade = `win_rate * P - (1 - win_rate)`.
At P=0.85 you need win_rate > 54.05% to make money long-term.

## Files you get

- `watchlist.csv` - top-N markets (recreated by `scan`)
- `signal_log.jsonl` - the verified track record (append-only)
- `subscribers.json` - chat ids that receive broadcasts
- `sent_state.json` / `serve_state.json` - dedup bookkeeping
- `data_cache/` - fetched OHLCV cache (safe to delete, rebuilt on demand)