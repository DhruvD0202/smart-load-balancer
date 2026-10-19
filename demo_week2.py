"""Demonstrate round-robin pools, route boundaries, and a POST body."""

import argparse
import http.client
import json
from pathlib import Path
from urllib.request import Request, urlopen

from lb_config import load_config


def fetch(host, port, path, data=None, headers=None):
    request = Request(f"http://{host}:{port}{path}", data=data, headers=headers or {})
    with urlopen(request, timeout=15) as response:
        if response.status != 200:
            raise ValueError(f"Expected HTTP 200, received {response.status}")
        document = json.load(response)
    if document.get("path") != path:
        raise ValueError(f"Response path does not match {path}")
    return document


def verify_cycle(observed, expected):
    # Browsers/readiness checks may have advanced a pool before this demo.
    rotations = [expected[i:] + expected[:i] for i in range(len(expected))]
    if not any(observed == [cycle[i % len(cycle)] for i in range(len(observed))]
               for cycle in rotations):
        raise ValueError(f"Not a round-robin cycle: {observed}; expected members {expected}")


def run_demo(config):
    cases = [("/hello", config.default_pool),
             ("/api/items?course=CN", "api"),
             ("/static/style.css", "static"),
             ("/apiculture", config.default_pool)]
    for path, pool in cases:
        members = list(config.pools[pool])
        replies = [fetch(config.host, config.port, path) for _ in range(6)]
        names = [reply["backend"] for reply in replies]
        verify_cycle(names, members)
        print(f"{path} -> pool={pool}: " + " -> ".join(names), flush=True)
    payload = "hello from the client".encode("utf-8")
    reply = fetch(config.host, config.port, "/api/echo", payload,
                  {"Content-Type": "text/plain", "X-Demo": "week-2"})
    if (reply.get("body_bytes") != len(payload)
            or reply.get("body_utf8") != payload.decode()
            or reply.get("demo_header") != "week-2"
            or reply["backend"] not in config.pools["api"]):
        raise ValueError("POST body/header forwarding check failed")
    print(f"POST /api/echo -> {reply['backend']}: body and X-Demo header preserved.",
          flush=True)
    print("PASS: round-robin, path routing, route boundary, and POST checks.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path,
                        default=Path(__file__).with_name("config.json"))
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if not {"api", "static"} <= config.pools.keys():
            raise ValueError("This demo expects the supplied api and static pools/routes")
        run_demo(config)
    except (OSError, ValueError, KeyError, http.client.HTTPException) as exc:
        parser.exit(1, f"DEMO FAILED: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Demo interrupted.\n")


if __name__ == "__main__":
    main()
