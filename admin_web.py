"""Lightweight admin web panel for the CRYTO Signal Bot.

A small, dependency-free dashboard (Python stdlib only) that lets the admin:

  - see every user using the bot (username, name, plan, signal usage)
  - activate PRO for a user BY USERNAME or chat id (with optional days)
  - revoke PRO, view user details
  - monitor the live track record + watchlist

Security:
  - bound to 127.0.0.1 by default (cfg.PANEL_HOST / PANEL_PORT)
  - password from cfg.ADMIN_PASSWORD; refuses to start without one
  - session cookie signed with an HMAC key derived from the password
  - constant-time password comparison

Usage:
    python admin_web.py            # serve on 127.0.0.1:8080 (blocking)
    python admin_web.py --port 9000

Set the password first:  python tokens.py password
"""
import argparse
import datetime as dt
import hashlib
import hmac
import http.cookies
import http.server
import json
import os
import secrets
import sys
import urllib.parse

import config as cfg
import subscriptions

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

SESSION_TTL_HOURS = 24
# pepper + password -> signing key; rotating the password invalidates sessions
_KEY = hashlib.sha256(
    ("crypto-signal-panel::" + cfg.ADMIN_PASSWORD).encode("utf-8")
).digest() if cfg.ADMIN_PASSWORD else b""


def _sign(value):
    return hmac.new(_KEY, value.encode("utf-8"), hashlib.sha256).hexdigest()


def _session_ok(cookie_val):
    """Validate a 'user:<exp>:<sig>' session cookie."""
    if not cookie_val:
        return False
    parts = cookie_val.split(":")
    if len(parts) != 3 or parts[0] != "user":
        return False
    _, exp, sig = parts
    try:
        exp_i = int(exp)
    except ValueError:
        return False
    if exp_i < int(dt.datetime.now(dt.timezone.utc).timestamp()):
        return False
    return hmac.compare_digest(sig, _sign(f"user:{exp}"))


def _cookie_for():
    exp = int(dt.datetime.now(dt.timezone.utc).timestamp()) + SESSION_TTL_HOURS * 3600
    val = f"user:{exp}:{_sign(f'user:{exp}')}"
    return f"crypto_admin={val}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL_HOURS * 3600}"


def _check_password(given):
    return hmac.compare_digest(given.encode("utf-8"), cfg.ADMIN_PASSWORD.encode("utf-8"))


# ---------------- page rendering ----------------

_CSS = """
body{font-family:Segoe UI,Arial,sans-serif;margin:0;background:#0f172a;color:#e2e8f0}
.wrap{max-width:1100px;margin:0 auto;padding:24px}
h1{font-size:22px;margin:0 0 4px} h2{font-size:17px;margin:24px 0 8px}
.sub{color:#94a3b8;font-size:13px;margin-bottom:18px}
.cards{display:flex;gap:14px;flex-wrap:wrap;margin:18px 0}
.card{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px 20px;min-width:150px}
.card b{display:block;font-size:24px} .card span{color:#94a3b8;font-size:12px}
table{width:100%;border-collapse:collapse;background:#1e293b;border-radius:10px;overflow:hidden;font-size:13px}
th{background:#334155;text-align:left;padding:9px 12px;color:#cbd5e1}
td{padding:9px 12px;border-top:1px solid #334155}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600}
.pro{background:#f59e0b;color:#0f172a} .free{background:#475569;color:#e2e8f0}
a{color:#7dd3fc;text-decoration:none} a:hover{text-decoration:underline}
form.inline{display:inline}
input,button{padding:7px 10px;border-radius:8px;border:1px solid #475569;background:#0f172a;color:#e2e8f0;font-size:13px}
button{background:#2563eb;border:none;cursor:pointer;font-weight:600}
button.danger{background:#dc2626}
button.ok{background:#16a34a}
.err{color:#fca5a5;background:#7f1d1d;padding:10px 14px;border-radius:8px;margin:12px 0}
.msg{color:#86efac;background:#14532d;padding:10px 14px;border-radius:8px;margin:12px 0}
.nav{background:#1e293b;padding:10px 24px;border-bottom:1px solid #334155;display:flex;gap:18px;align-items:center}
.nav b{color:#f59e0b} .nav a{font-size:14px}
.login{max-width:360px;margin:80px auto;background:#1e293b;border:1px solid #334155;border-radius:12px;padding:28px}
.table-wrap{overflow-x:auto;border-radius:10px}
.search{width:100%;max-width:320px;margin-bottom:12px;display:block}
tr.hide{display:none}
.badge.warn{background:#f59e0b;color:#0f172a}
@media (max-width:640px){
  .wrap{padding:14px}
  .nav{padding:10px 14px;flex-wrap:wrap;gap:10px}
  .cards{gap:8px}
  .card{min-width:120px;padding:10px 14px}
  .card b{font-size:19px}
  table{font-size:12px}
  th,td{padding:7px 8px}
}
"""

_SEARCH_JS = """
<script>
function filterUsers(el){
  var q=(el.value||'').toLowerCase();
  document.querySelectorAll('table.searchable tr[data-row]').forEach(function(tr){
    var hit=tr.dataset.row.indexOf(q)!==-1;
    tr.classList.toggle('hide', q.length>0 && !hit);
  });
}
</script>
"""


def _page(title, body, nav=True):
    nav_html = ('<div class="nav"><b>CRYTO Admin</b>'
                '<a href="/">Overview</a><a href="/users">Users</a>'
                '<a href="/track">Track record</a><a href="/watchlist">Watchlist</a>'
                '<a href="/logout">Logout</a></div>') if nav else ""
    return ("<!doctype html><html><head><meta charset=utf-8>"
            f"<title>{title} - CRYTO Admin</title><style>{_CSS}</style></head>"
            f"<body>{nav_html}<div class='wrap'><h1>{title}</h1>"
            f"<div class='sub'>{dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC</div>"
            f"{body}</div></body></html>")


def _overview_html():
    users = subscriptions.list_users()
    pro_n = sum(1 for u in users if u.get("plan") == "pro")
    free_n = len(users) - pro_n
    active, delivered = subscriptions.active_today()
    try:
        from signal_log import stats
        s = stats()
    except Exception:
        s = {"total": 0, "resolved": 0, "open": 0, "win_rate": 0.0}
    try:
        from scanner import load_watchlist
        watch_n = len(load_watchlist())
    except Exception:
        watch_n = 0
    cards = [
        ("Users", len(users)), ("PRO 👑", pro_n), ("FREE", free_n),
        ("Signals today", delivered), ("Active today", active),
        ("Watchlist pairs", watch_n), ("Signals sent", s["total"]),
        ("Resolved", s["resolved"]),
        ("Live win rate", f"{s['win_rate']:.0%}"),
    ]
    html = "<div class='cards'>" + "".join(
        f"<div class='card'><b>{v}</b><span>{k}</span></div>" for k, v in cards) + "</div>"
    if not users:
        html += ("<p>No users yet. Share the bot link "
                 f"<b>t.me/{_bot_handle()}</b> so people can /start it.</p>")
    else:
        rows = "".join(
            f"<tr><td>{_user_link(u)}</td><td>{u.get('name') or '-'}</td>"
            f"<td><span class='badge {('pro' if u.get('plan')=='pro' else 'free')}'>"
            f"{'PRO' if u.get('plan')=='pro' else 'FREE'}</span></td>"
            f"<td>{u['signals_today']}</td><td>{u['signals_total']}</td>"
            f"<td>{str(u.get('last_seen'))[:16]}</td></tr>"
            for u in sorted(users, key=lambda r: r.get('last_seen') or '', reverse=True)[:12])
        html += ("<h2>Recent users</h2><div class='table-wrap'><table><tr><th>User</th><th>Name</th>"
                 "<th>Plan</th><th>Today</th><th>Total</th><th>Last seen</th></tr>"
                 f"{rows}</table></div><p><a href='/users'>All users →</a></p>")
    return html


def _bot_handle():
    url = str(cfg.BOT_URL or "").replace("https://", "").replace("http://", "").replace("t.me/", "")
    return url.rstrip("/") or "cryto_trader_bot"


def _user_link(u):
    who = u.get("username") or u.get("name") or u["chat_id"]
    return f"<a href='/user/{u['chat_id']}'>{who}</a>"


def _users_html(flash=None):
    users = subscriptions.list_users()
    exp = subscriptions.expiring_pro(days=3)
    rows = "".join(
        f"<tr data-row='{(u.get('username') or '').lower()} {(u.get('name') or '').lower()} {u['chat_id']}'>"
        f"<td>{_user_link(u)}</td><td>{u.get('name') or '-'}</td>"
        f"<td>{u['chat_id']}</td>"
        f"<td><span class='badge {('pro' if u.get('plan')=='pro' else 'free')}'>"
        f"{'PRO' if u.get('plan')=='pro' else 'FREE'}</span></td>"
        f"<td>{u.get('pro_until') or ('lifetime' if u.get('plan')=='pro' else '-')}</td>"
        f"<td>{u['signals_today']}</td><td>{u['signals_total']}</td>"
        f"<td>{str(u.get('joined'))[:10]}</td></tr>"
        for u in users)
    exp_html = ""
    if exp:
        exp_html = ("<h2>\u26a0\ufe0f PRO expiring within 3 days</h2><div class='table-wrap'>"
                    "<table><tr><th>User</th>"
                    "<th>Until</th><th>Renew</th></tr>" + "".join(
                        f"<tr><td>{e.get('username') or e['chat_id']}</td>"
                        f"<td>{e['pro_until']:%Y-%m-%d}</td>"
                        f"<td><form class='inline' method='post' action='/grant'>"
                        f"<input type=hidden name=target value='{e.get('username') or e['chat_id']}'>"
                        f"<input type=hidden name=days value=30>"
                        f"<button class='ok'>+30 days</button></form></td></tr>"
                        for e in exp) + "</table></div>")
    flash_html = f"<div class='msg'>{flash}</div>" if flash else ""
    return flash_html + exp_html + (
            f"<h2>All users ({len(users)})</h2>"
            "<input class='search' type='text' placeholder='Search by username, name or chat id\u2026' "
            "oninput='filterUsers(this)'>"
            "<div class='table-wrap'><table class='searchable'><tr><th>User</th><th>Name</th>"
            "<th>Chat ID</th><th>Plan</th><th>PRO until</th><th>Signals today</th>"
            "<th>Signals total</th><th>Joined</th></tr>" + rows + "</table></div>"
            "<p><b>Activate PRO:</b> <form method='post' action='/grant'>"
            "<input name='target' placeholder='@username or chat id' required>"
            "<input name='days' type='number' min=1 placeholder='days (blank = lifetime)'>"
            "<button class='ok'>Grant PRO</button></form></p>" + _SEARCH_JS)



def _user_detail_html(cid, flash=None):
    u = subscriptions.user_detail(cid)
    if not u:
        return f"<div class='err'>No record for chat id {cid}.</div>"
    until = subscriptions.pro_until(cid)
    try:
        port = subscriptions.portfolio_stats(cid)
        p_line = (f"Resolved {port['n']} · win rate {port['win_rate']:.0%} · "
                  f"P&L {port['pnl']:+.2f} stakes")
    except Exception:
        p_line = "portfolio unavailable"
    flash_html = f"<div class='msg'>{flash}</div>" if flash else ""
    plan_badge = ("<span class='badge pro'>PRO</span>" if u.get("plan") == "pro"
                  else "<span class='badge free'>FREE</span>")
    rows = [
        ("Username", f"@{u.get('username')}" if u.get("username") else "-"),
        ("Name", u.get("name") or "-"),
        ("Chat ID", f"<code>{cid}</code>"),
        ("Plan", plan_badge),
        ("PRO until", until.strftime("%Y-%m-%d") if until else
         ("lifetime" if u.get("plan") == "pro" else "-")),
        ("Joined", str(u.get("joined"))[:16]),
        ("Last seen", str(u.get("last_seen"))[:16]),
        ("Signals today", u.get("daily", {}).get(
            dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"), 0)),
        ("Signals received", len(u.get("receipts", []))),
        ("Portfolio", p_line),
    ]
    body = flash_html + "<div class='table-wrap'><table>" + "".join(
        f"<tr><th style='width:160px'>{k}</th><td>{v}</td></tr>" for k, v in rows) + "</table></div>"
    if u.get("plan") == "pro":
        body += ("<h2>Manage</h2><form method='post' action='/revoke'>"
                 f"<input type=hidden name=target value='{cid}'>"
                 "<button class='danger'>Revoke PRO</button></form>")
    else:
        body += ("<h2>Activate PRO</h2>"
                 "<form method='post' action='/grant'>"
                 f"<input type=hidden name=target value='{cid}'>"
                 "<input name='days' type='number' min=1 placeholder='days (blank = lifetime)'>"
                 "<button class='ok'>Grant PRO</button></form>")
    body += "<p><a href='/users'>← back to users</a></p>"
    return body


def _track_html():
    try:
        from signal_log import report_text, stats
        s = stats()
    except Exception as exc:
        return f"<div class='err'>signal log unavailable: {exc}</div>"
    head = (f"<p>Signals sent: <b>{s['total']}</b> · open: {s['open']} · "
            f"resolved: {s['resolved']} · live win rate: "
            f"<b>{s['win_rate']:.0%}</b> · last 30: "
            f"{s['last30']['wins']}/{s['last30']['n']} ({s['last30']['win_rate']:.0%})</p>")
    rows = "".join(
        f"<tr><td>{k}</td><td>{v['wins']}/{v['n']}</td>"
        f"<td>{v['win_rate']:.0%}</td></tr>"
        for k, v in list(s["by_market"].items())[:15])
    return head + ("<h2>By market (resolved)</h2><div class='table-wrap'><table><tr><th>Market</th>"
                   "<th>Wins/N</th><th>Win rate</th></tr>" + rows + "</table></div>"
                   "<h2>Variants</h2><div class='table-wrap'><table><tr><th>Variant</th><th>Wins/N</th>"
                   "<th>Win rate</th></tr>" + "".join(
                       f"<tr><td>{k}</td><td>{v['wins']}/{v['n']}</td>"
                       f"<td>{v['win_rate']:.0%}</td></tr>"
                       for k, v in s["by_variant"].items()) + "</table></div>")


def _watchlist_html():
    try:
        import pandas as pd
        from scanner import watchlist_rows
        rows = watchlist_rows()
    except Exception as exc:
        return f"<div class='err'>watchlist unavailable: {exc}</div>"
    if not rows:
        return ("<p>No validated watchlist rows. Run "
                "<code>python bot.py scan --top 12</code> to build it.</p>")
    html_rows = "".join(
        f"<tr><td>{asset}</td><td>{tf}</td><td>{r['category']}</td>"
        f"<td>{r['variant']}</td><td>{r['oos_ev']:+.3f}</td>"
        f"<td>{r['win_rate']:.0%}</td><td>{r['n_trades']}</td>"
        f"<td>{r['rank_score']:.2f}</td></tr>"
        for (asset, tf), r in sorted(rows.items(), key=lambda kv: -kv[1]["rank_score"]))
    return ("<div class='table-wrap'><table><tr><th>Asset</th><th>TF</th><th>Category</th><th>Variant</th>"
            "<th>OOS EV</th><th>Win rate</th><th>OOS trades</th><th>Rank</th></tr>"
            + html_rows + "</table></div><p><em>Walk-forward out-of-sample validation - "
            "the only rows the live loop broadcasts.</em></p>")


# ---------------- handler ----------------

class PanelHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # quiet - admin panel shouldn't spam logs

    def _send(self, code, body, cookie=None):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body.encode("utf-8"))))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def _cookie(self):
        raw = self.headers.get("Cookie", "")
        c = http.cookies.SimpleCookie(raw)
        if "crypto_admin" in c:
            return c["crypto_admin"].value
        return None

    def _authed(self):
        return bool(cfg.ADMIN_PASSWORD) and _session_ok(self._cookie())

    def _login_page(self, err=None):
        err_html = f"<div class='err'>{err}</div>" if err else ""
        body = (f"<div class='login'><h1>CRYTO Admin</h1>"
                f"<div class='sub'>Password required</div>{err_html}"
                "<form method='post' action='/login'>"
                "<input type='password' name='pw' placeholder='panel password' "
                "autofocus style='width:100%;margin-bottom:10px'>"
                "<button style='width:100%'>Sign in</button></form></div>")
        return _page("Sign in", body, nav=False)

    def do_GET(self):
        if not self._authed():
            self._send(200, self._login_page())
            return
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/overview"):
            self._send(200, _page("Overview", _overview_html()))
        elif path == "/users":
            self._send(200, _page("Users", _users_html()))
        elif path == "/track":
            self._send(200, _page("Track record", _track_html()))
        elif path == "/watchlist":
            self._send(200, _page("Watchlist", _watchlist_html()))
        elif path.startswith("/user/"):
            cid = path[len("/user/"):]
            self._send(200, _page("User detail", _user_detail_html(cid)))
        elif path == "/logout":
            self._send(200, "Logged out.",
                       cookie="crypto_admin=; Path=/; HttpOnly; Max-Age=0")
        else:
            self._send(404, _page("Not found", "<p>404</p>"))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        params = dict(urllib.parse.parse_qsl(body))
        path = urllib.parse.urlparse(self.path).path

        if path == "/login":
            if _check_password(params.get("pw", "")):
                self._send(200, _page("Logged in", "<p>OK.</p>"), cookie=_cookie_for())
            else:
                self._send(200, self._login_page(err="Wrong password."))
            return
        if not self._authed():
            self._send(200, self._login_page())
            return
        if path == "/grant":
            target = params.get("target", "").strip()
            days_raw = params.get("days", "").strip()
            days = int(days_raw) if days_raw.isdigit() and int(days_raw) > 0 else None
            cid, display = subscriptions.resolve_target(target)
            if cid is None:
                self._send(200, _page("Users", _users_html(
                    flash=f"User {target!r} not found - they must /start the bot once.")))
                return
            subscriptions.grant_pro(cid, days)
            who = display or cid
            self._send(200, _page("Users", _users_html(
                flash=f"PRO granted to {who}{' for ' + str(days) + ' days' if days else ' (lifetime)'} 👑")))
            return
        if path == "/revoke":
            target = params.get("target", "").strip()
            cid, display = subscriptions.resolve_target(target)
            if cid is None:
                self._send(200, _page("Users", _users_html(flash=f"User {target!r} not found.")))
                return
            subscriptions.revoke_pro(cid)
            self._send(200, _page("Users", _users_html(
                flash=f"PRO revoked from {display or cid}.")))
            return
        self._send(404, _page("Not found", "<p>404</p>"))


def main():
    ap = argparse.ArgumentParser(description="CRYTO Signal Bot - admin web panel")
    ap.add_argument("--port", type=int, default=cfg.PANEL_PORT)
    ap.add_argument("--host", default=cfg.PANEL_HOST)
    args = ap.parse_args()
    if not cfg.ADMIN_PASSWORD:
        print("ERROR: ADMIN_PASSWORD is empty. Run:  python tokens.py password")
        return 1
    try:
        server = http.server.ThreadingHTTPServer((args.host, args.port), PanelHandler)
    except OSError as exc:
        print(f"ERROR: cannot bind {args.host}:{args.port} - {exc}")
        return 1
    print(f"CRYTO admin panel: http://{args.host}:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\npanel stopped.")


if __name__ == "__main__":
    sys.exit(main())