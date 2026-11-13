"""Discovery: sitemaps listed in robots.txt -> product page URLs that look like a part in the dataset.

The URL filter is only a pre-selection. A price is kept only when the page's own structured data carries a part
number or GTIN that matches the dataset (match.py).
"""
from __future__ import annotations

import gzip
import re
import xml.etree.ElementTree as ET
from typing import Iterable

from .match import PartIndex, normalise_mpn

MAX_SITEMAPS_PER_DOMAIN = 50
MAX_SITEMAP_BYTES = 50_000_000  # the sitemap protocol's own cap for an uncompressed file


def parse_sitemap(body: bytes) -> tuple[list[str], list[str]]:
    """Returns (child sitemap URLs, page URLs). Accepts gzip. Refuses DTDs and entities (no XML bombs)."""
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)[: MAX_SITEMAP_BYTES + 1]
    if len(body) > MAX_SITEMAP_BYTES:
        raise ValueError("sitemap larger than 50 MB")
    if b"<!DOCTYPE" in body[:2048].upper() or b"<!ENTITY" in body.upper():
        raise ValueError("sitemap with a DTD or entities refused")
    root = ET.fromstring(body)
    tag = root.tag.rsplit("}", 1)[-1]
    locs = [el.text.strip() for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "loc" and el.text]
    return (locs, []) if tag == "sitemapindex" else ([], locs)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def candidate(url: str, index: PartIndex) -> bool:
    """True if the URL path names a dataset part number or all words of a model name."""
    path = url.lower()
    compact = normalise_mpn(path)
    if any(len(mpn) >= 5 and mpn in compact for mpn in index.mpns()):
        return True
    slug = _slug(path)
    return any(all(w in slug.split("-") for w in words) for words in index.model_words())


def discover(fetcher, host: str, index: PartIndex, limit: int) -> list[str]:
    """Walks the domain's sitemaps through the polite fetcher and returns at most `limit` candidate URLs."""
    queue = list(fetcher.sitemaps(host))
    seen: set[str] = set()
    found: list[str] = []
    while queue and len(seen) < MAX_SITEMAPS_PER_DOMAIN and len(found) < limit:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        resp = fetcher.get(sm)
        if resp is None or resp.status != 200:
            continue
        try:
            children, pages = parse_sitemap(resp.body)
        except (ValueError, ET.ParseError, OSError):
            continue
        queue.extend(c for c in children if c not in seen)
        for page in pages:
            if candidate(page, index) and page not in found:
                found.append(page)
                if len(found) >= limit:
                    break
    return found


def filter_candidates(urls: Iterable[str], index: PartIndex) -> list[str]:
    return [u for u in urls if candidate(u, index)]
