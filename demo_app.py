"""A local browser classroom for the real balancer. Run: python demo_app.py."""

import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import webbrowser

from lb_config import ALGORITHMS, parse_config

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "demo_ui"
PATHS = ("/hello", "/api/items", "/static/style.css", "/apiculture",
         "/slow", "/truncate", "/api/echo")


class DemoError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def exchange(port, path, method="GET", body=None, timeout=6):
    """One actual HTTP exchange. A 200 header can still have a truncated body."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    started = time.perf_counter()
    try:
        headers = {"Connection": "close"}
        if body is not None:
            headers.update({"Content-Type": "application/json", "X-Demo": "browser-lab"})
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        complete = True
        try:
            raw = response.read()
        except http.client.IncompleteRead as exc:
            raw, complete = exc.partial, False
        text = raw.decode("utf-8", errors="replace")
        try:
            document = json.loads(text)
        except ValueError:
            document = None
        return {"status": response.status, "complete": complete,
                "body": text, "document": document,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2)}
    finally:
        connection.close()


class DemoLab:
    """Owns only its child processes; never attaches to the student's servers.

    The teaching UI uses threads for blocking HTTP/process operations. The
    balancer runs separately and retains its original asyncio event loop.
    """

    def __init__(self, output=None):
        self.output = output or ROOT / "demo-output" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        self.output.mkdir(parents=True, exist_ok=True)
        self.data = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        parse_config(self.data)
        if [b["name"] for b in self.data["backends"]] != ["backend-1", "backend-2", "backend-3"]:
            raise ValueError("The classroom uses the three named backends in the sample config.json")
        # Private ports and a private config preserve the original terminal demo.
        ports = set()
        while len(ports) < 4:
            ports.add(free_port())
        balancer_port, *backend_ports = ports
        self.data["listen"] = {"host": "127.0.0.1", "port": balancer_port}
        for backend, port, weight in zip(self.data["backends"], backend_ports, (3, 2, 1)):
            backend.update(host="127.0.0.1", port=port, weight=weight)
        self.data["health_check"] = {"enabled": True, "path": "/health", "interval": 0.75,
                                     "timeout": 0.5, "failure_threshold": 2, "recovery_threshold": 2}
        self.data["algorithm"] = "round_robin"
        self.processes = {}
        self.balancer = None
        self.log_path = None
        self.generation = 0
        self.history = deque(maxlen=80)
        self.data_lock = threading.Lock()
        self.operation_lock = threading.Lock()
        self.busy = None
        self.token = secrets.token_urlsafe(32)
        self.closing = False

    @property
    def port(self):
        return self.data["listen"]["port"]

    def spawn(self, args, log_name):
        with (self.output / log_name).open("ab") as output:
            return subprocess.Popen([sys.executable, "-u", *args], cwd=ROOT,
                                    stdout=output, stderr=subprocess.STDOUT,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                    start_new_session=sys.platform != "win32")

    @staticmethod
    def stop(process):
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    @staticmethod
    def wait_until(predicate, seconds=8):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                if predicate():
                    return
            except (OSError, http.client.HTTPException, ValueError):
                pass
            time.sleep(0.05)
        raise DemoError("A demo server did not become ready. Check the demo-output logs.", 503)

    def start_backend(self, name):
        if name in self.processes and self.processes[name].poll() is None:
            return
        backend = next(b for b in self.data["backends"] if b["name"] == name)
        self.processes[name] = self.spawn(
            [str(ROOT / "demo_backend.py"), "--port", str(backend["port"]), "--name", name,
             "--delay", "3" if name == "backend-1" else "0.1"], f"{name}.log")
        self.wait_until(lambda: self.processes[name].poll() is None and
                        exchange(backend["port"], "/health", timeout=0.5)["status"] == 200)

    def stats(self):
        response = exchange(self.port, "/stats", timeout=1)
        if response["status"] != 200 or not response["complete"]:
            raise DemoError("The balancer's statistics are temporarily unavailable.", 503)
        return response["document"]

    def reset(self, algorithm):
        if algorithm not in ALGORITHMS:
            raise DemoError("Choose a supported algorithm.")
        self.stop(self.balancer)
        self.balancer = None
        for backend in self.data["backends"]:
            self.start_backend(backend["name"])
        self.data["algorithm"] = algorithm
        with self.data_lock:
            self.history.clear()
            self.generation += 1
        config_path = self.output / "config.json"
        config_path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        self.log_path = self.output / f"balancer-{self.generation}.jsonl"
        self.balancer = self.spawn([str(ROOT / "load_balancer.py"), "--config", str(config_path)],
                                  self.log_path.name)
        self.wait_until(lambda: self.balancer.poll() is None and
                        all(b["health"] == "healthy" for b in self.stats()["backends"]))

    @contextmanager
    def operation(self, label):
        if not self.operation_lock.acquire(blocking=False):
            raise DemoError("An experiment is already running. Wait for it to finish.", 409)
        try:
            if self.closing:
                raise DemoError("The demo is shutting down.", 503)
            self.busy = label
            yield
        finally:
            self.busy = None
            self.operation_lock.release()

    def request(self, path):
        method = "POST" if path == "/api/echo" else "GET"
        body = b'{"message":"Hello from the browser classroom"}' if method == "POST" else None
        try:
            result = exchange(self.port, path, method, body)
            document = result["document"]
            backend = document.get("backend") if isinstance(document, dict) else None
            row = {"method": method, "path": path, "backend": backend,
                   "status": result["status"], "complete": result["complete"],
                   "duration_ms": result["duration_ms"], "body": result["body"],
                   "outcome": ("incomplete" if not result["complete"] else
                               "success" if result["status"] < 400 else "http_error")}
        except (OSError, http.client.HTTPException) as exc:
            row = {"method": method, "path": path, "backend": None, "status": None,
                   "complete": False, "duration_ms": None, "body": str(exc), "outcome": "connection_error"}
        with self.data_lock:
            row["id"] = self.history[-1]["id"] + 1 if self.history else 1
            self.history.append(row)
        return row

    def events(self):
        try:
            with self.log_path.open("rb") as stream:
                stream.seek(0, 2)
                size = stream.tell()
                stream.seek(max(0, size - 65536))
                lines = stream.read().decode("utf-8", errors="replace").splitlines()
            events = []
            for line in lines:
                try:
                    event = json.loads(line)
                    if event.get("event") in ("request", "health_change"):
                        events.append(event)
                except ValueError:
                    pass  # A partial line may be being written by the balancer.
            return events[-18:]
        except (AttributeError, OSError):
            return []

    def snapshot(self):
        try:
            stats, error = self.stats(), None
        except (OSError, http.client.HTTPException, DemoError, ValueError) as exc:
            stats, error = None, str(exc)
        with self.data_lock:
            history = list(self.history)
        return {"stats": stats, "error": error, "history": history,
                "events": self.events(), "busy": self.busy, "generation": self.generation,
                "algorithm": self.data["algorithm"], "token": self.token,
                "running": {name: p.poll() is None for name, p in self.processes.copy().items()},
                "balancer_url": f"http://127.0.0.1:{self.port}",
                "log_directory": str(self.output)}

    def act(self, action, data):
        with self.operation(action):
            if action == "reset":
                self.reset(data.get("algorithm", self.data["algorithm"]))
                return {"message": "Fresh run ready. All backends restored; counters cleared."}
            if action == "backend":
                name, command = data.get("name"), data.get("command")
                if name not in self.processes or command not in ("start", "stop"):
                    raise DemoError("Choose a known backend and start or stop.")
                if command == "stop":
                    self.stop(self.processes[name])
                else:
                    self.start_backend(name)
                return {"message": f"{name} process {'started' if command == 'start' else 'stopped'}. "
                        "Watch its health change after two background probes."}
            if action == "traffic":
                path, count = data.get("path"), data.get("count", 1)
                if path not in PATHS or type(count) is not int or count not in (1, 6, 12):
                    raise DemoError("Choose a listed path and a batch of 1, 6, or 12.")
                if path == "/slow" and count != 1:
                    raise DemoError("Send slow requests individually, or use the busy-server experiment.")
                results = [self.request(path) for _ in range(count)]
                return {"message": f"Finished {count} HTTP exchange(s). Inspect the results below.", "results": results}
            if action == "experiment":
                kind = data.get("kind")
                mapping = {"round_robin": "round_robin", "weighted": "weighted_round_robin",
                           "busy": "least_connections"}
                if kind not in mapping:
                    raise DemoError("Unknown experiment.")
                self.reset(mapping[kind])
                if kind == "busy":
                    # The slow client runs in another UI worker thread. Its HTTP
                    # request still traverses the real asyncio balancer.
                    with ThreadPoolExecutor(max_workers=1) as worker:
                        slow = worker.submit(self.request, "/slow")
                        self.wait_until(lambda: self.stats()["backends"][0]["active"] == 1, seconds=2)
                        fast = [self.request("/hello") for _ in range(6)]
                        slow_result = slow.result(timeout=6)
                    ok = (slow_result["backend"] == "backend-1" and slow_result["outcome"] == "success"
                          and all(r["backend"] in ("backend-2", "backend-3") and
                                  r["outcome"] == "success" for r in fast))
                    message = "Six fast requests avoided backend-1 while its three-second request was active."
                else:
                    results = [self.request("/hello") for _ in range(12 if kind == "weighted" else 6)]
                    names = [r["backend"] for r in results]
                    ok = all(r["outcome"] == "success" for r in results)
                    if kind == "weighted":
                        counts = [names.count(f"backend-{i}") for i in (1, 2, 3)]
                        ok = ok and counts == [6, 4, 2]
                        message = f"Twelve requests were divided {counts[0]} : {counts[1]} : {counts[2]}."
                    else:
                        ok = ok and names == ["backend-1", "backend-2", "backend-3"] * 2
                        message = "Six requests took turns: 1 → 2 → 3 → 1 → 2 → 3."
                return {"message": ("PASS — " if ok else "CHECK — Unexpected result. ") + message,
                        "passed": ok}
            raise DemoError("Unknown action.", 404)

    def close(self):
        # Let a bounded experiment finish before cleaning up its owned children.
        with self.operation_lock:
            self.closing = True
            self.stop(self.balancer)
            for process in self.processes.values():
                self.stop(process)


class DemoServer(ThreadingHTTPServer):
    daemon_threads = True


class DemoHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass  # Backend and balancer evidence goes to demo-output, not this UI log.

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def trusted_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def reply(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; "
                         "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def do_GET(self):
        if not self.trusted_host():
            return self.reply(403, {"error": "Use this app's localhost address."})
        path = urlsplit(self.path).path
        if path == "/api/state":
            return self.reply(200, self.server.lab.snapshot())
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if path in assets:
            name, content_type = assets[path]
            return self.reply(200, (ASSETS / name).read_bytes(), content_type)
        return self.reply(404, {"error": "Not found"})

    def do_POST(self):
        origin = self.headers.get("Origin")
        expected_origin = "http://" + self.headers.get("Host", "")
        if (not self.trusted_host() or (origin is not None and origin != expected_origin)
                or not secrets.compare_digest(self.headers.get("X-Demo-Token", "").encode("utf-8"),
                                              self.server.lab.token.encode("utf-8"))):
            return self.reply(403, {"error": "Open this local app and use its controls."})
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if (self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1
                    or not lengths[0].isascii() or not lengths[0].isdigit() or len(lengths[0]) > 5):
                raise DemoError("A single bounded Content-Length is required.")
            length = int(lengths[0])
            if length > 4096:
                raise DemoError("Control request too large.", 413)
            if self.headers.get_content_type() != "application/json":
                raise DemoError("Send JSON.", 415)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise DemoError("Incomplete control request.")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise DemoError("Send a JSON object.")
            path = urlsplit(self.path).path
            if not path.startswith("/api/"):
                raise DemoError("Not found", 404)
            result = self.server.lab.act(path.removeprefix("/api/"), data)
            self.reply(200, result)
        except DemoError as exc:
            self.reply(exc.status, {"error": str(exc)})
        except (ValueError, TypeError, OSError, http.client.HTTPException) as exc:
            self.reply(400 if isinstance(exc, (ValueError, TypeError)) else 503, {"error": str(exc)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8090, help="browser app port (default: 8090)")
    parser.add_argument("--no-browser", action="store_true", help="print the URL without opening a browser")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    server = lab = None
    try:
        server = DemoServer(("127.0.0.1", args.port), DemoHandler)
        lab = DemoLab()
        server.lab = lab
        print("Starting your private demo servers...", flush=True)
        lab.reset("round_robin")
        url = f"http://127.0.0.1:{server.server_port}"
        print(f"Open the classroom: {url}\nLogs: {lab.output}\nLeave this terminal running. Ctrl+C stops this demo.", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping the browser classroom and its demo servers.")
    except (OSError, ValueError, DemoError) as exc:
        parser.exit(1, f"Could not start the classroom: {exc}\nIf port {args.port} is busy, use --port 8091.\n")
    finally:
        if lab:
            lab.close()
        if server:
            server.server_close()


if __name__ == "__main__":
    main()
