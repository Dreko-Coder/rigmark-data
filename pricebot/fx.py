"""Exchange rates from the ECB daily reference file (eurofxref-daily.xml): units of a currency per one euro.

Read from a file the workflow downloaded (behind the same disabled guard); tests use a stored fixture.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from decimal import Decimal

ECB_DAILY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


def parse_ecb(xml_bytes: bytes) -> tuple[str, str, dict[str, Decimal]]:
    """Returns (base "EUR", date, rates). Refuses DTDs and entities."""
    if b"<!DOCTYPE" in xml_bytes.upper() or b"<!ENTITY" in xml_bytes.upper():
        raise ValueError("ECB file with a DTD or entities refused")
    root = ET.fromstring(xml_bytes)
    day, rates = None, {}
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] != "Cube":
            continue
        if "time" in el.attrib:
            day = el.attrib["time"]
        if "currency" in el.attrib and "rate" in el.attrib:
            rate = Decimal(el.attrib["rate"])
            if rate > 0:
                rates[el.attrib["currency"].upper()] = rate
    if not day or not rates:
        raise ValueError("ECB file has no date or no rates")
    return "EUR", day, rates
