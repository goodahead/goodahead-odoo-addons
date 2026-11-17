# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Vendor de-duplication: match an existing vendor by VAT or business id.

The business id (company_registry) is scoped by country and independent of the VAT.
The tests drive the search-plan step the core actually uses during a UBL import.
Fake `ZZ-…` identifiers avoid matching existing partners.
"""

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_ai_import.models.core_profile import (
    PROFILE_SEARCH_PLAN,
)

from .profiles import skip_method_unless_profile


@tagged("post_install", "-at_install")
class TestVendorMatching(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fi = cls.env.ref("base.fi")
        cls.de = cls.env.ref("base.de")
        cls.Partner = cls.env["res.partner"]
        cls.AM = cls.env["account.move"]
        # Finnish vendor: business id in company_registry, no VAT stored.
        cls.fi_vendor = cls.Partner.create(
            {
                "name": "ZZ Test Vendor Oy",
                "is_company": True,
                "company_registry": "ZZ-FI-0001",
                "country_id": cls.fi.id,
            }
        )
        # German vendor: Handelsregister-style number (unrelated to the VAT).
        cls.de_vendor = cls.Partner.create(
            {
                "name": "ZZ Test GmbH",
                "is_company": True,
                "company_registry": "ZZ-DE 9001",
                "country_id": cls.de.id,
            }
        )

    # ------------------------------------------------------------------
    # Pure helpers (no DB)
    # ------------------------------------------------------------------

    def test_registry_variants_formatting_only(self):
        v = self.Partner._registry_variants("1234567-8")
        self.assertIn("1234567-8", v)
        self.assertIn("12345678", v)
        self.assertIn("HRB12345", self.Partner._registry_variants("HRB 12345"))

    def test_vat_country_code(self):
        self.assertEqual(self.Partner._vat_country_code("DE811234567"), "DE")
        self.assertEqual(self.Partner._vat_country_code("FI12345678"), "FI")
        self.assertEqual(self.Partner._vat_country_code("ATU12345678"), "AT")
        self.assertIsNone(self.Partner._vat_country_code("1234567-8"))

    # ------------------------------------------------------------------
    # The plan step itself
    # ------------------------------------------------------------------

    def _step(self, customer_values=None, **ctx):
        """Run the plan step and return the partner it finds (or an empty set)."""
        Partner = self.Partner.with_context(**ctx)
        plan_values = Partner._import_retrieve_customer_from_ai_business_id(
            customer_values or {}
        )
        if not plan_values:
            return self.Partner
        found = self.Partner
        for criteria in plan_values["criteria"]:
            found = self.Partner.search(criteria["domain"], limit=1)
            if found:
                break
        return found

    def test_match_business_id_country_from_vat_prefix(self):
        """No country in the context: the VAT prefix supplies it."""
        self.assertEqual(
            self._step({"vat": "FI09990001"}, ai_seller_business_id="ZZ-FI-0001"),
            self.fi_vendor,
        )

    def test_match_raw_business_id_with_context_country(self):
        self.assertEqual(
            self._step(
                {"vat": "FI09990002"},
                ai_seller_business_id="ZZ-FI-0001",
                ai_seller_country_code="FI",
            ),
            self.fi_vendor,
        )

    def test_no_match_without_business_id(self):
        """Country alone must not fall back to a registry match."""
        self.assertFalse(self._step({"vat": "FI09990001"}))

    def test_no_cross_country_match(self):
        self.assertFalse(
            self._step(
                {"vat": "ZZ-FI-0001"},
                ai_seller_business_id="ZZ-FI-0001",
                ai_seller_country_code="SE",
            )
        )

    def test_no_match_without_country(self):
        """A business id is only unique within its country, so no country means no match."""
        self.assertFalse(
            self._step({"vat": "ZZ-FI-0001"}, ai_seller_business_id="ZZ-FI-0001")
        )

    def test_german_business_id_matches(self):
        """Not FI-specific: a German registry number matches by country."""
        self.assertEqual(
            self._step(
                {"vat": "DE099900001"},
                ai_seller_business_id="ZZ-DE 9001",
                ai_seller_country_code="DE",
            ),
            self.de_vendor,
        )

    # ------------------------------------------------------------------
    # The step must be in the search plan, in the right place
    # ------------------------------------------------------------------

    # The legacy build has no search plan; its ordering is covered by
    # `test_legacy_name_slot.TestTheNameSlotAdapter`.
    @skip_method_unless_profile(PROFILE_SEARCH_PLAN)
    def test_business_id_step_is_in_the_plan_before_name(self):
        """The business-id step sits after the exact keys and before the name match.

        Core's name step is an `ilike` substring match, so a business-id step after
        it would lose to "ABC Oy" matching "ABC Oy Holding".
        """
        plan = self.env["account.edi.xml.ubl_bis3"]._import_ubl_retrieve_customer_search_plan(
            {"company": self.env.company}
        )
        names = [getattr(step, "__name__", "") for step in plan]

        self.assertIn("_import_retrieve_customer_from_ai_business_id", names)
        ours = names.index("_import_retrieve_customer_from_ai_business_id")

        self.assertLess(
            ours,
            names.index("_import_retrieve_customer_from_name"),
            "the business id must beat core's name match",
        )
        self.assertGreater(
            ours,
            names.index("_import_retrieve_customer_from_vat"),
            "the business id must never beat a VAT match",
        )
        for exact in (
            "_import_retrieve_customer_from_email",
            "_import_retrieve_customer_from_phone",
        ):
            if exact in names:
                self.assertGreater(
                    ours, names.index(exact), f"{exact} is exact; it goes first"
                )

    def test_decoder_matches_the_vendor_by_business_id(self):
        """The business-id step runs inside a real decode.

        Name and VAT match nothing, so only the business id can resolve the vendor.
        Asserted before `_ai_post_process`, which has its own business-id matching.
        """
        extracted = {
            "type": "Invoice",
            "invoice_number": "ZZ-BIZID-1",
            "issue_date": "2026-09-09",
            "currency": "EUR",
            "seller": {
                # Matches no stored partner by name, and no stored VAT.
                "name": "ZZ Completely Different Trading Name",
                "vat": "FI09999999",
                "business_id": "ZZ-FI-0001",
                "country_code": "FI",
            },
            "buyer": {"name": self.env.company.name, "country_code": "FI"},
            "lines": [
                {
                    "name": "a line",
                    "quantity": 1.0,
                    "unit_code": "C62",
                    "price_unit": 10.0,
                    "line_total": 10.0,
                    "tax_percent": 0.0,
                    "tax_amount": 0.0,
                    "tax_category": "Z",
                }
            ],
            "tax_subtotals": [
                {
                    "tax_percent": 0.0,
                    "taxable_amount": 10.0,
                    "tax_amount": 0.0,
                    "tax_category": "Z",
                }
            ],
            "totals": {
                "line_extension": 10.0,
                "tax_exclusive": 10.0,
                "tax_inclusive": 10.0,
                "payable": 10.0,
            },
        }

        journal = self.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", self.env.company.id)],
            limit=1,
        )
        xml_bytes = self.AM._ai_generate_ubl_from_extracted(
            extracted, self.env.company
        )
        move = self.AM.create({"move_type": "in_invoice", "journal_id": journal.id})
        error = self.AM._ai_decode_ubl_into_move(move, xml_bytes, extracted)
        self.assertFalse(error, error)

        self.assertEqual(
            move.partner_id,
            self.fi_vendor,
            "the decoder did not resolve the vendor by business id — the "
            "re-hooked search-plan step did not run",
        )

    # ------------------------------------------------------------------
    # The post-process fallback (independent of the decoder)
    # ------------------------------------------------------------------

    def test_fallback_reuses_existing_vendor(self):
        # Different name than fi_vendor so the reuse can only come from the
        # business id (company_registry), not from an exact-name match.
        inv = self.AM.create({"move_type": "in_invoice"})
        self.AM._create_partner_from_extraction(
            inv,
            {"seller": {"name": "ZZ Renamed Supplier Oy", "business_id": "ZZ-FI-0001",
                        "country_code": "FI"}},
        )
        self.assertEqual(inv.partner_id, self.fi_vendor, "must reuse, not duplicate")

    def test_name_fallback_ignores_child_contacts(self):
        """The exact-name fallback must not assign a child contact as the vendor.

        The rule is `parent_id = False`, not `is_company`: in imported contact books
        the company flag is often unset on real vendors.
        """
        child = self.Partner.create(
            {
                "name": "ZZ Contact Person",
                "is_company": False,
                "parent_id": self.Partner.create(
                    {"name": "ZZ Parent Company", "is_company": True}
                ).id,
            }
        )
        inv = self.AM.create({"move_type": "in_invoice"})
        self.AM._create_partner_from_extraction(
            inv, {"seller": {"name": "ZZ Contact Person"}}
        )
        self.assertNotEqual(
            inv.partner_id, child, "a child contact must never become the vendor"
        )
        self.assertFalse(
            inv.partner_id,
            "and no company may be invented in its place",
        )

    def test_a_top_level_vendor_without_the_company_flag_is_still_matched(self):
        """Control: a top-level vendor with `is_company` unset is still matched by name."""
        vendor = self.Partner.create(
            {"name": "ZZ Reps Unflagged", "is_company": False, "supplier_rank": 5}
        )
        inv = self.AM.create({"move_type": "in_invoice"})
        self.AM._create_partner_from_extraction(
            inv, {"seller": {"name": "ZZ Reps Unflagged"}}
        )
        self.assertEqual(
            inv.partner_id,
            vendor,
            "a top-level vendor must match even with `is_company` unset, which "
            "is common in real contact books",
        )

    def test_a_real_vendor_outranks_a_namesake_individual(self):
        """Of two top-level namesakes, the one with higher `supplier_rank` wins.

        The namesake is created first, so this tests the ordering, not insertion order.
        """
        namesake = self.Partner.create(
            {"name": "ZZ Ambiguous Name", "is_company": False}
        )
        vendor = self.Partner.create(
            {"name": "ZZ Ambiguous Name", "is_company": True, "supplier_rank": 9}
        )
        inv = self.AM.create({"move_type": "in_invoice"})
        self.AM._create_partner_from_extraction(
            inv, {"seller": {"name": "ZZ Ambiguous Name"}}
        )
        self.assertEqual(inv.partner_id, vendor)
        self.assertNotEqual(inv.partner_id, namesake)

    def test_unmatched_vendor_is_not_created(self):
        """An unrecognised vendor leaves the bill without a partner; none is created.

        Creating one tended to duplicate an existing vendor stored under another name.
        """
        Partner = self.Partner.with_context(active_test=False)
        before = Partner.search_count([])
        inv = self.AM.create({"move_type": "in_invoice"})
        self.AM._create_partner_from_extraction(
            inv,
            {"seller": {"name": "ZZ Brand New Oy", "business_id": "ZZ-NEW-0002",
                        "country_code": "FI"}},
        )
        self.assertFalse(inv.partner_id, "the bill must be left without a vendor")
        self.assertEqual(
            Partner.search_count([]),
            before,
            "no partner may be created for an unrecognised vendor",
        )
        self.assertTrue(
            inv.message_ids.filtered(lambda m: "not recognised" in (m.body or "")),
            "the bill must say so in the chatter",
        )
