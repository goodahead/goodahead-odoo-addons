# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""A value read off a document is a value, never a search pattern.

``%`` and ``_`` in a printed VAT number, name or country code used to act as
LIKE wildcards, so a VAT number of ``%`` matched every partner and the
matcher picked the busiest supplier. Invented data throughout.
"""

from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestLikePatterns(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Partner = cls.env["res.partner"]
        cls.vendor = cls.Partner.create(
            {
                "name": "ZZ Wildcard Vendor Oy",
                "is_company": True,
                "vat": "FI99990001",
                "supplier_rank": 99,
                "country_id": cls.env.ref("base.fi").id,
                "company_registry": "9999000-1",
            }
        )

    def _found(self, domain):
        return self.Partner.with_context(active_test=False).search(domain) if domain else self.Partner

    def test_a_wildcard_vat_matches_nobody(self):
        for vat in ("%", "FI%", "FI9999000_", "_I99990001", "%%%%"):
            with self.subTest(vat=vat):
                found = self._found(self.Partner._ai_seller_vat_domain({"vat": vat}))
                self.assertNotIn(self.vendor, found)
                self.assertFalse(self.Partner._ai_find_vendor({"vat": vat}))
        # The control: the vendor's own VAT number still finds it, any case.
        self.assertIn(
            self.vendor, self._found(self.Partner._ai_seller_vat_domain({"vat": "fi 9999 0001"}))
        )

    def test_a_vat_number_of_the_wrong_shape_is_not_searched(self):
        for vat in ("FI99990001; DROP", "<FI99990001>", "F", "x" * 40):
            with self.subTest(vat=vat):
                self.assertEqual(self.Partner._ai_seller_vat_domain({"vat": vat}), [])

    def test_a_wildcard_name_matches_nobody(self):
        for name in ("%", "ZZ Wildcard%", "ZZ_Wildcard_Vendor_Oy"):
            with self.subTest(name=name):
                found = self._found(self.Partner._ai_seller_name_domain({"name": name}))
                self.assertNotIn(self.vendor, found)
                self.assertNotIn(
                    self.vendor,
                    self.Partner._ai_retrieve_partner_with_exact_name(name, []),
                )
        self.assertIn(
            self.vendor,
            self._found(self.Partner._ai_seller_name_domain({"name": "zz wildcard vendor oy"})),
        )

    def test_a_wildcard_name_is_not_a_candidate_for_everybody(self):
        candidates = self.env["account.move"]._ai_vendor_candidates(
            {"name": "%"}, limit=50
        )
        self.assertNotIn(self.vendor, [c["partner"] for c in candidates])

    def test_a_wildcard_country_code_resolves_to_nothing(self):
        Move = self.env["account.move"]
        for code in ("%", "F_", "_I"):
            with self.subTest(code=code):
                self.assertIsNone(Move._ai_resolve_location("country_id", code))
        self.assertEqual(Move._ai_resolve_location("country_id", "fi"), self.env.ref("base.fi"))
        domain = self.Partner._ai_vendor_registry_domain("9999000-1", "F_")
        self.assertNotIn(self.vendor, self._found(domain))
        self.assertIn(
            self.vendor, self._found(self.Partner._ai_vendor_registry_domain("9999000-1", "FI"))
        )
