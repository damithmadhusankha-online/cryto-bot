"""Token & configuration manager for the signal bot.

One tool to validate, rotate and manage the Telegram bot token and the
product-level settings stored in .env (admin chat, bot URL, payment link,
admin panel password). Never prints the full token - only a masked preview.

Usage:
    python tokens.py status                # validate token + show bot identity
    python tokens.py set <TOKEN>           # replace the bot token (backup .env)
    python tokens.py admin <CHAT_ID>       # set the admin chat id
    python tokens.py boturl <URL>          # set the promo bot link (t.me/...)
    python tokens.py payment <URL>         # set the payment/upgrade link
    python tokens.py password [VALUE]      # set/rotate the admin panel password
                                           # (generates a strong one if omitted)
    python tokens.py panel [HOST] [PORT]   # set admin panel bind address

Every command validates input before touching .env; .env is backed up to
.env.bak before each change. `status` talks to the Telegram API directly, so
it proves the token is live (not just present).
"""
import argparse
import hashlib
import json
import os
import random
import re
import secrets
import string
import sys
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")
CHAT_RE = re.compile(r"^-?\d{5,15}$")
URL_RE = re.compile(r"^(https?://|t\.me/)")


# ---------------- .env editing ----------------

def load_env():
    """Parse .env into (list_of_lines, dict). Preserves comments/order."""
    if not os.path.exists(ENV_PATH):
        return [], {}
    with open(ENV_PATH, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    env = {}
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, _, v = s.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return lines, env


def set_env_var(key, value, comment=None):
    """Set KEY=VALUE in .env (backup first). Returns the value written."""
    lines, _ = load_env()
    new_line = f"{key}={value}"
    if comment:
        new_line = f"# {comment}\n{new_line}"
    replaced = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("#") or "=" not in s:
            continue
        k = s.partition("=")[0].strip()
        if k == key:
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)
    if os.path.exists(ENV_PATH):
        os.replace(ENV_PATH, ENV_PATH + ".bak")
    with open(ENV_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return value


def masked(token):
    """Mask a token: 123456789:ABC...xyZ (safe to show anywhere)."""
    if not token:
        return "(empty)"
    if len(token) <= 12:
        return "*" * len(token)
    return token[:9] + "..." + token[-4:]


# ---------------- Telegram API ----------------

def get_me(token):
    """Validate a token against the Telegram API. Returns bot dict or None."""
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("result") if data.get("ok") else None
    except Exception as exc:
        print(f"  API unreachable: {exc}")
        return None


def check_token(token, label="configured"):
    """Validate a token; print a one-line verdict. Returns bool."""
    bot = get_me(token)
    if bot is None:
        print(f"  {label} token: INVALID or API unreachable (token {masked(token)})")
        return False
    print(f"  {label} token: VALID - @{bot.get('username', '?')} "
          f"({bot.get('first_name', '?')}, id {bot.get('id')})")
    return True


# ---------------- commands ----------------

def cmd_status(_args):
    print("=== CRYTO Signal Bot - token & config status ===\n")
    lines, env = load_env()
    if not lines:
        print("  .env not found - nothing configured.")
        return 1
    tok = env.get("TG_BOT_TOKEN", "")
    if not tok:
        print("  TG_BOT_TOKEN: (missing) - run `python tokens.py set <TOKEN>`")
    elif not TOKEN_RE.match(tok):
        print(f"  TG_BOT_TOKEN: MALFORMED ({masked(tok)}) - expected 123456:ABC...")
        check_token(tok)
    else:
        check_token(tok)
    chat = env.get("TG_CHAT_ID", "")
    print("  TG_CHAT_ID (admin): " + (chat if chat else "(missing)"))
    print("  TG_BOT_URL: " + (env.get("TG_BOT_URL") or "(missing - referral links broken)"))
    print("  PAYMENT_LINK: " + (env.get("PAYMENT_LINK") or "(not set - /upgrade shows no payment)"))
    pw = env.get("ADMIN_PASSWORD", "")
    print("  ADMIN_PASSWORD: " + ("SET (panel protected)" if pw else "NOT SET - admin panel refuses to start"))
    host = env.get("PANEL_HOST", "127.0.0.1")
    port = env.get("PANEL_PORT", "8080")
    print(f"  Admin panel bind: {host}:{port}")
    print("\nBot handle for referrals: t.me/" + (env.get("TG_BOT_URL", "cryto_trader_bot")
          .replace("https://", "").replace("http://", "").replace("t.me/", "").rstrip("/")))
    print("\nHint: `python tokens.py help` lists every command.")
    return 0


def cmd_set(args):
    tok = args.token.strip()
    if not TOKEN_RE.match(tok):
        print(f"ERROR: malformed token. Expected format 123456789:AAbbCC... (got {masked(tok)})")
        return 1
    print(f"Validating new token {masked(tok)} against the Telegram API...")
    bot = get_me(tok)
    if bot is None:
        print("  Validation failed - token rejected by Telegram. Not saved.")
        return 1
    print(f"  OK: @{bot.get('username', '?')} ({bot.get('first_name', '?')})")
    set_env_var("TG_BOT_TOKEN", tok,
                comment="Telegram bot token (from @BotFather). Never share or commit this.")
    print(f"  Token saved to .env (backup at .env.bak).")
    return 0


def cmd_admin(args):
    chat = str(args.chat_id).strip()
    if not CHAT_RE.match(chat):
        print(f"ERROR: chat id must be numeric (got {chat!r}).")
        return 1
    set_env_var("TG_CHAT_ID", chat, comment="Admin chat id - receives every signal + runs admin commands.")
    print(f"Admin chat id set to {chat}. Reload/restart the bot to apply.")
    return 0


def cmd_boturl(args):
    url = args.url.strip()
    if not URL_RE.match(url):
        print("ERROR: expected a link like t.me/your_bot or https://t.me/your_bot")
        return 1
    set_env_var("TG_BOT_URL", url, comment="Public bot link used on referral cards.")
    print(f"BOT_URL set to {url}.")
    return 0


def cmd_payment(args):
    url = args.url.strip()
    if not URL_RE.match(url):
        print("ERROR: expected a payment/upgrade link (https://...).")
        return 1
    set_env_var("PAYMENT_LINK", url, comment="Where users pay for PRO (PayPal, CoinsPaid, crypto address page, ...).")
    print(f"PAYMENT_LINK set to {url}.")
    return 0


def cmd_password(args):
    if args.value:
        pw = args.value.strip()
        if len(pw) < 8:
            print("ERROR: password must be at least 8 characters.")
            return 1
    else:
        alphabet = string.ascii_letters + string.digits
        pw = "".join(secrets.choice(alphabet) for _ in range(18))
    set_env_var("ADMIN_PASSWORD", pw, comment="Password for the admin web panel (admin_web.py).")
    print("Admin panel password set.")
    print(f"  -> {pw}")
    print("  Store it somewhere safe; it is only shown once (rotating it here revokes old sessions).")
    return 0


def cmd_panel(args):
    host = args.host or "127.0.0.1"
    port = str(args.port or 8080)
    if host not in ("127.0.0.1", "localhost"):
        print("WARNING: binding outside localhost exposes the panel to the network -")
        print("         make sure ADMIN_PASSWORD is a strong value and a firewall blocks it.")
    set_env_var("PANEL_HOST", host, comment="Admin web panel bind address (keep 127.0.0.1).")
    set_env_var("PANEL_PORT", port, comment="Admin web panel port.")
    print(f"Panel bind set to {host}:{port}.")
    return 0


def cmd_help(_args):
    print(__doc__)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="CRYTO Signal Bot - token & config manager")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="validate token + show config health")
    p_set = sub.add_parser("set", help="set/rotate the Telegram bot token")
    p_set.add_argument("token")
    p_admin = sub.add_parser("admin", help="set the admin chat id")
    p_admin.add_argument("chat_id")
    p_url = sub.add_parser("boturl", help="set the public bot link")
    p_url.add_argument("url")
    p_pay = sub.add_parser("payment", help="set the PRO payment link")
    p_pay.add_argument("url")
    p_pw = sub.add_parser("password", help="set/rotate the admin panel password")
    p_pw.add_argument("value", nargs="?", default=None)
    p_panel = sub.add_parser("panel", help="set the admin panel bind address")
    p_panel.add_argument("host", nargs="?", default=None)
    p_panel.add_argument("port", nargs="?", default=None)
    sub.add_parser("help", help="show all commands")

    args = ap.parse_args(argv)
    handlers = {
        "status": cmd_status, "set": cmd_set, "admin": cmd_admin,
        "boturl": cmd_boturl, "payment": cmd_payment,
        "password": cmd_password, "panel": cmd_panel, "help": cmd_help,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())