import unittest
from datetime import date
from decimal import Decimal as D

from helpers import FIXTURES, enabled_registry
from pricebot import compute
from pricebot.compute import Observation
from pricebot.fx import parse_ecb
from pricebot.match import PartIndex

DAY = date(2026, 11, 12)


def obs(part, group, price, region="GB", currency="GBP", observed="2026-11-12", **kw):
    cat = "psu" if part.startswith("fixture-psu") else "gpu"
    return Observation(part, cat, region, "seller-" + group, group, D(price), currency, observed, **kw)


class ComputeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = enabled_registry()
        cls.parts = PartIndex.load(FIXTURES / "parts").parts
        cls.base, _, cls.rates = parse_ecb((FIXTURES / "ecb-daily.xml").read_bytes())

    def run_(self, observations, previous=None):
        return compute.compute(observations, list(self.reg.markets), self.parts, DAY, self.base, self.rates, previous)

    def test_median_odd_even(self):
        self.assertEqual(compute.median([D(3), D(1), D(2)]), D(2))
        self.assertEqual(compute.median([D(1), D(2), D(3), D(10)]), D("2.5"))

    def test_band_needs_three_independent_sellers(self):
        r = self.run_([obs("fixture-gpu-alpha", "a", "540"), obs("fixture-gpu-alpha", "b", "560")])
        self.assertEqual(r.bands, [])
        self.assertIn("too few sellers (2 of 3)", r.withheld[0]["reason"])
        r = self.run_([obs("fixture-gpu-alpha", g, p) for g, p in (("a", "540"), ("b", "560"), ("c", "550"))])
        [b] = r.bands
        self.assertEqual((b["median"], b["min"], b["max"], b["sellers"], b["currency"]), (D("550.00"), D("540.00"), D("560.00"), 3, "GBP"))
        self.assertNotIn("seller", "".join(b.keys()).lower().replace("sellers", ""))

    def test_one_owner_counts_once_with_its_lowest_price(self):
        r = self.run_([obs("fixture-gpu-alpha", "a", "540"), obs("fixture-gpu-alpha", "b", "560"),
                       obs("fixture-gpu-alpha", "d", "555"), obs("fixture-gpu-alpha", "d", "551")])
        [b] = r.bands
        self.assertEqual((b["sellers"], b["median"]), (3, D("551.00")))
        r = self.run_([obs("fixture-gpu-alpha", "a", "540"), obs("fixture-gpu-alpha", "d", "560"), obs("fixture-gpu-alpha", "d", "570")])
        self.assertEqual(r.bands, [])

    def test_outlier_dropped(self):
        r = self.run_([obs("fixture-gpu-alpha", g, p) for g, p in (("a", "540"), ("b", "560"), ("c", "550"), ("e", "300"))])
        [b] = r.bands
        self.assertEqual((b["min"], b["sellers"]), (D("540.00"), 3))
        self.assertTrue(any("outlier" in d["reason"] for d in r.dropped))

    def test_stale_wrong_currency_used_and_out_of_stock_dropped(self):
        r = self.run_([
            obs("fixture-gpu-alpha", "a", "540"), obs("fixture-gpu-alpha", "b", "560"),
            obs("fixture-gpu-alpha", "c", "550", observed="2026-11-01"),
            obs("fixture-gpu-alpha", "e", "550", currency="USD"),
            obs("fixture-gpu-alpha", "f", "550", condition="used"),
            obs("fixture-gpu-alpha", "g", "550", availability="outofstock"),
            obs("fixture-gpu-alpha", "h", "550", observed="2026-11-13"),
        ])
        self.assertEqual(r.bands, [])
        reasons = sorted(d["reason"] for d in r.dropped)
        self.assertEqual(reasons, ["currency is not the market's", "not in stock or orderable", "not new", "stale", "stale"])

    def test_launch_estimate_matches_the_app_rule(self):
        gb, ca = self.reg.market("GB"), self.reg.market("CA")
        msrp = {"amount": 599, "currency": "USD"}
        # 599 USD -> GBP through EUR (0.85 / 1.10), plus 20% VAT because GB prices include tax
        self.assertEqual(compute.launch_estimate(msrp, gb, "EUR", self.rates), D(599) * D("0.85") / D("1.10") * D("1.2"))
        # Canada: before tax, no tax added
        self.assertEqual(compute.launch_estimate(msrp, ca, "EUR", self.rates), D(599) * D("1.5") / D("1.10"))
        # A launch price in EUR carries another market's tax: no estimate where tax varies inside the country
        self.assertIsNone(compute.launch_estimate({"amount": 500, "currency": "EUR"}, ca, "EUR", self.rates))
        self.assertIsNone(compute.launch_estimate(None, gb, "EUR", self.rates))

    def test_index_needs_two_sellers_and_two_parts(self):
        est_a = compute.launch_estimate(self.parts["fixture-gpu-alpha"].msrp, self.reg.market("GB"), "EUR", self.rates)
        est_b = compute.launch_estimate(self.parts["fixture-gpu-beta"].msrp, self.reg.market("GB"), "EUR", self.rates)
        one_part = [obs("fixture-gpu-alpha", "a", "540"), obs("fixture-gpu-alpha", "b", "560")]
        r = self.run_(one_part)
        self.assertEqual(r.indices, [])
        self.assertIn("too few parts", r.withheld[-1]["reason"])
        r = self.run_(one_part + [obs("fixture-gpu-beta", "a", "500"), obs("fixture-gpu-beta", "c", "520")])
        [i] = r.indices
        ra, rb = D(550) / est_a, D(510) / est_b
        q = D("0.0001")
        self.assertEqual(i["multiplier"], ((ra + rb) / 2).quantize(q, rounding="ROUND_HALF_UP"))
        self.assertEqual((i["low"], i["high"]), (min(ra, rb).quantize(q, rounding="ROUND_HALF_UP"), max(ra, rb).quantize(q, rounding="ROUND_HALF_UP")))
        self.assertEqual((i["sellers"], i["sample"], i["seeded"], i["category"], i["region"]), (3, 4, False, "gpu", "GB"))

    def test_index_from_one_seller_is_withheld(self):
        r = self.run_([obs("fixture-gpu-alpha", "a", "540"), obs("fixture-gpu-beta", "a", "500")])
        self.assertEqual(r.indices, [])

    def test_seeded_prices_label_the_index_but_never_make_a_band(self):
        seeded = [obs(p, g, v, seeded=True) for p, g, v in (("fixture-gpu-alpha", "s1", "540"), ("fixture-gpu-alpha", "s2", "560"),
                                                            ("fixture-gpu-alpha", "s3", "550"), ("fixture-gpu-beta", "s1", "500"), ("fixture-gpu-beta", "s2", "520"))]
        r = self.run_(seeded)
        self.assertEqual(r.bands, [])
        [i] = r.indices
        self.assertTrue(i["seeded"])

    def test_multiplier_out_of_range_withheld(self):
        r = self.run_([obs(p, g, v) for p, g, v in (("fixture-gpu-alpha", "a", "5000"), ("fixture-gpu-alpha", "b", "5100"),
                                                    ("fixture-gpu-beta", "a", "5000"), ("fixture-gpu-beta", "b", "5100"))])
        self.assertEqual(r.indices, [])
        self.assertIn("outside 0.3 to 3", r.withheld[-1]["reason"])

    def test_movement_gate_holds_the_old_value(self):
        three = [obs("fixture-gpu-alpha", g, p) for g, p in (("a", "740"), ("b", "760"), ("c", "750"))]
        old_band = {"partId": "fixture-gpu-alpha", "region": "GB", "currency": "GBP", "median": D("550.00"), "min": D("540.00"),
                    "max": D("560.00"), "sellers": 3, "dataDate": "2026-11-05"}
        r = self.run_(three, previous={"bands": [old_band], "indices": []})
        self.assertEqual(r.bands, [old_band])
        self.assertEqual(r.held[0]["new"], "750.00")
        small_move = [obs("fixture-gpu-alpha", g, p) for g, p in (("a", "590"), ("b", "610"), ("c", "600"))]
        r = self.run_(small_move, previous={"bands": [old_band], "indices": []})
        self.assertEqual(r.bands[0]["median"], D("600.00"))
        self.assertEqual(r.held, [])


if __name__ == "__main__":
    unittest.main()
