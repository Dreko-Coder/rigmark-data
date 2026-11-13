"""ECDSA P-256 / SHA-256 signatures made and checked with the OpenSSL command line.

The app (Rigmark/Rig/Pricing/PriceFile.cs) verifies with .NET ECDsa.VerifyData(..., SHA256,
DSASignatureFormat.Rfc3279DerSequence) against a SubjectPublicKeyInfo (DER) public key. `openssl dgst -sign`
writes exactly that DER SEQUENCE { r INTEGER, s INTEGER }, so the signature bytes go into the file unchanged.

The private key is never written to the repository: in Actions it comes from the PRICE_SIGNING_KEY secret (PEM
text), is written to a private temporary file for the one openssl call and deleted straight after.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

PREFIX = b"Rigmark.PriceFile.v1\n"
MAX_PAYLOAD_BYTES = 6 * 1024 * 1024


class SigningError(Exception):
    pass


def openssl() -> str:
    exe = shutil.which("openssl")
    if not exe:
        raise SigningError("openssl was not found on PATH")
    return exe


def signing_bytes(payload: bytes) -> bytes:
    if not 1 <= len(payload) <= MAX_PAYLOAD_BYTES:
        raise SigningError("price payload is outside its bound")
    return PREFIX + payload


def _run(args: list[str], stdin: bytes | None = None) -> bytes:
    r = subprocess.run([openssl(), *args], input=stdin, capture_output=True)
    if r.returncode != 0:
        raise SigningError(f"openssl {args[0]} failed: {r.stderr.decode(errors='replace').strip()[:300]}")
    return r.stdout


@contextmanager
def private_key_file(pem: str | bytes):
    """A 0600 temporary file holding the key for one call; removed on exit whatever happens."""
    data = pem.encode() if isinstance(pem, str) else pem
    if b"PRIVATE KEY" not in data:
        raise SigningError("the signing key is not a PEM private key")
    d = tempfile.mkdtemp(prefix="rigmark-sign-")
    path = Path(d) / "key.pem"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        yield path
    finally:
        try:
            path.unlink(missing_ok=True)
        finally:
            shutil.rmtree(d, ignore_errors=True)


def check_p256(key_path: Path) -> None:
    text = _run(["pkey", "-in", str(key_path), "-noout", "-text"]).decode(errors="replace")
    if "prime256v1" not in text and "P-256" not in text:
        raise SigningError("the signing key is not an ECDSA P-256 key")


def public_key_der(key_path: Path) -> bytes:
    """SubjectPublicKeyInfo DER: the bytes the app lists in PriceFile.TrustedKeys."""
    return _run(["pkey", "-in", str(key_path), "-pubout", "-outform", "DER"])


def sign(payload: bytes, key_path: Path) -> bytes:
    check_p256(key_path)
    with tempfile.TemporaryDirectory(prefix="rigmark-msg-") as d:
        msg = Path(d) / "msg.bin"
        msg.write_bytes(signing_bytes(payload))
        return _run(["dgst", "-sha256", "-sign", str(key_path), str(msg)])


def verify(payload: bytes, signature: bytes, public_der: bytes) -> bool:
    with tempfile.TemporaryDirectory(prefix="rigmark-verify-") as d:
        msg, sig, pub = Path(d) / "msg.bin", Path(d) / "sig.der", Path(d) / "pub.der"
        msg.write_bytes(signing_bytes(payload))
        sig.write_bytes(signature)
        pub.write_bytes(public_der)
        r = subprocess.run(
            [openssl(), "dgst", "-sha256", "-verify", str(pub), "-keyform", "DER", "-signature", str(sig), str(msg)],
            capture_output=True,
        )
        return r.returncode == 0


def der_signature_parts(sig: bytes) -> tuple[int, int]:
    """Parses DER SEQUENCE { INTEGER r, INTEGER s } strictly; raises ValueError otherwise."""
    def read_len(b: bytes, i: int) -> tuple[int, int]:
        n = b[i]
        if n < 0x80:
            return n, i + 1
        k = n & 0x7F
        if k != 1 or b[i + 1] < 0x80:
            raise ValueError("unexpected DER length form")
        return b[i + 1], i + 2

    if len(sig) < 8 or sig[0] != 0x30:
        raise ValueError("not a DER sequence")
    total, i = read_len(sig, 1)
    if i + total != len(sig):
        raise ValueError("DER sequence length mismatch")
    vals = []
    for _ in range(2):
        if sig[i] != 0x02:
            raise ValueError("not a DER integer")
        n, i = read_len(sig, i + 1)
        raw = sig[i:i + n]
        if n == 0 or (raw[0] & 0x80) or (n > 1 and raw[0] == 0 and not raw[1] & 0x80):
            raise ValueError("non-minimal DER integer")
        vals.append(int.from_bytes(raw, "big"))
        i += n
    if i != len(sig):
        raise ValueError("trailing bytes after DER sequence")
    return vals[0], vals[1]


def generate_test_key(directory: Path) -> Path:
    """Throwaway P-256 key for tests only. Never used for a published file."""
    path = Path(directory) / "test-key.pem"
    _run(["genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", str(path)])
    return path
