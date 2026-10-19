"""Local socket integration tests: no external services or packages needed."""

import asyncio
import logging
import unittest

from proxy import TCPProxy, close_stream


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers = []
        self.writers = []
        self.handlers = set()
        self.errors = []
        self.old_log_level = logging.getLogger("proxy").level
        logging.getLogger("proxy").setLevel(logging.CRITICAL)

    async def asyncTearDown(self):
        for server in self.servers:
            server.close()
            await server.wait_closed()
        tasks = list(self.handlers)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(close_stream(w) for w in self.writers))
        logging.getLogger("proxy").setLevel(self.old_log_level)
        self.assertEqual(self.errors, [], "Unexpected exception in server handler")

    async def listen(self, handler, port=0):
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

        server = await asyncio.start_server(tracked, "127.0.0.1", port)
        self.servers.append(server)
        return server, server.sockets[0].getsockname()[1]

    async def proxy_for(self, backend, io_timeout=2):
        server, port = await self.listen(backend)
        proxy = TCPProxy("127.0.0.1", port, connect_timeout=1, io_timeout=io_timeout)
        _, proxy_port = await self.listen(proxy.handle_client)
        return proxy_port, server, port

    async def connect(self, port):
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), 3
        )
        self.writers.append(writer)
        return reader, writer

    async def read_all(self, reader):
        return await asyncio.wait_for(reader.read(), 3)

    async def test_http_request_and_response_are_unchanged(self):
        request = b"GET /hello?x=1 HTTP/1.1\r\nHost: example.test\r\n\r\n"
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello"
        received = []

        async def backend(reader, writer):
            received.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(response)
            await writer.drain()

        port, _, _ = await self.proxy_for(backend)
        reader, writer = await self.connect(port)
        writer.write(request)
        await writer.drain()
        self.assertEqual(await self.read_all(reader), response)
        self.assertEqual(received, [request])

    async def test_large_binary_transfer_is_not_truncated(self):
        payload = bytes(range(256)) * 8192  # 2 MiB, much larger than one read.

        async def backend(reader, writer):
            while data := await reader.read(8192):
                writer.write(data)
                await writer.drain()

        port, _, _ = await self.proxy_for(backend)
        reader, writer = await self.connect(port)

        async def upload():
            for offset in range(0, len(payload), 4096):
                writer.write(payload[offset:offset + 4096])
                await writer.drain()
            writer.write_eof()

        _, actual = await asyncio.gather(upload(), self.read_all(reader))
        self.assertEqual(actual, payload)

    async def test_server_can_speak_before_client(self):
        async def backend(reader, writer):
            writer.write(b"ready\n")
            await writer.drain()
            reply = await reader.readline()
            writer.write(b"received:" + reply)
            await writer.drain()

        port, _, _ = await self.proxy_for(backend)
        reader, writer = await self.connect(port)
        self.assertEqual(await asyncio.wait_for(reader.readline(), 3), b"ready\n")
        writer.write(b"hello\n")
        await writer.drain()
        self.assertEqual(await self.read_all(reader), b"received:hello\n")

    async def test_client_half_close_keeps_delayed_response(self):
        async def backend(reader, writer):
            data = await reader.read()  # Wait specifically for client EOF.
            await asyncio.sleep(0.05)
            writer.write(b"reply:" + data)
            await writer.drain()

        port, _, _ = await self.proxy_for(backend)
        reader, writer = await self.connect(port)
        writer.write(b"request")
        await writer.drain()
        writer.write_eof()
        self.assertEqual(await self.read_all(reader), b"reply:request")

    async def test_backend_half_close_keeps_client_upload(self):
        received = asyncio.get_running_loop().create_future()

        async def backend(reader, writer):
            writer.write(b"send your data")
            await writer.drain()
            writer.write_eof()
            received.set_result(await reader.read())

        port, _, _ = await self.proxy_for(backend)
        reader, writer = await self.connect(port)
        self.assertEqual(await self.read_all(reader), b"send your data")
        writer.write(b"still sending after backend EOF")
        await writer.drain()
        writer.write_eof()
        self.assertEqual(
            await asyncio.wait_for(received, 3), b"still sending after backend EOF"
        )

    async def test_clients_make_progress_concurrently(self):
        arrived = 0
        all_arrived = asyncio.Event()

        async def backend(reader, writer):
            nonlocal arrived
            arrived += 1
            if arrived == 6:
                all_arrived.set()
            # A proxy serving one client at a time cannot pass this barrier.
            await all_arrived.wait()
            writer.write(b"ok")
            await writer.drain()

        port, _, _ = await self.proxy_for(backend)

        async def client():
            reader, _ = await self.connect(port)
            return await self.read_all(reader)

        self.assertEqual(await asyncio.gather(*(client() for _ in range(6))),
                         [b"ok"] * 6)

    async def test_backend_down_then_restarted_without_proxy_restart(self):
        async def backend(reader, writer):
            writer.write(b"recovered")
            await writer.drain()

        port, backend_server, backend_port = await self.proxy_for(backend)
        backend_server.close()
        await backend_server.wait_closed()
        reader, _ = await self.connect(port)
        try:
            self.assertEqual(await self.read_all(reader), b"")
        except ConnectionResetError:
            pass  # Windows may report a reset rather than EOF.
        await self.listen(backend, backend_port)
        reader, _ = await self.connect(port)
        self.assertEqual(await self.read_all(reader), b"recovered")

    async def test_truncated_backend_response_does_not_kill_proxy(self):
        attempts = 0

        async def backend(reader, writer):
            nonlocal attempts
            await reader.readuntil(b"\r\n\r\n")
            attempts += 1
            body = b"abc" if attempts == 1 else b"0123456789"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n" + body)
            await writer.drain()
            # First response ends early. The TCP proxy cannot repair HTTP.

        port, _, _ = await self.proxy_for(backend)
        bodies = []
        for _ in range(2):
            reader, writer = await self.connect(port)
            writer.write(b"GET / HTTP/1.1\r\nHost: demo\r\n\r\n")
            await writer.drain()
            response = await self.read_all(reader)
            bodies.append(response.split(b"\r\n\r\n", 1)[1])
            await close_stream(writer)
        self.assertEqual(bodies, [b"abc", b"0123456789"])

    async def test_idle_connection_times_out(self):
        async def backend(reader, writer):
            await reader.read()

        port, _, _ = await self.proxy_for(backend, io_timeout=0.1)
        reader, _ = await self.connect(port)
        self.assertEqual(await self.read_all(reader), b"")

    async def test_client_disconnect_does_not_kill_proxy(self):
        attempts = 0
        first_connected = asyncio.Event()

        async def backend(reader, writer):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                first_connected.set()
                await reader.read()
            else:
                writer.write(b"still alive")
                await writer.drain()

        port, _, _ = await self.proxy_for(backend)
        _, writer = await self.connect(port)
        await asyncio.wait_for(first_connected.wait(), 3)
        writer.transport.abort()
        reader, _ = await self.connect(port)
        self.assertEqual(await self.read_all(reader), b"still alive")


if __name__ == "__main__":
    unittest.main()
