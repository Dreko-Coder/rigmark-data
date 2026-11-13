"""Layer 2 (regional index) and layer 3 (market band) from verified observations, with the quality gates.

Neutral by construction: every published number is a median and range across independent sellers (sellers under
one owner count once), never one seller's price. Rules (Proposed unless marked; docs/work/MY-RIG-SPEC section 5):
- Kept: currency is the market's, item new, in stock or orderable, observed within STALE_DAYS of the data date.
- One price per seller group and part: the group's lowest valid offer (what a buyer would pay there).
- Outlier: with three or more sellers, a price more than 35% from the median of the others is dropped.
- Band (layer 3): at least 3 independent sellers left. Median, min, max.
- Index (layer 2): street price over the app's launch-price estimate, per region and category, from parts that have
  at least 2 independent sellers; at least 2 sellers across the index and MIN_INDEX_PARTS parts. Labelled seeded
  when any price in it came from the hand seed.
- Movement gate: a band median or index multiplier that moved more than 25% since the last file is held (the old
  value stays) and reported.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from statistics import median as _median

from .registry import Market

MIN_BAND_SELLERS = 3
MIN_INDEX_SELLERS = 2
MIN_INDEX_PARTS = 2
OUTLIER_FRACTION = Decimal("0.35")
MOVEMENT_FRACTION = Decimal("0.25")
STALE_DAYS = 7
BEFORE_TAX_CURRENCIES = ("USD", "CAD")
TAX_VARIES_INSIDE = ("US", "CA")
CENT = Decimal("0.01")


@dataclass(frozen=True)
class Observation:
    part_id: str
    category: str
    region: str
    seller_id: str
    seller_group: str
    price: Decimal
    currency: str
    observed: str  # ISO date
    availability: str | None = "instock"
    condition: str | None = "new"
    seeded: bool = False


@dataclass
class Computed:
    bands: list[dict] = field(default_factory=list)
    indices: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)  # observation-level drops with reasons
    withheld: list[dict] = field(default_factory=list)  # bands/indices not published, with reasons
    held: list[dict] = field(default_factory=list)  # movement gate: previous value kept


def money(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def median(values: list[Decimal]) -> Decimal:
    return Decimal(_median(sorted(values)))


def outliers(prices: dict[str, Decimal]) -> set[str]:
    """Groups whose price is more than 35% from the median of the others (only with 3 or more groups)."""
    if len(prices) < 3:
        return set()
    out = set()
    for g, p in prices.items():
        others = median([q for h, q in prices.items() if h != g])
        if others > 0 and abs(p - others) / others > OUTLIER_FRACTION:
            out.add(g)
    return out


def fx_convert(amount: Decimal, frm: str, to: str, fx_base: str, rates: dict[str, Decimal]) -> Decimal | None:
    """Same rule as the app (BaselinePrice.Convert): amount * rate(to) / rate(from), rates per one unit of base."""
    if frm == to:
        return amount
    rf = Decimal(1) if frm == fx_base else rates.get(frm)
    rt = Decimal(1) if to == fx_base else rates.get(to)
    if not rf or not rt:
        return None
    return amount * rt / rf


def launch_estimate(msrp: dict | None, market: Market, fx_base: str, rates: dict[str, Decimal]) -> Decimal | None:
    """The app's launch-price estimate for a part in a market (BaselinePrice.LaunchCore), unrounded."""
    if not msrp or Decimal(str(msrp.get("amount", 0))) <= 0:
        return None
    cur = msrp["currency"]
    before_tax = cur in BEFORE_TAX_CURRENCIES
    if not before_tax and market.region in TAX_VARIES_INSIDE:
        return None
    amount = fx_convert(Decimal(str(msrp["amount"])), cur, market.currency, fx_base, rates)
    if amount is None or amount <= 0:
        return None
    if before_tax and market.prices_include_tax:
        amount *= 1 + Decimal(str(market.tax_rate_percent)) / 100
    return amount


def _fresh(observed: str, data_date: date) -> bool:
    try:
        d = date.fromisoformat(observed)
    except ValueError:
        return False
    return data_date - timedelta(days=STALE_DAYS) <= d <= data_date


def compute(
    observations: list[Observation],
    markets: list[Market],
    parts: dict,  # part_id -> PartRef (category, msrp)
    data_date: date,
    fx_base: str,
    rates: dict[str, Decimal],
    previous: dict | None = None,
) -> Computed:
    res = Computed()
    by_region = {m.region: m for m in markets}
    dd = data_date.isoformat()

    # 1. keep only valid observations; one (lowest) price per part, region and seller group
    best: dict[tuple[str, str], dict[str, Observation]] = {}
    for o in observations:
        m = by_region.get(o.region)
        reason = (
            "unknown market" if m is None
            else "unknown part" if o.part_id not in parts
            else "currency is not the market's" if o.currency != m.currency
            else "not new" if o.condition not in (None, "new")
            else "not in stock or orderable" if o.availability not in (None, "instock", "onlineonly", "instoreonly", "limitedavailability", "preorder", "presale", "backorder")
            else "stale" if not _fresh(o.observed, data_date)
            else "price not positive" if o.price <= 0
            else None
        )
        if reason:
            res.dropped.append({"partId": o.part_id, "region": o.region, "seller": o.seller_id, "reason": reason})
            continue
        slot = best.setdefault((o.part_id, o.region), {})
        cur = slot.get(o.seller_group)
        if cur is None or o.price < cur.price:
            slot[o.seller_group] = o

    # 2. outliers, then bands
    kept: dict[tuple[str, str], dict[str, Observation]] = {}
    for key, groups in best.items():
        bad = outliers({g: ob.price for g, ob in groups.items()})
        for g in bad:
            res.dropped.append({"partId": key[0], "region": key[1], "seller": groups[g].seller_id, "reason": "outlier (more than 35% from the median of the others)"})
        kept[key] = {g: ob for g, ob in groups.items() if g not in bad}

    for (part_id, region), groups in sorted(kept.items()):
        live = {g: ob for g, ob in groups.items() if not ob.seeded}
        if len(live) < MIN_BAND_SELLERS:
            res.withheld.append({"kind": "band", "partId": part_id, "region": region, "reason": f"too few sellers ({len(live)} of {MIN_BAND_SELLERS})"})
            continue
        prices = [ob.price for ob in live.values()]
        res.bands.append({
            "partId": part_id, "region": region, "currency": by_region[region].currency,
            "median": money(median(prices)), "min": money(min(prices)), "max": money(max(prices)),
            "sellers": len(live), "dataDate": dd,
        })

    # 3. indices: street price over the launch-price estimate, per region and category
    ratios: dict[tuple[str, str], list[tuple[Decimal, set[str], int, bool]]] = {}
    for (part_id, region), groups in kept.items():
        if len(groups) < MIN_INDEX_SELLERS:
            continue
        est = launch_estimate(parts[part_id].msrp, by_region[region], fx_base, rates)
        if est is None:
            continue
        street = median([ob.price for ob in groups.values()])
        ratios.setdefault((region, parts[part_id].category), []).append(
            (street / est, set(groups), len(groups), any(ob.seeded for ob in groups.values())))
    for (region, category), rows in sorted(ratios.items()):
        sellers = set().union(*(r[1] for r in rows))
        values = [r[0] for r in rows]
        why = (f"too few parts ({len(rows)} of {MIN_INDEX_PARTS})" if len(rows) < MIN_INDEX_PARTS
               else f"too few sellers ({len(sellers)} of {MIN_INDEX_SELLERS})" if len(sellers) < MIN_INDEX_SELLERS else None)
        mult = median(values).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        low = min(values).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        high = max(values).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        if why is None and not (Decimal("0.3") <= mult <= Decimal("3")):
            why = f"multiplier {mult} is outside 0.3 to 3"
        if why:
            res.withheld.append({"kind": "index", "region": region, "category": category, "reason": why})
            continue
        res.indices.append({
            "region": region, "category": category, "multiplier": mult, "low": low, "high": high,
            "sellers": len(sellers), "sample": sum(r[2] for r in rows), "dataDate": dd, "seeded": any(r[3] for r in rows),
        })

    if previous:
        _movement_gate(res, previous)
    return res


def _movement_gate(res: Computed, previous: dict) -> None:
    old_bands = {(b["partId"], b["region"]): b for b in previous.get("bands", [])}
    old_idx = {(i["region"], i["category"]): i for i in previous.get("indices", [])}
    for i, b in enumerate(res.bands):
        old = old_bands.get((b["partId"], b["region"]))
        if old and _moved(Decimal(str(old["median"])), b["median"]):
            res.held.append({"kind": "band", "partId": b["partId"], "region": b["region"], "old": str(old["median"]), "new": str(b["median"])})
            res.bands[i] = old
    for i, x in enumerate(res.indices):
        old = old_idx.get((x["region"], x["category"]))
        if old and _moved(Decimal(str(old["multiplier"])), x["multiplier"]):
            res.held.append({"kind": "index", "region": x["region"], "category": x["category"], "old": str(old["multiplier"]), "new": str(x["multiplier"])})
            res.indices[i] = old


def _moved(old: Decimal, new: Decimal) -> bool:
    return old > 0 and abs(new - old) / old > MOVEMENT_FRACTION
