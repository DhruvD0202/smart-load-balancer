"""Validate JSON configuration before opening a listening socket."""

from dataclasses import dataclass
import json
import math
from pathlib import Path

ALGORITHMS = ("round_robin", "least_connections", "weighted_round_robin")

@dataclass(frozen=True)
class Backend:
    name: str
    host: str
    port: int
    weight: int = 1


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    backends: dict
    pools: dict
    default_pool: str
    routes: tuple
    timeouts: dict
    header_bytes: int
    body_bytes: int
    algorithm: str
    health_check: dict


def object_fields(value, allowed, required, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    if set(value) - set(allowed):
        raise ValueError(f"Unknown fields in {label}: {sorted(set(value) - set(allowed))}")
    if set(required) - set(value):
        raise ValueError(f"Missing fields in {label}: {sorted(set(required) - set(value))}")


def text_field(value, label):
    if not isinstance(value, str) or not value or any(ord(c) <= 32 for c in value):
        raise ValueError(f"{label} must be a nonempty string without whitespace")
    return value


def integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label} must be an integer in {low}..{high}")
    return value


def parse_config(data):
    required = ("listen", "algorithm", "backends", "pools", "default_pool", "routes")
    object_fields(data, (*required, "timeouts", "limits", "health_check"), required, "config")
    if data["algorithm"] not in ALGORITHMS:
        raise ValueError(f"algorithm must be one of {', '.join(ALGORITHMS)}")
    listen = data["listen"]
    object_fields(listen, ("host", "port"), ("host", "port"), "listen")
    host = text_field(listen["host"], "listen.host")
    port = integer(listen["port"], 1, 65535, "listen.port")
    if not isinstance(data["backends"], list) or not data["backends"]:
        raise ValueError("backends must be a nonempty list")
    backends, addresses = {}, set()
    for item in data["backends"]:
        object_fields(item, ("name", "host", "port", "weight"), ("name", "host", "port"), "backend")
        backend = Backend(text_field(item["name"], "backend.name"),
                          text_field(item["host"], "backend.host"),
                          integer(item["port"], 1, 65535, "backend.port"),
                          integer(item.get("weight", 1), 1, 1000, "backend.weight"))
        if not backend.host.isascii():
            raise ValueError("Use an ASCII hostname or IP address for backend.host")
        address = (backend.host, backend.port)
        if backend.name in backends or address in addresses:
            raise ValueError("Backend names and host/port pairs must be unique")
        if address == (host, port):
            raise ValueError("A backend cannot be the load balancer itself")
        backends[backend.name] = backend
        addresses.add(address)
    pools = data["pools"]
    if not isinstance(pools, dict) or not pools:
        raise ValueError("pools must be a nonempty object")
    for name, members in pools.items():
        text_field(name, "pool name")
        if (not isinstance(members, list) or not members
                or any(not isinstance(m, str) or m not in backends for m in members)):
            raise ValueError(f"Pool {name} needs a nonempty list of known backend names")
        if len(set(members)) != len(members):
            raise ValueError(f"Pool {name} contains duplicate members")
    default = text_field(data["default_pool"], "default_pool")
    if default not in pools:
        raise ValueError("default_pool must name an existing pool")
    if not isinstance(data["routes"], list):
        raise ValueError("routes must be a list")
    routes, prefixes = [], set()
    for route in data["routes"]:
        object_fields(route, ("prefix", "pool"), ("prefix", "pool"), "route")
        prefix = text_field(route["prefix"], "route.prefix")
        if (not prefix.startswith("/") or "//" in prefix
                or any(c in prefix for c in "?%#\\*")
                or any(part in (".", "..") for part in prefix.split("/"))
                or (len(prefix) > 1 and prefix.endswith("/"))):
            raise ValueError("Route prefixes must be literal paths like /api (no trailing slash)")
        pool = text_field(route["pool"], "route.pool")
        if prefix in prefixes or pool not in pools:
            raise ValueError("Routes need unique prefixes and existing pool names")
        routes.append((prefix, pool))
        prefixes.add(prefix)
    timeouts = {"headers": 5, "body": 5, "connect": 3, "response": 10}
    supplied = data.get("timeouts", {})
    object_fields(supplied, timeouts, (), "timeouts")
    timeouts.update(supplied)
    for name, value in timeouts.items():
        if (type(value) not in (int, float) or not math.isfinite(value)
                or not 0 < value <= 300):
            raise ValueError(f"timeouts.{name} must be a finite number in (0, 300]")
    limits = data.get("limits", {})
    object_fields(limits, ("header_bytes", "body_bytes"), (), "limits")
    header_bytes = integer(limits.get("header_bytes", 16384), 1024, 65536,
                           "limits.header_bytes")
    body_bytes = integer(limits.get("body_bytes", 1048576), 0, 16777216,
                         "limits.body_bytes")
    health = {"enabled": True, "path": "/health", "interval": 2, "timeout": 1,
              "failure_threshold": 2, "recovery_threshold": 2}
    supplied_health = data.get("health_check", {})
    object_fields(supplied_health, health, (), "health_check")
    health.update(supplied_health)
    if type(health["enabled"]) is not bool:
        raise ValueError("health_check.enabled must be true or false")
    path = text_field(health["path"], "health_check.path")
    if (not path.isascii() or not path.startswith("/") or path.startswith("//")
            or any(c in path for c in "#\\") or path == "/stats"):
        raise ValueError("health_check.path must be an ASCII origin path, other than /stats")
    for name in ("interval", "timeout"):
        value = health[name]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 300:
            raise ValueError(f"health_check.{name} must be a finite number in (0, 300]")
    for name in ("failure_threshold", "recovery_threshold"):
        integer(health[name], 1, 20, f"health_check.{name}")
    return Config(host, port, backends,
                  {name: tuple(members) for name, members in pools.items()},
                  default, tuple(sorted(routes, key=lambda r: -len(r[0]))),
                  timeouts, header_bytes, body_bytes, data["algorithm"], health)


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f"Duplicate JSON key: {name}")
        result[name] = value
    return result


def load_config(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"),
                      object_pairs_hook=unique_object)
    return parse_config(data)
