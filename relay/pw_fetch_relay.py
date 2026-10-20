"""Fetch relay for "testing daily post fb with n8n".

The source site sits behind Cloudflare, which blocks some data-centre IPs (including the n8n host).
n8n asks this relay to fetch the page instead. The relay is deliberately narrow:

- only https URLs on ALLOWED_HOST, and never a path disallowed by that site's robots.txt
- at most 1 upstream request per second (requests are served one at a time)
- needs the shared key in the X-Relay-Key header
- returns the upstream status code and body unchanged

GET /fetch?url=<encoded URL on ALLOWED_HOST>   ->  upstream body
Config (env): RELAY_KEY and ALLOWED_HOST (required), RELAY_PORT (default 8661)
"""
import hmac
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

KEY = os.environ["RELAY_KEY"]
PORT = int(os.environ.get("RELAY_PORT", "8661"))
ALLOWED_HOST = os.environ["ALLOWED_HOST"]
DISALLOW = ("/video/5469/", "/language/", "/admin", "/v2_admin/", "/ajax/", "/api/v1/", "/uploads/",
            "/cgi-bin/", "/db/", "/old/", "/test/", "/backup/")
USER_AGENT = "Mozilla/5.0 (compatible; dailypost-fb-test/1.0; +assessment, max 1 req/s)"
MIN_GAP = 1.0
MAX_BYTES = 8 * 1024 * 1024
_last = 0.0


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body, ctype="text/plain; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        global _last
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/fetch":
            return self.reply(404, "not found")
        if not hmac.compare_digest(self.headers.get("X-Relay-Key", ""), KEY):
            return self.reply(401, "bad key")
        target = urllib.parse.parse_qs(parsed.query).get("url", [""])[0]
        t = urllib.parse.urlparse(target)
        if t.scheme != "https" or t.hostname != ALLOWED_HOST:
            return self.reply(400, f"only https://{ALLOWED_HOST} URLs are allowed")
        if any(t.path.startswith(p) for p in DISALLOW):
            return self.reply(403, "path disallowed by robots.txt")

        wait = _last + MIN_GAP - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last = time.monotonic()

        req = urllib.request.Request(target, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    return self.reply(502, "upstream response too large")
                return self.reply(r.status, body, r.headers.get("Content-Type", "text/html"))
        except urllib.error.HTTPError as e:
            return self.reply(e.code, f"upstream returned {e.code}")
        except Exception as e:  # timeout, DNS, connection reset
            return self.reply(502, f"upstream error: {type(e).__name__}")

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    # Single-threaded on purpose: requests queue up, which enforces the 1 req/s limit.
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
