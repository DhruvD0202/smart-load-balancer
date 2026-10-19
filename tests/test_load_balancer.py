"""HTTP integration tests against real local sockets, including hostile framing."""

import asyncio
import json
import logging
from pathlib import Path
import unittest

from lb_config import parse_config
from load_balancer import HTTPLoadBalancer
from proxy import close_stream


def make_request(path="/", method="GET", headers=b"", body=b""):
    return (f"{method} {path} HTTP/1.1\r\nHost: demo\r\n".encode()
            + headers + b"\r\n" + body)


def reply(body=b"ok", extra=b""):
    return (b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode()
            + b"\r\n" + extra + b"\r\n" + body)


class HTTPTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers, self.writers, self.errors = [], [], []
        self.handlers = set()
        self.previous_level = logging.getLogger("load_balancer").level
        logging.getLogger("load_balancer").setLevel(logging.CRITICAL)

    async def asyncTearDown(self):
        for server in self.servers:
            server.close()
        tasks = list(self.handlers)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(close_stream(w) for w in self.writers))
        for server in self.servers:
            await server.wait_closed()
        logging.getLogger("load_balancer").setLevel(self.previous_level)
        self.assertEqual(self.errors, [])

    async def listen(self, handler, limit=16384):
        async def tracked(reader, writer):
            task = asyncio.current_task()
            self.handlers.add(task)
            try:
                await handler(reader, writer)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors.append(exc)
            finally:
                await close_stream(writer)
                self.handlers.discard(task)
        server = await asyncio.start_server(tracked, "127.0.0.1", 0, limit=limit)
        self.servers.append(server)
        return server, server.sockets[0].getsockname()[1]

    async def start_lb(self, handlers, header_bytes=16384, body_bytes=1048576,
                       response_timeout=2, health_enabled=False, algorithm="round_robin"):
        data = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())
        for index, handler in enumerate(handlers):
            _, port = await self.listen(handler)
            data["backends"][index]["port"] = port
        if len(handlers) == 1:
            data["backends"] = data["backends"][:1]
            data["pools"] = {name: ["backend-1"] for name in data["pools"]}
        data["limits"] = {"header_bytes": header_bytes, "body_bytes": body_bytes}
        data["timeouts"] = {"headers": 0.3, "body": 0.3, "connect": 0.5,
                            "response": response_timeout}
        data["algorithm"] = algorithm
        data["health_check"].update(enabled=health_enabled, interval=0.05, timeout=0.15)
        config = parse_config(data)
        self.balancer = HTTPLoadBalancer(config)
        _, port = await self.listen(self.balancer.handle_client, header_bytes)
        return port

    async def connect(self, port):
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), 3
        )
        self.writers.append(writer)
        return reader, writer

    async def exchange(self, port, raw):
        reader, writer = await self.connect(port)
        writer.write(raw)
        await writer.drain()
        return await asyncio.wait_for(reader.read(), 3)

    async def simple_backend(self, reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(reply())
        await writer.drain()


class HTTPIntegrationTests(HTTPTestCase):
    async def test_three_backends_route_and_rotate_over_http(self):
        def backend(name):
            async def respond(reader, writer):
                await reader.readuntil(b"\r\n\r\n")
                writer.write(reply(name.encode()))
                await writer.drain()
            return respond
        port = await self.start_lb([backend(f"backend-{n}") for n in (1, 2, 3)])
        cases = {
            "/hello": ["backend-1", "backend-2", "backend-3"] * 2,
            "/api/items?x=/static": ["backend-1", "backend-2"] * 3,
            "/static/a.css": ["backend-3"] * 6,
            "/apiculture": ["backend-1", "backend-2", "backend-3"] * 2,
        }
        for path, expected in cases.items():
            actual = []
            for _ in expected:
                response = await self.exchange(port, make_request(path))
                actual.append(response.split(b"\r\n\r\n")[1].decode())
            self.assertEqual(actual, expected)

    async def test_fragmented_headers_and_body_reach_backend_unchanged(self):
        seen = []
        payload = b"\x00binary\xff\r\n\r\n"

        async def backend(reader, writer):
            head = await reader.readuntil(b"\r\n\r\n")
            body = await reader.readexactly(len(payload))
            seen.append((head, body))
            writer.write(reply(body))
            await writer.drain()

        port = await self.start_lb([backend])
        reader, writer = await self.connect(port)
        raw = make_request("/api/echo", "POST",
                           f"Content-Length: {len(payload)}\r\nX-Demo: split\r\n".encode(),
                           payload)
        for offset in range(0, len(raw), 3):
            writer.write(raw[offset:offset + 3])
            await writer.drain()
            await asyncio.sleep(0)
        response = await asyncio.wait_for(reader.read(), 3)
        self.assertEqual(response.partition(b"\r\n\r\n")[2], payload)
        self.assertEqual(seen[0][1], payload)
        self.assertIn(b"x-demo: split\r\n", seen[0][0])
        self.assertIn(b"connection: close\r\n", seen[0][0])

    async def test_invalid_request_never_contacts_backend(self):
        called = []

        async def backend(reader, writer):
            called.append(True)

        port = await self.start_lb([backend], body_bytes=16)
        cases = [
            (make_request(headers=b"Content-Length: 1\r\nContent-Length: 1\r\n"), 400),
            (make_request(method="POST", headers=b"Content-Length: 17\r\n"), 413),
            (make_request(headers=b"Transfer-Encoding: chunked\r\n"), 501),
            (make_request(headers=b"Expect: 100-continue\r\n"), 417),
        ]
        for raw, status in cases:
            response = await self.exchange(port, raw)
            self.assertTrue(response.startswith(f"HTTP/1.1 {status} ".encode()), response)
        self.assertEqual(called, [])
        self.assertTrue(all(cursor == 0 for cursor in self.balancer.router.cursors.values()))

    async def test_incomplete_upload_returns_400(self):
        port = await self.start_lb([self.simple_backend])
        reader, writer = await self.connect(port)
        writer.write(make_request(method="POST", headers=b"Content-Length: 10\r\n", body=b"abc"))
        await writer.drain()
        writer.write_eof()
        response = await asyncio.wait_for(reader.read(), 3)
        self.assertTrue(response.startswith(b"HTTP/1.1 400 "))

    async def test_header_and_body_timeouts_return_408(self):
        port = await self.start_lb([self.simple_backend])
        for raw in (b"GET / HTTP/1.1\r\n",
                    make_request(method="POST", headers=b"Content-Length: 10\r\n")):
            response = await self.exchange(port, raw)
            self.assertTrue(response.startswith(b"HTTP/1.1 408 "))

    async def test_header_limit_returns_431(self):
        port = await self.start_lb([self.simple_backend], header_bytes=1024)
        response = await self.exchange(port, make_request(headers=b"X: " + b"a" * 1100 + b"\r\n"))
        self.assertTrue(response.startswith(b"HTTP/1.1 431 "))

    async def test_head_response_preserves_length_without_waiting_for_body(self):
        async def backend(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 999\r\n\r\n")
            await writer.drain()
            await asyncio.Event().wait()
        port = await self.start_lb([backend])
        response = await self.exchange(port, make_request(method="HEAD"))
        self.assertIn(b"content-length: 999\r\n", response)
        self.assertEqual(response.partition(b"\r\n\r\n")[2], b"")

    async def test_head_error_also_has_no_response_body(self):
        port = await self.start_lb([self.simple_backend])
        response = await self.exchange(port, make_request(
            method="HEAD", headers=b"Expect: 100-continue\r\n"
        ))
        self.assertTrue(response.startswith(b"HTTP/1.1 417 "))
        self.assertEqual(response.partition(b"\r\n\r\n")[2], b"")

    async def test_content_length_finishes_response_without_backend_eof(self):
        payload = bytes(range(256)) * 8192

        async def backend(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(reply(payload))
            await writer.drain()
            await asyncio.Event().wait()
        port = await self.start_lb([backend])
        response = await self.exchange(port, make_request())
        self.assertEqual(response.partition(b"\r\n\r\n")[2], payload)

    async def test_pipelined_second_request_is_not_forwarded(self):
        received = []

        async def backend(reader, writer):
            received.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(reply())
            await writer.drain()
            received.append(await reader.read())
        port = await self.start_lb([backend])
        response = await self.exchange(port, make_request("/first") + make_request("/second"))
        self.assertEqual(response.count(b"HTTP/1.1 200"), 1)
        # Let the backend observe the socket close and finish its second read.
        for _ in range(20):
            if len(received) == 2:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(received[1], b"")
        self.assertNotIn(b"/second", received[0])

    async def test_backend_refusal_returns_gateway_error(self):
        port = await self.start_lb([self.simple_backend])
        self.servers[0].close()
        await self.servers[0].wait_closed()
        response = await self.exchange(port, make_request())
        # Windows may hit the short connect deadline before reporting refusal.
        self.assertIn(int(response.split(b" ")[1]), (502, 504))

    async def test_backend_response_timeout_returns_504(self):
        async def backend(reader, writer):
            await reader.read()
        port = await self.start_lb([backend], response_timeout=0.1)
        response = await self.exchange(port, make_request())
        self.assertTrue(response.startswith(b"HTTP/1.1 504 "))

    async def test_invalid_backend_headers_return_502(self):
        async def backend(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n")
            await writer.drain()
        port = await self.start_lb([backend])
        response = await self.exchange(port, make_request())
        self.assertTrue(response.startswith(b"HTTP/1.1 502 "))

    async def test_truncation_does_not_append_error_or_kill_listener(self):
        calls = 0

        async def backend(reader, writer):
            nonlocal calls
            calls += 1
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n"
                         + (b"abc" if calls == 1 else b"0123456789"))
            await writer.drain()
        port = await self.start_lb([backend])
        first = await self.exchange(port, make_request())
        self.assertEqual(first.partition(b"\r\n\r\n")[2], b"abc")
        self.assertEqual(first.count(b"HTTP/1.1"), 1)
        second = await self.exchange(port, make_request())
        self.assertEqual(second.partition(b"\r\n\r\n")[2], b"0123456789")

    async def test_requests_progress_concurrently(self):
        count = 0
        ready = asyncio.Event()

        async def backend(reader, writer):
            nonlocal count
            await reader.readuntil(b"\r\n\r\n")
            count += 1
            if count == 6:
                ready.set()
            await ready.wait()
            writer.write(reply())
            await writer.drain()
        port = await self.start_lb([backend])
        responses = await asyncio.gather(*(self.exchange(port, make_request()) for _ in range(6)))
        self.assertTrue(all(response.endswith(b"\r\n\r\nok") for response in responses))
