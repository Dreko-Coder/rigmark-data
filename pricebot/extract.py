"""Read schema.org Product/Offer data from a page: JSON-LD first, then microdata. Nothing else on the page is read.

A page without structured data gives no offers (counted and skipped by the caller).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

IN_STOCK = {"instock", "onlineonly", "instoreonly", "limitedavailability", "preorder", "presale", "backorder"}
NOT_ORDERABLE = {"outofstock", "soldout", "discontinued"}


@dataclass
class Offer:
    name: str | None
    brand: str | None
    mpn: str | None
    gtins: list[str]
    price: str | None  # decimal text, normalised ("1299.00")
    currency: str | None
    availability: str | None  # schema.org term, lower case, no URL ("instock")
    condition: str | None  # "new", "used", "refurbished", "damaged" or None
    source: str  # "json-ld" or "microdata"
    snippet: str  # the structured-data text the offer came from (evidence; kept private)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def orderable(self) -> bool:
        return self.availability is None or self.availability in IN_STOCK

    @property
    def new(self) -> bool:
        return self.condition in (None, "new")


def parse_price(value) -> str | None:
    """'549.99', '549,99', '1.299,00', '1,299.00', 549.99 -> '549.99' / '1299.00'. None if not a price."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        text = repr(value)
    else:
        text = re.sub(r"[^\d.,]", "", str(value))
    if not text or not re.search(r"\d", text):
        return None
    if "," in text and "." in text:
        decimal_sep = "," if text.rfind(",") > text.rfind(".") else "."
        thousands = "." if decimal_sep == "," else ","
        text = text.replace(thousands, "").replace(decimal_sep, ".")
    elif "," in text:
        tail = text.rpartition(",")[2]
        # one comma not followed by exactly three digits is a decimal comma ("549,99"); otherwise thousands
        text = text.replace(",", ".") if text.count(",") == 1 and len(tail) != 3 else text.replace(",", "")
    elif text.count(".") > 1:
        text = text.replace(".", "", text.count(".") - 1)
    try:
        d = Decimal(text)
    except InvalidOperation:
        return None
    if d <= 0 or d > Decimal("1000000"):
        return None
    return str(d.quantize(Decimal("0.01")))


def _term(value) -> str | None:
    if not value:
        return None
    if isinstance(value, dict):
        value = value.get("@id") or value.get("name")
    text = str(value).strip().rsplit("/", 1)[-1].rsplit("#", 1)[-1].lower()
    return text or None


def _condition(value) -> str | None:
    t = _term(value)
    if t is None:
        return None
    for word in ("new", "used", "refurbished", "damaged"):
        if t.startswith(word):
            return word
    return t


def _types(node: dict) -> set[str]:
    t = node.get("@type", [])
    t = t if isinstance(t, list) else [t]
    return {str(x).rsplit("/", 1)[-1].lower() for x in t}


def _text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("name") or value.get("@value")
    if isinstance(value, list):
        value = value[0] if value else None
    return str(value).strip() if value not in (None, "") else None


def _gtins(node: dict) -> list[str]:
    out: list[str] = []
    for key in ("gtin", "gtin8", "gtin12", "gtin13", "gtin14"):
        v = node.get(key)
        for item in v if isinstance(v, list) else [v]:
            digits = re.sub(r"\D", "", str(item or ""))
            if 8 <= len(digits) <= 14 and digits not in out:
                out.append(digits)
    return out


# -- JSON-LD ------------------------------------------------------------------------------------------------------
class _ScriptCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._in = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and (dict(attrs).get("type") or "").lower().strip() == "application/ld+json":
            self._in, self._buf = True, []

    def handle_endtag(self, tag):
        if tag == "script" and self._in:
            self.blocks.append("".join(self._buf))
            self._in = False

    def handle_data(self, data):
        if self._in:
            self._buf.append(data)


def _walk(node):
    if isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "mainEntity", "itemListElement", "item"):
            if key in node:
                yield from _walk(node[key])


def _offers_from_product(product: dict, snippet: str, source: str) -> list[Offer]:
    offers_raw = product.get("offers")
    items = offers_raw if isinstance(offers_raw, list) else [offers_raw] if offers_raw else []
    flat: list[dict] = []
    for o in items:
        if isinstance(o, dict) and "aggregateoffer" in _types(o) and isinstance(o.get("offers"), list):
            flat.extend(x for x in o["offers"] if isinstance(x, dict))
        elif isinstance(o, dict):
            flat.append(o)
    result = []
    for o in flat:
        spec = o.get("priceSpecification") if isinstance(o.get("priceSpecification"), dict) else {}
        price = o.get("price") if o.get("price") is not None else spec.get("price")
        if price is None and "aggregateoffer" in _types(o):
            price = o.get("lowPrice")  # a shop's own aggregate (several variants); lowest variant
        currency = o.get("priceCurrency") or spec.get("priceCurrency")
        result.append(Offer(
            name=_text(product.get("name")), brand=_text(product.get("brand")),
            mpn=_text(product.get("mpn") or o.get("mpn")), gtins=_gtins(product) or _gtins(o),
            price=parse_price(price), currency=(str(currency).strip().upper() if currency else None),
            availability=_term(o.get("availability")), condition=_condition(o.get("itemCondition") or product.get("itemCondition")),
            source=source, snippet=snippet[:4000],
        ))
    return result


def from_json_ld(html: str) -> list[Offer]:
    c = _ScriptCollector()
    c.feed(html)
    offers: list[Offer] = []
    for block in c.blocks:
        try:
            data = json.loads(block.strip())
        except (json.JSONDecodeError, ValueError):
            continue  # a broken block is ignored, never guessed at
        for node in _walk(data):
            if "product" in _types(node) or "productmodel" in _types(node):
                offers.extend(_offers_from_product(node, block.strip(), "json-ld"))
    return offers


# -- microdata ----------------------------------------------------------------------------------------------------
class _Item:
    def __init__(self, itemtype: str):
        self.type = itemtype.rsplit("/", 1)[-1].lower()
        self.props: dict[str, list] = {}


class _MicrodataParser(HTMLParser):
    VALUE_ATTRS = {"meta": "content", "link": "href", "a": "href", "img": "src", "data": "value", "time": "datetime", "meter": "value"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, _Item | None, str | None]] = []  # (tag, item opened by tag, pending text prop)
        self.items: list[_Item] = []
        self.texts: list[tuple[_Item, str, list[str], int]] = []
        self.depth = 0

    def _current(self) -> _Item | None:
        for _, item, _ in reversed(self.stack):
            if item is not None:
                return item
        return None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        parent = self._current()
        new_item = _Item(a.get("itemtype", "")) if "itemscope" in a else None
        prop = a.get("itemprop")
        pending = None
        if prop and parent is not None:
            for name in prop.split():
                if new_item is not None:
                    parent.props.setdefault(name, []).append(new_item)
                elif "content" in a:
                    parent.props.setdefault(name, []).append(a["content"])
                elif tag in self.VALUE_ATTRS and self.VALUE_ATTRS[tag] in a:
                    parent.props.setdefault(name, []).append(a[self.VALUE_ATTRS[tag]])
                else:
                    pending = name
        if new_item is not None:
            self.items.append(new_item)
        if tag in ("meta", "link", "img", "br", "hr", "input"):
            return  # void elements never close
        self.stack.append((tag, new_item, pending))
        if pending:
            self.texts.append((parent, pending, [], len(self.stack)))

    def handle_endtag(self, tag):
        while self.stack:
            t, _, pending = self.stack.pop()
            if pending:
                for i in range(len(self.texts) - 1, -1, -1):
                    item, name, buf, depth = self.texts[i]
                    if depth == len(self.stack) + 1:
                        item.props.setdefault(name, []).append(" ".join("".join(buf).split()))
                        del self.texts[i]
                        break
            if t == tag:
                break

    def handle_data(self, data):
        for _, _, buf, _ in self.texts:
            buf.append(data)


def _first(item: _Item, *names):
    for n in names:
        for v in item.props.get(n, []):
            if isinstance(v, _Item):
                inner = _first(v, "name")
                if inner:
                    return inner
            elif str(v).strip():
                return str(v).strip()
    return None


def from_microdata(html: str) -> list[Offer]:
    p = _MicrodataParser()
    p.feed(html)
    p.close()
    offers: list[Offer] = []
    for item in p.items:
        if item.type != "product":
            continue
        gt = []
        for key in ("gtin", "gtin8", "gtin12", "gtin13", "gtin14"):
            for v in item.props.get(key, []):
                d = re.sub(r"\D", "", str(v))
                if 8 <= len(d) <= 14 and d not in gt:
                    gt.append(d)
        offer_items = [v for v in item.props.get("offers", []) if isinstance(v, _Item)]
        for o in offer_items:
            snippet = json.dumps({"product": {k: [x if not isinstance(x, _Item) else x.type for x in v] for k, v in item.props.items()},
                                  "offer": {k: [x if not isinstance(x, _Item) else x.type for x in v] for k, v in o.props.items()}},
                                 ensure_ascii=False)
            offers.append(Offer(
                name=_first(item, "name"), brand=_first(item, "brand"), mpn=_first(item, "mpn") or _first(o, "mpn"),
                gtins=gt, price=parse_price(_first(o, "price", "lowPrice")),
                currency=(_first(o, "priceCurrency") or "").upper() or None,
                availability=_term(_first(o, "availability")), condition=_condition(_first(o, "itemCondition") or _first(item, "itemCondition")),
                source="microdata", snippet=snippet[:4000],
            ))
    return offers


def extract(html: str) -> list[Offer]:
    """All Product offers on the page; JSON-LD wins when present."""
    return from_json_ld(html) or from_microdata(html)
