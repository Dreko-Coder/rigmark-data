import unittest

from helpers import FIXTURES, enabled_registry
from pricebot.extract import extract, parse_price
from pricebot.match import PartIndex, gtin_valid, normalise_gtin
from pricebot import pipeline

PAGES = FIXTURES / "pages"


def page(name):
    return (PAGES / name).read_text(encoding="utf-8")


class ExtractTests(unittest.TestCase):
    def test_json_ld_graph(self):
        [o] = extract(page("jsonld-graph.html"))
        self.assertEqual((o.price, o.currency, o.availability, o.condition), ("549.99", "GBP", "instock", "new"))
        self.assertEqual((o.mpn, o.brand, o.gtins), ("11348-03-20G", "Sapphire", ["4000000000013"]))
        self.assertEqual(o.source, "json-ld")
        self.assertIn('"price":"549.99"', o.snippet)

    def test_body_text_is_never_read(self):
        [o] = extract(page("jsonld-graph.html"))
        self.assertNotEqual(o.price, "1.00")

    def test_aggregate_offer_uses_its_offers(self):
        [o] = extract(page("jsonld-aggregate.html"))
        self.assertEqual((o.price, o.currency, o.gtins), ("579.00", "GBP", ["04000000000020"]))

    def test_offer_list_and_price_specification(self):
        offers = extract(page("jsonld-offer-list.html"))
        self.assertEqual([(o.price, o.condition, o.availability) for o in offers],
                         [("499.00", "refurbished", "instock"), ("529.95", None, "backorder")])
        self.assertFalse(offers[0].new)
        self.assertTrue(offers[1].orderable)

    def test_microdata(self):
        gold, silver = extract(page("microdata.html"))
        self.assertEqual((gold.mpn, gold.brand, gold.price, gold.currency, gold.gtins), ("EP-850G", "Example Power", "1299.00", "EUR", ["4000000000044"]))
        self.assertEqual(gold.source, "microdata")
        self.assertEqual((silver.price, silver.availability), ("94.90", "outofstock"))
        self.assertFalse(silver.orderable)

    def test_no_data_and_broken_json_give_nothing(self):
        self.assertEqual(extract(page("no-data.html")), [])
        self.assertEqual(extract(page("broken-jsonld.html")), [])

    def test_price_formats(self):
        cases = {"549.99": "549.99", "549,99": "549.99", "1.299,00": "1299.00", "1,299.00": "1299.00", "1,299": "1299.00",
                 549.99: "549.99", 579: "579.00", "£ 1 299.50": "1299.50", "": None, "free": None, "0": None, None: None, True: None}
        for raw, want in cases.items():
            self.assertEqual(parse_price(raw), want, raw)


class MatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = PartIndex.load(FIXTURES / "parts")

    def test_gtin_check_digit(self):
        self.assertTrue(gtin_valid("4000000000013"))
        self.assertFalse(gtin_valid("4000000000014"))
        self.assertEqual(normalise_gtin("000000007771"), "00000000007771")
        self.assertIsNone(normalise_gtin("12345"))

    def test_match_by_gtin_and_padding(self):
        [o] = extract(page("jsonld-aggregate.html"))  # GTIN-14 with a leading zero, dataset holds GTIN-13
        m = self.index.match(o)
        self.assertEqual((m.part.part_id, m.by), ("fixture-gpu-alpha", "gtin"))

    def test_match_by_part_number_and_brand(self):
        offers = extract(page("jsonld-offer-list.html"))
        m = self.index.match(offers[0])
        self.assertEqual((m.part.part_id, m.by, m.part.category), ("fixture-gpu-beta", "mpn", "gpu"))

    def test_ambiguous_and_wrong_brand_are_refused(self):
        shared, other_brand = extract(page("ambiguous.html"))
        self.assertIsNone(self.index.match(shared).part)
        self.assertEqual(self.index.match(shared).by, "part number belongs to more than one part")
        self.assertIsNone(self.index.match(other_brand).part)
        self.assertEqual(self.index.match(other_brand).by, "part number matched but the brand differs")


class DryRunTests(unittest.TestCase):
    def test_dry_run_over_fixture_pages(self):
        reg = enabled_registry()
        result = pipeline.dry_run_pages(PAGES, reg, PartIndex.load(FIXTURES / "parts"), "2026-11-12")
        rep = pipeline.report(result, reg)
        self.assertEqual(rep["domains"]["shop-a.example"]["noStructuredData"], 1)
        self.assertEqual(rep["domains"]["shop-c.example"]["wrongCurrency"], 1)
        self.assertEqual(rep["domains"]["shop-d1.example"]["matched"], 0)
        self.assertEqual(rep["coverage"]["GB"]["partsWith2Sellers"], 1)
        self.assertEqual(len(result["evidence"]), len(result["observations"]))
        self.assertTrue(all(e["url"].startswith("https://") and e["snippet"] for e in result["evidence"]))
        self.assertIn("| shop-a.example |", pipeline.report_markdown(rep))


if __name__ == "__main__":
    unittest.main()
