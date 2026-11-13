"""Source registry (sources/registry.json) and the collection guard.

The registry is the public allow-list. Nothing is fetched from a domain that is not in it, not enabled in it, or
while the top-level "collectionEnabled" is false (which is how it ships).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY = ROOT / "sources" / "registry.json"

MIN_CRAWL_DELAY_SECONDS = 10


class CollectionDisabled(Exception):
    """Raised when anything would reach the network, sign or publish while collection is disabled."""


@dataclass(frozen=True)
class Domain:
    domain: str
    enabled: bool
    crawl_delay: float
    max_requests_per_run: int | None
    robots_notes: str
    structured_data: str


@dataclass(frozen=True)
class Seller:
    id: str
    name: str
    ownership_group: str
    region: str
    status: str
    enabled: bool
    domains: tuple[Domain, ...]


@dataclass(frozen=True)
class Market:
    region: str
    currency: str
    tax_rate_percent: float
    prices_include_tax: bool


@dataclass(frozen=True)
class Registry:
    collection_enabled: bool
    user_agent: str
    defaults: dict
    markets: tuple[Market, ...]
    sellers: tuple[Seller, ...]
    path: Path = field(default=DEFAULT_REGISTRY)

    def market(self, region: str) -> Market | None:
        return next((m for m in self.markets if m.region == region), None)

    def seller_for_domain(self, host: str) -> tuple[Seller, Domain] | None:
        host = host.lower()
        for s in self.sellers:
            for d in s.domains:
                if d.domain.lower() == host:
                    return s, d
        return None

    def domain_allowed(self, host: str) -> bool:
        """True only if collection is on and both the seller and the domain entry are enabled."""
        found = self.seller_for_domain(host)
        return bool(self.collection_enabled and found and found[0].enabled and found[1].enabled)


def load(path: Path | str | None = None) -> Registry:
    p = Path(path) if path else DEFAULT_REGISTRY
    raw = json.loads(p.read_text(encoding="utf-8"))
    problems = check(raw)
    if problems:
        raise ValueError(f"registry {p} is not valid: " + "; ".join(problems))
    defaults = raw.get("defaults", {})
    floor = max(MIN_CRAWL_DELAY_SECONDS, float(defaults.get("minCrawlDelaySeconds", MIN_CRAWL_DELAY_SECONDS)))
    sellers = tuple(
        Seller(
            id=s["id"], name=s["name"], ownership_group=s["ownershipGroup"], region=s["region"],
            status=s["status"], enabled=bool(s["enabled"]),
            domains=tuple(
                Domain(
                    domain=d["domain"], enabled=bool(d["enabled"]),
                    crawl_delay=max(floor, float(d.get("crawlDelaySeconds", floor))),
                    max_requests_per_run=d.get("maxRequestsPerRun"),
                    robots_notes=d.get("robotsNotes", ""), structured_data=d.get("structuredData", ""),
                )
                for d in s["domains"]
            ),
        )
        for s in raw["sellers"]
    )
    markets = tuple(
        Market(m["region"], m["currency"], float(m["taxRatePercent"]), bool(m["pricesIncludeTax"])) for m in raw["markets"]
    )
    return Registry(raw["collectionEnabled"] is True, raw["userAgent"], defaults, markets, sellers, p)


def check(raw: dict) -> list[str]:
    """Structural rules for the registry. Kept strict: the registry is what a reviewer approves."""
    problems: list[str] = []
    if not isinstance(raw.get("collectionEnabled"), bool):
        problems.append("collectionEnabled must be true or false")
    ua = raw.get("userAgent", "")
    if "github.com/Dreko-Coder/rigmark-data" not in ua or "Mozilla" in ua:
        problems.append("userAgent must name the bot and the repository and must not imitate a browser")
    regions = {m.get("region") for m in raw.get("markets", [])}
    seen_domains: set[str] = set()
    for s in raw.get("sellers", []):
        sid = s.get("id", "?")
        for key in ("id", "name", "ownershipGroup", "region", "status", "enabled", "domains"):
            if key not in s:
                problems.append(f"seller {sid}: {key} missing")
        if s.get("region") not in regions:
            problems.append(f"seller {sid}: region {s.get('region')} has no market")
        for d in s.get("domains", []):
            name = d.get("domain", "")
            if name in seen_domains:
                problems.append(f"domain {name} listed twice")
            seen_domains.add(name)
            if float(d.get("crawlDelaySeconds", 0)) < MIN_CRAWL_DELAY_SECONDS:
                problems.append(f"domain {name}: crawlDelaySeconds below {MIN_CRAWL_DELAY_SECONDS}")
            if d.get("enabled") and not s.get("enabled"):
                problems.append(f"domain {name}: enabled while its seller is not")
    return problems


def require_collection_enabled(registry: Registry) -> None:
    if not registry.collection_enabled:
        raise CollectionDisabled(
            "Collection is disabled (sources/registry.json has \"collectionEnabled\": false). "
            "Nothing was fetched, signed or published."
        )


def write_github_output(name: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
