"""Behavior of health probes, live stats, accounting, and JSON logs."""

import asyncio
import json
import logging
import unittest

from event_log import JSONFormatter
from health_checks import HealthChecker
from test_load_balancer import HTTPTestCase, make_request, reply


class OperationsTests(HTTPTestCase):
    async def idle(self):
        async with asyncio.timeout(3):
            while self.balancer.router.snapshot()["totals"]["active"]:
                await asyncio.sleep(0.01)

    async def stats(self, port):
        raw = await self.exchange(port, make_request("/stats"))
        self.assertTrue(raw.startswith(b"HTTP/1.1 200 "))
        return json.loads(raw.partition(b"\r\n\r\n")[2])

    async def test_stats_read_and_head_do_not_affect_assignment_counts(self):
        port = await self.start_lb([self.simple_backend])
        first = await self.stats(port)
        self.assertEqual(first["totals"]["requests"], 0)
        await self.exchange(port, make_request("/hello"))
        await self.idle()
        for _ in range(3):
            stats = await self.stats(port)
            self.assertEqual(stats["totals"], {"active": 0, "requests": 1,
                                              "completed": 1, "failed": 0, "cancelled": 0})
        head = await self.exchange(port, make_request("/stats", "HEAD"))
        self.assertIn(b"cache-control: no-store", head)
        self.assertEqual(head.partition(b"\r\n\r\n")[2], b"")
        rejected = await self.exchange(port, make_request("/stats", "POST",
                                                         b"Content-Length: 0\r\n"))
        self.assertTrue(rejected.startswith(b"HTTP/1.1 405 "))
        self.assertIn(b"allow: GET, HEAD\r\n", rejected)
        self.assertEqual((await self.stats(port))["totals"]["requests"], 1)

    async def test_reservation_visible_during_request_and_released_afterwards(self):
        arrived, finish = asyncio.Event(), asyncio.Event()

        async def backend(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            arrived.set()
            await finish.wait()
            writer.write(reply())
            await writer.drain()

        port = await self.start_lb([backend])
        client = asyncio.create_task(self.exchange(port, make_request("/slow")))
        try:
            await asyncio.wait_for(arrived.wait(), 1)
            self.assertEqual((await self.stats(port))["totals"]["active"], 1)
            finish.set()
            await client
            await self.idle()
            self.assertEqual((await self.stats(port))["totals"]["active"], 0)
        finally:
            client.cancel()
            await asyncio.gather(client, return_exceptions=True)

    async def test_shutdown_releases_active_reservations_as_cancelled(self):
        arrived = asyncio.Event()

        async def backend(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            arrived.set()
            await reader.read()

        port = await self.start_lb([backend])
        client = asyncio.create_task(self.exchange(port, make_request()))
        try:
            await asyncio.wait_for(arrived.wait(), 1)
            await self.balancer.close()
            await client
            totals = self.balancer.router.snapshot()["totals"]
            self.assertEqual((totals["active"], totals["cancelled"]), (0, 1))
        finally:
            client.cancel()
            await asyncio.gather(client, return_exceptions=True)

    async def test_truncated_response_counted_failed_without_latency_sample(self):
        async def backend(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nabc")
            await writer.drain()

        port = await self.start_lb([backend])
        raw = await self.exchange(port, make_request())
        self.assertEqual(raw.count(b"HTTP/1.1"), 1)
        await self.idle()
        row = (await self.stats(port))["backends"][0]
        self.assertEqual((row["active"], row["failed"], row["completed"]), (0, 1, 0))
        self.assertIsNone(row["avg_response_ms"])

    async def test_complete_origin_error_counted_separately(self):
        async def backend(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 500 Error\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        port = await self.start_lb([backend])
        await self.exchange(port, make_request())
        await self.idle()
        row = (await self.stats(port))["backends"][0]
        self.assertEqual((row["completed"], row["failed"], row["http_error_responses"]),
                         (1, 0, 1))

    async def test_failed_connect_releases_reservation(self):
        port = await self.start_lb([self.simple_backend])
        self.servers[0].close()
        await self.servers[0].wait_closed()
        raw = await self.exchange(port, make_request())
        self.assertIn(int(raw.split(b" ")[1]), (502, 504))
        await self.idle()
        row = (await self.stats(port))["backends"][0]
        self.assertEqual((row["requests"], row["failed"], row["active"]), (1, 1, 0))

    async def test_http_health_thresholds_and_probe_metrics(self):
        status = 200
        paths = []

        async def backend(reader, writer):
            nonlocal status
            head = await reader.readuntil(b"\r\n\r\n")
            paths.append(head.split(b" ")[1])
            writer.write(f"HTTP/1.1 {status} Status\r\nContent-Length: 2\r\n\r\nok".encode())
            await writer.drain()

        port = await self.start_lb([backend], health_enabled=True)
        checker = HealthChecker(self.balancer.router)
        raw = await self.exchange(port, make_request("/hello"))
        self.assertTrue(raw.startswith(b"HTTP/1.1 503 "))
        await checker.check_once()
        self.assertEqual(paths, [b"/health"])
        stats = await self.stats(port)
        self.assertEqual(stats["totals"]["requests"], 0)
        self.assertEqual(stats["backends"][0]["health"], "healthy")
        self.assertEqual(stats["unassigned_errors"], 1)
        status = 503
        await checker.check_once()
        self.assertEqual(self.balancer.router.states["backend-1"].health, "healthy")
        await checker.check_once()
        self.assertEqual(self.balancer.router.states["backend-1"].health, "unhealthy")
        raw = await self.exchange(port, make_request("/hello"))
        self.assertTrue(raw.startswith(b"HTTP/1.1 503 "))
        status = 200
        await checker.check_once()
        self.assertFalse(self.balancer.router.states["backend-1"].eligible)
        await checker.check_once()
        self.assertTrue(self.balancer.router.states["backend-1"].eligible)
        await self.exchange(port, make_request("/hello"))
        await self.idle()
        stats = await self.stats(port)
        self.assertEqual(stats["totals"]["requests"], 1)
        self.assertEqual(stats["backends"][0]["health_checks"], 5)

    async def test_periodic_probes_detect_failure_without_client_traffic(self):
        status = 200

        async def backend(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(f"HTTP/1.1 {status} Status\r\nContent-Length: 0\r\n\r\n".encode())
            await writer.drain()

        await self.start_lb([backend], health_enabled=True)
        checker = HealthChecker(self.balancer.router)
        await checker.check_once()
        task = asyncio.create_task(checker.run())
        try:
            status = 503
            async with asyncio.timeout(2):
                while self.balancer.router.states["backend-1"].health != "unhealthy":
                    await asyncio.sleep(0.02)
            self.assertEqual(self.balancer.router.snapshot()["totals"]["requests"], 0)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_health_rejects_truncated_or_stalled_response(self):
        async def backend(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nx")
            await writer.drain()
            await reader.read()

        await self.start_lb([backend], health_enabled=True)
        checker = HealthChecker(self.balancer.router)
        await checker.check_once()
        await checker.check_once()
        state = self.balancer.router.states["backend-1"]
        self.assertEqual(state.health, "unhealthy")
        self.assertIn("TimeoutError", state.last_error)

    async def test_response_timeout_releases_reservation(self):
        async def backend(reader, writer):
            await reader.read()
        port = await self.start_lb([backend], response_timeout=0.1)
        raw = await self.exchange(port, make_request())
        self.assertTrue(raw.startswith(b"HTTP/1.1 504 "))
        await self.idle()
        row = (await self.stats(port))["backends"][0]
        self.assertEqual((row["active"], row["failed"]), (0, 1))


class LogTests(unittest.TestCase):
    def test_log_is_json_and_escapes_control_characters(self):
        record = logging.LogRecord("test", logging.INFO, "", 1, "request", (), None)
        record.event_data = {"event": "request", "path": "/hello\ninjected",
                             "client_ip": "127.0.0.1", "backend": "backend-1",
                             "response_time_ms": 12.5}
        line = JSONFormatter().format(record)
        self.assertNotIn("\n", line)
        data = json.loads(line)
        self.assertEqual(data["path"], "/hello\ninjected")
        self.assertTrue(data["timestamp"].endswith("+00:00"))
