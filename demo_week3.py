"""Self-contained Week 3 review: real backend processes, algorithms, and failures."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
EVIDENCE = ROOT / "docs" / "evidence" / "week3"


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


def request(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path, headers={"Connection": "close"})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def document(port, path="/stats"):
    status, body = request(port, path)
    if status != 200:
        raise AssertionError(f"{path} returned {status}: {body!r}")
    return json.loads(body)


def wait_until(check, label, timeout=12):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (OSError, http.client.HTTPException) as exc:
            last_error = exc
        time.sleep(0.05)
    raise TimeoutError(f"Timed out waiting for {label}; last error={last_error}")


def main():
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    output = []

    def record(text):
        output.append(text)
        print(text, flush=True)

    try:
        with ExitStack() as stack:
            def launch(script, arguments, label):
                suffix = "jsonl" if script == "load_balancer.py" else "txt"
                log = stack.enter_context(
                    (EVIDENCE / f"{label}.{suffix}").open("w", encoding="utf-8")
                )
                process = subprocess.Popen(
                    [sys.executable, "-u", str(ROOT / script), *arguments],
                    cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                stack.callback(stop, process)
                return process

            temporary = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="cn-week3-")))
            data = json.loads((ROOT / "config.json").read_text())
            if len(data["backends"]) != 3:
                raise ValueError("This review demo expects the supplied three-backend configuration")
            ports = set()
            while len(ports) < 4:
                ports.add(free_port())
            balancer_port, *backend_ports = sorted(ports)
            data["listen"] = {"host": "127.0.0.1", "port": balancer_port}
            data["algorithm"] = "round_robin"
            data["health_check"].update(enabled=True, interval=0.2, timeout=0.4,
                                        failure_threshold=2, recovery_threshold=2, path="/health")
            for backend, port, weight in zip(data["backends"], backend_ports, (3, 2, 1)):
                backend.update(host="127.0.0.1", port=port, weight=weight)
            names = [b["name"] for b in data["backends"]]
            first, second, third = names
            processes = {}
            generations = Counter()

            def start_backend(name):
                backend = next(b for b in data["backends"] if b["name"] == name)
                generations[name] += 1
                processes[name] = launch(
                    "demo_backend.py", ["--port", str(backend["port"]), "--name", name,
                                        "--delay", "3" if name == first else "0.1"],
                    f"{name}-{generations[name]}",
                )
                wait_until(lambda: document(backend["port"], "/health"),
                           f"{name} startup")

            for name in names:
                start_backend(name)
            config_path = temporary / "config.json"
            config_path.write_text(json.dumps(data), encoding="utf-8")
            record(f"Python {sys.version.split()[0]}; platform={sys.platform}")
            record(f"Temporary ports: balancer={balancer_port}, backends={backend_ports}")
            record("Controlled demo: weights 3:2:1; health interval 0.2s, timeout 0.4s, thresholds 2/2.")
            record("Least-connections workload: one 3-second request plus six fast requests.")

            def stats():
                return document(balancer_port)

            def health_is(name, state):
                return next(b for b in stats()["backends"] if b["name"] == name)["health"] == state

            def start_balancer(algorithm, label=None):
                process = launch(
                    "load_balancer.py", ["--config", str(config_path), "--algorithm", algorithm],
                    label or algorithm,
                )
                wait_until(lambda: all(b["health"] == "healthy" for b in stats()["backends"]),
                           f"{algorithm} initial probes")
                if stats()["totals"]["requests"] != 0:
                    raise AssertionError("Health checks or stats reads changed request counts")
                return process

            def get_backend(path="/hello"):
                return document(balancer_port, path)["backend"]

            def idle():
                return stats()["totals"]["active"] == 0

            balancer = start_balancer("round_robin")
            observed = [get_backend() for _ in range(6)]
            if observed != names * 2:
                raise AssertionError(f"Unexpected round-robin order: {observed}")
            record("ROUND-ROBIN: " + " -> ".join(observed))
            wait_until(idle, "round-robin completion")
            stop(balancer)

            balancer = start_balancer("weighted_round_robin")
            observed = Counter(get_backend() for _ in range(12))
            if observed != dict(zip(names, (6, 4, 2))):
                raise AssertionError(f"Unexpected weighted counts: {observed}")
            record("WEIGHTED (12 requests): " + json.dumps(dict(observed), sort_keys=True))
            wait_until(idle, "weighted completion")
            stop(balancer)

            balancer = start_balancer("least_connections")
            with ThreadPoolExecutor(max_workers=1) as client:
                slow = client.submit(get_backend, "/slow")
                wait_until(lambda: stats()["backends"][0]["active"] == 1, "busy first backend")
                observed = [get_backend() for _ in range(6)]
                if first in observed or set(observed) != {second, third}:
                    raise AssertionError(f"Busy backend was not avoided: {observed}")
                if slow.result(timeout=5) != first:
                    raise AssertionError("Slow request did not use the first backend")
            wait_until(idle, "least-connections completion")
            record(f"LEAST-CONNECTIONS: while {first} was busy, fast requests used "
                   + " -> ".join(observed))
            stop(balancer)

            balancer = start_balancer("round_robin", "health_recovery")
            stop(processes[second])
            wait_until(lambda: health_is(second, "unhealthy"), "failed backend removal")
            observed = [get_backend() for _ in range(6)]
            if second in observed or set(observed) != {first, third}:
                raise AssertionError("Unhealthy backend received traffic")
            record("FAILURE: health checks removed " + second + "; requests used "
                   + " -> ".join(observed))
            start_backend(second)
            wait_until(lambda: health_is(second, "healthy"), "backend recovery")
            observed = [get_backend() for _ in range(6)]
            if Counter(observed) != dict.fromkeys(names, 2):
                raise AssertionError("Recovered backend did not rejoin rotation")
            record("RECOVERY: " + " -> ".join(observed))

            try:
                request(balancer_port, "/truncate")
            except http.client.IncompleteRead as exc:
                record(f"MID-RESPONSE FAILURE: client detected truncation ({len(exc.partial)} body bytes).")
            else:
                raise AssertionError("Expected incomplete response from /truncate")
            wait_until(idle, "truncated-response cleanup")
            if stats()["totals"]["failed"] != 1:
                raise AssertionError("Truncated response was not recorded as a failed transfer")
            get_backend()  # Listener remains usable.

            stop(processes[third])
            wait_until(lambda: health_is(third, "unhealthy"), "static pool failure")
            status, _ = request(balancer_port, "/static/file.css")
            if status != 503:
                raise AssertionError(f"Expected 503 for an empty healthy pool, got {status}")
            if get_backend("/api/items") not in {first, second}:
                raise AssertionError("API pool stopped working")
            record("EMPTY STATIC POOL: /static -> HTTP 503; /api still works.")
            for name in (first, second):
                stop(processes[name])
            wait_until(lambda: all(b["health"] == "unhealthy" for b in stats()["backends"]),
                       "all backends down")
            if request(balancer_port, "/hello")[0] != 503:
                raise AssertionError("Expected 503 with every backend down")
            final = stats()
            if final["totals"]["active"] != 0:
                raise AssertionError("Active reservations leaked")
            for row in final["backends"]:
                if row["requests"] != sum(row[k] for k in ("active", "completed", "failed", "cancelled")):
                    raise AssertionError("Backend counters do not reconcile")
            record("ALL DOWN: /hello -> HTTP 503; /stats stays available; active=0.")
            (EVIDENCE / "final-stats.json").write_text(json.dumps(final, indent=2), encoding="utf-8")
            watcher = subprocess.run(
                [sys.executable, str(ROOT / "watch_stats.py"), "--once",
                 "--url", f"http://127.0.0.1:{balancer_port}/stats"],
                capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if watcher.returncode:
                raise RuntimeError(watcher.stderr)
            record(watcher.stdout.rstrip())
            stop(balancer)
            events = [json.loads(line) for line in
                      (EVIDENCE / "health_recovery.jsonl").read_text().splitlines()]
            requests = [e for e in events if e["event"] == "request"]
            if not any(e["outcome"] == "incomplete" for e in requests):
                raise AssertionError("Missing incomplete-request log")
            required = {"timestamp", "client_ip", "path", "backend", "response_time_ms"}
            if not all(required <= e.keys() for e in requests):
                raise AssertionError("Missing required structured log fields")
            record("JSON LOGS: request fields and incomplete-response event verified.")
            record("PASS: Week 3 algorithms, health recovery, stats, and failure checks.")
    except Exception as exc:
        record(f"FAIL: {type(exc).__name__}: {exc}")
        raise
    finally:
        (EVIDENCE / "demo.txt").write_text("\n".join(output) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
