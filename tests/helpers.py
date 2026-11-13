from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import socket  # noqa: E402


def _no_network(*_args, **_kwargs):
    raise AssertionError("tests must not open a network connection")


# Belt and braces: every test module imports this file, so no test can reach any site.
socket.socket.connect = _no_network
socket.socket.connect_ex = _no_network
socket.create_connection = _no_network

from pricebot.fetch import Response  # noqa: E402
from pricebot.registry import load  # noqa: E402


def enabled_registry():
    return load(FIXTURES / "registry-enabled.json")


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeTransport:
    """Serves stored responses; records every request. No network."""

    def __init__(self, routes: dict[str, object], clock: FakeClock | None = None):
        self.routes = routes
        self.requests: list[tuple[str, dict, float]] = []
        self.clock = clock

    def __call__(self, url, headers, timeout, max_bytes):
        self.requests.append((url, dict(headers), self.clock.now if self.clock else 0.0))
        r = self.routes.get(url, 404)
        if callable(r):
            r = r()
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        if isinstance(r, int):
            return Response(r, {}, b"", url)
        if isinstance(r, tuple):
            status, headers_, body = r
            return Response(status, headers_, body, url)
        return Response(200, {}, r if isinstance(r, bytes) else str(r).encode(), url)
