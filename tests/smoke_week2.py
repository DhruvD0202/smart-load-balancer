"""Run three real backends + HTTP balancer on temporary ports; save evidence."""

from contextlib import ExitStack
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from urllib.error import HTTPError

from smoke_demo import ROOT, free_port, read_url, stop, wait_ready

EVIDENCE = ROOT / "docs" / "evidence" / "week2"


def main():
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    output = []

    def record(text):
        output.append(text)
        print(text, flush=True)

    try:
        with ExitStack() as stack:
            def launch(script, arguments, name):
                log = stack.enter_context((EVIDENCE / f"{name}.txt").open("w", encoding="utf-8"))
                process = subprocess.Popen(
                    [sys.executable, "-u", str(ROOT / script), *arguments],
                    cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                stack.callback(stop, process)
                return process

            temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="cn-week2-")))
            data = json.loads((ROOT / "config.json").read_text())
            # Preserve the historical Week 2 behavior: no active health checks.
            data["health_check"]["enabled"] = False
            ports = set()
            while len(ports) < 4:
                ports.add(free_port())
            proxy_port, *backend_ports = sorted(ports)
            data["listen"]["port"] = proxy_port
            backends = []
            for backend, port in zip(data["backends"], backend_ports):
                backend["port"] = port
                process = launch("demo_backend.py",
                                 ["--port", str(port), "--name", backend["name"]],
                                 backend["name"])
                backends.append(process)
                wait_ready(process, port)
            config_path = temporary / "config.json"
            config_path.write_text(json.dumps(data), encoding="utf-8")
            proxy = launch("load_balancer.py", ["--config", str(config_path)], "load-balancer")
            wait_ready(proxy, proxy_port)
            record(f"Python {sys.version.split()[0]}; platform={sys.platform}")
            record(f"Temporary ports: balancer={proxy_port}, backends={backend_ports}")
            for script, arguments in (
                ("demo_week2.py", ["--config", str(config_path)]),
                ("demo_check.py", ["--port", str(proxy_port)]),
            ):
                result = subprocess.run(
                    [sys.executable, str(ROOT / script), *arguments],
                    cwd=ROOT, capture_output=True, text=True, timeout=40,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                record(result.stdout.rstrip())
                if result.returncode:
                    raise RuntimeError(result.stderr)

            with socket.create_connection(("127.0.0.1", proxy_port), timeout=5) as client:
                client.sendall(b"GET / HTTP/1.1\r\nHost: demo\r\n"
                               b"Content-Length: 0\r\nContent-Length: 0\r\n\r\n")
                response = b""
                while chunk := client.recv(4096):
                    response += chunk
            if not response.startswith(b"HTTP/1.1 400 "):
                raise AssertionError("Duplicate Content-Length was not rejected")
            record("Malformed request: duplicate Content-Length -> HTTP 400.")

            stop(backends[2])
            try:
                read_url(proxy_port, "/static/failure")
            except HTTPError as exc:
                if exc.code != 502:
                    raise
                record("Backend-3 stopped: /static/failure -> HTTP 502.")
            else:
                raise AssertionError("Expected HTTP 502 with static backend down")
            status, _ = read_url(proxy_port, "/api/still-working")
            if status != 200 or proxy.poll() is not None:
                raise AssertionError("Healthy pool or balancer failed")
            record("API pool still returns HTTP 200; balancer stayed alive.")
            recovered = launch(
                "demo_backend.py", ["--port", str(backend_ports[2]), "--name", "backend-3"],
                "backend-3-restarted",
            )
            wait_ready(recovered, backend_ports[2])
            status, body = read_url(proxy_port, "/static/recovered")
            if status != 200 or json.loads(body)["backend"] != "backend-3":
                raise AssertionError("Static backend recovery failed")
            record("Backend-3 restarted: /static/recovered -> HTTP 200.")
            record("PASS: Week 2 CLI demonstration completed; no health checks/retries implied.")
    except Exception as exc:
        record(f"FAIL: {type(exc).__name__}: {exc}")
        raise
    finally:
        (EVIDENCE / "demo.txt").write_text("\n".join(output) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
