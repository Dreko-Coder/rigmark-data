"""Polite fetching: allow-list, robots.txt, per-domain delay, honest user agent, no cookies, no evasion.

Conduct (docs: README "Bot conduct"):
- Only hosts that are enabled in sources/registry.json, and only while "collectionEnabled" is true.
- One plain request at a time per domain, at least the registry delay apart (10 s minimum, longer where robots.txt
  or the shop says so). robots.txt is read first and obeyed; it counts as a request.
- The user agent names the bot and this repository. No cookies are stored or sent, no login, no proxy, no
  browser imitation, no CAPTCHA solving, no retry from another address.
- 429: wait (Retry-After when given, else exponential), at most a few times; repeated 429s pause the domain.
- 403 (or 401) disables the domain for the run and is reported. A blocked shop is never worked around.
- A redirect to another host is not followed.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Callable, Protocol
from urllib.parse import urlsplit

from .registry import Registry, require_collection_enabled

ROBOTS_TOKEN = "RigmarkPriceBot"


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str


class Transport(Protocol):
    def __call__(self, url: str, headers: dict[str, str], timeout: float, max_bytes: int) -> Response: ...


class DomainNotAllowed(Exception):
    pass


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).hostname != urlsplit(req.full_url).hostname or urlsplit(newurl).scheme != "https":
            raise urllib.error.HTTPError(req.full_url, 310, "cross-host redirect refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UrllibTransport:
    """The only code that opens a network connection. Refuses to exist while collection is disabled."""

    def __init__(self, registry: Registry):
        require_collection_enabled(registry)
        # build_opener without HTTPCookieProcessor: no cookie jar, so no cookie is ever stored or sent.
        self._opener = urllib.request.build_opener(_SameHostRedirects())

    def __call__(self, url: str, headers: dict[str, str], timeout: float, max_bytes: int) -> Response:
        if urlsplit(url).scheme != "https":
            raise DomainNotAllowed(f"only https is fetched: {url}")
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self._opener.open(req, timeout=timeout) as r:
                body = r.read(max_bytes + 1)
                return Response(r.status, {k.lower(): v for k, v in r.headers.items()}, body, r.url)
        except urllib.error.HTTPError as e:
            return Response(e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, b"", url)


@dataclass
class DomainState:
    last_request: float | None = None
    requests: int = 0
    robots: urllib.robotparser.RobotFileParser | None = None
    robots_delay: float = 0.0
    paused: str | None = None  # reason, when the domain is paused or disabled for this run
    count_429: int = 0
    sitemaps: list[str] = field(default_factory=list)


@dataclass
class FetchEvent:
    url: str
    status: int | str
    note: str = ""


class Fetcher:
    def __init__(
        self,
        registry: Registry,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        deadline: float | None = None,
    ):
        require_collection_enabled(registry)
        self.registry = registry
        self.transport = transport if transport is not None else UrllibTransport(registry)
        self.clock, self.sleep, self.deadline = clock, sleep, deadline
        d = registry.defaults
        self.max_bytes = int(d.get("maxResponseBytes", 5_000_000))
        self.timeout = float(d.get("requestTimeoutSeconds", 30))
        self.max_retries = int(d.get("maxBackoffRetries", 2))
        self.pause_after_429 = int(d.get("pauseDomainAfter429", 3))
        self.max_pages = int(d.get("maxPagesPerDomainPerRun", 500))
        self.state: dict[str, DomainState] = {}
        self.events: list[FetchEvent] = []

    # -- public -------------------------------------------------------------------------------------------------
    def headers(self) -> dict[str, str]:
        return {"User-Agent": self.registry.user_agent, "Accept": "text/html,application/xml;q=0.9,*/*;q=0.5",
                "Accept-Encoding": "identity"}

    def paused(self, host: str) -> str | None:
        return self._state(host).paused

    def sitemaps(self, host: str) -> list[str]:
        st = self._state(host)
        if st.robots is None:
            self._load_robots(host)
        return list(st.sitemaps)

    def get(self, url: str) -> Response | None:
        """Fetch a page if the allow-list, robots.txt and the domain's state allow it, else None."""
        host = self._host(url)
        st = self._state(host)
        if st.robots is None:
            self._load_robots(host)
        if st.paused:
            self._log(url, "skipped", f"domain paused: {st.paused}")
            return None
        if not st.robots.can_fetch(ROBOTS_TOKEN, url):
            self._log(url, "skipped", "disallowed by robots.txt")
            return None
        return self._request(host, url)

    # -- internals ----------------------------------------------------------------------------------------------
    def _host(self, url: str) -> str:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not self.registry.domain_allowed(host):
            raise DomainNotAllowed(f"{host or url} is not an enabled domain in the allow-list")
        return host

    def _state(self, host: str) -> DomainState:
        return self.state.setdefault(host, DomainState())

    def _delay(self, host: str) -> float:
        _, dom = self.registry.seller_for_domain(host)
        return max(dom.crawl_delay, self._state(host).robots_delay)

    def _load_robots(self, host: str) -> None:
        st = self._state(host)
        rp = urllib.robotparser.RobotFileParser()
        resp = self._request(host, f"https://{host}/robots.txt", robots=True)
        if resp is None:
            rp.disallow_all = True
            st.paused = st.paused or "robots.txt could not be read"
        elif resp.status == 200:
            rp.parse(resp.body.decode("utf-8", "replace").splitlines())
            delay = rp.crawl_delay(ROBOTS_TOKEN)
            st.robots_delay = float(delay) if delay else 0.0
            st.sitemaps = list(rp.site_maps() or [])
        elif resp.status in (404, 410):
            rp.allow_all = True  # no robots.txt: everything allowed, our own delay still applies
        else:
            rp.disallow_all = True  # 401/403/5xx or anything else: read nothing from this domain this run
            if not st.paused:
                st.paused = f"robots.txt answered {resp.status}"
        st.robots = rp

    def _request(self, host: str, url: str, robots: bool = False) -> Response | None:
        st = self._state(host)
        _, dom = self.registry.seller_for_domain(host)
        limit = dom.max_requests_per_run if dom.max_requests_per_run is not None else self.max_pages + 1
        retries = 0
        while True:
            if st.paused and not robots:
                return None
            if st.requests >= limit:
                self._log(url, "skipped", "request budget for this run used up")
                if robots:
                    return None
                st.paused = st.paused or "request budget for this run used up"
                return None
            if not self._wait_turn(host, url):
                return None
            st.last_request = self.clock()
            st.requests += 1
            try:
                resp = self.transport(url, self.headers(), self.timeout, self.max_bytes)
            except DomainNotAllowed:
                raise
            except Exception as e:  # network error: reported, no tricks
                self._log(url, "error", type(e).__name__)
                return None
            if len(resp.body) > self.max_bytes:
                self._log(url, resp.status, "response larger than the size cap; ignored")
                return None
            if resp.status in (401, 403):
                st.paused = f"answered {resp.status}; domain disabled for this run and reported"
                self._log(url, resp.status, st.paused)
                return None
            if resp.status == 429:
                st.count_429 += 1
                if st.count_429 >= self.pause_after_429 or retries >= self.max_retries:
                    st.paused = f"answered 429 {st.count_429} time(s); paused and reported"
                    self._log(url, 429, st.paused)
                    return None
                wait = self._retry_after(resp.headers.get("retry-after"), retries)
                self._log(url, 429, f"backing off {wait:.0f} s")
                st.last_request = self.clock() + wait - self._delay(host)  # next turn no sooner than now + wait
                retries += 1
                continue
            self._log(url, resp.status)
            return resp

    @staticmethod
    def _retry_after(value: str | None, retries: int) -> float:
        """Retry-After in seconds when the shop gives one (never under 10 s), else 60 s doubling."""
        if value and value.strip().isdigit():
            return max(float(value.strip()), 10.0)
        return 60.0 * (2 ** retries)

    def _wait_turn(self, host: str, url: str) -> bool:
        st = self._state(host)
        if st.last_request is None:
            return True
        due = st.last_request + self._delay(host)
        now = self.clock()
        if self.deadline is not None and due > self.deadline:
            st.paused = st.paused or "next allowed request falls after the run deadline"
            self._log(url, "skipped", st.paused)
            return False
        if due > now:
            self.sleep(due - now)
        return True

    def _log(self, url: str, status, note: str = "") -> None:
        self.events.append(FetchEvent(url, status, note))
