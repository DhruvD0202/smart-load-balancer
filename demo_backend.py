"""A local demo HTTP backend. Reuse with different ports/names in Week 2."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import time
from urllib.parse import urlsplit


class DemoHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DemoBackend/0.1"

    def do_GET(self):
        if urlsplit(self.path).path == "/truncate":
            # A controlled mid-response failure for reviews: claim 64 bytes,
            # send fewer, then close. The /health endpoint still works.
            try:
                self.send_response(200)
                self.send_header("Content-Length", "64")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b"partial response")
                self.wfile.flush()
            except OSError:
                self.close_connection = True
            return
        self.respond(include_body=True)

    def do_HEAD(self):
        self.respond(include_body=False)

    def do_POST(self):
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") is not None:
            self.send_error(501, "Transfer-Encoding is not supported by this demo")
            return
        if not lengths:
            self.send_error(411, "Content-Length required")
            return
        if (len(lengths) != 1 or not lengths[0].isascii()
                or not lengths[0].isdigit() or len(lengths[0]) > 10):
            self.send_error(400, "Invalid Content-Length")
            return
        length = int(lengths[0])
        if length > 1048576:
            self.send_error(413, "Demo body limit is 1 MiB")
            return
        self.connection.settimeout(5)
        try:
            request_body = self.rfile.read(length)
        except OSError:
            self.send_error(408, "Body read timed out")
            return
        if len(request_body) != length:
            self.send_error(400, "Incomplete request body")
            return
        self.respond(include_body=True, request_body=request_body)

    def handle_expect_100(self):
        self.send_error(417, "Expect / 100-continue is unsupported in this demo")
        return False

    def respond(self, include_body, request_body=None):
        if urlsplit(self.path).path == "/slow":
            # This runs in a backend worker THREAD, not the proxy's event loop.
            time.sleep(self.server.delay)
        document = {
            "backend": self.server.backend_name,
            "port": self.server.server_port,
            "method": self.command,
            "path": self.path,
            "message": "Hello from the demo backend!",
        }
        if request_body is not None:
            document.update(body_bytes=len(request_body),
                            body_utf8=request_body.decode("utf-8", errors="replace"),
                            demo_header=self.headers.get("X-Demo"))
        body = (json.dumps(document, indent=2) + "\n").encode("utf-8")
        try:
            status = self.server.health_status if urlsplit(self.path).path == "/health" else 200
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # One request per connection keeps the initial demo easy to follow.
            self.send_header("Connection", "close")
            self.end_headers()
            if include_body:
                self.wfile.write(body)
        except OSError:
            # A disconnected demo client should not produce a server traceback.
            self.close_connection = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--name", default="backend-1")
    parser.add_argument("--health-status", type=int, choices=(200, 503), default=200,
                        help="HTTP status of /health, for health-check experiments")
    parser.add_argument("--delay", type=float, default=1,
                        help="seconds to delay /slow responses")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not math.isfinite(args.delay) or not 0 <= args.delay <= 10:
        parser.error("--delay must be finite and between 0 and 10 seconds")
    try:
        with ThreadingHTTPServer((args.host, args.port), DemoHandler) as server:
            server.backend_name = args.name
            server.delay = args.delay
            server.health_status = args.health_status
            print(
                f"{args.name} listening on http://{args.host}:{args.port}; "
                f"/slow delay={args.delay}s (Ctrl+C to stop)", flush=True,
            )
            server.serve_forever()
    except KeyboardInterrupt:
        print("\nBackend stopped.")
    except OSError as exc:
        parser.exit(1, f"Could not start backend: {exc}\n")


if __name__ == "__main__":
    main()
