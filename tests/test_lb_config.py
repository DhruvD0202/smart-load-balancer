import copy
import json
from pathlib import Path
import unittest

from lb_config import parse_config, unique_object
from scheduler import BackendScheduler

EXAMPLE = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text())
EXAMPLE["health_check"]["enabled"] = False


class ConfigTests(unittest.TestCase):
    def test_round_robin_is_independent_per_pool(self):
        router = BackendScheduler(parse_config(copy.deepcopy(EXAMPLE)))
        paths = ["/", "/api/items", "/", "/static/a", "/api/items", "/", "/api/items"]
        actual = [(pool, backend.name) for pool, backend in map(router.select, paths)]
        self.assertEqual(actual, [
            ("default", "backend-1"), ("api", "backend-1"), ("default", "backend-2"),
            ("static", "backend-3"), ("api", "backend-2"), ("default", "backend-3"),
            ("api", "backend-1")])

    def test_longest_segment_prefix_wins(self):
        data = copy.deepcopy(EXAMPLE)
        data["routes"].append({"prefix": "/api/admin", "pool": "static"})
        router = BackendScheduler(parse_config(data))
        actual = [router.select(path)[0] for path in
                  ("/api", "/api/", "/api/admin/users", "/apiculture", "/api/administrator")]
        self.assertEqual(actual, ["api", "api", "static", "default", "api"])

    def test_invalid_config_fails_before_startup(self):
        changes = [
            lambda d: d.update(algorithm="unsupported_algorithm"),
            lambda d: d.update(algoritm="round_robin"),
            lambda d: d["listen"].update(port=True),
            lambda d: d["backends"].append(d["backends"][0]),
            lambda d: d["pools"].update(api=[]),
            lambda d: d["pools"].update(api=["missing"]),
            lambda d: d["pools"].update(api=["backend-1", "backend-1"]),
            lambda d: d.update(default_pool="missing"),
            lambda d: d["routes"].append({"prefix": "/api", "pool": "static"}),
            lambda d: d["routes"].append({"prefix": "/wrong/*", "pool": "static"}),
            lambda d: d["routes"].append({"prefix": "/api", "pool": []}),
            lambda d: d["timeouts"].update(response=float("nan")),
            lambda d: d["timeouts"].update(headers=-1),
            lambda d: d["limits"].update(body_bytes=100000000),
            lambda d: d["backends"][0].update(weight=0),
            lambda d: d["health_check"].update(enabled="yes"),
            lambda d: d["health_check"].update(interval=float("inf")),
            lambda d: d["health_check"].update(failure_threshold=0),
            lambda d: d["health_check"].update(recovery_threshold=True),
            lambda d: d["health_check"].update(path="bad\r\npath"),
        ]
        for change in changes:
            data = copy.deepcopy(EXAMPLE)
            change(data)
            with self.subTest(config=data):
                with self.assertRaises(ValueError):
                    parse_config(data)

    def test_duplicate_json_keys_are_rejected(self):
        with self.assertRaises(ValueError):
            json.loads('{"algorithm":"round_robin","algorithm":"other"}',
                       object_pairs_hook=unique_object)
