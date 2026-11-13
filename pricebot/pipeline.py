"""Stages wired together: pages -> offers -> matched observations + private evidence + the dry-run report."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from .compute import Observation
from .discover import discover
from .extract import extract
from .match import PartIndex
from .registry import Registry


def _domain_stats() -> dict:
    return {"pages": 0, "noStructuredData": 0, "offers": 0, "matched": 0, "unmatched": Counter(), "wrongCurrency": 0,
            "notOrderableOrNotNew": 0, "noPrice": 0}


def process_page(html: str, url: str, registry: Registry, index: PartIndex, observed: str, stats: dict,
                 evidence: list[dict], observations: list[Observation]) -> None:
    host = (urlsplit(url).hostname or "").lower()
    found = registry.seller_for_domain(host)
    if not found:
        stats["unmatched"]["host not in the registry"] += 1
        return
    seller, _ = found
    market = registry.market(seller.region)
    stats["pages"] += 1
    offers = extract(html)
    if not offers:
        stats["noStructuredData"] += 1
        return
    for offer in offers:
        stats["offers"] += 1
        m = index.match(offer)
        if m.part is None:
            stats["unmatched"][m.by] += 1
            continue
        if offer.price is None:
            stats["noPrice"] += 1
            continue
        if offer.currency != market.currency:
            stats["wrongCurrency"] += 1
            continue
        if not (offer.orderable and offer.new):
            stats["notOrderableOrNotNew"] += 1
        stats["matched"] += 1
        observations.append(Observation(
            part_id=m.part.part_id, category=m.part.category, region=seller.region, seller_id=seller.id,
            seller_group=seller.ownership_group, price=Decimal(offer.price), currency=offer.currency, observed=observed,
            availability=offer.availability, condition=offer.condition))
        evidence.append({"url": url, "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "seller": seller.id, "partId": m.part.part_id, "matchedBy": m.by, "price": offer.price,
                         "currency": offer.currency, "availability": offer.availability, "condition": offer.condition,
                         "structuredData": offer.source, "snippet": offer.snippet})


def dry_run_pages(pages_dir: Path, registry: Registry, index: PartIndex, observed: str) -> dict:
    """Offline: runs extraction and matching over stored pages listed in pages_dir/manifest.json."""
    manifest = json.loads((pages_dir / "manifest.json").read_text(encoding="utf-8"))
    per_domain: dict[str, dict] = {}
    evidence: list[dict] = []
    observations: list[Observation] = []
    for entry in manifest["pages"]:
        host = urlsplit(entry["url"]).hostname
        stats = per_domain.setdefault(host, _domain_stats())
        html = (pages_dir / entry["file"]).read_text(encoding="utf-8")
        process_page(html, entry["url"], registry, index, observed, stats, evidence, observations)
    return {"domains": per_domain, "observations": observations, "evidence": evidence, "fetchEvents": []}


def collect(registry: Registry, index: PartIndex, fetcher, observed: str, pages_per_domain: int) -> dict:
    """Live collection (only reachable when the registry enables it; the fetcher refuses otherwise)."""
    per_domain: dict[str, dict] = {}
    evidence: list[dict] = []
    observations: list[Observation] = []
    for seller in registry.sellers:
        for dom in seller.domains:
            if not registry.domain_allowed(dom.domain):
                continue
            stats = per_domain.setdefault(dom.domain, _domain_stats())
            for url in discover(fetcher, dom.domain, index, pages_per_domain):
                resp = fetcher.get(url)
                if resp is None or resp.status != 200:
                    continue
                process_page(resp.body.decode("utf-8", "replace"), url, registry, index, observed, stats, evidence, observations)
            stats["paused"] = fetcher.paused(dom.domain)
    return {"domains": per_domain, "observations": observations, "evidence": evidence,
            "fetchEvents": [asdict(e) for e in fetcher.events]}


def report(result: dict, registry: Registry) -> dict:
    """The dry-run report: what answered, what had structured data, what matched, coverage per market.
    Seller ids appear here (it is an operations report), never in the price file."""
    groups: dict[tuple[str, str], set[str]] = {}
    for o in result["observations"]:
        groups.setdefault((o.part_id, o.region), set()).add(o.seller_group)
    coverage: dict[str, dict] = {}
    for m in registry.markets:
        counts = [len(g) for (p, r), g in groups.items() if r == m.region]
        coverage[m.region] = {"partsSeen": len(counts), "partsWith2Sellers": sum(c >= 2 for c in counts),
                              "partsWith3Sellers": sum(c >= 3 for c in counts)}
    domains = {h: {**s, "unmatched": dict(s["unmatched"])} for h, s in result["domains"].items()}
    return {"collectionEnabled": registry.collection_enabled, "domains": domains, "coverage": coverage,
            "observations": len(result["observations"]), "fetchEvents": len(result["fetchEvents"])}


def report_markdown(rep: dict) -> str:
    lines = ["# Pricing bot dry-run report", "", f"Collection enabled: {rep['collectionEnabled']}", "",
             "| Domain | Pages | No structured data | Offers | Matched | Wrong currency | Paused |", "|---|---|---|---|---|---|---|"]
    for host, s in sorted(rep["domains"].items()):
        lines.append(f"| {host} | {s['pages']} | {s['noStructuredData']} | {s['offers']} | {s['matched']} | {s['wrongCurrency']} | {s.get('paused') or ''} |")
    lines += ["", "| Market | Parts seen | With 2+ sellers (index) | With 3+ sellers (band) |", "|---|---|---|---|"]
    for region, c in rep["coverage"].items():
        lines.append(f"| {region} | {c['partsSeen']} | {c['partsWith2Sellers']} | {c['partsWith3Sellers']} |")
    return "\n".join(lines) + "\n"


def observations_to_jsonl(observations: list[Observation]) -> str:
    return "".join(json.dumps({**asdict(o), "price": str(o.price)}, sort_keys=True) + "\n" for o in observations)


def observations_from_jsonl(text: str) -> list[Observation]:
    out = []
    for line in text.splitlines():
        if line.strip():
            d = json.loads(line)
            d["price"] = Decimal(d["price"])
            out.append(Observation(**d))
    return out
