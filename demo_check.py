"""Verify responses and compare sequential/concurrent requests through the proxy."""

import argparse
import asyncio
import json
import time


async def fetch(host, port, path):
    writer = None
    started = time.perf_counter()
    try:
        async with asyncio.timeout(15):
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(
                f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                "Connection: close\r\n\r\n".encode("ascii")
            )
            await writer.drain()
            # Only this controlled demo backend promises a small response + EOF.
            response = await reader.read()
            headers, body = response.split(b"\r\n\r\n", 1)
            if headers.split(b"\r\n", 1)[0] != b"HTTP/1.1 200 OK":
                raise ValueError("Backend did not return HTTP 200")
            document = json.loads(body)
            if document["path"] != path:
                raise ValueError("Response belongs to a different request")
            return document, time.perf_counter() - started
    finally:
        if writer is not None:
            writer.close()
            try:
                async with asyncio.timeout(2):
                    await writer.wait_closed()
            except (OSError, TimeoutError):
                writer.transport.abort()


async def run_demo(args):
    timings = {}
    for mode in ("sequential", "concurrent"):
        started = time.perf_counter()
        paths = [f"/slow?request={i}" for i in range(1, args.requests + 1)]
        if mode == "sequential":
            results = []
            for path in paths:
                results.append(await fetch(args.host, args.port, path))
        else:
            results = await asyncio.gather(
                *(fetch(args.host, args.port, path) for path in paths)
            )
        timings[mode] = time.perf_counter() - started
        print(f"{mode.upper()}: {len(results)}/{args.requests} valid responses "
              f"in {timings[mode]:.2f}s")
        for document, elapsed in results:
            print(f"  {document['path']} -> {document['backend']} ({elapsed:.2f}s)")
    print("PASS: all responses contain the expected request path.")
    print("Timing demonstrates overlapping waits; it is not a capacity benchmark.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--requests", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.requests <= 20:
        parser.error("use port 1..65535 and requests 1..20")
    try:
        asyncio.run(run_demo(args))
    except (OSError, TimeoutError, ValueError, KeyError) as exc:
        parser.exit(1, f"DEMO FAILED: {exc}. Check that backend and proxy are running.\n")
    except KeyboardInterrupt:
        parser.exit(130, "Demo interrupted.\n")


if __name__ == "__main__":
    main()
