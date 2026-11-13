import base64
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

from helpers import ROOT
from pricebot import pricefile, signing

NOW = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)
KEY_ID = "rigmark-test-1"
CONFORMANCE = ROOT / "conformance" / "PriceFileCheck"


def sample_payload(sequence=1):
    regions = [{"region": "GB", "currency": "GBP", "taxRatePercent": D(20), "pricesIncludeTax": True},
               {"region": "CA", "currency": "CAD", "taxRatePercent": D(0), "pricesIncludeTax": False}]
    indices = [{"region": "GB", "category": "gpu", "multiplier": D("1.0571"), "low": D("0.9812"), "high": D("1.1330"),
                "sellers": 3, "sample": 4, "dataDate": "2026-11-12", "seeded": False}]
    bands = [{"partId": "fixture-gpu-alpha", "region": "GB", "currency": "GBP", "median": D("550.00"), "min": D("540.00"),
              "max": D("560.00"), "sellers": 3, "dataDate": "2026-11-12"}]
    return pricefile.payload_dict(sequence, NOW - timedelta(minutes=1), "2026-11-12", "EUR",
                                  {"USD": D("1.1"), "GBP": D("0.85"), "CAD": D("1.5")}, regions, indices, bands)


class SigningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="rigmark-test-"))
        cls.key_path = signing.generate_test_key(cls.tmp)  # throwaway key; never the production key
        cls.key_pem = cls.key_path.read_text()
        cls.pub = signing.public_key_der(cls.key_path)
        cls.dll = None
        if shutil.which("dotnet"):
            out = cls.tmp / "conformance"
            r = subprocess.run(["dotnet", "build", str(CONFORMANCE), "-c", "Release", "-o", str(out), "-nologo", "-v", "q"], capture_output=True)
            if r.returncode == 0:
                cls.dll = out / "PriceFileCheck.dll"
            else:
                raise RuntimeError(r.stdout.decode(errors="replace")[-2000:])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def write(self, sequence=1):
        out = Path(tempfile.mkdtemp(dir=self.tmp)) / "price-file.json"
        info = pricefile.build_and_write(out, sample_payload(sequence), self.key_pem, KEY_ID, NOW)
        return out, info

    def dotnet_verify(self, path: Path, pub: bytes = None, key_id: str = KEY_ID):
        if self.dll is None:
            if os.environ.get("RIGMARK_REQUIRE_DOTNET") == "1":
                self.fail(".NET SDK required for the conformance check")
            self.skipTest(".NET SDK not installed")
        r = subprocess.run(["dotnet", str(self.dll), str(path), base64.b64encode(pub or self.pub).decode(), key_id], capture_output=True)
        return r.returncode, r.stdout.decode(errors="replace").strip()

    def test_round_trip_python(self):
        out, info = self.write()
        data = pricefile.verify_file(out.read_bytes(), {KEY_ID: self.pub}, NOW)
        self.assertEqual((data["sequence"], data["bands"][0]["median"]), (1, D("550.0")))
        self.assertEqual(info["publicKeyBase64"], base64.b64encode(self.pub).decode())

    def test_signature_is_strict_der_from_openssl(self):
        out, _ = self.write()
        env = json.loads(out.read_bytes())
        sig = base64.b64decode(env["signatureBase64"])
        r, s = signing.der_signature_parts(sig)
        self.assertTrue(0 < r and 0 < s)
        self.assertTrue(8 <= len(sig) <= 72)
        self.assertEqual(set(env), {"schemaVersion", "keyId", "payloadBase64", "signatureBase64"})

    def test_signing_bytes_prefix(self):
        self.assertEqual(signing.signing_bytes(b"{}"), b"Rigmark.PriceFile.v1\n{}")

    def test_payload_has_no_seller_field(self):
        out, _ = self.write()
        payload = base64.b64decode(json.loads(out.read_bytes())["payloadBase64"]).decode()
        for word in ("shop", "seller\"", "url", "http", "link", "store"):
            self.assertNotIn(word, payload.lower())

    def test_tampered_payload_fails(self):
        out, _ = self.write()
        env = json.loads(out.read_bytes())
        payload = base64.b64decode(env["payloadBase64"]).replace(b"550.0", b"450.0")
        env["payloadBase64"] = base64.b64encode(payload).decode()
        bad = self.tmp / "tampered.json"
        bad.write_text(json.dumps(env))
        with self.assertRaises(ValueError):
            pricefile.verify_file(bad.read_bytes(), {KEY_ID: self.pub}, NOW)
        code, text = self.dotnet_verify(bad)
        self.assertEqual(code, 1, text)
        self.assertIn("signature check failed", text)

    def test_other_key_fails(self):
        out, _ = self.write()
        other = signing.public_key_der(signing.generate_test_key(Path(tempfile.mkdtemp(dir=self.tmp))))
        with self.assertRaises(ValueError):
            pricefile.verify_file(out.read_bytes(), {KEY_ID: other}, NOW)
        self.assertEqual(self.dotnet_verify(out, pub=other)[0], 1)

    def test_dotnet_verifies_what_the_bot_signs(self):
        out, _ = self.write()
        code, text = self.dotnet_verify(out)
        self.assertEqual(code, 0, text)
        self.assertEqual(text, "verified sequence 1 bands 1 indices 1")

    def test_dotnet_refuses_an_unknown_field(self):
        payload = pricefile.encode_payload({**sample_payload(), "shopName": "x"})
        with signing.private_key_file(self.key_pem) as k:
            sig = signing.sign(payload, k)
        bad = self.tmp / "unknown-field.json"
        bad.write_bytes(pricefile.envelope_bytes(KEY_ID, payload, sig))
        self.assertEqual(self.dotnet_verify(bad)[0], 1)
        with self.assertRaises(ValueError):
            pricefile.verify_file(bad.read_bytes(), {KEY_ID: self.pub}, NOW)

    def test_non_p256_key_refused(self):
        other = self.tmp / "p384.pem"
        subprocess.run([signing.openssl(), "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-384", "-out", str(other)], check=True, capture_output=True)
        with self.assertRaises(pricefile.BuildRefused):
            pricefile.build_and_write(self.tmp / "p384-file.json", sample_payload(), other.read_text(), KEY_ID, NOW)
        self.assertFalse((self.tmp / "p384-file.json").exists())

    def test_temporary_key_file_is_removed(self):
        with signing.private_key_file(self.key_pem) as k:
            path = k
            self.assertTrue(path.exists())
        self.assertFalse(path.exists())
        with self.assertRaises(signing.SigningError):
            with signing.private_key_file("not a key"):
                pass


class WriterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="rigmark-writer-"))
        cls.key_pem = signing.generate_test_key(cls.tmp).read_text()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_broken_run_keeps_the_last_file(self):
        out = self.tmp / "keep.json"
        pricefile.build_and_write(out, sample_payload(1), self.key_pem, KEY_ID, NOW)
        before = out.read_bytes()
        broken = sample_payload(2)
        broken["bands"][0]["sellers"] = 2  # below the three-seller minimum
        with self.assertRaises(pricefile.BuildRefused):
            pricefile.build_and_write(out, broken, self.key_pem, KEY_ID, NOW)
        self.assertEqual(out.read_bytes(), before)
        with self.assertRaises(pricefile.BuildRefused):
            pricefile.build_and_write(out, sample_payload(2), "-----BEGIN PRIVATE KEY-----\nbroken\n-----END PRIVATE KEY-----\n", KEY_ID, NOW)
        self.assertEqual(out.read_bytes(), before)
        self.assertFalse((self.tmp / "keep.json.tmp").exists())

    def test_sequence_must_move_forward(self):
        out = self.tmp / "seq.json"
        pricefile.build_and_write(out, sample_payload(5), self.key_pem, KEY_ID, NOW)
        with self.assertRaises(pricefile.BuildRefused):
            pricefile.build_and_write(out, sample_payload(5), self.key_pem, KEY_ID, NOW)
        pricefile.build_and_write(out, sample_payload(6), self.key_pem, KEY_ID, NOW)
        self.assertEqual(pricefile.previous_payload(out)["sequence"], 6)

    def test_validate_port_matches_the_app_rules(self):
        good = json.loads(pricefile.encode_payload(sample_payload()), parse_float=D)
        self.assertEqual(pricefile.validate(good, NOW), [])
        cases = [
            (lambda p: p["bands"][0].update(sellers=2), "needs at least 3 sellers"),
            (lambda p: p["indices"][0].update(sellers=1), "needs at least 2 sellers"),
            (lambda p: p["indices"][0].update(sample=2), "sample is smaller"),
            (lambda p: p["indices"][0].update(multiplier=D("3.5"), high=D("3.6")), "multiplier or its band"),
            (lambda p: p["bands"][0].update(currency="EUR"), "currency does not match"),
            (lambda p: p["bands"][0].update(min=D("600")), "out of order"),
            (lambda p: p["bands"][0].update(partId="Bad Id"), "malformed"),
            (lambda p: p["bands"].append(dict(p["bands"][0])), "repeated"),
            (lambda p: p["indices"][0].update(category="monitor"), "unknown region or category"),
            (lambda p: p.update(issuedUtc="2026-11-12T07:00:00Z"), "not in the future"),
            (lambda p: p.update(fxBase="eur"), "fxBase"),
            (lambda p: p["fxRates"].update(XXX=0), "fx rate"),
            (lambda p: p.update(sellerName="x"), "unknown field"),
            (lambda p: p["bands"][0].update(shop="x"), "malformed"),
            (lambda p: p["regions"][0].update(taxRatePercent=60), "tax rate"),
        ]
        for mutate, expect in cases:
            p = json.loads(pricefile.encode_payload(sample_payload()), parse_float=D)
            mutate(p)
            problems = pricefile.validate(p, NOW)
            self.assertTrue(any(expect in x for x in problems), (expect, problems))


if __name__ == "__main__":
    unittest.main()
