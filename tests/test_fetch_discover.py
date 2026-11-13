import gzip
import unittest

from helpers import FIXTURES, FakeClock, FakeTransport, enabled_registry
from pricebot.discover import candidate, discover, parse_sitemap
from pricebot.fetch import DomainNotAllowed, Fetcher
from pricebot.match import PartIndex

ROBOTS_A = b"""User-agent: *
Disallow: /search
Disallow: /account/
Sitemap: https://shop-a.example/sitemap-index.xml
"""


def fetcher(routes, registry=None, deadline=None):
    clock = FakeClock()
    t = FakeTransport(routes, clock)
    return Fetcher(registry or enabled_registry(), transport=t, clock=clock.clock, sleep=clock.sleep, deadline=deadline), t, clock


class FetchConductTests(unittest.TestCase):
    def test_honest_headers_and_no_cookies(self):
        f, t, _ = fetcher({"https://shop-a.example/robots.txt": ROBOTS_A, "https://shop-a.example/p/1": b"<html></html>"})
        f.get("https://shop-a.example/p/1")
        for _, headers, _ in t.requests:
            self.assertIn("RigmarkPriceBot", headers["User-Agent"])
            self.assertIn("github.com/Dreko-Coder/rigmark-data", headers["User-Agent"])
            self.assertNotIn("Mozilla", headers["User-Agent"])
            self.assertNotIn("Cookie", headers)

    def test_set_cookie_is_never_sent_back(self):
        f, t, _ = fetcher({"https://shop-a.example/robots.txt": (200, {"set-cookie": "session=abc"}, ROBOTS_A),
                           "https://shop-a.example/p/1": b"x", "https://shop-a.example/p/2": b"y"})
        f.get("https://shop-a.example/p/1")
        f.get("https://shop-a.example/p/2")
        self.assertTrue(all("Cookie" not in h for _, h, _ in t.requests))

    def test_robots_read_first_and_obeyed(self):
        f, t, _ = fetcher({"https://shop-a.example/robots.txt": ROBOTS_A, "https://shop-a.example/search?q=gpu": b"x"})
        self.assertIsNone(f.get("https://shop-a.example/search?q=gpu"))
        self.assertEqual([u for u, _, _ in t.requests], ["https://shop-a.example/robots.txt"])

    def test_robots_403_reads_nothing_more(self):
        f, t, _ = fetcher({"https://shop-a.example/robots.txt": 403, "https://shop-a.example/p/1": b"x"})
        self.assertIsNone(f.get("https://shop-a.example/p/1"))
        self.assertEqual(len(t.requests), 1)
        self.assertIn("403", f.paused("shop-a.example"))

    def test_missing_robots_allows_but_keeps_delay(self):
        f, t, clock = fetcher({"https://shop-b.example/p/1": b"x", "https://shop-b.example/p/2": b"y"})
        f.get("https://shop-b.example/p/1")
        f.get("https://shop-b.example/p/2")
        times = [at for _, _, at in t.requests]
        self.assertEqual(len(times), 3)
        self.assertTrue(all(b - a >= 10 for a, b in zip(times, times[1:])), times)

    def test_crawl_delay_from_robots_when_slower(self):
        robots = b"User-agent: *\nCrawl-delay: 25\n"
        f, t, _ = fetcher({"https://shop-a.example/robots.txt": robots, "https://shop-a.example/p/1": b"x", "https://shop-a.example/p/2": b"y"})
        f.get("https://shop-a.example/p/1"); f.get("https://shop-a.example/p/2")
        times = [at for _, _, at in t.requests]
        self.assertTrue(all(b - a >= 25 for a, b in zip(times, times[1:])), times)

    def test_crawl_delay_from_robots_never_shortens_ours(self):
        robots = b"User-agent: *\nCrawl-delay: 2\n"
        f, t, _ = fetcher({"https://shop-a.example/robots.txt": robots, "https://shop-a.example/p/1": b"x"})
        f.get("https://shop-a.example/p/1")
        times = [at for _, _, at in t.requests]
        self.assertGreaterEqual(times[1] - times[0], 10)

    def test_403_disables_the_domain(self):
        f, t, _ = fetcher({"https://shop-a.example/robots.txt": ROBOTS_A, "https://shop-a.example/p/1": 403, "https://shop-a.example/p/2": b"y"})
        self.assertIsNone(f.get("https://shop-a.example/p/1"))
        self.assertIsNone(f.get("https://shop-a.example/p/2"))
        self.assertNotIn("https://shop-a.example/p/2", [u for u, _, _ in t.requests])
        self.assertIn("403", f.paused("shop-a.example"))

    def test_429_backs_off_with_retry_after_then_succeeds(self):
        f, t, clock = fetcher({"https://shop-a.example/robots.txt": ROBOTS_A,
                               "https://shop-a.example/p/1": [(429, {"retry-after": "120"}, b""), b"ok"]})
        r = f.get("https://shop-a.example/p/1")
        self.assertEqual(r.body, b"ok")
        times = [at for u, _, at in t.requests if u.endswith("/p/1")]
        self.assertGreaterEqual(times[1] - times[0], 120)

    def test_repeated_429_pauses_the_domain(self):
        f, t, _ = fetcher({"https://shop-a.example/robots.txt": ROBOTS_A, "https://shop-a.example/p/1": 429, "https://shop-a.example/p/2": b"y"})
        self.assertIsNone(f.get("https://shop-a.example/p/1"))
        self.assertIn("429", f.paused("shop-a.example"))
        self.assertIsNone(f.get("https://shop-a.example/p/2"))
        p1 = [u for u, _, _ in t.requests if u.endswith("/p/1")]
        self.assertEqual(len(p1), 3)  # first try + 2 backoff retries, then paused
        self.assertNotIn("https://shop-a.example/p/2", [u for u, _, _ in t.requests])

    def test_only_allow_listed_https_hosts(self):
        f, t, _ = fetcher({})
        for url in ("https://not-listed.example/p", "http://shop-a.example/p", "https://evil.shop-a.example.attacker/p"):
            with self.assertRaises(DomainNotAllowed):
                f.get(url)
        self.assertEqual(t.requests, [])

    def test_request_budget_one_request_an_hour_shop(self):
        f, t, _ = fetcher({"https://shop-slow.example/p/1": b"x"})
        self.assertIsNone(f.get("https://shop-slow.example/p/1"))  # the one request went to robots.txt
        self.assertEqual(len(t.requests), 1)

    def test_deadline_stops_instead_of_waiting(self):
        f, t, clock = fetcher({"https://shop-a.example/robots.txt": ROBOTS_A, "https://shop-a.example/p/1": b"x"}, deadline=1005.0)
        self.assertIsNone(f.get("https://shop-a.example/p/1"))
        self.assertEqual(len(t.requests), 1)
        self.assertIn("deadline", f.paused("shop-a.example"))

    def test_oversized_response_ignored(self):
        f, _, _ = fetcher({"https://shop-a.example/robots.txt": ROBOTS_A, "https://shop-a.example/p/1": b"x" * 300_000})
        self.assertIsNone(f.get("https://shop-a.example/p/1"))


class DiscoverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = PartIndex.load(FIXTURES / "parts")

    def test_parse_index_urlset_and_gzip(self):
        children, pages = parse_sitemap((FIXTURES / "sitemap-index.xml").read_bytes())
        self.assertEqual(len(children), 2)
        self.assertEqual(pages, [])
        children, pages = parse_sitemap(gzip.compress((FIXTURES / "sitemap-products.xml").read_bytes()))
        self.assertEqual((children, len(pages)), ([], 4))

    def test_entities_refused(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><urlset><url><loc>&a;</loc></url></urlset>'
        with self.assertRaises(ValueError):
            parse_sitemap(bomb)

    def test_candidate_filter(self):
        self.assertTrue(candidate("https://shop-a.example/p/fixture-pulse-9070-xt-11348-03-20g", self.index))
        self.assertTrue(candidate("https://shop-a.example/p/radeon-rx-9070-xt-some-other-card", self.index))
        self.assertTrue(candidate("https://shop-a.example/p/ep-850g-netzteil", self.index))
        self.assertFalse(candidate("https://shop-a.example/p/garden-hose", self.index))

    def test_discover_walks_sitemaps_through_the_polite_fetcher(self):
        routes = {"https://shop-a.example/robots.txt": ROBOTS_A,
                  "https://shop-a.example/sitemap-index.xml": (FIXTURES / "sitemap-index.xml").read_bytes(),
                  "https://shop-a.example/sitemap-products.xml.gz": gzip.compress((FIXTURES / "sitemap-products.xml").read_bytes()),
                  "https://shop-a.example/sitemap-blog.xml": (FIXTURES / "sitemap-blog.xml").read_bytes()}
        f, t, _ = fetcher(routes)
        found = discover(f, "shop-a.example", self.index, limit=10)
        self.assertEqual(len(found), 3)
        self.assertNotIn("https://shop-a.example/p/garden-hose", found)
        times = [at for _, _, at in t.requests]
        self.assertTrue(all(b - a >= 10 for a, b in zip(times, times[1:])))


if __name__ == "__main__":
    unittest.main()
