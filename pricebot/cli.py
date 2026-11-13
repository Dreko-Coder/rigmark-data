"""Command line. Each stage is its own command so a run can stop cleanly between them.

  python -m pricebot guard                       report whether collection is enabled (sets the 'enabled' output)
  python -m pricebot dry-run --pages DIR ...     offline: extraction + matching over stored pages, report only
  python -m pricebot collect ...                 live collection (refuses while collection is disabled)
  python -m pricebot fetch-fx                    ECB reference rates (refuses while collection is disabled)
  python -m pricebot build ...                   bands, indices, gates, sign, write (refuses while disabled)
  python -m pricebot encrypt-evidence ...        age-encrypt the evidence (refuses while disabled or placeholder)

Exit codes: 0 done, 2 refused because collection is disabled, 3 a gate failed (the last file stays), 1 other error.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from . import compute, evidence, fx, pipeline, pricefile
from .match import PartIndex
from .registry import CollectionDisabled, load, require_collection_enabled, write_github_output

EXIT_DISABLED, EXIT_GATE = 2, 3


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def cmd_guard(a) -> int:
    reg = load(a.registry)
    enabled = reg.collection_enabled
    write_github_output("enabled", "true" if enabled else "false")
    print("collection enabled" if enabled else "collection disabled: sources/registry.json has \"collectionEnabled\": false; nothing will run")
    return 0


def _save_run(result: dict, reg, out: Path) -> None:
    rep = pipeline.report(result, reg)
    _write(out / "dry-run-report.json", json.dumps(rep, indent=2, sort_keys=True))
    _write(out / "dry-run-report.md", pipeline.report_markdown(rep))
    _write(out / "observations.jsonl", pipeline.observations_to_jsonl(result["observations"]))
    evidence.write_jsonl(result["evidence"], out / "evidence.jsonl")  # private: encrypted before any upload
    print(pipeline.report_markdown(rep))


def cmd_dry_run(a) -> int:
    reg = load(a.registry)
    result = pipeline.dry_run_pages(Path(a.pages), reg, PartIndex.load(a.parts), a.date or date.today().isoformat())
    _save_run(result, reg, Path(a.out))
    return 0


def cmd_collect(a) -> int:
    from .fetch import Fetcher
    reg = load(a.registry)
    require_collection_enabled(reg)
    deadline = time.monotonic() + a.minutes * 60
    fetcher = Fetcher(reg, deadline=deadline)
    result = pipeline.collect(reg, PartIndex.load(a.parts), fetcher, a.date or date.today().isoformat(), a.pages_per_domain)
    _save_run(result, reg, Path(a.out))
    return 0


def load_seed(seed_dir: Path, index: PartIndex) -> list[compute.Observation]:
    """Hand-collected seed prices: seed/<yyyy-mm>/<region>.csv with partId,sellerGroup,price,currency,observed."""
    obs = []
    for f in sorted(seed_dir.glob("*/*.csv")):
        with open(f, encoding="utf-8", newline="") as h:
            for row in csv.DictReader(h):
                part = index.parts.get(row["partId"])
                if part is None:
                    continue
                obs.append(compute.Observation(row["partId"], part.category, f.stem.upper(), "seed:" + row["sellerGroup"],
                                               row["sellerGroup"], Decimal(row["price"]), row["currency"], row["observed"],
                                               row.get("availability") or "instock", "new", seeded=True))
    return obs


def cmd_build(a) -> int:
    reg = load(a.registry)
    require_collection_enabled(reg)
    key = os.environ.get("PRICE_SIGNING_KEY", "")
    key_id = os.environ.get("PRICE_KEY_ID", "")
    if not key or not key_id:
        print("PRICE_SIGNING_KEY and PRICE_KEY_ID must be set (Actions secrets); the last file stays", file=sys.stderr)
        return EXIT_GATE
    index = PartIndex.load(a.parts)
    obs = pipeline.observations_from_jsonl(Path(a.observations).read_text(encoding="utf-8"))
    if a.seed and Path(a.seed).exists():
        obs += load_seed(Path(a.seed), index)
    fx_base, fx_date, rates = fx.parse_ecb(Path(a.fx).read_bytes())
    out = Path(a.out)
    previous = pricefile.previous_payload(out)
    now = datetime.now(timezone.utc)
    data_date = date.fromisoformat(a.date) if a.date else now.date()
    res = compute.compute(obs, list(reg.markets), index.parts, data_date, fx_base, rates, previous)
    regions = [{"region": m.region, "currency": m.currency, "taxRatePercent": Decimal(str(m.tax_rate_percent)),
                "pricesIncludeTax": m.prices_include_tax} for m in reg.markets]
    payload = pricefile.payload_dict((previous or {}).get("sequence", 0) + 1, now, data_date.isoformat(), fx_base,
                                     rates, regions, res.indices, res.bands)
    _write(Path(a.report), json.dumps({"fxDate": fx_date, "bands": len(res.bands), "indices": len(res.indices),
                                       "held": res.held, "withheld": res.withheld,
                                       "droppedByReason": dict(Counter(d["reason"] for d in res.dropped))}, indent=2, default=str))
    try:
        info = pricefile.build_and_write(out, payload, key, key_id, now)
    except pricefile.BuildRefused as e:
        print(f"gate failed, the last file stays: {e}", file=sys.stderr)
        return EXIT_GATE
    print(json.dumps(info))
    return 0


def cmd_fetch_fx(a) -> int:
    """Downloads the ECB daily reference rates (behind the guard; plain request under the bot's own name)."""
    import urllib.request
    reg = load(a.registry)
    require_collection_enabled(reg)
    req = urllib.request.Request(fx.ECB_DAILY_URL, headers={"User-Agent": reg.user_agent})
    with urllib.request.build_opener().open(req, timeout=30) as r:
        body = r.read(1_000_001)
    if len(body) > 1_000_000:
        raise ValueError("ECB file larger than expected")
    fx.parse_ecb(body)  # refuse to save something that is not the rates file
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_bytes(body)
    return 0


def cmd_encrypt(a) -> int:
    reg = load(a.registry)
    require_collection_enabled(reg)
    evidence.encrypt(Path(a.input), Path(a.out), evidence.recipient())
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pricebot")
    p.add_argument("--registry", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("guard").set_defaults(fn=cmd_guard)
    d = sub.add_parser("dry-run")
    d.add_argument("--pages", required=True); d.add_argument("--parts", default="parts")
    d.add_argument("--out", default="run"); d.add_argument("--date")
    d.set_defaults(fn=cmd_dry_run)
    c = sub.add_parser("collect")
    c.add_argument("--parts", default="parts"); c.add_argument("--out", default="run"); c.add_argument("--date")
    c.add_argument("--minutes", type=float, default=60); c.add_argument("--pages-per-domain", type=int, default=50)
    c.set_defaults(fn=cmd_collect)
    b = sub.add_parser("build")
    b.add_argument("--observations", required=True); b.add_argument("--parts", default="parts")
    b.add_argument("--fx", required=True); b.add_argument("--seed", default="seed")
    b.add_argument("--out", default="out/price-file.json"); b.add_argument("--report", default="run/build-report.json")
    b.add_argument("--date")
    b.set_defaults(fn=cmd_build)
    x = sub.add_parser("fetch-fx")
    x.add_argument("--out", default="run/ecb-daily.xml")
    x.set_defaults(fn=cmd_fetch_fx)
    e = sub.add_parser("encrypt-evidence")
    e.add_argument("--in", dest="input", required=True); e.add_argument("--out", required=True)
    e.set_defaults(fn=cmd_encrypt)
    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except CollectionDisabled as ex:
        print(str(ex), file=sys.stderr)
        return EXIT_DISABLED
    except evidence.EvidenceError as ex:
        print(str(ex), file=sys.stderr)
        return EXIT_GATE
