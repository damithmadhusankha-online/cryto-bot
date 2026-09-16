# Expert-Signal-Bot v3 — Handoff Report

**Date:** 2026-09-09 · **Host:** DAMITH (Windows) · **Bot:** @cryto_trader_bot (token from `.env`)

## Status: RUNNING ✅

- Process: `pythonw.exe` PID **20628** — `serve.py` product runner (3 threads)
- Watchlist: 6 entries (USDCAD, SOLUSD, GBPUSD, EURUSD) — signal loop scans every **2 min**
- Telegram delivery **verified**: test message `message_id 8` delivered to chat 5691208941 (`@madhux`, Damith Madhushanka)
- Startup notice "🟢 Signal Bot is online" sent on launch
- `serve.lock` + `serve_state.json` present in `C:\Users\DAMITH MADHUSHANKHA\signal-bot` — proves the loop ran and saved state

## Bugs fixed during this session

1. **`telegram_bot.py` AI analysis crash** — `ai_analysis(None, sig, …)` → `ai_analysis(sig.get("_df"), sig, …)` (PRO cards crashed when `_df` wasn't threaded through).
2. **`serve.py` startup crash (NameError)** — `_single_instance()` used `os.path` but `os` was never imported → bot died instantly on every launch. Added `import os`.
3. **`serve.py` single-instance lock leak** — the lock file handle was a local variable, so the lock was released as soon as `_single_instance()` returned (second instance could start). Now returns the handle and `main()` keeps it referenced for process lifetime.

## How to operate

| Action | Command |
|---|---|
| Start / restart | `cd "C:\Users\DAMITH MADHUSHANKHA\signal-bot"` then `pythonw serve.py` (or run `autostart_bot.bat`) |
| Stop | `taskkill /PID 20628 /F` (check current PID with `tasklist \| grep -i pythonw`) |
| Logs | `serve.log` (stdout/stderr — note: pythonw buffers, so it may be empty until a scan; state lives in `serve_state.json`) |
| Manual signal test | `python bot.py scan` or send `/signal` to the bot in Telegram |

### Telegram commands
- `/start` — welcome + subscription status
- `/help` — command list
- `/status` — bot health, watchlist, quota
- `/signal` — manual signal for a pair (admin)
- `/scan` — force watchlist scan (admin)
- `/subscribe` — manage plan (FREE = 5 signals/day, PRO = unlimited + real-time alerts + advanced indicators + portfolio tracking + AI analysis)
- `/grantpro <chat_id>` — admin-only: promote a user to PRO (stored in `subscriptions.json`)
- `/mystats` — personal signal history

## Auto-start (no admin rights needed)
Task Scheduler registration was blocked ("Access is denied"), so autostart uses the **Windows Startup folder**:
`C:\Users\DAMITH MADHUSHANKHA\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\signal_bot_autostart.vbs`
→ runs `autostart_bot.bat` hidden → `pythonw serve.py >> serve.log 2>&1`

The bot will start automatically at every Windows logon. To disable: delete the `.vbs` file.
(Optional: `register_task.bat` is kept in the bot folder for a future admin-elevated Task Scheduler setup.)

## Config (`.env`)
- `TG_BOT_TOKEN` / `TG_CHAT_ID` — set and working
- `LOOP_INTERVAL_MINUTES=2` — signal cadence
- `FREE_DAILY_SIGNALS=5` — FREE tier quota
- `DAILY_DIGEST_HOUR_UTC=20` — verified-performance digest + news briefing
- `CACHE_TTL` — yfinance disk/memory cache (data_cache/)

## Notes & caveats
- **`EURUSD,M1,momentum` watchlist row is unvalidated** — re-validate with `python bot.py scan`; if it errors, remove the row from `watchlist.csv`.
- First scan completed with no qualifying signals (strategy only fires when indicators align) — this is normal.
- `serve.log` may stay empty under `pythonw` until output flushes; monitor via `serve_state.json` mtime and `tasklist` instead.
- **Disclaimer:** trading-signal bots are experimental tools. Signals are generated from technical indicators + sentiment; **no signal generator guarantees profits**. Binary-options trading is illegal in many jurisdictions (see README). Use at your own risk — never trade money you can't afford to lose.