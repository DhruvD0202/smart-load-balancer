"""Backend selection, health state, and counters; mutations contain no await."""

from dataclasses import dataclass
import time

from event_log import log_event, utc_now
from http_protocol import HTTPError


class NoHealthyBackends(HTTPError):
    def __init__(self, pool):
        super().__init__(503, f"No healthy backends in pool {pool}")
        self.pool = pool


@dataclass
class BackendState:
    health: str
    active: int = 0
    assigned: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    http_errors: int = 0
    duration_total_ms: float = 0
    last_response_ms: float | None = None
    successes: int = 0
    failures: int = 0
    checks: int = 0
    last_checked: str | None = None
    last_error: str | None = None

    @property
    def eligible(self):
        return self.health in ("healthy", "unchecked")


class BackendScheduler:
    def __init__(self, config):
        self.config = config
        initial = "unknown" if config.health_check["enabled"] else "unchecked"
        self.states = {name: BackendState(initial) for name in config.backends}
        self.cursors = {name: 0 for name in config.pools}
        self.scores = {pool: {name: 0 for name in members}
                       for pool, members in config.pools.items()}
        self.previous_eligible = {}
        self.started = time.perf_counter()
        self.unassigned_errors = 0

    def pool_for(self, path):
        for prefix, pool in self.config.routes:
            if prefix == "/" or path == prefix or path.startswith(prefix + "/"):
                return pool
        return self.config.default_pool

    def select(self, path):
        pool = self.pool_for(path)
        members = self.config.pools[pool]
        eligible = tuple(name for name in members if self.states[name].eligible)
        if not eligible:
            raise NoHealthyBackends(pool)
        if self.config.algorithm == "weighted_round_robin":
            # Reset accumulated scores when eligibility changes; recovered
            # backends must not receive a burst from obsolete accumulated scores.
            if self.previous_eligible.get(pool) != eligible:
                self.scores[pool] = dict.fromkeys(members, 0)
                self.previous_eligible[pool] = eligible
            scores = self.scores[pool]
            for name in eligible:
                scores[name] += self.config.backends[name].weight
            chosen = max(eligible, key=lambda name: scores[name])
            scores[chosen] -= sum(self.config.backends[name].weight for name in eligible)
        else:
            # Scan in rotating order, skipping unhealthy members. This order
            # also provides a fair tie-break for least-connections.
            start = self.cursors[pool]
            ordered = [members[(start + i) % len(members)] for i in range(len(members))]
            ordered = [name for name in ordered if name in eligible]
            chosen = (min(ordered, key=lambda name: self.states[name].active)
                      if self.config.algorithm == "least_connections" else ordered[0])
            self.cursors[pool] = (members.index(chosen) + 1) % len(members)
        state = self.states[chosen]
        state.active += 1  # Reserve BEFORE a connection attempt can suspend.
        state.assigned += 1
        return pool, self.config.backends[chosen]

    def release(self, name, outcome, duration_ms, status):
        state = self.states[name]
        if state.active <= 0:
            raise RuntimeError(f"Unbalanced backend reservation for {name}")
        state.active -= 1
        if outcome == "complete":
            state.completed += 1
            state.duration_total_ms += duration_ms
            state.last_response_ms = duration_ms
            state.http_errors += int(status >= 400)
        elif outcome == "cancelled":
            state.cancelled += 1
        else:
            state.failed += 1

    def record_probe(self, name, ok, error=None):
        state = self.states[name]
        before = state.health
        state.checks += 1
        state.last_checked = utc_now()
        state.last_error = error
        if ok:
            state.successes += 1
            state.failures = 0
            if (state.health == "unknown"
                    or state.successes >= self.config.health_check["recovery_threshold"]):
                state.health = "healthy"
        else:
            state.failures += 1
            state.successes = 0
            if state.failures >= self.config.health_check["failure_threshold"]:
                state.health = "unhealthy"
        if before != state.health:
            log_event("health_change", backend=name, previous=before,
                      health=state.health, error=error)

    def snapshot(self):
        backends = []
        for name, backend in self.config.backends.items():
            state = self.states[name]
            backends.append({
                "name": name, "host": backend.host, "port": backend.port,
                "weight": backend.weight, "health": state.health,
                "eligible": state.eligible, "active": state.active,
                "requests": state.assigned, "completed": state.completed,
                "failed": state.failed, "cancelled": state.cancelled,
                "http_error_responses": state.http_errors,
                "avg_response_ms": (round(state.duration_total_ms / state.completed, 3)
                                    if state.completed else None),
                "last_response_ms": (round(state.last_response_ms, 3)
                                     if state.last_response_ms is not None else None),
                "health_checks": state.checks, "consecutive_successes": state.successes,
                "consecutive_failures": state.failures,
                "last_checked": state.last_checked, "last_health_error": state.last_error,
            })
        return {
            "timestamp": utc_now(), "algorithm": self.config.algorithm,
            "uptime_seconds": round(time.perf_counter() - self.started, 3),
            "health_check": dict(self.config.health_check),
            "pools": {pool: {"members": list(members),
                            "eligible": [name for name in members if self.states[name].eligible]}
                      for pool, members in self.config.pools.items()},
            "totals": {key: sum(row[key] for row in backends)
                       for key in ("active", "requests", "completed", "failed", "cancelled")},
            "unassigned_errors": self.unassigned_errors,
            "backends": backends,
            "latency_definition": "Completed assigned requests: accept-to-transfer end, "
                                  "including client upload and backend waits; excludes cleanup.",
        }
