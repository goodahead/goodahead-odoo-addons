# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Vendor de-duplication: match an existing vendor by VAT or business id
(company_registry, scoped by country). VAT and business id are independent
identifiers — neither is derived from the other.

🔴 THESE TESTS WERE REWRITTEN FOR THIS TREE. On the source project the business
id fallback hangs off `res.partner._retrieve_partner`, and these tests called
that method. The core here never calls it during a UBL import, so those tests
would have gone on passing while testing a code path production never reaches —
a green suite proving nothing. They now drive the search-plan step that this
core actually uses, and `test_business_id_step_is_in_the_plan_before_name` plus
`test_decoder_matches_the_vendor_by_business_id` fail if the re-hook is removed.

Uses clearly-fake identifiers (ZZ-…) so the DB-touching tests never collide with
real vendors that may already exist in the database.
"""

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_import.models.core_profile import (
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
        v = self.Partner._registry_variants("0000007-8")
        self.assertIn("0000007-8", v)
        self.assertIn("00000078", v)
        self.assertIn("HRB12345", self.Partner._registry_variants("HRB 12345"))

    def test_vat_country_code(self):
        self.assertEqual(self.Partner._vat_country_code("DE811234567"), "DE")
        self.assertEqual(self.Partner._vat_country_code("FI00000078"), "FI")
        self.assertEqual(self.Partner._vat_country_code("ATU12345678"), "AT")
        self.assertIsNone(self.Partner._vat_country_code("0000007-8"))

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
        """A business id is only unique within its country — with no country
        this must decline rather than search the whole database on a short
        numeric string."""
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
    # 🔴 The re-hook: the step must be IN the plan, and in the right place
    # ------------------------------------------------------------------

    # 🔴 The SEARCH PLAN itself is the build. `_import_ubl_retrieve_customer_
    # search_plan` has no core definition on the legacy build — there is no plan
    # there at all — so `super()` raises and no portable spelling of this test
    # exists. The legacy build's equivalent ordering claim (our keys below core's
    # exact keys, above core's substring name match) is asserted at its own seam
    # by `test_legacy_name_slot.TestTheNameSlotAdapter`, which is where the
    # ordering actually lives there. The two together cover the rule on both.
    @skip_method_unless_profile(PROFILE_SEARCH_PLAN)
    def test_business_id_step_is_in_the_plan_before_name(self):
        """Proves the re-hook is wired, and wired at the slot we chose.

        This is the test that fails if somebody deletes
        `_import_ubl_retrieve_customer_search_plan`, or "tidies" the insertion
        into an append. Position matters: core matches names with
        ``('name', 'ilike', name)``, a SUBSTRING match, so a business-id step
        placed after it would be beaten by "ABC Oy" matching "ABC Oy Holding".
        It must also stay below the exact identity keys (vat, eas endpoint,
        email, phone), which a plain `insert(0, ...)` would break.
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
        """🔴 The fact, not the proxy: the step RUNS inside a real decode.

        The seller is given a name that matches no partner and a VAT that
        matches no partner, so the ONLY thing that can resolve this vendor is
        the business id arriving on the context. The assertion is made on the
        move straight after `_ai_decode_ubl_into_move` and BEFORE
        `_ai_post_process` — deliberately, because post-processing has its own
        business-id matching in `_create_partner_from_extraction`, and going
        through `_process_import` would let this pass with the decoder hook
        removed. Isolating the decoder is what makes it evidence.
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
        """The weak exact-name fallback must not assign a CHILD CONTACT as the
        vendor — and the test for that is `parent_id`, not `is_company`.

        ⚠️ REWRITTEN TWICE, AND THE SECOND REWRITE IS THE INTERESTING ONE.
        It first asserted `is_company`, which passed only because the import
        MINTED a company. Then, with nothing minted any more, it asserted that a
        partner with `is_company = False` is never picked — and that assertion
        was WRONG ABOUT THIS BUSINESS.

        🔴 MEASURED ON a migrated test database, of the partners with
        `supplier_rank > 0`: **most are `is_company = True`, and a large
        minority are `is_company = False` with no parent** — the vendors the
        `Northwind` and `Bluebird` sample documents come from among them. The
        flag is simply unset across a large part of a contact book that was
        imported years ago; it says nothing about whether the row is a company.
        Filtering on it silently excluded a large part of the vendor book, and
        `_ai_vendor_candidates` returned an EMPTY list for `Northwind`, the one
        case F27 was designed around.

        So the rule is `parent_id = False`: a child contact has a parent and a
        vendor does not. That is what the original comment meant, and it
        excludes exactly the rows it was written to exclude.

        ⚠️ THE COST, STATED RATHER THAN HIDDEN: a TOP-LEVEL individual who
        happens to share a vendor's exact name can now be picked by the name
        step. Nothing in this database distinguishes them, so the mitigation is
        ordering, not filtering — `supplier_rank desc` puts the real vendor
        first, and the name step is the LAST step, after VAT, business id,
        phone and domain. The second half of this test pins that ordering.
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
        """CONTROL for the test above, and the regression the sample documents
        actually hit.

        Vendors such as `Northwind`'s rep agency and `Bluebird` are
        `is_company = False` and top-level. If the name step ever goes back to filtering on
        `is_company`, this fails — and this is the half that matters, because
        the other half fails safe (nothing is picked) while this one fails by
        minting a duplicate vendor beside a real one.
        """
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
            "a top-level vendor must match even with `is_company` unset — a "
            "large part of a real vendor book is in exactly that state",
        )

    def test_a_real_vendor_outranks_a_namesake_individual(self):
        """The mitigation for what `parent_id = False` lets through.

        Two top-level partners share a name. One has bought nothing; the other
        is a vendor. The ordering is `supplier_rank desc, create_date asc`, so
        the vendor wins — and it wins even though the namesake was created
        FIRST, which is what makes this a test of the ordering rather than of
        the insertion order.
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
        """🔴 The 19.0.3.0.0 behaviour change, pinned.

        This test replaces `test_fallback_creates_with_registry_when_no_match`,
        which asserted the opposite and was correct until a sample run measured
        what it cost: most sample bills filed against a brand-new partner beside
        an active, long-standing vendor.

        CONTROL: `res.partner.search_count` is read before and after, so a
        version of the code that creates the partner under a different name — or
        in a different method — still fails here.
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
