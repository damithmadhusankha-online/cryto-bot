"""Tiny HTTP health-check server, used only when deployed on Render.

Render's free "Web Service" tier requires the process to bind to the port
given in the PORT env var, and only counts the service as "active" while it
receives HTTP traffic - after ~15 minutes with none, it spins the instance
down. This server exists purely to satisfy the port-binding requirement and
give an external uptime pinger (e.g. UptimeRobot) something to hit every few
minutes to keep the instance awake. It does not affect the bot's actual
Telegram/signal logic at all - see serve.py for that.

Locally (not on Render) PORT won't be set, so start_health_server() is a
no-op there - nothing changes for local/VPS runs.
"""
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK - signal bot is running\n")

    def log_message(self, format, *args):
        pass  # keep serve.log free of health-check noise


def start_health_server():
    """Start the health server in a background thread if PORT is set
    (i.e. we're running on Render). Safe no-op everywhere else."""
    port = os.environ.get("PORT")
    if not port:
        return
    try:
        port = int(port)
    except ValueError:
        return

    def _serve():
        httpd = HTTPServer(("0.0.0.0", port), _Handler)
        httpd.serve_forever()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    print(f"health_server: listening on 0.0.0.0:{port} (Render keep-alive)")
