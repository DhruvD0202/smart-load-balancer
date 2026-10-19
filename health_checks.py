"""Periodic HTTP probes; health traffic never advances routing or request stats."""

import asyncio

from http_protocol import HTTPError, parse_response, read_head
from proxy import close_stream


class HealthChecker:
    def __init__(self, scheduler):
        self.scheduler = scheduler
        self.config = scheduler.config

    async def probe(self, backend):
        writer = None
        try:
            async with asyncio.timeout(self.config.health_check["timeout"]):
                reader, writer = await asyncio.open_connection(
                    backend.host, backend.port, limit=self.config.header_bytes
                )
                host = f"[{backend.host}]" if ":" in backend.host else backend.host
                writer.write(
                    f"GET {self.config.health_check['path']} HTTP/1.1\r\n"
                    f"Host: {host}:{backend.port}\r\nConnection: close\r\n\r\n".encode("ascii")
                )
                await writer.drain()
                response = parse_response(
                    await read_head(reader, self.config.header_bytes, 502), "GET"
                )
                if response.status != 200:
                    raise HTTPError(502, f"Health endpoint returned {response.status}")
                if response.body_length > 65536:
                    raise HTTPError(502, "Health response exceeds 64 KiB")
                await reader.readexactly(response.body_length)
        finally:
            if writer is not None:
                await close_stream(writer)

    async def check_backend(self, backend):
        try:
            await self.probe(backend)
        except (OSError, TimeoutError, HTTPError, asyncio.IncompleteReadError) as exc:
            self.scheduler.record_probe(backend.name, False, f"{type(exc).__name__}: {exc}")
        else:
            self.scheduler.record_probe(backend.name, True)

    async def check_once(self):
        await asyncio.gather(*(self.check_backend(b) for b in self.config.backends.values()))

    async def run(self):
        while True:
            # Sleeping yields control; it does not block client handlers.
            await asyncio.sleep(self.config.health_check["interval"])
            await self.check_once()
