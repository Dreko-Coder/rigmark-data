"""Match an extracted offer to a dataset part by GTIN or part number (data/rig parts files, schema 1).

Exact identifiers only. A GTIN must pass its check digit. A part number must belong to exactly one dataset part,
and when the page names a brand it must agree with the brand of the SKU or part the number belongs to.
An offer that matches nothing, or matches two parts, is counted and dropped: never guessed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .extract import Offer

CATEGORIES = ("case", "cooler", "cpu", "gpu", "motherboard", "psu", "ram", "storage")


def normalise_mpn(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(text).upper())


def gtin_valid(digits: str) -> bool:
    if not re.fullmatch(r"\d{8}|\d{12,14}", digits):
        return False
    body, check = digits[:-1], int(digits[-1])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def normalise_gtin(text: str) -> str | None:
    digits = re.sub(r"\D", "", str(text))
    return digits.zfill(14) if gtin_valid(digits) else None


@dataclass(frozen=True)
class PartRef:
    part_id: str
    category: str
    brands: frozenset[str]  # lower case: the part's brand and the brand of the SKU the identifier came from
    msrp: dict | None = field(default=None, compare=False, hash=False)


@dataclass(frozen=True)
class Match:
    part: PartRef | None
    by: str  # "gtin", "mpn" or a reason it did not match


class PartIndex:
    def __init__(self, parts_by_category: dict[str, list[dict]]):
        self._gtin: dict[str, set[PartRef]] = {}
        self._mpn: dict[str, set[PartRef]] = {}
        self._words: list[tuple[str, ...]] = []
        self.parts: dict[str, PartRef] = {}
        for category, parts in parts_by_category.items():
            for p in parts:
                if p.get("recognitionOnly"):
                    continue
                self._words.append(tuple(w for w in re.sub(r"[^a-z0-9]+", "-", p["model"].lower()).split("-") if w))
                base = PartRef(p["id"], category, frozenset({p["brand"].lower()}), p.get("msrp"))
                self.parts[p["id"]] = base
                self._add(base, p.get("partNumbers", []), p.get("gtins", []))
                for sku in p.get("skus", []):
                    ref = PartRef(p["id"], category, frozenset({p["brand"].lower(), sku["brand"].lower()}), p.get("msrp"))
                    self._add(ref, sku.get("partNumbers", []), sku.get("gtins", []))

    def _add(self, ref: PartRef, mpns, gtins) -> None:
        for m in mpns:
            n = normalise_mpn(m)
            if len(n) >= 4:
                self._mpn.setdefault(n, set()).add(ref)
        for g in gtins:
            n = normalise_gtin(g)
            if n:
                self._gtin.setdefault(n, set()).add(ref)

    @classmethod
    def load(cls, parts_dir: Path | str) -> "PartIndex":
        d = Path(parts_dir)
        data: dict[str, list[dict]] = {}
        for category in CATEGORIES:
            f = d / f"{category}.json"
            if f.exists():
                doc = json.loads(f.read_text(encoding="utf-8"))
                if doc.get("schemaVersion") != 1 or doc.get("category") != category:
                    raise ValueError(f"{f}: not a schema 1 parts file for {category}")
                data[category] = doc["parts"]
        return cls(data)

    def mpns(self) -> list[str]:
        return list(self._mpn)

    def model_words(self) -> list[tuple[str, ...]]:
        return [w for w in self._words if len(w) >= 2]

    def match(self, offer: Offer) -> Match:
        for g in offer.gtins:
            n = normalise_gtin(g)
            if n and n in self._gtin:
                ids = {r.part_id for r in self._gtin[n]}
                if len(ids) == 1:
                    return Match(next(iter(self._gtin[n])), "gtin")
                return Match(None, "gtin belongs to more than one part")
        if offer.mpn:
            n = normalise_mpn(offer.mpn)
            refs = self._mpn.get(n)
            if refs:
                ids = {r.part_id for r in refs}
                if len(ids) > 1:
                    return Match(None, "part number belongs to more than one part")
                ref = next(iter(refs))
                brand = (offer.brand or "").lower().strip()
                if brand and not any(b in brand or brand in b for b in set().union(*(r.brands for r in refs))):
                    return Match(None, "part number matched but the brand differs")
                return Match(ref, "mpn")
        return Match(None, "no matching gtin or part number")
