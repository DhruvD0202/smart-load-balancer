"""Exercise the browser controller against its real owned backend processes."""

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from demo_app import DemoHandler, DemoLab, DemoServer


class BrowserDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.lab = DemoLab(Path(cls.temp.name))
        cls.server = DemoServer(("127.0.0.1", 0), DemoHandler)
        cls.server.lab = cls.lab
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join(timeout=3)
        cls.server.server_close()
        processes = [cls.lab.balancer, *cls.lab.processes.values()]
        cls.lab.close()
        assert all(p is None or p.poll() is not None for p in processes), "Demo child leaked"
        cls.temp.cleanup()

    def setUp(self):
        self.lab.reset("round_robin")

    def api(self, path, data=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=20)
        request_headers = {"Content-Type": "application/json", "X-Demo-Token": self.lab.token}
        request_headers.update(headers or {})
        try:
            connection.request("POST" if data is not None else "GET", path,
                               body=json.dumps(data) if data is not None else None,
                               headers=request_headers)
            response = connection.getresponse()
            body = response.read()
            return response.status, json.loads(body) if "json" in response.getheader("Content-Type", "") else body
        finally:
            connection.close()

    def idle_stats(self):
        self.lab.wait_until(lambda: self.lab.stats()["totals"]["active"] == 0)
        return self.lab.stats()

    def test_static_app_and_polling_are_not_backend_traffic(self):
        for path in ("/", "/app.js", "/style.css"):
            status, body = self.api(path)
            self.assertEqual(status, 200)
            self.assertGreater(len(body), 100)
        for _ in range(3):
            status, state = self.api("/api/state")
            self.assertEqual(status, 200)
            self.assertEqual(state["stats"]["totals"]["requests"], 0)
        self.assertEqual(self.api("/../config.json")[0], 404)

    def test_real_guided_algorithms(self):
        for kind, expected in (("round_robin", [2, 2, 2]), ("weighted", [6, 4, 2]), ("busy", [1, 3, 3])):
            with self.subTest(kind=kind):
                status, result = self.api("/api/experiment", {"kind": kind})
                self.assertEqual(status, 200, result)
                self.assertTrue(result["passed"], result)
                stats = self.idle_stats()
                self.assertEqual([b["completed"] for b in stats["backends"]], expected)
                self.assertEqual(stats["totals"]["failed"], 0)

    def test_path_pools_and_post_body(self):
        for path, allowed in (("/api/items", {"backend-1", "backend-2"}),
                              ("/static/style.css", {"backend-3"}),
                              ("/apiculture", {"backend-1", "backend-2", "backend-3"})):
            status, result = self.api("/api/traffic", {"path": path, "count": 6})
            self.assertEqual(status, 200)
            self.assertEqual({r["backend"] for r in result["results"]}, allowed)
        status, result = self.api("/api/traffic", {"path": "/api/echo"})
        document = json.loads(result["results"][0]["body"])
        self.assertEqual(status, 200)
        self.assertEqual(document["demo_header"], "browser-lab")
        self.assertIn("Hello from the browser classroom", document["body_utf8"])

    def test_real_health_failure_recovery_and_partial_response(self):
        self.api("/api/backend", {"name": "backend-2", "command": "stop"})
        self.lab.wait_until(lambda: self.lab.stats()["backends"][1]["health"] == "unhealthy")
        _, result = self.api("/api/traffic", {"path": "/hello", "count": 6})
        self.assertEqual({r["backend"] for r in result["results"]}, {"backend-1", "backend-3"})
        self.api("/api/backend", {"name": "backend-2", "command": "start"})
        self.lab.wait_until(lambda: self.lab.stats()["backends"][1]["health"] == "healthy")
        _, result = self.api("/api/traffic", {"path": "/truncate"})
        self.assertEqual(result["results"][0]["status"], 200)
        self.assertEqual(result["results"][0]["outcome"], "incomplete")
        self.assertEqual(self.idle_stats()["totals"]["failed"], 1)
        self.api("/api/backend", {"name": "backend-3", "command": "stop"})
        self.lab.wait_until(lambda: self.lab.stats()["backends"][2]["health"] == "unhealthy")
        _, result = self.api("/api/traffic", {"path": "/static/style.css"})
        self.assertEqual(result["results"][0]["status"], 503)
        _, result = self.api("/api/traffic", {"path": "/api/items"})
        self.assertEqual(result["results"][0]["status"], 200)
        for name in ("backend-1", "backend-2"):
            self.api("/api/backend", {"name": name, "command": "stop"})
        self.lab.wait_until(lambda: all(b["health"] == "unhealthy" for b in self.lab.stats()["backends"]))
        _, result = self.api("/api/traffic", {"path": "/hello"})
        self.assertEqual(result["results"][0]["status"], 503)
        self.assertEqual(self.api("/api/state")[0], 200)
        # Fresh run restores stopped children and resets measured counters.
        self.assertEqual(self.api("/api/reset", {"algorithm": "round_robin"})[0], 200)
        self.assertEqual(self.lab.stats()["totals"]["requests"], 0)

    def test_controls_are_bounded_and_local(self):
        for path, data in (("/api/traffic", {"path": "http://example.com"}),
                           ("/api/traffic", {"path": "/hello", "count": 1000}),
                           ("/api/traffic", {"path": "/slow", "count": 12}),
                           ("/api/backend", {"name": "other-process", "command": "stop"}),
                           ("/api/reset", {"algorithm": "unknown"})):
            self.assertEqual(self.api(path, data)[0], 400)
        self.assertEqual(self.api("/api/reset", {}, {"X-Demo-Token": "invalid"})[0], 403)
        self.assertEqual(self.api("/api/reset", {}, {"X-Demo-Token": "£"})[0], 403)
        self.assertEqual(self.api("/api/reset", {}, {"Origin": "https://example.com"})[0], 403)
        self.assertEqual(self.api("/api/state", headers={"Host": "example.com"})[0], 403)
        self.assertEqual(self.api("/api/traffic", {"path": "/hello", "extra": "x" * 5000})[0], 413)
        self.assertEqual(self.lab.stats()["totals"]["requests"], 0)

    def test_second_action_does_not_interrupt_experiment(self):
        with self.lab.operation("busy experiment"):
            status, result = self.api("/api/reset", {})
            self.assertEqual(status, 409)
            self.assertIn("already running", result["error"])
            status, state = self.api("/api/state")
            self.assertEqual(status, 200)
            self.assertEqual(state["busy"], "busy experiment")


if __name__ == "__main__":
    unittest.main()
