"""Week 1: an asyncio TCP proxy forwarding to one backend (Python 3.11+)."""

import argparse
import asyncio
import logging
import math
import time

LOG = logging.getLogger("proxy")
BUFFER_SIZE = 64 * 1024


async def close_stream(writer):
    """Close a socket, but do not wait forever for a stalled peer."""
    writer.close()
    try:
        async with asyncio.timeout(2):
            await writer.wait_closed()
    except (OSError, TimeoutError):
        writer.transport.abort()


async def relay(reader, writer, counts, direction, io_timeout):
    """Copy bytes in ONE direction until EOF, failure, or a timeout."""
    while True:
        async with asyncio.timeout(io_timeout):
            data = await reader.read(BUFFER_SIZE)
        if not data:
            # EOF means this peer finished sending. The reverse direction
            # may still have a response to deliver, so only close this half.
            if writer.can_write_eof():
                writer.write_eof()
                async with asyncio.timeout(io_timeout):
                    await writer.drain()
            return
        writer.write(data)
        async with asyncio.timeout(io_timeout):
            # Backpressure: pause if the receiving side cannot keep up.
            await writer.drain()
        counts[direction] += len(data)


class TCPProxy:
    def __init__(self, backend_host, backend_port, connect_timeout=3, io_timeout=30):
        self.backend_host = backend_host
        self.backend_port = backend_port
        self.connect_timeout = connect_timeout
        self.io_timeout = io_timeout

    async def handle_client(self, client_reader, client_writer):
        """Own both sockets and both relay tasks for one client connection."""
        peer = client_writer.get_extra_info("peername")
        backend = f"{self.backend_host}:{self.backend_port}"
        started = time.perf_counter()
        counts = {"up": 0, "down": 0}
        backend_writer = None
        tasks = []
        outcome = "closed"
        phase = "connect"
        LOG.info("connection_open client=%s backend=%s", peer, backend)
        try:
            async with asyncio.timeout(self.connect_timeout):
                backend_reader, backend_writer = await asyncio.open_connection(
                    self.backend_host, self.backend_port
                )
            phase = "relay"
            # Schedule BOTH directions before waiting for either to finish.
            tasks = [
                asyncio.create_task(relay(
                    client_reader, backend_writer, counts, "up", self.io_timeout
                )),
                asyncio.create_task(relay(
                    backend_reader, client_writer, counts, "down", self.io_timeout
                )),
            ]
            await asyncio.gather(*tasks)
        except (OSError, TimeoutError) as exc:
            outcome = "failed"
            LOG.warning(
                "connection_failed client=%s backend=%s phase=%s error=%s detail=%s",
                peer, backend, phase, type(exc).__name__, exc,
            )
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            # gather() does not cancel the other relay when one raises.
            # Stop it before closing the sockets it may still be using.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            writers = [client_writer]
            if backend_writer is not None:
                writers.append(backend_writer)
            await asyncio.gather(*(close_stream(writer) for writer in writers))
            LOG.info(
                "connection_end client=%s backend=%s outcome=%s "
                "bytes_up=%d bytes_down=%d connection_ms=%.2f",
                peer, backend, outcome, counts["up"], counts["down"],
                (time.perf_counter() - started) * 1000,
            )


async def serve(args):
    proxy = TCPProxy(
        args.backend_host, args.backend_port, args.connect_timeout, args.io_timeout
    )
    server = await asyncio.start_server(proxy.handle_client, args.host, args.port)
    LOG.info(
        "Listening on %s:%d -> %s:%d (Ctrl+C to stop)",
        args.host, args.port, args.backend_host, args.backend_port,
    )
    async with server:
        await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--backend-host", default="127.0.0.1")
    parser.add_argument("--backend-port", type=int, default=9001)
    parser.add_argument("--connect-timeout", type=float, default=3)
    parser.add_argument("--io-timeout", type=float, default=30,
                        help="seconds allowed for each read or drain in either direction")
    args = parser.parse_args()
    for name in ("port", "backend_port"):
        if not 1 <= getattr(args, name) <= 65535:
            parser.error(f"--{name.replace('_', '-')} must be between 1 and 65535")
    for name in ("connect_timeout", "io_timeout"):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            parser.error(f"--{name.replace('_', '-')} must be finite and positive")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        asyncio.run(serve(args))
    except KeyboardInterrupt:
        LOG.info("Proxy stopped.")
    except OSError as exc:
        parser.exit(1, f"Could not start proxy: {exc}\n")


if __name__ == "__main__":
    main()
