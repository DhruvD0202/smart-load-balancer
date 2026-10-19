"""Run the actual CLI programs on temporary local ports and save demo evidence."""

from contextlib import ExitStack
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "evidence"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def read_url(port, path="/", method="GET"):
    request = Request(f"http://127.0.0.1:{port}{path}", method=method)
    with urlopen(request, timeout=5) as response:
        return response.status, response.read()


def wait_ready(process, port):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Demo process exited with code {process.returncode}")
        try:
            read_url(port, "/ready")
            return
        except (OSError, http.client.HTTPException):
            time.sleep(0.1)
    raise TimeoutError(f"Demo process on port {port} did not become ready")


def main():
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    lines = []

    def record(message):
        lines.append(message)
        print(message, flush=True)

    try:
        with ExitStack() as stack:
            def launch(filename, arguments, log_name):
                log = stack.enter_context(
                    (EVIDENCE / log_name).open("w", encoding="utf-8")
                )
                process = subprocess.Popen(
                    [sys.executable, "-u", str(ROOT / filename), *arguments],
                    cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                stack.callback(stop, process)
                return process

            backend_port, proxy_port = free_port(), free_port()
            while proxy_port == backend_port:
                proxy_port = free_port()
            backend_args = ["--port", str(backend_port), "--name", "backend-1"]
            backend = launch("demo_backend.py", backend_args, "backend.txt")
            wait_ready(backend, backend_port)
            proxy = launch("proxy.py", [
                "--port", str(proxy_port), "--backend-port", str(backend_port)
            ], "proxy.txt")
            wait_ready(proxy, proxy_port)
            record(f"Python {sys.version.split()[0]}; platform={sys.platform}")
            record(f"Temporary local ports: proxy={proxy_port}, backend={backend_port}")

            status, body = read_url(proxy_port, "/hello?course=CN")
            document = json.loads(body)
            if status != 200 or document["path"] != "/hello?course=CN":
                raise AssertionError("GET response did not match the request")
            record("GET through proxy: HTTP 200\n" + body.decode().rstrip())
            status, body = read_url(proxy_port, "/hello", method="HEAD")
            if status != 200 or body:
                raise AssertionError("HEAD response was incorrect")
            record("HEAD through proxy: HTTP 200, empty body")

            demo = subprocess.run(
                [sys.executable, str(ROOT / "demo_check.py"), "--port", str(proxy_port)],
                cwd=ROOT, capture_output=True, text=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            record(demo.stdout.rstrip())
            if demo.returncode:
                raise RuntimeError(demo.stderr)

            stop(backend)
            try:
                read_url(proxy_port, "/backend-down")
            except (OSError, http.client.HTTPException) as exc:
                record(f"Backend stopped: client received {type(exc).__name__} (expected)")
            else:
                raise AssertionError("Expected client failure with backend stopped")
            if proxy.poll() is not None:
                raise AssertionError("Proxy exited after backend failure")
            record("Proxy process stayed alive.")

            backend = launch("demo_backend.py", backend_args, "backend-restarted.txt")
            wait_ready(backend, backend_port)
            status, body = read_url(proxy_port, "/recovered")
            if status != 200 or json.loads(body)["path"] != "/recovered":
                raise AssertionError("Recovery request failed")
            record("Backend restarted: HTTP 200 through the same proxy process.")
            record("PASS: Week 1 CLI demo checks completed.")
    except Exception as exc:
        record(f"FAIL: {type(exc).__name__}: {exc}")
        raise
    finally:
        (EVIDENCE / "week1-demo.txt").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
