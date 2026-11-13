import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from helpers import ROOT, enabled_registry
from pricebot import cli, evidence
from pricebot.fetch import Fetcher, UrllibTransport
from pricebot.registry import CollectionDisabled, check, load

WORKFLOWS = ROOT / ".github" / "workflows"


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(args))
    return code, out.getvalue(), err.getvalue()


class ShippedRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = json.loads((ROOT / "sources" / "registry.json").read_text(encoding="utf-8"))
        cls.reg = load()

    def test_ships_disabled(self):
        self.assertIs(self.raw["collectionEnabled"], False)
        self.assertFalse(self.reg.collection_enabled)
        self.assertTrue(all(not s.enabled and all(not d.enabled for d in s.domains) for s in self.reg.sellers))
        self.assertFalse(any(self.reg.domain_allowed(d.domain) for s in self.reg.sellers for d in s.domains))

    def test_nine_sellers_eleven_domains_all_pending_legal_read(self):
        self.assertEqual(len(self.reg.sellers), 9)
        self.assertEqual(sum(len(s.domains) for s in self.reg.sellers), 11)
        self.assertTrue(all(s.status == "proposed - legal read pending" for s in self.reg.sellers))
        self.assertEqual(len({s.ownership_group for s in self.reg.sellers}), 9)
        ldlc = next(s for s in self.reg.sellers if s.id == "ldlc-group-fr")
        self.assertEqual(sorted(d.domain for d in ldlc.domains), ["www.ldlc.com", "www.materiel.net", "www.topachat.com"])
        for s in self.raw["sellers"]:
            self.assertTrue(s["termsUrl"].startswith("https://") and s["termsNotes"])
            self.assertTrue(all(d["robotsNotes"] for d in s["domains"]))

    def test_crawl_delays(self):
        for s in self.reg.sellers:
            for d in s.domains:
                self.assertGreaterEqual(d.crawl_delay, 10, d.domain)
        megekko = next(s for s in self.reg.sellers if s.id == "megekko-nl").domains[0]
        self.assertGreaterEqual(megekko.crawl_delay, 3600)
        self.assertEqual(megekko.max_requests_per_run, 1)

    def test_honest_user_agent(self):
        self.assertIn("RigmarkPriceBot", self.reg.user_agent)
        self.assertIn("https://github.com/Dreko-Coder/rigmark-data", self.reg.user_agent)
        self.assertNotIn("Mozilla", self.reg.user_agent)

    def test_registry_rules(self):
        bad = json.loads(json.dumps(self.raw))
        bad["sellers"][0]["domains"][0]["crawlDelaySeconds"] = 5
        bad["sellers"][1]["domains"][0]["enabled"] = True
        bad["userAgent"] = "Mozilla/5.0"
        problems = check(bad)
        self.assertEqual(len(problems), 3, problems)


class DisabledGuardTests(unittest.TestCase):
    def test_no_transport_or_fetcher_while_disabled(self):
        reg = load()
        with self.assertRaises(CollectionDisabled):
            UrllibTransport(reg)
        with self.assertRaises(CollectionDisabled):
            Fetcher(reg)
        with self.assertRaises(CollectionDisabled):
            Fetcher(reg, transport=lambda *a: None)

    def test_disabled_domain_refused_even_when_collection_is_on(self):
        reg = enabled_registry()
        raw = json.loads(Path(reg.path).read_text())
        raw["sellers"][0]["enabled"] = False
        raw["sellers"][0]["domains"][0]["enabled"] = False
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "r.json").write_text(json.dumps(raw))
            reg2 = load(tmp / "r.json")
            self.assertFalse(reg2.domain_allowed("shop-a.example"))
            self.assertTrue(reg2.domain_allowed("shop-b.example"))
        finally:
            shutil.rmtree(tmp)

    def test_cli_commands_refuse(self):
        for args in (["collect"], ["build", "--observations", "x", "--fx", "y"], ["fetch-fx"],
                     ["encrypt-evidence", "--in", "x", "--out", "y"]):
            code, _, err = run_cli(*args)
            self.assertEqual(code, cli.EXIT_DISABLED, args)
            self.assertIn("Collection is disabled", err)

    def test_guard_output(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            out = tmp / "github_output"
            os.environ["GITHUB_OUTPUT"] = str(out)
            code, text, _ = run_cli("guard")
            self.assertEqual(code, 0)
            self.assertIn("collection disabled", text)
            self.assertEqual(out.read_text().strip(), "enabled=false")
        finally:
            os.environ.pop("GITHUB_OUTPUT", None)
            shutil.rmtree(tmp)

    def test_dry_run_on_stored_pages_needs_no_network(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            code, text, _ = run_cli("--registry", str(ROOT / "tests/fixtures/registry-enabled.json"), "dry-run",
                                    "--pages", str(ROOT / "tests/fixtures/pages"), "--parts", str(ROOT / "tests/fixtures/parts"),
                                    "--out", str(tmp), "--date", "2026-11-12")
            self.assertEqual(code, 0)
            self.assertTrue((tmp / "dry-run-report.md").exists())
        finally:
            shutil.rmtree(tmp)


class WorkflowTests(unittest.TestCase):
    def workflows(self):
        return {p.name: p.read_text(encoding="utf-8") for p in WORKFLOWS.glob("*.yml")}

    def test_no_schedule_anywhere(self):
        for name, text in self.workflows().items():
            self.assertNotRegex(text, r"(?m)^\s*schedule\s*:", name)
            self.assertNotIn("cron", text, name)

    def test_tests_workflow_runs_only_on_pull_requests_and_holds_no_secret(self):
        text = self.workflows()["tests.yml"]
        on = text.split("\non:", 1)[1].split("\npermissions:", 1)[0]
        self.assertEqual([l.strip() for l in on.strip().splitlines()], ["pull_request:"])
        self.assertNotIn("secrets.", text)
        self.assertIn("contents: read", text)

    def test_collect_workflow_is_manual_and_guarded(self):
        text = self.workflows()["collect.yml"]
        on = text.split("\non:", 1)[1].split("\npermissions:", 1)[0]
        self.assertIn("workflow_dispatch:", on)
        self.assertNotRegex(on, r"(?m)^  (push|pull_request|schedule|workflow_run|repository_dispatch)")
        jobs = re.split(r"(?m)^  (?=[a-z-]+:\n)", text.split("\njobs:\n", 1)[1])
        jobs = {j.split(":", 1)[0]: j for j in jobs if j.strip()}
        self.assertEqual(set(jobs), {"guard", "collect"})
        self.assertIn("python -m pricebot guard", jobs["guard"])
        for name, body in jobs.items():
            if name == "guard":
                self.assertNotIn("secrets.", body)
                continue
            self.assertIn("needs: guard", body, name)
            self.assertIn("if: needs.guard.outputs.enabled == 'true'", body, name)
        self.assertIn("evidence.jsonl.age", text)
        self.assertNotRegex(text, r"path:[^\n]*\n(\s+run/[^\n]*\n)*\s+run/evidence\.jsonl\n")


class EvidenceTests(unittest.TestCase):
    def test_placeholder_recipient_refused(self):
        with self.assertRaises(evidence.EvidenceError):
            evidence.recipient()
        with self.assertRaises(evidence.EvidenceError):
            evidence.encrypt(Path("x"), Path("y"), evidence.PLACEHOLDER)

    def test_age_round_trip(self):
        if not (shutil.which("age") and shutil.which("age-keygen")):
            if os.environ.get("RIGMARK_REQUIRE_AGE") == "1":
                self.fail("age is required")
            self.skipTest("age not installed")
        tmp = Path(tempfile.mkdtemp())
        try:
            subprocess.run(["age-keygen", "-o", str(tmp / "id.txt")], check=True, capture_output=True)
            pub = next(l.split(": ", 1)[1] for l in (tmp / "id.txt").read_text().splitlines() if l.startswith("# public key:"))
            (tmp / "r.txt").write_text("# test\n" + pub + "\n")
            self.assertEqual(evidence.recipient(tmp / "r.txt"), pub)
            plain = tmp / "evidence.jsonl"
            evidence.write_jsonl([{"url": "https://shop-a.example/p/1", "price": "549.99"}], plain)
            enc = evidence.encrypt(plain, tmp / "evidence.jsonl.age", pub)
            self.assertNotIn(b"549.99", enc.read_bytes())
            back = subprocess.run(["age", "--decrypt", "-i", str(tmp / "id.txt"), str(enc)], check=True, capture_output=True).stdout
            self.assertEqual(back, plain.read_bytes())
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
