"""Poll /stats and show a compact live table. Ctrl+C exits; --once prints once."""

import argparse
import json
import math
import sys
import time
from urllib.request import urlopen


def render(stats):
    lines = [
        f"Algorithm: {stats['algorithm']} | Uptime: {stats['uptime_seconds']:.1f}s",
        "Backend      Health     Wgt Active Requests Complete Failed Cancel Avg ms",
    ]
    for backend in stats["backends"]:
        latency = backend["avg_response_ms"]
        average = "-" if latency is None else f"{latency:.1f}"
        lines.append(
            f"{backend['name']:<12} {backend['health']:<10} {backend['weight']:>3} "
            f"{backend['active']:>6} {backend['requests']:>8} {backend['completed']:>8} "
            f"{backend['failed']:>6} {backend['cancelled']:>6} {average:>7}"
        )
    lines.append("Health probes and /stats reads are excluded from backend request counts.")
    lines.append("Avg ms: completed request duration, including upload and backend waits.")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080/stats")
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("--interval must be finite and positive")
    try:
        while True:
            try:
                with urlopen(args.url, timeout=3) as response:
                    text = render(json.load(response))
            except (OSError, ValueError, KeyError) as exc:
                text = f"Stats unavailable: {exc}"
                if args.once:
                    parser.exit(1, text + "\n")
            if not args.once and sys.stdout.isatty():
                print("\033[2J\033[H", end="")
            print(text, flush=True)
            if args.once:
                return
            time.sleep(args.interval)  # This is a separate synchronous CLI client.
    except KeyboardInterrupt:
        print("\nStats display stopped.")


if __name__ == "__main__":
    main()
