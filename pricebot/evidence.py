"""Evidence: every kept price with its link, time, currency, stock status and structured-data snippet.

Evidence names shops and single prices, so it is private. It is encrypted with age to the owner's recipient key
before any upload; the plain file never leaves the runner. The recipient in sources/evidence-recipient.txt ships
as a placeholder, and encryption refuses to run until the owner replaces it with his own age public key.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from .registry import ROOT

RECIPIENT_FILE = ROOT / "sources" / "evidence-recipient.txt"
PLACEHOLDER = "age1-PLACEHOLDER-owner-must-replace"
AGE_RECIPIENT = re.compile(r"^age1[02-9ac-hj-np-z]{58}$")


class EvidenceError(Exception):
    pass


def recipient(path: Path = RECIPIENT_FILE) -> str:
    lines = [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    value = lines[0] if lines else ""
    if value == PLACEHOLDER or not AGE_RECIPIENT.match(value):
        raise EvidenceError("the evidence recipient is still the placeholder (or not an age public key); nothing is uploaded")
    return value


def write_jsonl(records: list[dict], path: Path) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


def encrypt(plain: Path, out: Path, recipient_key: str) -> Path:
    if not AGE_RECIPIENT.match(recipient_key):
        raise EvidenceError("not an age public key")
    age = shutil.which("age")
    if not age:
        raise EvidenceError("age was not found on PATH")
    r = subprocess.run([age, "--encrypt", "-r", recipient_key, "-o", str(out), str(plain)], capture_output=True)
    if r.returncode != 0:
        raise EvidenceError("age failed: " + r.stderr.decode(errors="replace")[:300])
    return out
