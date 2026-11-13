"""The signed price file (schema 1): payload, content rules, envelope, and the keep-the-last-file writer.

Format and rules mirror the app's Rigmark/Rig/Pricing/PriceFile.cs and data/rig/price-file.schema.json:
envelope { schemaVersion, keyId, payloadBase64, signatureBase64 }; the signature covers
"Rigmark.PriceFile.v1\\n" + the payload bytes exactly as published. There is no field for a shop name, a link or
one shop's price, and unknown fields are refused by the app, so this module never adds one.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from . import signing

SCHEMA = 1
MIN_BAND_SELLERS, MIN_INDEX_SELLERS = 3, 2
CATEGORIES = ("case", "cooler", "cpu", "gpu", "motherboard", "psu", "ram", "storage")
MAX_ENVELOPE_BYTES = 8 * 1024 * 1024
KEY_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CURRENCY = re.compile(r"^[A-Z]{3}$")
REGION = re.compile(r"^[A-Z]{2}$")
SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

PAYLOAD_KEYS = ["schemaVersion", "sequence", "issuedUtc", "dataDate", "fxBase", "fxRates", "regions", "indices", "bands"]
REGION_KEYS = ["region", "currency", "taxRatePercent", "pricesIncludeTax"]
INDEX_KEYS = ["region", "category", "multiplier", "low", "high", "sellers", "sample", "dataDate", "seeded"]
BAND_KEYS = ["partId", "region", "currency", "median", "min", "max", "sellers", "dataDate"]


class BuildRefused(Exception):
    """The new file failed a gate; the previous file stays."""


def _num(x):
    # Decimals leave as JSON numbers; values here have at most 6 significant decimals, so float text is exact.
    return float(x) if isinstance(x, Decimal) else x


def payload_dict(sequence: int, issued: datetime, data_date: str, fx_base: str, fx_rates: dict, regions: list[dict],
                 indices: list[dict], bands: list[dict]) -> dict:
    return {
        "schemaVersion": SCHEMA,
        "sequence": sequence,
        "issuedUtc": issued.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dataDate": data_date,
        "fxBase": fx_base,
        "fxRates": {k: _num(Decimal(str(v))) for k, v in sorted(fx_rates.items())},
        "regions": [{k: _num(r[k]) if not isinstance(r[k], bool) else r[k] for k in REGION_KEYS} for r in regions],
        "indices": [{k: _num(i[k]) if not isinstance(i[k], bool) else i[k] for k in INDEX_KEYS} for i in indices],
        "bands": [{k: _num(b[k]) for k in BAND_KEYS} for b in bands],
    }


def encode_payload(payload: dict) -> bytes:
    """Deterministic bytes: fixed key order, no spaces, ASCII only."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)


def validate(data: dict, now: datetime) -> list[str]:
    """Port of PriceFile.Validate plus the app's refusal of unknown fields. Both sides must agree."""
    problems: list[str] = []
    bad = problems.append
    if not isinstance(data, dict):
        return ["payload is not an object"]
    for key in data:
        if key not in PAYLOAD_KEYS:
            bad(f"unknown field '{key}'")
    if data.get("schemaVersion") != SCHEMA:
        bad(f"schemaVersion {data.get('schemaVersion')} is not supported")
    seq = data.get("sequence")
    if not _is_int(seq) or not 1 <= seq <= 1_000_000_000:
        bad("sequence is outside its range")
    issued = data.get("issuedUtc")
    try:
        if not isinstance(issued, str) or not issued.endswith("Z"):
            raise ValueError
        t = datetime.fromisoformat(issued.replace("Z", "+00:00"))
        if (t - now).total_seconds() > 300:
            raise ValueError
    except ValueError:
        bad("issuedUtc must be UTC and not in the future")
    if not isinstance(data.get("dataDate"), str) or not ISO_DATE.match(data["dataDate"]):
        bad("dataDate must be a date")
    if not isinstance(data.get("fxBase"), str) or not CURRENCY.match(data["fxBase"]):
        bad("fxBase must be a three-letter currency")
    fx, regions, indices, bands = (data.get(k) for k in ("fxRates", "regions", "indices", "bands"))
    if not isinstance(fx, dict) or not all(isinstance(x, list) for x in (regions, indices, bands)):
        bad("fxRates, regions, indices and bands are required")
        return problems
    if len(fx) > 400 or len(regions) > 400 or len(indices) > 4000 or len(bands) > 100_000:
        bad("a table exceeds its size bound")
    for cur, rate in fx.items():
        if not CURRENCY.match(cur) or not _is_num(rate) or rate <= 0:
            bad(f"fx rate '{cur}' is malformed")

    region_currency: dict[str, str] = {}
    for r in regions:
        if not isinstance(r, dict) or set(r) - set(REGION_KEYS) or not isinstance(r.get("region"), str) \
                or not REGION.match(r["region"]) or r["region"] in region_currency:
            bad(f"region '{r.get('region') if isinstance(r, dict) else r}' is missing, malformed or repeated")
            continue
        region_currency[r["region"]] = r.get("currency")
        if not isinstance(r.get("currency"), str) or not CURRENCY.match(r["currency"]):
            bad(f"region {r['region']}: currency is malformed")
        if not _is_num(r.get("taxRatePercent")) or not 0 <= r["taxRatePercent"] <= 50:
            bad(f"region {r['region']}: tax rate is outside 0 to 50")
        if not isinstance(r.get("pricesIncludeTax"), bool):
            bad(f"region {r['region']}: pricesIncludeTax must be true or false")

    seen = set()
    for i in indices:
        key = (i.get("region"), i.get("category")) if isinstance(i, dict) else None
        if not key or set(i) - set(INDEX_KEYS) or key[0] not in region_currency or key[1] not in CATEGORIES or key in seen:
            bad(f"index '{key}' names an unknown region or category, or is repeated")
            continue
        seen.add(key)
        at = f"index {key[0]}/{key[1]}"
        if not _is_int(i.get("sellers")) or i["sellers"] < MIN_INDEX_SELLERS:
            bad(f"{at}: needs at least {MIN_INDEX_SELLERS} sellers")
        if not _is_int(i.get("sample")) or not _is_int(i.get("sellers")) or i["sample"] < i["sellers"]:
            bad(f"{at}: sample is smaller than its seller count")
        m, lo, hi = i.get("multiplier"), i.get("low"), i.get("high")
        if not all(_is_num(v) for v in (m, lo, hi)) or not 0.3 <= m <= 3 or lo > m or hi < m or lo <= 0:
            bad(f"{at}: multiplier or its band is out of order")
        if not isinstance(i.get("dataDate"), str) or not ISO_DATE.match(i["dataDate"]):
            bad(f"{at}: dataDate must be a date")
        if not isinstance(i.get("seeded"), bool):
            bad(f"{at}: seeded must be true or false")

    seen = set()
    for b in bands:
        key = (b.get("partId"), b.get("region")) if isinstance(b, dict) else None
        if not key or set(b) - set(BAND_KEYS) or not isinstance(key[0], str) or not SLUG.match(key[0]) \
                or key[1] not in region_currency or key in seen:
            bad(f"band '{key}' is malformed, names an unknown region, or is repeated")
            continue
        seen.add(key)
        at = f"band {key[0]}/{key[1]}"
        if not _is_int(b.get("sellers")) or b["sellers"] < MIN_BAND_SELLERS:
            bad(f"{at}: needs at least {MIN_BAND_SELLERS} sellers")
        if b.get("currency") != region_currency[key[1]]:
            bad(f"{at}: currency does not match its region")
        lo, mid, hi = b.get("min"), b.get("median"), b.get("max")
        if not all(_is_num(v) for v in (lo, mid, hi)) or lo <= 0 or lo > mid or mid > hi:
            bad(f"{at}: min, median and max are out of order")
        if not isinstance(b.get("dataDate"), str) or not ISO_DATE.match(b["dataDate"]):
            bad(f"{at}: dataDate must be a date")
    return problems[:200]


def envelope_bytes(key_id: str, payload: bytes, signature: bytes) -> bytes:
    env = {"schemaVersion": SCHEMA, "keyId": key_id, "payloadBase64": base64.b64encode(payload).decode("ascii"),
           "signatureBase64": base64.b64encode(signature).decode("ascii")}
    return json.dumps(env, separators=(",", ":")).encode("ascii")


def read_envelope(raw: bytes) -> tuple[dict, bytes, bytes]:
    if not 1 <= len(raw) <= MAX_ENVELOPE_BYTES:
        raise ValueError("price file is outside its bounds")
    env = json.loads(raw)
    if set(env) != {"schemaVersion", "keyId", "payloadBase64", "signatureBase64"} or env["schemaVersion"] != SCHEMA:
        raise ValueError("envelope is not schema 1")
    return env, base64.b64decode(env["payloadBase64"], validate=True), base64.b64decode(env["signatureBase64"], validate=True)


def verify_file(raw: bytes, trusted: dict[str, bytes], now: datetime, minimum_sequence: int = 0) -> dict:
    """Python twin of PriceFile.Verify, used by the gates and tests. Raises ValueError on any failure."""
    env, payload, sig = read_envelope(raw)
    if not KEY_ID.match(env["keyId"]) or env["keyId"] not in trusted:
        raise ValueError("signing key is not trusted")
    if not 8 <= len(sig) <= 80:
        raise ValueError("signature is outside its bounds")
    signing.der_signature_parts(sig)
    if not signing.verify(payload, sig, trusted[env["keyId"]]):
        raise ValueError("signature check failed")
    data = json.loads(payload, parse_float=Decimal)
    problems = validate(data, now)
    if problems:
        raise ValueError(f"content is not valid: {problems[0]} ({len(problems)} problem(s))")
    if data["sequence"] < minimum_sequence:
        raise ValueError("older than the file already in use")
    return data


def previous_payload(path: Path) -> dict | None:
    """The last published payload (unverified read, only for sequence and the movement gate)."""
    if not path.exists():
        return None
    _, payload, _ = read_envelope(path.read_bytes())
    return json.loads(payload, parse_float=Decimal)


def build_and_write(out: Path, payload: dict, key_pem: str | bytes, key_id: str, now: datetime) -> dict:
    """Validates, signs, verifies the signature round trip, then atomically replaces `out`.
    Any failure raises BuildRefused and leaves `out` exactly as it was (a broken run keeps the last file)."""
    if not KEY_ID.match(key_id):
        raise BuildRefused("key id is malformed")
    data = encode_payload(payload)
    problems = validate(json.loads(data, parse_float=Decimal), now)
    if problems:
        raise BuildRefused(f"gate failed: {problems[0]} ({len(problems)} problem(s))")
    old = previous_payload(out) if out.exists() else None
    if old is not None and payload["sequence"] <= old["sequence"]:
        raise BuildRefused("sequence does not move forward")
    try:
        with signing.private_key_file(key_pem) as key:
            sig = signing.sign(data, key)
            pub = signing.public_key_der(key)
    except signing.SigningError as e:
        raise BuildRefused(str(e)) from e
    env = envelope_bytes(key_id, data, sig)
    try:
        verify_file(env, {key_id: pub}, now)
    except ValueError as e:
        raise BuildRefused(f"self-check failed: {e}") from e
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_bytes(env)
    os.replace(tmp, out)
    return {"sequence": payload["sequence"], "payloadSha256": hashlib.sha256(data).hexdigest().upper(),
            "publicKeyBase64": base64.b64encode(pub).decode("ascii"), "bytes": len(env)}
