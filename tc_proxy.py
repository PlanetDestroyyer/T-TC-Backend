"""
TinyCell Secret Path Proxy
--------------------------
Runs in front of a deployed app. Requires ALL requests to start with
the secret path prefix; everything else gets a 404. Uses only stdlib.

Usage: python tc_proxy.py <proxy_port> <app_port> <secret>
Example: python tc_proxy.py 4001 3001 f8a2d91c
  → Cloudflare tunnels to :4001
  → Only https://xxx.trycloudflare.com/tc-f8a2d91c/... is forwarded
  → All other paths → 404
"""

import sys
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer


PROXY_PORT = int(sys.argv[1])
APP_PORT   = int(sys.argv[2])
SECRET     = sys.argv[3]
PREFIX     = f"/tc-{SECRET}"


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Silence access logs

    def _send_response(self, status: int, body: bytes, headers: dict):
        self.send_response(status)
        for k, v in headers.items():
            lower = k.lower()
            if lower in ("transfer-encoding", "connection", "keep-alive"):
                continue
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_request(self):
        path = self.path

        # Security check: must start with the secret prefix
        if not path.startswith(PREFIX):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return

        # Strip the prefix, keep everything after it
        stripped = path[len(PREFIX):]
        if not stripped.startswith("/"):
            stripped = "/" + stripped

        target = f"http://127.0.0.1:{APP_PORT}{stripped}"

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else None

            req = urllib.request.Request(
                url=target,
                data=body,
                method=self.command,
                headers={k: v for k, v in self.headers.items()
                         if k.lower() not in ("host",)},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_body = resp.read()
                self._send_response(
                    resp.status,
                    resp_body,
                    dict(resp.headers),
                )
        except urllib.error.HTTPError as e:
            resp_body = e.read()
            self._send_response(e.code, resp_body, dict(e.headers))
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(f"Proxy error: {e}".encode())

    do_GET  = do_request
    do_POST = do_request
    do_PUT  = do_request
    do_DELETE = do_request
    do_PATCH  = do_request
    do_HEAD   = do_request
    do_OPTIONS = do_request


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PROXY_PORT), ProxyHandler)
    print(f"[tc_proxy] Listening on :{PROXY_PORT}, forwarding /{PREFIX}/ → :{APP_PORT}")
    server.serve_forever()
