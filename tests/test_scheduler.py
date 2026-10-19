from collections import Counter
import copy
import unittest

from lb_config import parse_config
from scheduler import BackendScheduler, NoHealthyBackends
from test_lb_config import EXAMPLE


def scheduler(algorithm="round_robin", health=False):
    data = copy.deepcopy(EXAMPLE)
    data["algorithm"] = algorithm
    data["health_check"]["enabled"] = health
    return BackendScheduler(parse_config(data))


class SchedulerTests(unittest.TestCase):
    def test_weighted_distribution_and_independent_pools(self):
        router = scheduler("weighted_round_robin")
        counts = Counter()
        for _ in range(60):
            pool, backend = router.select("/")
            counts[backend.name] += 1
            router.release(backend.name, "complete", 10, 200)
        self.assertEqual(counts, {"backend-1": 30, "backend-2": 20, "backend-3": 10})
        api_counts = Counter()
        for _ in range(50):
            _, backend = router.select("/api/items")
            api_counts[backend.name] += 1
            router.release(backend.name, "complete", 10, 200)
        self.assertEqual(api_counts, {"backend-1": 30, "backend-2": 20})

    def test_least_connections_uses_global_active_reservations(self):
        router = scheduler("least_connections")
        _, busy = router.select("/api/slow")  # Backend 1 is busy in another pool.
        first = router.select("/")[1]
        second = router.select("/")[1]
        self.assertEqual((busy.name, first.name, second.name),
                         ("backend-1", "backend-2", "backend-3"))
        router.release(first.name, "complete", 10, 200)
        self.assertEqual(router.select("/")[1].name, "backend-2")

    def test_least_connections_rotates_when_all_counts_tie(self):
        router = scheduler("least_connections")
        names = []
        for _ in range(6):
            _, backend = router.select("/")
            names.append(backend.name)
            router.release(backend.name, "complete", 1, 200)
        self.assertEqual(names, ["backend-1", "backend-2", "backend-3"] * 2)

    def test_failure_and_recovery_thresholds(self):
        router = scheduler(health=True)
        with self.assertRaises(NoHealthyBackends):
            router.select("/")
        router.record_probe("backend-1", True)
        self.assertTrue(router.states["backend-1"].eligible)
        router.record_probe("backend-1", False, "timeout")
        self.assertTrue(router.states["backend-1"].eligible)
        router.record_probe("backend-1", False, "timeout")
        with self.assertRaises(NoHealthyBackends):
            router.select("/")
        router.record_probe("backend-1", True)
        self.assertFalse(router.states["backend-1"].eligible)
        router.record_probe("backend-1", True)
        self.assertTrue(router.states["backend-1"].eligible)

    def test_failed_probe_resets_recovery_streak(self):
        router = scheduler(health=True)
        for result in (False, False, True, False, True):
            router.record_probe("backend-1", result)
        self.assertEqual(router.states["backend-1"].health, "unhealthy")
        router.record_probe("backend-1", True)
        self.assertEqual(router.states["backend-1"].health, "healthy")

    def test_every_algorithm_skips_down_backends(self):
        for algorithm in ("round_robin", "least_connections", "weighted_round_robin"):
            router = scheduler(algorithm, health=True)
            router.record_probe("backend-2", True)
            names = []
            for _ in range(6):
                _, backend = router.select("/")
                names.append(backend.name)
                router.release(backend.name, "complete", 1, 200)
            self.assertEqual(names, ["backend-2"] * 6)
            with self.assertRaises(NoHealthyBackends):
                router.select("/static/file")

    def test_weighted_membership_change_recovers_proportion(self):
        router = scheduler("weighted_round_robin", health=True)
        for name in router.states:
            router.record_probe(name, True)
        router.select("/")
        for _ in range(2):
            router.record_probe("backend-1", False, "down")
        down_counts = Counter(router.select("/")[1].name for _ in range(30))
        self.assertEqual(down_counts, {"backend-2": 20, "backend-3": 10})
        for _ in range(2):
            router.record_probe("backend-1", True)
        recovered = Counter(router.select("/")[1].name for _ in range(60))
        self.assertEqual(recovered, {"backend-1": 30, "backend-2": 20, "backend-3": 10})

    def test_accounting_and_latency_exclude_failed_and_cancelled_transfers(self):
        router = scheduler()
        for outcome, duration, status in (("complete", 10, 200), ("complete", 30, 500),
                                           ("incomplete", 1000, 200), ("cancelled", 500, 0)):
            _, backend = router.select("/static/x")
            router.release(backend.name, outcome, duration, status)
        row = router.snapshot()["backends"][2]
        self.assertEqual((row["requests"], row["active"], row["completed"], row["failed"],
                          row["cancelled"], row["http_error_responses"], row["avg_response_ms"]),
                         (4, 0, 2, 1, 1, 1, 20))
        with self.assertRaises(RuntimeError):
            router.release("backend-3", "complete", 1, 200)
