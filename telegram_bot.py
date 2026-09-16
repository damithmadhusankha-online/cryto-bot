"""Telegram integration (raw HTTP API): broadcaster + command responder.

- broadcast(): posts to the admin chat AND every subscriber chat id, so the
  bot can be sold as an access product (subscribers.json).
- broadcast_signal(): plan-aware signal delivery - FREE users get the compact
  card, capped at cfg.FREE_DAILY_SIGNALS *winning* signals/day (losses don't
  count against the cap, only a verified win does), PRO users get the full
  card with advanced indicators + AI analysis, unlimited.
- Command responder (process_updates): /start /plans /pro /subscribe
  /unsubscribe /portfolio /news /markets /signal /scan /status /mystats
  /top /calc /grantpro /revokepro /help, plus a "Chart" inline button.
"""
import datetime as dt
import json
import os
import time

import requests

import bot_control as bc
import config as cfg

# ---------------- low-level API ----------------


def configured():
    return bool(cfg.TELEGRAM_BOT_TOKEN)


def _api(method, token):
    return f"https://api.telegram.org/bot{token}/{method}"


_MD_SPECIAL = ("_", "*", "`", "[")


def md_escape(text):
    """Escape characters that break Telegram's legacy Markdown parser when
    dynamic text (a Telegram @username, display name, or asset code) is
    interpolated into a formatted message. An unmatched '_', '*', '`' or
    '[' anywhere in the message causes Telegram to reject the WHOLE
    message with a 400 (\"can't parse entities\") - which looks like the
    bot silently gave no reply. Usernames very commonly contain
    underscores, so this must be applied anywhere a username/name is
    interpolated, not just asset codes."""
    if text is None:
        return text
    text = str(text)
    for ch in _MD_SPECIAL:
        text = text.replace(ch, "\\" + ch)
    return text


# ---------------- main menu (tap instead of type) ----------------
# A persistent reply-keyboard so a new user never has to learn command
# syntax - tapping "Instant Signal" sends the same text as typing /signal.
# Telegram delivers a tap as a plain text message, so process_updates()
# maps the label back to its command below.
MAIN_MENU_ROWS = [
    ["\u26a1 Instant Signal", "\U0001f525 Hot Markets"],
    ["\U0001f4c8 Markets", "\U0001f4ca My Stats"],
    ["\U0001f451 Plans", "\u2753 Help"],
]
MENU_LABEL_TO_CMD = {
    "\u26a1 Instant Signal": "/signal",
    "\U0001f525 Hot Markets": "/top",
    "\U0001f4c8 Markets": "/markets",
    "\U0001f4ca My Stats": "/mystats",
    "\U0001f451 Plans": "/plans",
    "\u2753 Help": "/help",
}


def _main_menu_markup():
    return {"keyboard": MAIN_MENU_ROWS, "resize_keyboard": True,
            "is_persistent": True}


def send_message(text, photo_path=None, chat_id=None, token=None,
                 buttons=None, parse_mode="Markdown", menu=False):
    """Send text (optionally with a chart photo, inline buttons, or the
    persistent tap-to-use main menu when menu=True)."""
    chat_id = chat_id or cfg.TELEGRAM_CHAT_ID
    token = token or cfg.TELEGRAM_BOT_TOKEN
    if not chat_id or not token:
        return {"ok": False, "error": "Telegram not configured (set TG_BOT_TOKEN)"}
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if menu:
        payload["reply_markup"] = json.dumps(_main_menu_markup())
    elif buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    try:
        if photo_path:
            photo_data = {"chat_id": chat_id, "caption": text,
                         "parse_mode": parse_mode}
            if "reply_markup" in payload:
                photo_data["reply_markup"] = payload["reply_markup"]
            with open(photo_path, "rb") as fh:
                resp = requests.post(
                    _api("sendPhoto", token), timeout=25,
                    data=photo_data,
                    files={"photo": fh},
                )
            result = resp.json()
            if not result.get("ok") and parse_mode:
                # Markdown entity error (unescaped _ * ` [ somewhere) makes
                # Telegram reject the WHOLE message - retry once as plain
                # text so the user gets a reply instead of silence.
                photo_data.pop("parse_mode", None)
                with open(photo_path, "rb") as fh:
                    resp = requests.post(
                        _api("sendPhoto", token), timeout=25,
                        data=photo_data,
                        files={"photo": fh},
                    )
                result = resp.json()
            return result
        else:
            resp = requests.post(_api("sendMessage", token), timeout=25, json=payload)
            result = resp.json()
            if not result.get("ok") and parse_mode:
                plain_payload = dict(payload)
                plain_payload.pop("parse_mode", None)
                resp = requests.post(_api("sendMessage", token), timeout=25,
                                    json=plain_payload)
                result = resp.json()
            return result
    except requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


# ---------------- subscribers ----------------


def edit_message(chat_id, message_id, text, buttons=None, parse_mode="Markdown"):
    """Edit a previously sent message - used to turn the initial 'scanning
    now' acknowledgment into a live, real progress countdown instead of
    sending a stream of new messages."""
    token = cfg.TELEGRAM_BOT_TOKEN
    if not token or not message_id:
        return {"ok": False}
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text,
              "parse_mode": parse_mode}
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    try:
        resp = requests.post(_api("editMessageText", token), timeout=15, json=payload)
        result = resp.json()
        if not result.get("ok"):
            plain_payload = dict(payload)
            plain_payload.pop("parse_mode", None)
            resp = requests.post(_api("editMessageText", token), timeout=15,
                                json=plain_payload)
            result = resp.json()
        return result
    except requests.RequestException as exc:
        return {"ok": False, "error": str(exc)}


def _subs_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), cfg.SUBSCRIBERS_FILE)


def load_subscribers():
    ids = []
    extra = [x.strip() for x in cfg.TG_SUBSCRIBERS.split(",") if x.strip()]
    ids.extend(extra)
    try:
        with open(_subs_path(), encoding="utf-8") as fh:
            ids.extend(json.load(fh))
    except (OSError, json.JSONDecodeError):
        pass
    seen = set()
    out = []
    for cid in ids:
        if cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out


def save_subscribers(ids):
    with open(_subs_path(), "w", encoding="utf-8") as fh:
        json.dump(list(dict.fromkeys(ids)), fh, indent=2)


def add_subscriber(chat_id):
    ids = load_subscribers()
    if str(chat_id) not in ids:
        ids.append(str(chat_id))
        save_subscribers(ids)
        return True
    return False


def remove_subscriber(chat_id):
    ids = load_subscribers()
    if str(chat_id) in ids:
        save_subscribers([x for x in ids if x != str(chat_id)])
        return True
    return False


def broadcast(text, photo_path=None, buttons=None):
    """Send to the admin chat + every subscriber. Returns list of results."""
    targets = [cfg.TELEGRAM_CHAT_ID] if cfg.TELEGRAM_CHAT_ID else []
    for cid in load_subscribers():
        if cid not in targets:
            targets.append(cid)
    results = []
    for cid in targets:
        results.append(send_message(text, photo_path=photo_path, chat_id=cid,
                                    buttons=buttons))
        time.sleep(0.25)
    return results


def broadcast_signal(sig, asset, tf, variant="momentum", sentiment=None,
                     chart_photo=None):
    """Plan-aware broadcast of one signal.

    Returns (sent_to, free_blocked) - how many chats got it and how many
    FREE users were skipped because they hit the daily cap.
    """
    from subscriptions import (auto_signals_enabled, is_admin, is_pro,
                               plan_name, record_signal, remaining_today,
                               should_notify_upgrade, signal_allowed,
                               today_used)
    from analysis import advanced_indicators_text, ai_analysis

    signal_ts = dt.datetime.now(dt.timezone.utc).isoformat()
    sent_to = 0
    blocked = []

    def _send(cid, pro):
        if pro:
            card = format_signal(sig, asset, tf, variant, pro=True,
                                 sentiment=sentiment,
                                 ai_text=ai_analysis(sig.get("_df"), sig, asset, tf,
                                                     variant, sentiment),
                                 indicators_text=advanced_indicators_text(sig.get("_df")))
        else:
            card = format_signal(sig, asset, tf, variant, pro=False,
                                 sentiment=sentiment)
        return send_message(card, photo_path=chart_photo, chat_id=cid,
                            buttons=signal_buttons(asset, tf, variant))

    # 1) admin chat always receives the full card
    if cfg.TELEGRAM_CHAT_ID:
        _send(cfg.TELEGRAM_CHAT_ID, pro=True)
        sent_to += 1

    # 2) subscribers according to their plan
    for cid in load_subscribers():
        if str(cid) == str(cfg.TELEGRAM_CHAT_ID):
            continue
        if not auto_signals_enabled(cid):
            continue  # opted out of automatic broadcasts with /autosignal off
        if not signal_allowed(cid):
            blocked.append((cid, plan_name(cid), today_used(cid)))
            if should_notify_upgrade(cid):
                try:
                    send_message(_upgrade_text(), chat_id=cid)
                except Exception:
                    pass
            continue
        pro = is_pro(cid)
        ok = _send(cid, pro).get("ok", False)
        if ok:
            record_signal(cid, signal_ts, asset, tf, variant,
                          sig["direction"], sig["entry"])
            sent_to += 1
        time.sleep(0.2)

    # log the signal once for the verified track record
    if sent_to:
        from signal_log import log_signal
        log_signal(asset, tf, variant, sig)

    return sent_to, blocked


# ---------------- signal card formatting ----------------

def market_display(asset):
    """Display name: BTCUSD -> BTC/USD, XAUUSD -> XAU/USD, NAS100 stays."""
    if asset.endswith("USD") and len(asset) > 3:
        return asset[:-3] + "/USD"
    return asset


def _arrow(direction, pro=False):
    if direction == "CALL":
        return "BUY 🟢"
    return "SELL 🔴"


def _fmt_entry(entry):
    """6-decimal entry (like 1.164770), enough for forex, gold and crypto."""
    return f"{float(entry):.6f}"


def _conf_bar(confidence):
    """Visual 10-block confidence bar: 78 -> ▰▰▰▰▰▰▰▱▱▱."""
    conf = max(0, min(100, int(round(float(confidence)))))
    return "▰" * (conf // 10) + "▱" * (10 - conf // 10)


def _watchlist_edge(asset, tf):
    """Validated out-of-sample stats for (asset, tf) from the scan watchlist.
    Returns None when the pair has no validated positive edge."""
    try:
        from scanner import watchlist_rows
        row = watchlist_rows().get((asset, tf))
        if row and row["oos_ev"] > 0:
            return row
    except Exception:
        pass
    return None


def format_signal(sig, asset, tf, variant="momentum", pro=False,
                  sentiment=None, ai_text=None, indicators_text=None,
                  extra_note=None, qualified=True):
    """Professional signal card (Pocket Option quick-trade style).

    FREE -> compact: direction, entry, expiry, confidence, drivers, sentiment.
    PRO  -> full card: adds the validated edge line, advanced indicator block
            and AI analysis - the selling points of the product.

    `qualified=False` renders this as an unconfirmed reading (didn't clear
    the confidence/confluence bar) instead of a validated signal - the
    header and emoji change so it can never be mistaken for a real card at
    a glance.
    """
    display = market_display(asset)
    expiry_short = cfg.EXPIRY_SHORT.get(tf, "30 SEC")
    expiry_words = cfg.EXPIRY_WORDS.get(tf, "30 seconds")
    ts = sig["at"]
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    ts_utc = ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    confidence = int(round(float(sig["confidence"])))
    cat = cfg.ASSET_CATEGORY.get(asset, "Markets")
    emoji = "🟢" if sig["direction"] == "CALL" else "🔴"

    header_word = "SIGNAL" if qualified else "UNCONFIRMED READING"
    lines = [
        f"*{cfg.BOT_NAME} {header_word}* · {cat}" if qualified else
        f"\u26a0\ufe0f *{cfg.BOT_NAME} {header_word}* · {cat}",
        f"{emoji} *{sig['direction']}* — {display} · {expiry_short}",
        "",
        f"Entry: `{_fmt_entry(sig['entry'])}`",
        f"Expiry: {expiry_words} (quick trade)",
        f"Confidence: {_conf_bar(confidence)} {confidence}/100",
    ]

    # variant + drivers line (compact)
    drivers = sig.get("drivers") or [variant]
    lines += ["", f"🎯 {variant.title()} · Tier {sig['tier']} · "
                  f"{' + '.join(drivers[:3])}"]

    # validated out-of-sample edge (PRO + admin only)
    edge = _watchlist_edge(asset, tf)
    if pro and edge:
        lines += ["", f"✅ Validated edge: {edge['oos_ev']:+.2f} EV/trade · "
                      f"{edge['win_rate']:.0%} win rate "
                      f"({edge['n_trades']} OOS trades)"]

    # news sentiment line
    if sentiment:
        from sentiment import sentiment_line
        lines.append(sentiment_line(sentiment))

    if pro and indicators_text:
        lines += ["", "📈 *Advanced Indicators*", indicators_text]
    if pro and ai_text:
        lines += ["", "🧠 *AI Analysis*", ai_text]
    if extra_note:
        lines += ["", extra_note]

    lines += ["", f"⏰ {ts_utc} · payout ~{cfg.PAYOUT:.0%} · "
                  f"break-even {100/(1+cfg.PAYOUT):.0f}%",
              cfg.CARD_FOOTER]
    return "\n".join(lines)


def signal_buttons(asset, tf, variant):
    return [
        [
            {"text": "📊 Chart", "callback_data": f"chart:{asset}:{tf}:{variant}"},
            {"text": "🔄 Re-check", "callback_data": f"signal:{asset}:{tf}:{variant}"},
        ],
        [
            {"text": f"🏦 Trade {market_display(asset)} on Pocket Option",
             "url": cfg.POCKET_OPTION_URL},
        ],
    ]


# ---------------- plans / help text ----------------

PLANS_TEXT = (
    f"*{cfg.BOT_DISPLAY}* - {cfg.BOT_TAGLINE}\n\n"
    "*FREE*\n"
    f"  \u2022 {cfg.FREE_DAILY_SIGNALS} winning signals per day, generated "
    "on request (losing trades never count against this \u2014 only a "
    "verified win does)\n"
    "  \u2022 All markets: forex, crypto, commodities, indices, stocks\n"
    "  \u2022 Quick-trade expiries 1m - 1h (Pocket Option ready)\n"
    "  \u2022 \u26a1 Instant Signal \u2014 scan every market on demand\n"
    "  \u2022 One-tap \"Trade on Pocket Option\" link on every signal\n"
    "  \u2022 Chart button on every signal\n"
    "\n"
    "*PRO* \U0001f451\n"
    "  \u2022 Unlimited signals on every market, every day\n"
    "  \u2022 Validated edge per market (walk-forward tested)\n"
    "  \u2022 Advanced indicator cards + AI analysis\n"
    "  \u2022 Portfolio tracking with live win rate & P&L\n"
    "  \u2022 Stake calculator (/calc) + hot markets (/top)\n"
    "  \u2022 Available as a 30-day plan or a one-time lifetime upgrade\n"
    "\n"
    "Send /start or /subscribe to begin receiving signals on FREE. "
    f"To upgrade to PRO, contact {cfg.ADMIN_CONTACT}."
)


def _upgrade_text():
    """Shown once a FREE user's daily WIN quota is used up."""
    return (f"⚠️ You've used your {cfg.FREE_DAILY_SIGNALS} winning FREE "
            f"signals for today (losing trades never count against this "
            f"quota). Upgrade to *PRO* for unlimited signals - contact "
            f"{cfg.ADMIN_CONTACT} to upgrade. 👑")


def _welcome_text(chat_id):
    from subscriptions import (is_pro, plan_name, pro_until, remaining_today)
    plan = plan_name(chat_id)
    left = remaining_today(chat_id)
    extra = ""
    if is_pro(chat_id):
        until = pro_until(chat_id)
        extra = f"\nPRO active {'until ' + until.strftime('%Y-%m-%d') if until else '(lifetime)'} 👑"
    elif left is not None:
        extra = f"\nSignals remaining today: {left}"
    return (
        f"*{cfg.BOT_DISPLAY}* 🔥\n\n"
        f"{cfg.BOT_TAGLINE}.\n"
        "Every signal is validated walk-forward on live market data before "
        "it reaches you - no noise, no pump groups.\n\n"
        f"Your plan: *{plan}*{extra}\n\n"
        f"{PLANS_TEXT}\n\n"
        "Commands: /signal · /news · /top · /calc · /portfolio · /help"
    )


HELP_TEXT = (
    f"*{cfg.BOT_DISPLAY} commands*\n\n"
    "Tip: use the menu buttons below instead of typing - "
    "*⚡ Instant Signal* = /signal, *🔥 Hot Markets* = /top, "
    "*📈 Markets* = /markets, *📊 My Stats* = /mystats, "
    "*👑 Plans* = /plans.\n\n"
    "`/signal` - scan ALL markets now, get every signal that currently "
    "clears the quality bar (FREE: up to your daily quota · PRO: up to "
    f"{cfg.PRO_SIGNAL_BURST} at once)\n"
    "`/signal ASSET TF [variant]` - live signal for one market (e.g. "
    "/signal EURUSD M1)\n"
    "`/markets` - list all supported assets\n"
    "`/news` - market news & sentiment snapshot\n"
    "`/top` - hottest markets right now (validated edge + live track record)\n"
    "`/calc BALANCE [risk%] [payout%]` - PRO stake calculator\n"
    "`/plans` - FREE vs PRO comparison\n"
    "`/ref` - your referral link (earn PRO days)\n"
    "`/pro` - your subscription status\n"
    "`/portfolio` - PRO portfolio tracking\n"
    "`/status` - bot health + today's activity\n"
    "`/mystats` - verified live track record + streaks\n"
    "`/scan` - re-rank markets (takes a while)\n"
    "`/subscribe` / `/unsubscribe` - start / stop signals\n"
    "`/autosignal on|off` - toggle automatic background-loop signals for "
    "you personally (`/signal` always works regardless)\n"
    "`/help` - this menu\n\n"
    "*Admin only*\n"
    "`/users` - list all users + plans + expiring PRO\n"
    "`/user <@username|CHAT_ID>` - user detail\n"
    "`/grantpro <@username|CHAT_ID> [days]` - activate PRO: give a day "
    "count for a term plan (e.g. `/grantpro @john 30` for 30 days) or "
    "omit it for a lifetime upgrade\n"
    "`/revokepro <@username|CHAT_ID>` - revoke PRO\n"
    "`/broadcast <text>` - announce to every subscriber\n"
    "`/autosignal all on|off` - global on/off switch for the automatic "
    "loop (everyone)\n"
    "`/panel` - admin web panel link\n\n"
    "Variants: momentum, reversal, breakout. "
    "Timeframes: M1 M5 M15 M30 H1.\n\n"
    "_Confidence and \"validated edge\" come from walk-forward tested, "
    "out-of-sample stats - not a promise. No signal service can guarantee "
    f"a win rate; breakeven at {cfg.PAYOUT:.0%} payout is "
    f"{100/(1+cfg.PAYOUT):.1f}%. Trade only what you can afford to lose._"
)


# ---------------- command responder ----------------

def _handle_command(text, chat_id, ctx):
    """Return a list of (chat_id, text, photo_path, buttons) replies."""
    from subscriptions import (is_admin, is_pro, plan_name, grant_pro,
                               revoke_pro, portfolio_text, remaining_today)
    parts = text.strip().split()
    cmd = parts[0].lower()
    # Telegram appends "@YourBotName" to commands in groups (and sometimes
    # in DMs depending on client) - e.g. "/signal@my_signal_bot". Strip it
    # so the command still matches; this was silently failing every
    # command whenever it happened.
    cmd = cmd.split("@", 1)[0]
    args = parts[1:]

    if cmd in ("/start",):
        # /start auto-subscribes: the user immediately begins receiving
        # real-time signals on every market (FREE plan by default).
        # Deep links (t.me/<bot>?start=r<chat_id>) register the referral.
        if args:
            try:
                from subscriptions import register_referral
                register_referral(chat_id, args[0])
            except Exception:
                pass
        return [(chat_id, _subscribe_text(chat_id), None, "MENU")]
    if cmd in ("/help",):
        return [(chat_id, HELP_TEXT, None, "MENU")]
    if cmd == "/plans":
        return [(chat_id, PLANS_TEXT, None, None)]
    if cmd == "/ref":
        from subscriptions import referral_stats
        st = referral_stats(chat_id)
        return [(chat_id,
                 f"*\ud83d\udd17 Referral program*\n\n"
                 f"Share your link - when a friend goes PRO you earn PRO days!\n\n"
                 f"Your link: `{st['link']}`\n"
                 f"Invites: {st['total']}  |  PRO bonuses earned: {st['pro_grants']}\n\n"
                 f"_PRO bonus: {cfg.REFERRAL_REWARD_DAYS} days per paid friend "
                 f"(max {cfg.REFERRAL_REWARD_MAX_DAYS} days total)._",
                 None, None)]

    if cmd == "/panel":
        if not is_admin(chat_id):
            return [(chat_id, "Only the admin can view the panel link.", None, None)]
        url = cfg.PANEL_PUBLIC_URL or f"http://{cfg.PANEL_HOST}:{cfg.PANEL_PORT}"
        return [(chat_id, f"*\ud83d\udda5\ufe0f Admin panel*\n\n{url}\n\n"
                         f"Password is in `.env` (`ADMIN_PASSWORD`).", None, None)]

    if cmd == "/pro":
        from subscriptions import pro_until
        until = pro_until(chat_id)
        left = remaining_today(chat_id)
        status = (f"You are *{plan_name(chat_id)}*"
                  + (f" until {until.strftime('%Y-%m-%d')}" if until else "")
                  + (f" - {left} signals left today" if left is not None else ""))
        return [(chat_id, status + "\n\n" + PLANS_TEXT, None, None)]

    if cmd == "/subscribe":
        return [(chat_id, _subscribe_text(chat_id), None, "MENU")]

    if cmd == "/unsubscribe":
        ok = remove_subscriber(chat_id)
        msg = "Unsubscribed. You can rejoin anytime with /subscribe." if ok \
            else "You were not subscribed."
        return [(chat_id, msg, None, None)]

    if cmd == "/portfolio":
        if not is_pro(chat_id) and not is_admin(chat_id):
            return [(chat_id, "👑 Portfolio tracking is a *PRO* feature. "
                              f"Contact {cfg.ADMIN_CONTACT} to upgrade.", None, None)]
        return [(chat_id, portfolio_text(chat_id), None, None)]

    if cmd == "/news":
        from sentiment import briefing_text
        return [(chat_id, briefing_text(max_assets=6), None, None)]

    if cmd == "/grantpro":
        if not is_admin(chat_id):
            return [(chat_id, "Only the admin can grant PRO.", None, None)]
        if not args:
            return [(chat_id, "*Usage:* `/grantpro @username [days]`\n\n"
                              "Examples:\n"
                              "`/grantpro @john 30` \u2192 PRO for 30 days\n"
                              "`/grantpro @john` \u2192 PRO for life (no "
                              "expiry)\n\n"
                              "The user must have sent the bot at least one "
                              "message (e.g. /start) so their username is "
                              "registered.", None, None)]
        from subscriptions import resolve_target
        target, display = resolve_target(args[0])
        if target is None:
            return [(chat_id, f"User {args[0]!r} not found \u2014 they need to "
                              "message the bot at least once (e.g. /start) "
                              "so their username is registered first.",
                     None, None)]
        days = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
        grant_pro(target, days)
        if days:
            until_str = (dt.datetime.now(dt.timezone.utc) +
                        dt.timedelta(days=days)).strftime("%Y-%m-%d")
            duration_admin = f"for {days} days (until {until_str})"
            duration_user = f"for *{days} days*, until *{until_str}*"
        else:
            duration_admin = "for life"
            duration_user = "for *life* \u2014 no expiry"
        msg = f"\u2705 PRO granted to {display or target} {duration_admin} \U0001f451"
        # notify the user directly
        try:
            send_message(f"\U0001f389 Congratulations! You've been upgraded to "
                         f"*PRO* {duration_user}. You now get unlimited "
                         f"real-time signals on every market, advanced "
                         f"indicators, AI analysis and portfolio tracking. "
                         f"Enjoy! \U0001f451", chat_id=target)
        except Exception:
            pass
        return [(chat_id, msg, None, None)]

    if cmd == "/revokepro":
        if not is_admin(chat_id):
            return [(chat_id, "Only the admin can revoke PRO.", None, None)]
        if not args:
            return [(chat_id, "Usage: /revokepro @username|CHAT_ID", None, None)]
        from subscriptions import resolve_target
        target, display = resolve_target(args[0])
        if target is None:
            return [(chat_id, f"User {args[0]!r} not found.", None, None)]
        revoke_pro(target)
        try:
            send_message("Your PRO access has been revoked. You are back on "
                         f"FREE ({cfg.FREE_DAILY_SIGNALS} winning signals/day).",
                         chat_id=target)
        except Exception:
            pass
        return [(chat_id, f"PRO revoked from {display or target}.", None, None)]

    if cmd == "/users":
        return [(chat_id, _users_text(), None, None)]

    if cmd == "/user":
        return [(chat_id, _user_text(args), None, None)]

    if cmd == "/broadcast":
        return [(chat_id, _broadcast_text(args), None, None)]

    if cmd == "/autosignal":
        from subscriptions import auto_signals_enabled, set_auto_signals

        # Admin-only global kill switch for the whole loop: /autosignal all on|off
        if args and args[0].lower() == "all":
            if not is_admin(chat_id):
                return [(chat_id, "Only the admin can use /autosignal all.",
                         None, None)]
            if len(args) < 2 or args[1].lower() not in ("on", "off"):
                return [(chat_id, "Usage: /autosignal all on|off", None, None)]
            state = bc.load()
            enabled = bc.set_auto_broadcast(state, args[1].lower() == "on")
            return [(chat_id,
                     f"🌐 Automatic loop broadcasts are now *"
                     f"{'ON' if enabled else 'OFF'}* for everyone. "
                     f"On-demand /signal always keeps working regardless.",
                     None, None)]

        if not args:
            mine = auto_signals_enabled(chat_id)
            lines = [f"Your automatic signals are *"
                     f"{'ON' if mine else 'OFF'}* (only matters if the "
                     "admin has also enabled the global auto-broadcast "
                     "loop below \u2014 otherwise all signals are on-demand "
                     "via /signal).",
                     "Usage: /autosignal on|off"]
            if is_admin(chat_id):
                state = bc.load()
                glob = bc.auto_broadcast_enabled(state)
                lines.append(f"Global loop (admin): *"
                             f"{'ON' if glob else 'OFF'}* "
                             f"(`/autosignal all on|off` to change)")
            return [(chat_id, "\n".join(lines), None, None)]

        if args[0].lower() not in ("on", "off"):
            return [(chat_id, "Usage: /autosignal on|off", None, None)]
        enabled = set_auto_signals(chat_id, args[0].lower() == "on")
        msg = ("🔔 You'll now receive automatic signals from the background "
               "scan loop as soon as one qualifies." if enabled else
               "🔕 Automatic signals are off. You can still request one "
               "anytime with /signal.")
        return [(chat_id, msg, None, None)]

    if cmd == "/markets":
        from config import ASSETS, ASSET_CATEGORY, TIMEFRAMES
        cats = {}
        for a in ASSETS:
            cats.setdefault(ASSET_CATEGORY[a], []).append(a)
        lines = ["*Supported markets*", ""]
        for cat, names in cats.items():
            lines.append(f"*{cat}:* {', '.join(sorted(names))}")
        lines += ["", f"Timeframes: {', '.join(TIMEFRAMES)}",
                  "Try: /signal (all markets) or /signal EURUSD M1",
                  "", "Every one of these is tradeable on Pocket Option "
                      "with quick-trade expiries \u2013 tap \U0001f3e6 below."]
        buttons = [[{"text": "🏦 Open Pocket Option", "url": cfg.POCKET_OPTION_URL}]]
        return [(chat_id, "\n".join(lines), None, buttons)]

    if cmd == "/mystats":
        from signal_log import report_text, streaks
        txt = report_text()
        st = streaks()
        if st["total"]:
            cur = {"won": "🟢", "lost": "🔴"}.get(st["current"], "—")
            txt += (f"\n\n*Streaks*\nCurrent: {cur} {st['current_len']} in a row\n"
                     f"Best win streak: {st['best_won']}  |  "
                     f"Best loss streak: {st['best_lost']}")
        return [(chat_id, txt, None, None)]

    if cmd == "/top":
        return [(chat_id, _hot_markets_text(), None, None)]

    if cmd == "/calc":
        return [(chat_id, _calc_text(chat_id, args), None, None)]

    if cmd == "/status":
        return [(chat_id, _status_text(ctx), None, None)]

    if cmd == "/signal":
        return _handle_signal(chat_id, args, ctx)

    if cmd == "/scan":
        return _handle_scan(chat_id, ctx)

    return [(chat_id, "Unknown command. Try /help", None, None)]


def _subscribe_text(chat_id):
    """/start and /subscribe share this: register identity, join the
    broadcast list and return the welcome message."""
    from subscriptions import get_user, plan_name, pro_until
    add_subscriber(chat_id)
    get_user(chat_id, create=True)  # ensure a plan record exists (FREE)
    plan = plan_name(chat_id)
    extra = ""
    if plan.startswith("PRO"):
        until = pro_until(chat_id)
        extra = (f"\nPRO active {'until ' + until.strftime('%Y-%m-%d') if until else '(lifetime)'} 👑")
    return (
        f"*✅ {cfg.BOT_NAME} account ready!*\n\n"
        "You're set up on *every market* - forex, crypto, commodities, "
        "indices and stocks - ready for Pocket Option quick trading "
        "(1m - 1h expiries).\n\n"
        "Signals are generated on demand: tap *⚡ Instant Signal* any time "
        "and it scans every market right now, showing you whichever ones "
        "currently clear the quality bar, with a one-tap link to open that "
        "market on Pocket Option.\n\n"
        "Use the buttons below instead of typing commands 👇\n\n"
        f"Your plan: *{plan}*{extra}\n\n"
        f"{PLANS_TEXT}\n\n"
        "_No signal service can guarantee profit - see /help for how the "
        "validated edge and confidence score are calculated._"
    )


def _hot_markets_text():
    """🔥 /top: best markets right now - validated scan edge + live track record."""
    from scanner import watchlist_rows
    from signal_log import market_leaderboard
    rows = watchlist_rows()
    lines = [f"*🔥 {cfg.BOT_NAME} - Hot markets now*", ""]
    if not rows:
        lines.append("No validated markets yet - the admin can run `/scan` "
                     "to build the list.")
        return "\n".join(lines)
    lines.append("*Best validated markets (out-of-sample)*")
    ranked = sorted(rows.items(), key=lambda kv: -kv[1]["rank_score"])[:8]
    for i, ((asset, tf), r) in enumerate(ranked, 1):
        lines.append(f"{i}. {market_display(asset)} {tf} ({r['category']}) - "
                     f"EV {r['oos_ev']:+.2f} · {r['win_rate']:.0%} win "
                     f"· {r['n_trades']} OOS trades")
    lb = market_leaderboard(5)
    if lb:
        lines += ["", "*Live verified track record*", ""]
        for m in lb:
            lines.append(f"  {m['market']}: {m['wins']}/{m['n']} "
                         f"({m['win_rate']:.0%})")
    lines += ["", "_Edge = walk-forward out-of-sample validation; live stats "
                   "are verified from real market data after every expiry._"]
    return "\n".join(lines)


def _calc_text(chat_id, args):
    """🧮 /calc <balance> [risk%] [payout%]: PRO stake calculator."""
    from subscriptions import is_admin, is_pro
    if not (is_pro(chat_id) or is_admin(chat_id)):
        return ("👑 The stake calculator is a *PRO* feature. "
                f"Contact {cfg.ADMIN_CONTACT} to upgrade.")
    if not args:
        return "Usage: /calc <balance> [risk%] [payout%]  e.g. /calc 1000 2 85"
    try:
        balance = float(args[0].replace(",", "."))
    except ValueError:
        return "Balance must be a number, e.g. /calc 1000"
    risk_pct = 2.0
    payout = cfg.PAYOUT
    if len(args) >= 2:
        try:
            risk_pct = float(args[1].replace(",", "."))
        except ValueError:
            pass
    if len(args) >= 3:
        try:
            payout = float(args[2].replace(",", ".")) / 100.0
        except ValueError:
            pass
    risk_pct = max(0.5, min(10.0, risk_pct))
    payout = max(0.5, min(0.99, payout))
    stake = balance * risk_pct / 100.0
    profit = stake * payout
    loss = stake
    be = 100.0 / (1.0 + payout)
    lines = [
        "*🧮 Stake Calculator (PRO)*", "",
        f"Balance: ${balance:,.2f}",
        f"Risk per trade: {risk_pct:.1f}% → *${stake:,.2f}*",
        "",
        f"Win (payout {payout:.0%}): +${profit:,.2f}",
        f"Loss: -${loss:,.2f}",
        f"Breakeven win rate needed: {be:.1f}%",
        "",
        f"Daily stop (10% of balance): max {max(1, int(10.0 / risk_pct))} losses",
        "",
        "_Keep risk at 1-2% per trade; never chase losses._",
    ]
    return "\n".join(lines)


def _users_text():
    """Admin /users: headcount summary + per-user line + expiring PRO."""
    from subscriptions import active_today, expiring_pro, list_users
    users = list_users()
    if not users:
        return "No users have contacted the bot yet.\nShare the bot link: " \
               "https://t.me/" + (cfg.TELEGRAM_BOT_TOKEN.split(":")[0] if ":" in cfg.TELEGRAM_BOT_TOKEN else "yourbot") + "\n"
    pro_n = sum(1 for u in users if u.get("plan") == "pro")
    free_n = len(users) - pro_n
    today_n = sum(u["signals_today"] for u in users)
    active, delivered = active_today()
    lines = [
        f"*👥 {cfg.BOT_NAME} - users*", "",
        f"Total: {len(users)}  |  PRO: {pro_n} 👑  |  FREE: {free_n}",
        f"Signals delivered today: {today_n}  |  Active today: {active}",
        "",
        "*User  |  Plan  |  Signals (today/total)*",
    ]
    for u in users:
        who = u.get("username") or u.get("name") or u["chat_id"]
        if u.get("username"):
            who = "@" + u["username"]
        who = md_escape(who)
        plan = "PRO👑" if u.get("plan") == "pro" else "free"
        lines.append(f"{who}  |  {plan}  |  {u['signals_today']}/{u['signals_total']}")
    exp = expiring_pro(days=3)
    if exp:
        lines += ["", "*⚠️ PRO expiring within 3 days*", ""]
        for e in exp:
            who = "@" + e["username"] if e.get("username") else e["chat_id"]
            who = md_escape(who)
            lines.append(f"  {who} → {e['pro_until']:%Y-%m-%d} "
                         f"(renew: /grantpro {who} 30)")
    lines += ["", "_Use /user <@username> for details, /grantpro <@username> "
                   "to activate PRO._"]
    return "\n".join(lines)


def _user_text(args):
    """Admin /user <target>: full record for one user."""
    from subscriptions import (plan_name, pro_until, resolve_target,
                               today_used, user_detail)
    if not args:
        return "Usage: /user @username|CHAT_ID"
    target, display = resolve_target(args[0])
    if target is None:
        return f"User {args[0]!r} not found - they must /start the bot once."
    u = user_detail(target)
    if not u:
        return f"No record for {display or target}."
    until = pro_until(target)
    lines = [
        f"*User: {md_escape(display or target)}*", "",
        f"Name: {md_escape(u.get('name')) or '-'}",
        f"Username: @{md_escape(u['username'])}" if u.get("username") else "Username: -",
        f"Chat ID: `{target}`",
        f"Plan: *{plan_name(target)}*",
        f"PRO until: {until:%Y-%m-%d}" if until else "PRO until: lifetime" \
            if u.get("plan") == "pro" else "PRO until: -",
        f"Joined: {str(u.get('joined'))[:16]}",
        f"Last seen: {str(u.get('last_seen'))[:16]}",
        f"Signals today: {today_used(target)}",
        f"Signals received total: {len(u.get('receipts', []))}",
    ]
    return "\n".join(lines)


def _broadcast_text(args):
    """Admin /broadcast <message>: send to every subscriber + admin chat."""
    if not args:
        return "Usage: /broadcast <message text>"
    text = " ".join(args)
    results = broadcast(text)
    ok = sum(1 for r in results if r.get("ok"))
    return f"📣 Broadcast sent to {ok}/{len(results)} chats."


def _status_text(ctx):
    from scanner import load_watchlist
    from signal_log import stats
    from subscriptions import plan_name
    watch = load_watchlist()
    s = stats()
    uptime = ""
    if ctx.get("started_at"):
        mins = int((dt.datetime.now(dt.timezone.utc) - ctx["started_at"]).total_seconds() // 60)
        uptime = f"Uptime: {mins} min\n"
    lines = [
        "*Bot status*", "",
        uptime and uptime.rstrip(),
        f"Watchlist pairs: {len(watch)}",
        f"Signals sent: {s['total']}  (resolved {s['resolved']}, "
        f"open {s['open']})",
        f"Live win rate: {s['win_rate']:.0%}" if s["resolved"] else
        "Live win rate: n/a yet",
    ]
    if watch:
        lines += ["", "*Watchlist:*"]
        for asset, tf, variant in watch:
            lines.append(f"  - {asset} {tf} [{variant}]")
    return "\n".join(lines)


def _handle_signal_burst(chat_id, ctx):
    """⚡ Instant Signal: scan EVERY Pocket Option asset (not just the
    validated watchlist - see signal_scan.scan_live_all) right now.

    Runs in a background thread so the bot replies immediately with a
    "scanning" message, then turns that same message into a real progress
    countdown (X/Y markets checked - not a fake timer) as results come in,
    then delivers whichever markets actually clear the quality bar as soon
    as the scan finishes. If nothing qualifies, says so plainly instead of
    padding the result.

    FREE: up to the user's remaining daily quota (FREE_DAILY_SIGNALS/day
    total - a burst counts against the same cap as single-asset pulls).
    PRO/admin: up to PRO_SIGNAL_BURST per call (a message-flood cap, not a
    quality relaxation - every market passes the exact same bar).
    """
    from subscriptions import (is_admin, is_pro, remaining_today,
                               record_signal)

    pro = is_pro(chat_id) or is_admin(chat_id)
    if pro:
        limit = cfg.PRO_SIGNAL_BURST
    else:
        remaining = remaining_today(chat_id)
        if not remaining:
            return [(chat_id, _upgrade_text(), None, None)]
        limit = remaining

    n_assets = len(cfg.open_assets())
    n_tf = len(cfg.INSTANT_SCAN_TIMEFRAMES)
    total_pairs = n_assets * n_tf
    ack = send_message(
        f"🔍 *Scanning {n_assets} currently-open Pocket Option markets* "
        f"({', '.join(cfg.INSTANT_SCAN_TIMEFRAMES)}) for a live signal now - "
        f"{total_pairs} checks queued...",
        chat_id=chat_id,
    )
    msg_id = (ack or {}).get("result", {}).get("message_id")

    def worker():
        from sentiment import sentiment_for
        from analysis import advanced_indicators_text, ai_analysis
        from signal_log import log_signal
        from signal_scan import scan_live_all

        last_shown = {"n": -1}

        def _progress(done, total):
            # Edit at most every ~10% so we stay well under Telegram's
            # edit rate limit while still showing genuine progress.
            step = max(1, total // 10)
            if done != total and (done - last_shown["n"]) < step:
                return
            last_shown["n"] = done
            edit_message(chat_id, msg_id,
                        f"🔍 *Scanning Pocket Option markets...* "
                        f"{done}/{total} checked")

        try:
            results = scan_live_all(limit=limit, progress=_progress,
                                    near_miss_fallback=cfg.INSTANT_SCAN_NEAR_MISS_FALLBACK)
        except Exception as exc:
            text = f"Scan failed: {exc}"
            if msg_id:
                edit_message(chat_id, msg_id, text)
            else:
                send_message(text, chat_id=chat_id)
            return

        if not results:
            text = (f"📡 Scanned all {total_pairs} market/timeframe checks - "
                    "nothing with a clear direction right now (fresh data, "
                    "confluence and quality checks all ran; the market's "
                    "just flat). Try again shortly, or check /top.")
            if msg_id:
                edit_message(chat_id, msg_id, text)
            else:
                send_message(text, chat_id=chat_id)
            return

        qualified = [r for r in results if r.get("qualified", True)]
        near_miss = [] if qualified else results
        shown = qualified or near_miss
        calls = sum(1 for r in shown if r["sig"]["direction"] == "CALL")
        puts = sum(1 for r in shown if r["sig"]["direction"] == "PUT")
        tally = f" \u2014 \U0001f7e2 {calls} CALL / \U0001f534 {puts} PUT" if shown else ""

        if qualified:
            plan_word = "PRO" if pro else "FREE"
            header = (f"🎯 *Live scan done:* {len(qualified)} market"
                      f"{'s' if len(qualified) != 1 else ''} out of {total_pairs} "
                      f"checks currently meet the quality bar ({plan_word} plan)"
                      f"{tally}. Sending now \u2193")
        else:
            header = (f"📡 *Live scan done:* nothing out of {total_pairs} checks "
                      f"cleared the full quality bar right now, but here "
                      f"{'is' if len(near_miss) == 1 else 'are'} the "
                      f"{len(near_miss)} closest live reading"
                      f"{'s' if len(near_miss) != 1 else ''}{tally} - marked "
                      "*unconfirmed*, not validated signals. Sending now \u2193")
        if msg_id:
            edit_message(chat_id, msg_id, header)
        else:
            send_message(header, chat_id=chat_id)

        signal_ts = dt.datetime.now(dt.timezone.utc).isoformat()
        for r in (qualified or near_miss):
            asset, tf, sig, row = r["asset"], r["tf"], r["sig"], r.get("row")
            variant = "confluence"
            sentiment = sentiment_for(asset)
            df = sig.get("_df")
            if r.get("qualified", True):
                note = None if row else (
                    "_Live confluence signal - same real-time quality gates as "
                    "every other card, but this market isn't on the "
                    "walk-forward-validated watchlist yet, so there's no "
                    "historical edge/win-rate stat for it. Confidence above is "
                    "computed fresh right now, not backtested._")
            else:
                note = (
                    f"_\u26a0\ufe0f *Below the quality bar - not a confirmed "
                    f"signal.* Confidence {sig['confidence']:.0f}% is under "
                    f"the {r.get('min_conf', cfg.BROADCAST_MIN_CONFIDENCE):.0f}% "
                    f"bar every other card on this bot clears. Shown for "
                    f"reference only - trade it at your own risk, and it "
                    f"does not count toward your daily quota or the "
                    f"verified track record._")
            card = format_signal(
                sig, asset, tf, variant, pro=pro, sentiment=sentiment,
                ai_text=ai_analysis(df, sig, asset, tf, variant, sentiment) if pro else None,
                indicators_text=advanced_indicators_text(df) if pro else None,
                extra_note=note, qualified=r.get("qualified", True),
            )
            send_message(card, chat_id=chat_id, buttons=signal_buttons(asset, tf, variant))
            if r.get("qualified", True):
                record_signal(chat_id, signal_ts, asset, tf, variant,
                              sig["direction"], sig["entry"])
                log_signal(asset, tf, variant, sig)

    import threading
    threading.Thread(target=worker, daemon=True).start()
    return []


def _handle_signal(chat_id, args, ctx):
    """Plan-aware on-demand signal: FREE gets the compact card (counts toward
    the daily cap), PRO/admin get the full card with indicators + AI.

    No args -> burst mode: scan ALL markets at once (see _handle_signal_burst).
    """
    from subscriptions import (is_admin, is_pro, remaining_today,
                               signal_allowed, record_signal)
    from sentiment import sentiment_for
    from analysis import advanced_indicators_text, ai_analysis

    if not args:
        return _handle_signal_burst(chat_id, ctx)
    asset = args[0].upper()
    tf = args[1].upper() if len(args) > 1 else "M1"
    variant = args[2].lower() if len(args) > 2 else None
    if tf not in cfg.DATA_INTERVAL_MAP:
        return [(chat_id, f"Unknown timeframe {tf}. Try M1 M5 M15 M30 H1.", None, None)]
    if asset in cfg.ASSETS and not cfg.is_session_open(asset):
        cat = cfg.ASSET_CATEGORY.get(asset)
        span = cfg.SESSION_UTC.get(cat)
        window = (f"{span[0]:02d}:00\u2013{span[1]:02d}:00 UTC, Mon\u2013Fri"
                  if span else "24/7")
        return [(chat_id,
                 f"*{asset}* isn't currently tradeable on Pocket Option "
                 f"({cat} session: {window}). Try a Crypto pair (24/7) or "
                 f"check back when the session opens - see /markets.",
                 None, None)]
    if not signal_allowed(chat_id):
        return [(chat_id, _upgrade_text(), None, None)]

    # "state" and "confluence" are pseudo-variants used by a Quick Read /
    # Confluence card's own Re-check button - they aren't real event
    # strategies, so route them straight to the matching fallback instead
    # of the per-variant event loop below (which would raise on an unknown
    # strategy name).
    pseudo = variant if variant in ("state", "confluence") else None
    variants = [] if pseudo else ([variant] if variant else cfg.VARIANTS)

    try:
        from data_fetcher import fetch
        from strategy import live_signal, confluence as _confluence, quick_read
        df = fetch(asset, tf)
        df_htf = None
        if tf in ("M1", "M5", "M15", "M30"):
            htf = {"M1": "M5", "M5": "M15", "M15": "M30", "M30": "H1"}[tf]
            try:
                df_htf = fetch(asset, htf)
            except Exception:
                df_htf = None
        pro = is_pro(chat_id) or is_admin(chat_id)
        sentiment = sentiment_for(asset)
        replies = []

        if pseudo == "confluence":
            from signal_scan import confluence_gate, build_signal
            c = _confluence(df, asset_name=asset, df_htf=df_htf)
            if c["direction"] is None:
                replies.append((chat_id, f"*{asset} {tf}*: no confluence "
                                         "direction on the current bar.",
                                None, None))
                return replies
            agree, _min_conf, ok = confluence_gate(c)
            sig = build_signal(c, df, agree)
            text = format_signal(
                sig, asset, tf, "confluence", pro=pro, sentiment=sentiment,
                ai_text=ai_analysis(df, sig, asset, tf, "confluence",
                                    sentiment, df_htf) if pro else None,
                indicators_text=advanced_indicators_text(df) if pro else None,
                qualified=ok,
            )
            replies.append((chat_id, text, None,
                            signal_buttons(asset, tf, "confluence")))
            if ok:
                record_signal(chat_id, dt.datetime.now(dt.timezone.utc).isoformat(),
                              asset, tf, "confluence", sig["direction"], sig["entry"])
            return replies

        best = None  # (variant, signal dict) of the strongest live signal
        no_event = []
        for v in variants:
            sig = live_signal(df, asset_name=asset, variant=v, df_htf=df_htf)
            if sig is None:
                no_event.append(v)
                continue
            sig["_df"] = df
            text = format_signal(
                sig, asset, tf, v, pro=pro, sentiment=sentiment,
                ai_text=ai_analysis(df, sig, asset, tf, v, sentiment,
                                    df_htf) if pro else None,
                indicators_text=advanced_indicators_text(df) if pro else None,
            )
            replies.append((chat_id, text, None, signal_buttons(asset, tf, v)))
            if best is None or sig["confidence"] > best[1]["confidence"]:
                best = (v, sig)

        if best is not None:
            # Record the STRONGEST signal (not the last one evaluated).
            v, sig = best
            signal_ts = dt.datetime.now(dt.timezone.utc).isoformat()
            record_signal(chat_id, signal_ts, asset, tf, v,
                          sig["direction"], sig["entry"])
        else:
            # Nothing requested fired a fresh event on this bar - give a
            # quick, honestly-capped state read instead of leaving the user
            # empty-handed. Pocket Option quick-trade users want SOMETHING
            # to look at right now; this is clearly marked unconfirmed and
            # never counts toward the daily quota or the track record.
            qr = None
            try:
                qr = quick_read(df, asset_name=asset, df_htf=df_htf)
            except Exception:
                qr = None
            if qr is not None:
                qr["_df"] = df
                checked = ", ".join(no_event) if no_event else "trend/RSI/MACD"
                note = (
                    "_\u26a1 *Quick Read* \u2014 no new crossover or breakout "
                    f"has triggered on the current {tf} bar ({checked}), so "
                    "this reading is based on live trend, RSI, MACD and band "
                    "position instead. Confidence is capped by design: use "
                    "it as a directional lean for quick trading, not a "
                    "validated signal. Doesn't count toward your daily "
                    "quota._"
                )
                text = format_signal(
                    qr, asset, tf, "state", pro=pro, sentiment=sentiment,
                    ai_text=ai_analysis(df, qr, asset, tf, "state", sentiment,
                                        df_htf) if pro else None,
                    indicators_text=advanced_indicators_text(df) if pro else None,
                    extra_note=note, qualified=False,
                )
                replies.append((chat_id, text, None,
                                signal_buttons(asset, tf, "state")))
            else:
                replies.append((chat_id,
                    f"*{asset} {tf}*: no directional edge right now \u2014 "
                    "trend, RSI, MACD and bands are all sitting near "
                    "neutral. Try another pair, a different timeframe, or "
                    "/scan to see what's currently moving.",
                    None, None))

        if len(variants) > 1:
            c = _confluence(df, asset_name=asset, df_htf=df_htf)
            if c["direction"]:
                replies.append((chat_id,
                                f"*Confluence:* {c['direction']} "
                                f"({c['confidence']:.0f}%, tier {c['tier']}) - "
                                f"votes: " + ", ".join(
                                    f"{v}:{'CALL' if o['dir'] > 0 else ('PUT' if o['dir'] < 0 else '-')}"
                                    for v, o in c["votes"].items()),
                                None, None))
        return replies
    except KeyError as exc:
        return [(chat_id, f"'{asset}' isn't a supported market. Send /markets "
                          "for the full list.", None, None)]
    except Exception as exc:
        print(f"[signal error] {asset} {tf}: {exc}")
        return [(chat_id, "That signal couldn't be generated right now. "
                          "Please try again in a moment.", None, None)]


def _handle_scan(chat_id, ctx):
    def worker():
        try:
            ctx["scan_lock"] = True
            from scanner import scan
            from data_fetcher import fetch
            from strategy import confluence as _confluence
            df = scan(progress=False)
            if df.empty:
                send_message("Scan finished: no pairs passed validation filters.",
                             chat_id=chat_id)
                return
            calls = puts = flat = 0
            lines = []
            for r in df.head(cfg.SCAN_TOP_N).itertuples(index=False):
                line = (
                    f"{r.asset} {r.timeframe} [{r.variant}]: {r.n_trades} OOS trades, "
                    f"win {r.win_rate:.0%}, EV {r.oos_ev:+.3f} "
                    f"(CI-lo {r.ci_lower:+.3f})"
                    f"{'  ✓consistent' if r.consistent else ''}"
                )
                # Live direction right now (best-effort - a stale/failed
                # fetch just leaves this pair out of the tally, never
                # invents a direction).
                try:
                    d = fetch(r.asset, r.timeframe)
                    c = _confluence(d, asset_name=r.asset)
                    if c["direction"] == "CALL":
                        calls += 1
                        line += "  \U0001f7e2 now: CALL"
                    elif c["direction"] == "PUT":
                        puts += 1
                        line += "  \U0001f534 now: PUT"
                    else:
                        flat += 1
                        line += "  \u26aa now: flat"
                except Exception:
                    pass
                lines.append(line)
            header = (f"*Top markets this window (OOS-validated)* \u2014 live "
                      f"direction right now: \U0001f7e2 {calls} CALL / "
                      f"\U0001f534 {puts} PUT" + (f" / \u26aa {flat} flat"
                      if flat else "") + "\n")
            send_message(header + "\n".join(lines), chat_id=chat_id)
        except Exception as exc:
            print(f"[scan error] {exc}")
            send_message("The scan couldn't complete. Please try again "
                        "shortly.", chat_id=chat_id)
        finally:
            ctx["scan_lock"] = False
    if ctx.get("scan_lock"):
        return [(chat_id, "A scan is already in progress \u2014 please wait "
                          "for it to finish before starting another.",
                 None, None)]
    import threading
    threading.Thread(target=worker, daemon=True).start()
    return [(chat_id, "Scan started \u2014 validating every market and "
                      "timeframe combination now. You'll be notified as "
                      "soon as it's ready.", None, None)]


def get_updates(offset=None, timeout=25):
    token = cfg.TELEGRAM_BOT_TOKEN
    if not token:
        return []
    try:
        resp = requests.get(_api("getUpdates", token), timeout=timeout + 5,
                            params={"offset": offset, "timeout": timeout})
        data = resp.json()
        if not data.get("ok"):
            return []
        return data.get("result", [])
    except requests.RequestException:
        return []


def process_updates(updates, ctx):
    """Handle raw getUpdates payloads; returns the next offset."""
    token = cfg.TELEGRAM_BOT_TOKEN
    offset = None
    for upd in updates:
        offset = upd["update_id"] + 1
        try:
            if "message" in upd:
                msg = upd["message"]
                chat_id = msg["chat"]["id"]
                text = msg.get("text", "")
                # identity registry: keep username/name fresh so the admin
                # can manage users (e.g. /grantpro @username) and view who
                # is using the bot.
                try:
                    frm = msg.get("from", {})
                    username = frm.get("username") or msg.get("chat", {}).get("username")
                    name = frm.get("first_name") or msg.get("chat", {}).get("first_name")
                    from subscriptions import update_identity
                    update_identity(chat_id, username=username, name=name)
                except Exception:
                    pass
                # A tap on the persistent menu arrives as plain text
                # matching a button label - translate it back to its
                # command so the rest of the flow is unchanged.
                if not text.startswith("/") and text.strip() in MENU_LABEL_TO_CMD:
                    text = MENU_LABEL_TO_CMD[text.strip()]
                if text.startswith("/"):
                    for cid, txt, photo, buttons in _handle_command(text, chat_id, ctx):
                        if buttons == "MENU":
                            send_message(txt, photo_path=photo, chat_id=cid, menu=True)
                        else:
                            send_message(txt, photo_path=photo, chat_id=cid, buttons=buttons)
            elif "callback_query" in upd:
                cb = upd["callback_query"]
                chat_id = cb["message"]["chat"]["id"]
                data = cb.get("data", "")
                parts = data.split(":")
                if len(parts) >= 2:
                    kind = parts[0]
                    asset, tf, variant = parts[1], parts[2], parts[3] if len(parts) > 3 else "momentum"
                    if kind == "chart":
                        try:
                            from data_fetcher import fetch
                            import chart as chart_mod
                            df = fetch(asset, tf)
                            photo = chart_mod.save_chart(df, asset, tf, variant=variant)
                            send_message(f"*{asset} {tf} [{variant}]* chart",
                                         photo_path=photo, chat_id=chat_id)
                        except Exception as exc:
                            print(f"[chart error] {asset} {tf}: {exc}")
                            send_message("That chart couldn't be generated "
                                        "right now. Please try again shortly.",
                                        chat_id=chat_id)
                    elif kind == "signal":
                        for cid, txt, photo, buttons in _handle_signal(chat_id, [asset, tf, variant], ctx):
                            send_message(txt, photo_path=photo, chat_id=cid, buttons=buttons)
        except Exception as exc:
            print(f"[handler error] {exc}")
            try:
                send_message("Something went wrong processing that. "
                            "Please try again.", chat_id=chat_id)
            except Exception:
                pass
    return offset