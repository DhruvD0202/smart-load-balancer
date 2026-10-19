"""HTTP load balancer with selectable algorithms, active health checks and stats."""

import argparse
import asyncio
from dataclasses import replace
import itertools
import json
import logging
from pathlib import Path
import time

from http_protocol import (HTTPError, encode_head, encode_request, encode_response, parse_request,
                           parse_response, read_body, read_head, send_error)
from event_log import JSONFormatter, log_event
from health_checks import HealthChecker
from lb_config import ALGORITHMS, load_config
from proxy import BUFFER_SIZE, close_stream
from scheduler import BackendScheduler

LOG = logging.getLogger("load_balancer")


class HTTPLoadBalancer:
    def __init__(self, config):
        self.config = config
        self.router = BackendScheduler(config)
        self.connections = set()
        self.request_ids = itertools.count(1)

    async def handle_client(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        try:
            await self._handle_client(reader, writer)
        finally:
            self.connections.discard(task)

    async def close(self):
        tasks = list(self.connections)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle_client(self, reader, writer):
        started = time.perf_counter()
        request_id = next(self.request_ids)
        client = writer.get_extra_info("peername")
        request = backend = backend_writer = None
        pool = "-"
        phase = "request"
        status = 0
        response_started = False
        head_only = False
        outcome = "error"
        is_stats = False
        error = None
        try:
            # A deadline covers the WHOLE header/body phase, not each byte.
            async with asyncio.timeout(self.config.timeouts["headers"]):
                raw = await read_head(reader, self.config.header_bytes)
                head_only = raw.startswith(b"HEAD ")
                request = parse_request(raw, self.config.body_bytes)
                is_stats = request.path == "/stats"
            async with asyncio.timeout(self.config.timeouts["body"]):
                body = await read_body(reader, request)
            if is_stats:
                # Local monitoring traffic does not select backends or change
                # application counters / round-robin order.
                if request.method not in ("GET", "HEAD"):
                    status = 405
                    await send_error(writer, status, "Use GET or HEAD for /stats",
                                     allow=("GET", "HEAD"))
                    return
                document = json.dumps(self.router.snapshot(), indent=2).encode("utf-8")
                status = 200
                response_started = True
                writer.write(encode_head("HTTP/1.1 200 OK", [
                    ("content-type", "application/json; charset=utf-8"),
                    ("content-length", str(len(document))),
                    ("cache-control", "no-store"), ("connection", "close"),
                ]) + (b"" if head_only else document))
                async with asyncio.timeout(2):
                    await writer.drain()
                outcome = "complete"
                return
            # This selection and cursor update are synchronous: no await / race.
            pool, backend = self.router.select(request.path)
            phase = "backend"
            async with asyncio.timeout(self.config.timeouts["connect"]):
                backend_reader, backend_writer = await asyncio.open_connection(
                    backend.host, backend.port, limit=self.config.header_bytes
                )
            async with asyncio.timeout(self.config.timeouts["response"]):
                backend_writer.write(encode_request(request) + body)
                await backend_writer.drain()
                raw = await read_head(backend_reader, self.config.header_bytes, 502)
                response = parse_response(raw, request.method)
                status = response.status
                response_started = True
                writer.write(encode_response(response))
                await writer.drain()
                # Stream exactly the declared response body, without buffering
                # the entire response or waiting for the backend to close.
                remaining = response.body_length
                while remaining:
                    chunk = await backend_reader.read(min(BUFFER_SIZE, remaining))
                    if not chunk:
                        raise HTTPError(502, "Backend response body ended early")
                    writer.write(chunk)
                    await writer.drain()
                    remaining -= len(chunk)
            outcome = "complete"
        except (HTTPError, OSError, TimeoutError) as exc:
            if isinstance(exc, HTTPError):
                error_status, message = exc.status, str(exc)
            elif isinstance(exc, TimeoutError):
                error_status = 408 if phase == "request" else 504
                message = "Request timed out" if phase == "request" else "Backend timed out"
            else:
                error_status = 400 if phase == "request" else 502
                message = ("Client connection failed" if phase == "request"
                           else "Backend connection failed")
            pool = getattr(exc, "pool", pool)
            error = f"{type(exc).__name__}: {exc}"
            if not response_started:
                status = error_status
                await send_error(writer, status, message, head_only=head_only)
            else:
                # A second status line would corrupt the response already sent.
                outcome = "incomplete"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            writers = [writer] + ([backend_writer] if backend_writer else [])
            try:
                await asyncio.gather(*(close_stream(w) for w in writers))
            finally:
                if backend is not None:
                    self.router.release(backend.name, outcome, elapsed_ms, status)
                elif not is_stats:
                    self.router.unassigned_errors += 1
                log_event(
                    "stats" if is_stats else "request", request_id=request_id,
                    client_ip=client[0] if client else None,
                    client_port=client[1] if client else None,
                    method=request.method if request else None,
                    path=request.path if request else None, pool=pool,
                    backend=backend.name if backend else None, status=status,
                    outcome=outcome, response_time_ms=round(elapsed_ms, 3), error=error,
                )


async def serve(config):
    balancer = HTTPLoadBalancer(config)
    server = await asyncio.start_server(
        balancer.handle_client, config.host, config.port, limit=config.header_bytes,
        start_serving=False,
    )
    checker = HealthChecker(balancer.router)
    try:
        if config.health_check["enabled"]:
            await checker.check_once()  # Unknown backends are never selected.
        log_event("startup", host=config.host, port=config.port,
                  algorithm=config.algorithm, pools=list(config.pools))
        async with asyncio.TaskGroup() as group:
            if config.health_check["enabled"]:
                group.create_task(checker.run(), name="health-checks")
            await server.serve_forever()
    finally:
        server.close()
        await balancer.close()
        await server.wait_closed()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path,
                        default=Path(__file__).with_name("config.json"))
    parser.add_argument("--check-config", action="store_true",
                        help="validate configuration and exit without opening a socket")
    parser.add_argument("--algorithm", choices=ALGORITHMS,
                        help="override the configured algorithm for this run")
    parser.add_argument("--log-file", type=Path,
                        help="append JSON logs to a file as well as the terminal")
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        if args.algorithm:
            config = replace(config, algorithm=args.algorithm)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Invalid configuration: {exc}\n")
    if args.check_config:
        print(f"Configuration valid: {len(config.backends)} backends, "
              f"{len(config.pools)} pools, {len(config.routes)} routes.")
        return
    handlers = [logging.StreamHandler()]
    if args.log_file:
        try:
            handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
        except OSError as exc:
            parser.exit(1, f"Could not open log file: {exc}\n")
    for handler in handlers:
        handler.setFormatter(JSONFormatter())
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    try:
        asyncio.run(serve(config))
    except KeyboardInterrupt:
        log_event("shutdown")
    except OSError as exc:
        parser.exit(1, f"Could not start load balancer: {exc}\n")


if __name__ == "__main__":
    main()
