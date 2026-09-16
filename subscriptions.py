"""Subscription system (FREE / PRO) + user registry for the admin panel.

Plans live in subscriptions.json, keyed by Telegram chat id:

    {
      "chat_id": {
        "plan": "free" | "pro",
        "pro_until": "2026-09-16T00:00:00+00:00" | null,
        "joined": "...",
        "username": "madhux",          # from Telegram (@-less, if set)
        "name": "Damith",              # first name from Telegram
        "last_seen": "...",
        "daily": {"2026-09-09": 3},    # WINNING signals credited today
        "upgrade_notified": "2026-09-09",  # last day we sent the upgrade nudge
        "receipts": [                 # every signal sent (portfolio ledger)
           {"signal_ts": "...", "asset": "EURUSD", "tf": "M1",
            "variant": "momentum", "direction": "CALL", "entry": 1.16477,
            "credited": false}         # true once resolved won/lost
        ]
      }
    }

Rules:
  FREE  -> up to cfg.FREE_DAILY_SIGNALS *winning* signals per UTC day.
           Every qualifying signal is still sent - losing trades never
           count against the quota, only a verified WIN does. A receipt is
           credited (and, if it won, added to that day's count) once the
           trade resolves against live market data (see _credit_wins /
           signal_log.verify_expired). Once the daily win quota is used up,
           the user is nudged once per day to upgrade by contacting
           cfg.ADMIN_CONTACT.
  PRO   -> unlimited signals + advanced indicator cards + portfolio tracking
           + AI analysis, until pro_until (None = lifetime).
  Admin (TG_CHAT_ID) always receives everything and can grant/revoke PRO
  with /grantpro and /revokepro - by CHAT_ID or by @username.

Admin commands (see telegram_bot.py):
  /users                 -> list every user with plan + usage
  /user <target>         -> full detail for one user
  /grantpro <target>     -> PRO by chat id or @username
  /revokepro <target>    -> revoke by chat id or @username
"""
import datetime as dt
import json
import os
import threading

import config as cfg

_lock = threading.Lock()


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        cfg.SUBSCRIPTIONS_FILE)


def _load():
    try:
        with open(_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data):
    with open(_path(), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def _today():
    return _now().strftime("%Y-%m-%d")


# ---------------- queries ----------------

def get_user(chat_id, create=True, username=None, name=None):
    """Return a user record; create a FREE record on first contact.
    `username`/`name` are stored when provided (identity registry)."""
    chat_id = str(chat_id)
    with _lock:
        data = _load()
        if chat_id not in data and create:
            data[chat_id] = {
                "plan": "free",
                "pro_until": None,
                "joined": _now().isoformat(),
                "username": None,
                "name": None,
                "last_seen": _now().isoformat(),
                "daily": {},
                "receipts": [],
                "auto_signals": True,
            }
        u = data.get(chat_id)
        if u is None:
            return {}
        changed = False
        if username is not None and str(username).strip():
            clean = str(username).lstrip("@").strip().lower()
            if u.get("username") != clean:
                u["username"] = clean
                changed = True
        if name is not None and str(name).strip():
            if u.get("name") != str(name).strip():
                u["name"] = str(name).strip()
                changed = True
        if u.get("last_seen") != _now().isoformat():
            u["last_seen"] = _now().isoformat()
            changed = True
        if changed:
            _save(data)
        return dict(u)


def update_identity(chat_id, username=None, name=None):
    """Refresh username/name/last_seen of an existing user (no-op if the
    chat has never interacted). Returns the record or None."""
    chat_id = str(chat_id)
    with _lock:
        data = _load()
        if chat_id not in data:
            return None
        u = data[chat_id]
        if username is not None and str(username).strip():
            u["username"] = str(username).lstrip("@").strip().lower()
        if name is not None and str(name).strip():
            u["name"] = str(name).strip()
        u["last_seen"] = _now().isoformat()
        _save(data)
        return dict(u)


def is_admin(chat_id):
    return str(chat_id) == str(cfg.TELEGRAM_CHAT_ID)


def auto_signals_enabled(chat_id):
    """True if this chat wants to receive the background loop's automatic
    broadcasts. Defaults to True (existing behavior) for anyone who hasn't
    explicitly opted out with /autosignal off. On-demand commands (/signal
    etc.) are never affected by this - only the unattended loop broadcast."""
    u = get_user(chat_id, create=False)
    if not u:
        return True
    return bool(u.get("auto_signals", True))


def set_auto_signals(chat_id, enabled):
    """Opt this chat in/out of automatic loop broadcasts."""
    chat_id = str(chat_id)
    with _lock:
        data = _load()
        if chat_id not in data:
            data[chat_id] = {
                "plan": "free", "pro_until": None, "joined": _now().isoformat(),
                "username": None, "name": None, "last_seen": _now().isoformat(),
                "daily": {}, "receipts": [],
            }
        data[chat_id]["auto_signals"] = bool(enabled)
        _save(data)
        return bool(enabled)


def is_pro(chat_id):
    u = get_user(chat_id, create=False)
    if not u:
        return False
    if u.get("plan") != "pro":
        return False
    until = u.get("pro_until")
    if until is None:
        return True
    try:
        return dt.datetime.fromisoformat(until) > _now()
    except (TypeError, ValueError):
        return False


def plan_name(chat_id):
    return "PRO 👑" if (is_pro(chat_id) or is_admin(chat_id)) else "FREE"


def pro_until(chat_id):
    u = get_user(chat_id, create=False)
    if not u or not u.get("pro_until"):
        return None
    try:
        return dt.datetime.fromisoformat(u["pro_until"])
    except (TypeError, ValueError):
        return None


# ---------------- win crediting (FREE quota only counts verified wins) ----

def _credit_wins(u):
    """Match this user's uncredited receipts against the verified signal
    log and credit each resolved trade. Only a verified WIN is added to
    that day's quota count; a loss is marked credited (so it's not
    rechecked forever) but never counts against the FREE quota. Still-open
    trades are left uncredited and are re-checked on the next call.
    Returns True if anything changed (caller should persist)."""
    receipts = u.get("receipts", [])
    pending = [r for r in receipts if not r.get("credited")]
    if not pending:
        return False
    from signal_log import load_records
    recs = load_records()
    by_ts = {}
    for r in recs:
        by_ts.setdefault(r.get("ts"), []).append(r)
    daily = u.setdefault("daily", {})
    changed = False
    for rec in pending:
        matches = by_ts.get(rec.get("signal_ts"), [])
        match = None
        for m in matches:
            if (m.get("asset") == rec.get("asset")
                    and m.get("tf") == rec.get("tf")
                    and m.get("direction") == rec.get("direction")):
                match = m
                break
        if match is None or match.get("status") not in ("won", "lost"):
            continue  # not resolved yet - try again next time
        rec["credited"] = True
        changed = True
        if match["status"] == "won":
            day = str(rec.get("signal_ts"))[:10]
            daily[day] = daily.get(day, 0) + 1
    return changed


def _load_credited(chat_id):
    """Return this user's record with any newly-resolved wins credited to
    the daily quota first, persisting if anything changed."""
    chat_id = str(chat_id)
    with _lock:
        data = _load()
        u = data.get(chat_id)
        if u is None:
            return {}
        if _credit_wins(u):
            _save(data)
        return dict(u)


def today_used(chat_id):
    u = _load_credited(chat_id)
    if not u:
        return 0
    return int(u.get("daily", {}).get(_today(), 0))


def remaining_today(chat_id):
    if is_pro(chat_id) or is_admin(chat_id):
        return None  # unlimited
    return max(0, cfg.FREE_DAILY_SIGNALS - today_used(chat_id))


def signal_allowed(chat_id):
    """May this chat receive a broadcast signal right now?"""
    if is_admin(chat_id) or is_pro(chat_id):
        return True
    return today_used(chat_id) < cfg.FREE_DAILY_SIGNALS


def should_notify_upgrade(chat_id):
    """True at most once per UTC day - the first time a FREE user's win
    quota for the day is found to be used up. Callers should follow a
    True result by messaging cfg.ADMIN_CONTACT's upgrade nudge."""
    chat_id = str(chat_id)
    today = _today()
    with _lock:
        data = _load()
        u = data.get(chat_id)
        if u is None or u.get("upgrade_notified") == today:
            return False
        u["upgrade_notified"] = today
        _save(data)
        return True


# ---------------- mutations ----------------

def grant_pro(chat_id, days=None):
    """Grant PRO (days=None = lifetime). Returns True on success."""
    chat_id = str(chat_id)
    with _lock:
        data = _load()
        u = data.setdefault(chat_id, {
            "plan": "free", "pro_until": None,
            "joined": _now().isoformat(), "username": None, "name": None,
            "last_seen": _now().isoformat(), "daily": {}, "receipts": [],
        })
        u["plan"] = "pro"
        u["pro_until"] = None if days is None else \
            (_now() + dt.timedelta(days=int(days))).isoformat()
        u["pro_expiry_notified"] = None  # reset so a renewal can be reminded again
        _save(data)
    # affiliate bonus: a referred user going PRO earns the referrer PRO days
    try:
        if days is not None and str(chat_id) != str(cfg.TELEGRAM_CHAT_ID):
            reward_referrer(chat_id)
    except Exception:
        pass
    return True


def revoke_pro(chat_id):
    chat_id = str(chat_id)
    with _lock:
        data = _load()
        if chat_id not in data:
            return False
        data[chat_id]["plan"] = "free"
        data[chat_id]["pro_until"] = None
        _save(data)
    return True


# ---------------- referrals (growth engine) ----------------
# Every user has a shareable deep link `t.me/<bot>?start=r<chat_id>`. When a
# new user joins through it, the referrer is credited. If the referred user
# later goes PRO, the referrer earns REFERRAL_REWARD_DAYS of PRO as a bonus
# (capped) - a simple, fully-automatic affiliate program.

def _ref_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        cfg.REFERRALS_FILE)


def _ref_load():
    try:
        with open(_ref_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _ref_save(data):
    with open(_ref_path(), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def referral_code(chat_id):
    """Stable referral code for a chat: `r<chat_id>`."""
    return f"r{chat_id}"


def referral_link(chat_id):
    """Shareable deep link. Uses cfg.BOT_URL when it looks like a bot handle."""
    base = str(cfg.BOT_URL).strip().rstrip("/")
    if not base:
        base = "t.me/cryto_trader_bot"
    if not base.startswith("http"):
        base = "https://" + base
    return f"{base}?start={referral_code(chat_id)}"


def register_referral(chat_id, code):
    """Credit a referral from a `/start <code>` deep link.
    Returns True when a new referral was registered (idempotent per user)."""
    chat_id = str(chat_id)
    code = str(code or "").strip().lower()
    if not code.startswith("r"):
        return False
    referrer = code[1:]
    if not referrer.isdigit() or referrer == chat_id:
        return False
    with _lock:
        data = _ref_load()
        entry = data.setdefault(referrer, {"referred": [], "pro_grants": 0})
        if chat_id not in entry["referred"]:
            entry["referred"].append(chat_id)
            _ref_save(data)
            return True
    return False


def referred_by(chat_id):
    """The referrer chat_id of this user, or None."""
    chat_id = str(chat_id)
    with _lock:
        data = _ref_load()
        for ref, e in data.items():
            if chat_id in e.get("referred", []):
                return ref
    return None


def referral_stats(chat_id):
    """{code, link, total, pro_grants} for one user's /ref card."""
    chat_id = str(chat_id)
    with _lock:
        data = _ref_load()
        e = data.get(chat_id)
    total = len(e.get("referred", [])) if e else 0
    grants = e.get("pro_grants", 0) if e else 0
    return {"code": referral_code(chat_id),
            "link": referral_link(chat_id),
            "total": total, "pro_grants": grants}


def reward_referrer(chat_id):
    """Extend the referrer's PRO when `chat_id` (a referred user) goes PRO.
    Returns the number of days added (0 when no referrer or cap reached)."""
    chat_id = str(chat_id)
    ref = referred_by(chat_id)
    if not ref:
        return 0
    with _lock:
        data = _ref_load()
        e = data.setdefault(ref, {"referred": [], "pro_grants": 0})
        max_grants = max(1, cfg.REFERRAL_REWARD_MAX_DAYS // cfg.REFERRAL_REWARD_DAYS)
        if e.get("pro_grants", 0) >= max_grants:
            return 0
        e["pro_grants"] = e.get("pro_grants", 0) + 1
        _ref_save(data)
    # extend the referrer's PRO, creating their record if needed
    with _lock:
        data = _load()
        u = data.setdefault(ref, {
            "plan": "free", "pro_until": None,
            "joined": _now().isoformat(), "username": None, "name": None,
            "last_seen": _now().isoformat(), "daily": {}, "receipts": [],
        })
        base = dt.datetime.fromisoformat(u["pro_until"]) \
            if u.get("pro_until") else _now()
        if base < _now():
            base = _now()
        u["plan"] = "pro"
        u["pro_until"] = (base + dt.timedelta(days=cfg.REFERRAL_REWARD_DAYS)).isoformat()
        _save(data)
    return cfg.REFERRAL_REWARD_DAYS


# ---------------- admin registry / username lookup ----------------

def normalize_target(value):
    """Normalize an admin target: '@user', 'user' or numeric chat id."""
    return str(value).strip().lstrip("@").strip().lower()


def find_by_username(value):
    """Resolve '@username' (or a chat id) to a stored chat_id, or None.
    Usernames match case-insensitively and without the leading @."""
    target = normalize_target(value)
    if not target:
        return None
    if target.isdigit():
        return target
    with _lock:
        data = _load()
        for cid, u in data.items():
            uname = (u.get("username") or "").lower()
            if uname == target:
                return cid
    return None


def resolve_target(value):
    """Admin-friendly resolution: returns (chat_id, display) or (None, None).
    Accepts a numeric chat id directly, or a username the user has used with
    the bot at least once (/start or any message)."""
    target = normalize_target(value)
    if not target:
        return None, None
    if target.isdigit():
        return target, target
    cid = find_by_username(target)
    if cid is not None:
        return cid, f"@{target}"
    return None, None


def expiring_pro(days=3):
    """PRO users whose plan expires within `days` (lifetime PRO excluded).
    Returns list of {chat_id, username, name, pro_until}."""
    now = _now()
    horizon = now + dt.timedelta(days=days)
    out = []
    with _lock:
        data = _load()
        for cid, u in data.items():
            if u.get("plan") != "pro" or not u.get("pro_until"):
                continue
            try:
                until = dt.datetime.fromisoformat(u["pro_until"])
            except (TypeError, ValueError):
                continue
            if now < until <= horizon:
                out.append({"chat_id": cid, "username": u.get("username"),
                            "name": u.get("name"), "pro_until": until})
    return sorted(out, key=lambda r: r["pro_until"])


def pro_expiry_already_notified(chat_id, until_iso):
    """True if this user was already reminded about this exact expiry date
    (so a renewal, which resets pro_expiry_notified, can be reminded again
    when it later approaches its own new expiry)."""
    u = get_user(chat_id, create=False)
    return bool(u) and u.get("pro_expiry_notified") == until_iso


def mark_pro_expiry_notified(chat_id, until_iso):
    chat_id = str(chat_id)
    with _lock:
        data = _load()
        if chat_id not in data:
            return
        data[chat_id]["pro_expiry_notified"] = until_iso
        _save(data)


def active_today():
    """Number of users who received at least one signal today + total signals."""
    today = _today()
    active = 0
    total = 0
    with _lock:
        data = _load()
        for u in data.values():
            n = int(u.get("daily", {}).get(today, 0))
            if n:
                active += 1
                total += n
    return active, total


def list_users():
    """All registered users, oldest first. Each item:
    {chat_id, username, name, plan, pro_until, joined, last_seen,
     signals_today, signals_total}"""
    today = _today()
    with _lock:
        data = _load()
        out = []
        for cid, u in sorted(data.items(), key=lambda kv: kv[1].get("joined", "")):
            rec = dict(u)
            rec["chat_id"] = cid
            rec["signals_today"] = int(rec.get("daily", {}).get(today, 0))
            rec["signals_total"] = len(rec.get("receipts", []))
            out.append(rec)
        return out


def user_detail(chat_id):
    """One user's full record for the admin /user command."""
    return get_user(chat_id, create=False)


def record_signal(chat_id, signal_ts, asset, tf, variant, direction, entry):
    """Append one sent signal to the user's receipt ledger. It does NOT
    count against the FREE daily quota yet - that only happens once the
    trade resolves as a verified WIN (see _credit_wins), so a losing trade
    never costs the user part of their daily allowance."""
    chat_id = str(chat_id)
    with _lock:
        data = _load()
        u = data.setdefault(chat_id, {
            "plan": "free", "pro_until": None,
            "joined": _now().isoformat(), "username": None, "name": None,
            "last_seen": _now().isoformat(), "daily": {}, "receipts": [],
        })
        u["receipts"].append({
            "signal_ts": signal_ts,
            "asset": asset,
            "tf": tf,
            "variant": variant,
            "direction": direction,
            "entry": round(float(entry), 8),
            "received_at": _now().isoformat(),
            "credited": False,
        })
        _save(data)
    return True


# ---------------- portfolio (PRO) ----------------

def portfolio_stats(chat_id):
    """PRO portfolio: resolve the user's received signals against the global
    verified signal log and return win rate + hypothetical P&L at PAYOUT."""
    from signal_log import load_records
    u = get_user(chat_id, create=False)
    if not u or not u.get("receipts"):
        return {"n": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "pnl": 0.0, "open": 0}
    recs = load_records()
    by_ts = {}
    for r in recs:
        by_ts.setdefault(r.get("ts"), []).append(r)
    wins = losses = open_n = 0
    for rec in u["receipts"]:
        matches = by_ts.get(rec["signal_ts"], [])
        match = None
        for m in matches:
            if (m.get("asset") == rec["asset"] and m.get("tf") == rec["tf"]
                    and m.get("direction") == rec["direction"]):
                match = m
                break
        if match is None:
            continue
        if match.get("status") in ("won", "lost"):
            if match["status"] == "won":
                wins += 1
            else:
                losses += 1
        else:
            open_n += 1
    n = wins + losses
    win_rate = wins / n if n else 0.0
    pnl = round(wins * cfg.PAYOUT - losses, 4)
    return {"n": n, "wins": wins, "losses": losses, "open": open_n,
            "win_rate": win_rate, "pnl": pnl}


def portfolio_text(chat_id):
    s = portfolio_stats(chat_id)
    lines = ["*📊 Portfolio (PRO)*", ""]
    if s["n"] == 0:
        lines.append("No resolved signals yet - your received signals are "
                     "tracked here automatically once they expire.")
        return "\n".join(lines)
    lines += [
        f"Resolved trades: {s['n']}  (open: {s['open']})",
        f"Wins: {s['wins']}  |  Losses: {s['losses']}",
        f"Win rate: *{s['win_rate']:.0%}*",
        f"Hypothetical P&L (payout {cfg.PAYOUT:.0%}): "
        f"{'+' if s['pnl'] >= 0 else ''}{s['pnl']:.2f} stakes",
        "",
        "_Verified against live market data after expiry - not a backtest._",
    ]
    return "\n".join(lines)