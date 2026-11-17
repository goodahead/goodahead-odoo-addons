# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""A document's vendor is never one of our own companies, and neither is its bank.

When the seller prints a phone that the company's own partner also carries,
core's legacy partner chain can resolve the vendor to the company partner on
its `company_id = False` pass, and the seller's IBAN then follows the vendor.
The guard therefore runs at core's partner-resolution seam, with the bank hook
as a second line of defence.

The guard is scoped to `ai_invoice_import`: in a multi-company database where
one company invoices the other, a structured intercompany document's
counterparty legitimately is another company's partner.

Key controls: without our context core still picks the company partner (so
the match is real), a genuine vendor still matches, and a genuine vendor's
IBAN is still recorded.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.base.models.res_bank import sanitize_account_number
from odoo.addons.goodahead_invoice_ai_import.lib.ubl_generator import (
    generate_peppol_invoice_xml,
)
from odoo.addons.goodahead_invoice_ai_import.models.core_profile import PROFILE_LEGACY

from .profiles import skip_method_unless_profile

#: Put on the company partner in `setUpClass` and printed as the seller's phone
#: on the document.
TRAP_PHONE = "+358400000999"

#: A valid German VAT that belongs to nobody here, so the VAT step cannot match
#: and resolution reaches the phone step.
UNKNOWN_VAT = "DE811569869"

#: A name that matches nothing, so core's name step cannot fire either.
TRAP_NAME = "ZZ Own Company Trap OU"

#: A valid Belgian IBAN (the canonical documentation example).
TRAP_IBAN = "BE68 5390 0754 7034"

#: A second valid IBAN, for the genuine-vendor control.
OTHER_IBAN = "BE71 0961 2345 6769"


class _OwnCompanyTrapHarness(TransactionCase):
    """A document whose seller shares a contact key with our own company."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.company.claude_api_key = "test-key"
        cls.company_partner = cls.company.partner_id
        # After this write, core's phone step resolves TRAP_PHONE to the company
        # partner alone on its `company_id = False` pass.
        cls.company_partner.phone = TRAP_PHONE
        cls.Partner = cls.env["res.partner"]
        cls.Bank = cls.env["res.partner.bank"]
        cls.AM = cls.env["account.move"]
        cls.EdiCommon = cls.env["account.edi.common"]
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", cls.company.id)],
            limit=1,
        )

    # -- fixtures ------------------------------------------------------

    def _seller(self, name=TRAP_NAME, vat=UNKNOWN_VAT, phone=TRAP_PHONE,
                iban=TRAP_IBAN):
        """The `seller` block alone, as the matchers expect it."""
        seller = {
            "name": name,
            "vat": vat,
            "country_code": "DE",
            "city": "Berlin",
            "street": "Beispielweg 1",
        }
        if phone:
            seller["phone"] = phone
        if iban:
            seller["iban"] = iban
        return seller

    def _extracted(self, **seller_kwargs):
        """The whole extraction, built around `_seller(**seller_kwargs)`."""
        return {
            "type": "Invoice",
            "invoice_number": "ZZ-OWNCO-1",
            "issue_date": "2026-09-30",
            "currency": "EUR",
            "seller": self._seller(**seller_kwargs),
            "buyer": {
                "name": self.company.name,
                "country_code": self.company.country_id.code or "FI",
            },
            "lines": [
                {
                    "product_code": "ZZ-OC-1",
                    "name": "ZZ Widget",
                    "quantity": 1.0,
                    "unit_code": "C62",
                    "price_unit": 100.0,
                    "line_total": 100.0,
                    "tax_percent": 0.0,
                    "tax_amount": 0.0,
                    "tax_category": "Z",
                }
            ],
            "tax_subtotals": [
                {
                    "tax_percent": 0.0,
                    "taxable_amount": 100.0,
                    "tax_amount": 0.0,
                    "tax_category": "Z",
                }
            ],
            "totals": {
                "line_extension": 100.0,
                "tax_exclusive": 100.0,
                "tax_inclusive": 100.0,
                "payable": 100.0,
            },
        }

    def _run_import(self, extracted=None):
        extracted = extracted or self._extracted()
        attachment = self.env["ir.attachment"].create(
            {
                "name": "zz-ownco.pdf",
                "raw": b"%PDF-1.4 fake",
                "mimetype": "application/pdf",
            }
        )
        record = self.env["ai.invoice.import"].create(
            {
                "name": "zz-ownco.pdf",
                "company_id": self.company.id,
                "pdf_attachment_id": attachment.id,
            }
        )
        with patch.object(
            type(self.AM),
            "_ai_extract_and_generate_ubl",
            return_value={
                "extracted": extracted,
                "usage": {"input_tokens": 10, "output_tokens": 20},
                "model_name": "test-model",
                "xml_bytes": generate_peppol_invoice_xml(extracted),
            },
        ):
            outcome = record._process_import()
        return record, outcome

    def _import_partner(self, ours=True, **overrides):
        """Call core's legacy seam the way `_import_fill_invoice` calls it."""
        vals = {
            "name": TRAP_NAME,
            "phone": TRAP_PHONE,
            "email": None,
            "vat": UNKNOWN_VAT,
            "postal_address": {"country_code": "DE", "city": "Berlin"},
        }
        vals.update(overrides)
        edi = (
            self.EdiCommon.with_context(ai_invoice_import=True)
            if ours
            else self.EdiCommon
        )
        return edi._import_partner(self.company, **vals)

    def _bank_rows_for(self, partner, number=TRAP_IBAN):
        return self.Bank.with_context(active_test=False).search(
            [
                ("sanitized_acc_number", "=", sanitize_account_number(number)),
                ("partner_id", "=", partner.id),
            ]
        )

    def _draft_bill(self, partner=None, move_type="in_invoice"):
        return self.AM.create(
            {
                "move_type": move_type,
                "journal_id": self.journal.id,
                "partner_id": partner.id if partner else False,
            }
        )

    def _genuine_vendor(self, **overrides):
        vals = {
            "name": "ZZ Genuine Vendor GmbH",
            "is_company": True,
            "phone": "+4930555000111",
            "vat": UNKNOWN_VAT,
            "country_id": self.env.ref("base.de").id,
        }
        vals.update(overrides)
        return self.Partner.create(vals)

    def _other_company(self):
        """A second company, so "ours" can be told apart from "any company".

        An existing one is preferred. Otherwise one is created with
        `current_test` muted: during tests `stock` auto-creates a warehouse for
        a new company that can fail on databases it did not seed.
        """
        existing = (
            self.env["res.company"]
            .sudo()
            .search([("id", "!=", self.company.id)], limit=1)
        )
        if existing:
            return existing

        from odoo.modules import module as _module

        previous = _module.current_test
        _module.current_test = False
        try:
            return self.env["res.company"].create({"name": "ZZ Other Co Oy"})
        finally:
            _module.current_test = previous


@tagged("post_install", "-at_install")
class TestTheVendorIsNeverOurOwnCompany(_OwnCompanyTrapHarness):
    """Partner resolution never returns our own company partner."""

    @skip_method_unless_profile(PROFILE_LEGACY)
    def test_a_full_import_never_files_the_bill_against_our_own_company(self):
        record, outcome = self._run_import()

        self.assertTrue(outcome.startswith("OK"), outcome)
        move = record.move_id
        self.assertTrue(move, "the bill itself must still be created")
        self.assertNotEqual(
            move.partner_id,
            self.company_partner,
            "a document's vendor must never be one of our own companies",
        )
        self.assertFalse(
            move.partner_id,
            "nothing else matched this seller, so the bill is left for a human",
        )

    @skip_method_unless_profile(PROFILE_LEGACY)
    def test_a_full_import_creates_no_bank_account_on_our_own_company(self):
        before = self._bank_rows_for(self.company_partner)
        self.assertFalse(before, "fixture check: the trap IBAN is not there yet")

        record, _outcome = self._run_import()

        self.assertFalse(
            self._bank_rows_for(self.company_partner),
            "the seller's IBAN must not be recorded against our own company",
        )
        self.assertFalse(
            record.move_id.partner_bank_id,
            "and the bill must not point at one either",
        )

    def test_the_legacy_seam_refuses_the_company_partner(self):
        partner, logs = self._import_partner()

        self.assertNotEqual(partner, self.company_partner)
        self.assertFalse(partner, "an empty res.partner, not False and not None")
        self.assertEqual(partner._name, "res.partner", "the return shape is the trap")
        self.assertTrue(logs, "the refusal has to be visible to the accountant")

    def test_the_refusal_does_not_depend_on_a_printed_tax_id(self):
        """The refusal applies even when the document prints no tax id, since
        core's phone step does not need one."""
        partner, _logs = self._import_partner(vat=None)

        self.assertNotEqual(partner, self.company_partner)
        self.assertFalse(partner)

    def test_our_own_matcher_never_returns_a_company_partner_on_its_phone_step(self):
        """Our matcher's phone step never returns the company partner."""
        seller = self._extracted()["seller"]

        match = self.Partner._ai_find_vendor(seller)

        self.assertNotEqual(match, self.company_partner)
        self.assertFalse(match)

    def test_our_own_matcher_never_returns_a_company_partner_on_its_vat_step(self):
        """Our matcher's VAT step never returns the company partner."""
        # A German VAT so it cannot collide with existing partners' VATs.
        self.company_partner.vat = UNKNOWN_VAT
        seller = self._seller(vat=self.company_partner.vat, phone=None)
        # `vat` is a commercial field and propagates to child contacts, so assert
        # that every row it reaches belongs to our own party.
        reached = self.Partner.search([("vat", "=ilike", seller["vat"])])
        self.assertIn(self.company_partner, reached, "fixture check: the key reaches us")
        self.assertEqual(
            reached.commercial_partner_id,
            self.company_partner,
            "fixture check: every row the printed VAT reaches is our own party",
        )

        match = self.Partner._ai_find_vendor(seller)

        self.assertNotEqual(match, self.company_partner)
        self.assertFalse(match)

    def test_our_own_matcher_never_returns_a_company_partner_on_its_name_step(self):
        """Our matcher's name step never returns the company partner."""
        # Rename the company to a sentinel so the name is unambiguous; the write
        # is rolled back with the test.
        self.company_partner.name = "ZZ Our Own Company Oy"
        seller = self._seller(name=self.company_partner.name, vat=None, phone=None)
        self.assertEqual(
            self.Partner.search(
                [("name", "=ilike", seller["name"]), ("parent_id", "=", False)]
            ),
            self.company_partner,
            "fixture check: the printed name resolves to the company and nobody else",
        )

        match = self.Partner._ai_find_vendor(seller)

        self.assertNotEqual(match, self.company_partner)
        self.assertFalse(match)

    def test_the_company_partner_is_not_offered_as_a_candidate_by_name(self):
        """The company partner is never offered as a vendor candidate by name."""
        self.company_partner.name = "ZZ Our Own Company Oy"
        seller = self._seller(name=self.company_partner.name, phone=None)

        candidates = self.AM._ai_vendor_candidates(seller, company=self.company)

        self.assertNotIn(
            self.company_partner,
            [c["partner"] for c in candidates],
            "our own company is never a candidate vendor",
        )

    def test_control_a_name_that_resembles_a_real_vendor_is_still_offered(self):
        """Control: a genuine vendor resembling the seller is still offered."""
        vendor = self._genuine_vendor()
        seller = self._seller(name="ZZ Genuine", vat=None, phone=None)

        candidates = self.AM._ai_vendor_candidates(seller, company=self.company)

        self.assertIn(vendor, [c["partner"] for c in candidates])

    # -- controls ------------------------------------------------------

    def test_control_without_our_context_core_still_picks_the_company_partner(self):
        """Control: without our context, core still picks the company partner.

        Proves the match is real and that the override is what changes the
        answer; core's behaviour on other documents is deliberately unchanged.
        """
        partner, _logs = self._import_partner(ours=False)

        self.assertEqual(
            partner,
            self.company_partner,
            "if this stops being true the other tests prove nothing",
        )

    def test_control_a_genuine_vendor_still_matches(self):
        vendor = self._genuine_vendor()

        partner, _logs = self._import_partner(phone=vendor.phone, vat=None)

        self.assertEqual(partner, vendor, "refusing the company is not refusing everybody")


@tagged("post_install", "-at_install")
class TestTheGuardIsScopedToTheBillsOwnCompany(_OwnCompanyTrapHarness):
    """The guard protects the bill's own company, not every company.

    The rule:

      1. refuse the bill's own company partner and its contact tree;
      2. refuse any partner carrying a `res.users` row;
      3. refuse a contact owned by another company (`company_id` set to it);
      4. allow another company's own partner (a shared contact), since
         intercompany invoices are legitimate.
    """

    # -- rule 1: the ancestry, not just the partner --------------------

    def test_a_child_of_the_company_partner_is_refused(self):
        """Contacts below the company partner are refused, including a grandchild
        whose parent has `is_company = True` (its own commercial partner)."""
        child = self.Partner.create(
            {"name": "ZZ Dept Of Ours", "is_company": True,
             "parent_id": self.company_partner.id}
        )
        grandchild = self.Partner.create(
            {"name": "ZZ Desk Of Ours", "parent_id": child.id}
        )
        self.assertEqual(
            grandchild.commercial_partner_id, child,
            "fixture check: commercial_partner_id stops at the is_company child",
        )

        for partner in (child, grandchild):
            self.assertEqual(
                self.Partner._ai_impossible_vendor_reason(
                    partner, company=self.company
                ),
                "own_company",
                "a contact in our own company's tree is never a vendor",
            )

    def test_control_a_vendor_with_a_parent_company_of_its_own_is_accepted(self):
        """Control: a vendor and its child contacts are accepted."""
        vendor = self._genuine_vendor()
        contact = self.Partner.create(
            {"name": "ZZ AP Desk", "parent_id": vendor.id}
        )

        for partner in (vendor, contact):
            self.assertFalse(
                self.Partner._ai_impossible_vendor_reason(
                    partner, company=self.company
                ),
                "an ordinary vendor and its contacts are perfectly good vendors",
            )

    # -- rule 2: a login is never a vendor -----------------------------

    def test_a_partner_carrying_a_user_is_refused(self):
        """A partner carrying a user is refused."""
        vendor = self._genuine_vendor()
        self.assertFalse(
            self.Partner._ai_impossible_vendor_reason(vendor, company=self.company),
            "fixture check: this partner is accepted BEFORE it has a login — so "
            "the test is about the user and not about the partner",
        )

        self.env["res.users"].create(
            {"name": vendor.name, "login": "zz-ownco-vendor-login",
             "partner_id": vendor.id}
        )

        self.assertEqual(
            self.Partner._ai_impossible_vendor_reason(vendor, company=self.company),
            "own_user",
        )

    def test_a_vendor_whose_child_contact_has_a_user_is_still_accepted(self):
        """A login on a vendor's child contact does not refuse the vendor: the
        ancestry walk only goes upward."""
        vendor = self._genuine_vendor()
        contact = self.Partner.create(
            {"name": "ZZ Portal Contact", "parent_id": vendor.id}
        )
        self.env["res.users"].create(
            {"name": contact.name, "login": "zz-ownco-portal-contact",
             "partner_id": contact.id}
        )

        self.assertFalse(
            self.Partner._ai_impossible_vendor_reason(vendor, company=self.company),
            "a login on a vendor's contact says nothing about the vendor",
        )

    # -- rules 3 and 4: ownership, and the intercompany case -----------

    def test_a_contact_owned_by_another_company_is_refused(self):
        """A contact owned solely by another company is refused."""
        other = self._other_company()
        theirs = self._genuine_vendor(
            name="ZZ Theirs Only Oy", company_id=other.id, vat=None, phone=None
        )

        self.assertEqual(
            self.Partner._ai_impossible_vendor_reason(theirs, company=self.company),
            "other_company_contact",
        )

    def test_control_the_same_contact_shared_is_accepted(self):
        """Control: the same contact with `company_id` cleared is accepted."""
        other = self._other_company()
        shared = self._genuine_vendor(
            name="ZZ Theirs Only Oy", company_id=other.id, vat=None, phone=None
        )
        self.assertEqual(
            self.Partner._ai_impossible_vendor_reason(shared, company=self.company),
            "other_company_contact",
            "fixture check: it is refused while it is owned",
        )

        shared.company_id = False

        self.assertFalse(
            self.Partner._ai_impossible_vendor_reason(shared, company=self.company),
            "a shared contact is reachable from every company",
        )

    def test_control_another_companys_own_partner_is_accepted(self):
        """Another company's own partner is accepted (the intercompany case)."""
        other = self._other_company()

        self.assertTrue(other.partner_id, "fixture check: the company has a partner")
        self.assertFalse(
            other.partner_id.company_id,
            "fixture check: a company's own partner is a SHARED contact, which "
            "is what makes it reachable under the ownership rule",
        )
        self.assertFalse(
            self.Partner._ai_impossible_vendor_reason(
                other.partner_id, company=self.company
            ),
            "another company's partner is a legitimate vendor",
        )

    def test_the_guard_follows_the_BILLS_company_not_the_environments(self):
        """The guard follows the bill's company, not `self.env.company`.

        The import runs in a queue job, so the environment's company is not
        reliably the bill's. Both directions are asserted.
        """
        other = self._other_company()

        self.assertEqual(
            self.Partner._ai_impossible_vendor_reason(
                self.company_partner, company=self.company
            ),
            "own_company",
        )
        self.assertFalse(
            self.Partner._ai_impossible_vendor_reason(
                self.company_partner, company=other
            ),
            "from the OTHER company's point of view, our partner is just a party",
        )

    # -- the ordering -------------------------------------------------

    def test_the_current_companys_contact_wins_over_an_equally_good_shared_one(self):
        """With two equally good name matches, the current company's own contact
        wins over a shared one."""
        shared = self._genuine_vendor(
            name="ZZ Tie Break Oy", vat=None, phone=None, company_id=False
        )
        ours = self._genuine_vendor(
            name="ZZ Tie Break Oy", vat=None, phone=None, company_id=self.company.id
        )
        self.assertNotEqual(shared, ours, "fixture check: two distinct rows")

        match = self.Partner._ai_find_vendor(
            self._seller(name="ZZ Tie Break Oy", vat=None, phone=None),
            company=self.company,
        )

        self.assertEqual(
            match, ours, "the current company's contact is checked first"
        )

    def test_control_with_no_current_company_match_the_shared_one_is_still_found(self):
        """Control: without a current-company match the shared contact is found."""
        shared = self._genuine_vendor(
            name="ZZ Tie Break Oy", vat=None, phone=None, company_id=False
        )

        match = self.Partner._ai_find_vendor(
            self._seller(name="ZZ Tie Break Oy", vat=None, phone=None),
            company=self.company,
        )

        self.assertEqual(match, shared, "a shared contact is still a match")

    # -- every matcher step applies the refusal ------------------------------

    def test_the_business_id_step_refuses_the_company_partner(self):
        """The business-id step refuses the company partner."""
        self.company_partner.company_registry = "ZZ-OWNCO-REG-1"
        self.company_partner.country_id = self.env.ref("base.fi").id
        seller = self._seller(
            name="ZZ Nothing Like Us", vat=None, phone=None
        )
        seller["business_id"] = "ZZ-OWNCO-REG-1"
        seller["country_code"] = "FI"
        self.assertEqual(
            self.Partner.search(
                self.Partner._ai_vendor_registry_domain("ZZ-OWNCO-REG-1", "FI")
            ).commercial_partner_id,
            self.company_partner,
            "fixture check: that registration number reaches us and nobody else",
        )

        match = self.Partner._ai_find_vendor(seller, company=self.company)

        self.assertNotEqual(match, self.company_partner)
        self.assertFalse(match)

    def test_the_import_path_caller_is_given_the_bills_company(self):
        """`_create_partner_from_extraction` passes the bill's company.

        Checked by recording what the matcher is asked, since the answer is the
        same on a one-company database.
        """
        seen = []
        original = type(self.Partner)._ai_find_vendor

        def recording(this, seller, company=None):
            seen.append(company)
            return original(this, seller, company=company)

        bill = self._draft_bill(None)
        with patch.object(type(self.Partner), "_ai_find_vendor", recording):
            self.AM._create_partner_from_extraction(
                bill, {"seller": self._seller(vat=None, phone=None)}
            )

        self.assertTrue(seen, "the matcher was reached at all")
        self.assertEqual(
            seen[0],
            bill.company_id,
            "the matcher is asked about the BILL's company, not the env's",
        )

    # -- the fetch window ----------------------------------------------------

    def test_a_refused_row_does_not_consume_the_fetch_window(self):
        """Refused rows do not use up the fetch window.

        Otherwise a legitimate vendor behind twenty refused rows would never be
        seen and the bill would match nothing. Uses the business-id step (no
        `parent_id` leaf) and `company_registry`, a commercial field inherited
        by the company partner's 25 child contacts, all refused by rule 1. The
        eligible vendor is created last so it sorts behind them.
        """
        registry = "ZZ-WINDOW-REG-1"
        country = self.env.ref("base.de")
        self.company_partner.write(
            {"company_registry": registry, "country_id": country.id}
        )
        for index in range(25):
            self.Partner.create(
                {
                    "name": "ZZ Filler %s" % index,
                    "parent_id": self.company_partner.id,
                }
            )
        domain = self.Partner._ai_vendor_registry_domain(registry, "DE")
        reachable = self.Partner.search(domain)
        self.assertGreater(
            len(reachable),
            20,
            "fixture check: more rows reach the key than the fetch window holds",
        )
        self.assertEqual(
            reachable.commercial_partner_id,
            self.company_partner,
            "fixture check: every one of them is our own party, so rule 1 "
            "refuses all of them",
        )
        eligible = self.Partner.create(
            {
                "name": "ZZ Eligible Beyond The Window",
                "is_company": True,
                "company_registry": registry,
                "country_id": country.id,
            }
        )

        match = self.Partner._ai_vendor_search(
            domain,
            company=self.company,
            order="supplier_rank desc, create_date asc, id asc",
        )

        self.assertEqual(
            match,
            eligible,
            "the eligible vendor behind the refused rows is still found",
        )

    def test_control_an_eligible_vendor_inside_the_window_costs_the_same_searches(self):
        """Control: the answer and the number of searches are unchanged.

        One fetch per ownership pass, whether or not the window is full, so the
        fix is a domain term and not a paging loop.
        """
        registry = "ZZ-NARROW-REG-1"
        country = self.env.ref("base.de")
        eligible = self.Partner.create(
            {
                "name": "ZZ Narrow Window Oy",
                "is_company": True,
                "company_registry": registry,
                "country_id": country.id,
            }
        )
        domain = self.Partner._ai_vendor_registry_domain(registry, "DE")

        # Count only searches mentioning the sentinel registration number: the
        # guard's own bookkeeping queries (contact tree, logins) are excluded.
        counted = []
        original = type(self.Partner)._search

        def counting(this, dom, *args, **kwargs):
            if this._name == "res.partner" and registry in str(dom):
                counted.append(dom)
            return original(this, dom, *args, **kwargs)

        with patch.object(type(self.Partner), "_search", counting):
            match = self.Partner._ai_vendor_search(domain, company=self.company)
        inside = len(counted)

        self.assertEqual(match, eligible, "the plain case still answers")
        self.assertEqual(
            inside, 2, "one fetch per ownership pass, and there are two passes"
        )

        # With the window full it is still two: refused rows are excluded by the
        # search itself.
        self.company_partner.write(
            {"company_registry": registry, "country_id": country.id}
        )
        for index in range(25):
            self.Partner.create(
                {
                    "name": "ZZ Narrow Filler %s" % index,
                    "parent_id": self.company_partner.id,
                }
            )
        self.assertGreater(
            self.Partner.search_count(domain),
            20,
            "fixture check: the window really is full this time",
        )
        counted.clear()
        with patch.object(type(self.Partner), "_search", counting):
            match = self.Partner._ai_vendor_search(domain, company=self.company)

        self.assertEqual(match, eligible, "still the eligible one")
        self.assertEqual(
            len(counted),
            inside,
            "and it cost the same number of fetches as the empty case — a "
            "paging loop would have had to go round again",
        )

    def test_control_the_post_filter_is_still_the_authority(self):
        """The post-filter still refuses what the domain narrowing misses."""
        with patch.object(
            type(self.Partner), "_ai_refused_vendor_domain", return_value=[]
        ):
            match = self.Partner._ai_vendor_search(
                [("id", "=", self.company_partner.id)], company=self.company
            )

        self.assertFalse(
            match, "with the domain disarmed, the filter still refuses us"
        )

    def test_a_contact_owned_by_another_company_never_wins_the_name_step(self):
        """Rule 3 inside the matcher, not just inside the predicate."""
        other = self._other_company()
        self._genuine_vendor(
            name="ZZ Only Theirs Oy", vat=None, phone=None, company_id=other.id
        )

        match = self.Partner._ai_find_vendor(
            self._seller(name="ZZ Only Theirs Oy", vat=None, phone=None),
            company=self.company,
        )

        self.assertFalse(
            match, "a contact owned solely by another company is never the vendor"
        )


@tagged("post_install", "-at_install")
class TestTheBankHookIsTheSecondLineOfDefence(_OwnCompanyTrapHarness):
    """`account.edi.common._import_partner_bank` on purchase documents.

    Core picks the account holder from `invoice.is_inbound()`. The guards (no
    vendor, or a vendor that is one of our companies) apply to every purchase
    document; sale documents and a correctly resolved vendor on an
    `in_invoice` keep core's behaviour, including `partner_bank_id`.
    """

    def test_no_bank_row_when_the_vendor_is_our_own_company(self):
        bill = self._draft_bill(self.company_partner)

        self.EdiCommon._import_partner_bank(bill, [TRAP_IBAN])

        self.assertFalse(
            self._bank_rows_for(self.company_partner),
            "a vendor's IBAN is never recorded against our own company",
        )
        # Core's compute may legitimately offer the company partner's own
        # account; what must not happen is the document's number landing there.
        self.assertNotEqual(
            bill.partner_bank_id.sanitized_acc_number,
            sanitize_account_number(TRAP_IBAN),
            "the number printed on the document never reaches partner_bank_id",
        )

    def test_no_bank_row_and_no_crash_when_the_bill_has_no_vendor(self):
        """A bill with no vendor gets no bank row and does not crash."""
        bill = self._draft_bill(None)

        self.EdiCommon._import_partner_bank(bill, [TRAP_IBAN])

        self.assertFalse(
            self.Bank.with_context(active_test=False).search(
                [("sanitized_acc_number", "=", sanitize_account_number(TRAP_IBAN))]
            ),
            "no vendor means no account holder, so nothing is created",
        )
        self.assertFalse(bill.partner_bank_id)

    # -- controls ------------------------------------------------------

    def test_control_a_genuine_vendors_iban_is_still_recorded(self):
        vendor = self._genuine_vendor()
        bill = self._draft_bill(vendor)

        self.EdiCommon._import_partner_bank(bill, [OTHER_IBAN])

        rows = self._bank_rows_for(vendor, OTHER_IBAN)
        self.assertEqual(len(rows), 1, "the vendor's own IBAN still lands on the vendor")
        self.assertEqual(
            bill.partner_bank_id,
            rows,
            "and core still fills partner_bank_id on a bill we pay",
        )

    def test_control_a_received_credit_note_still_goes_to_the_vendor(self):
        """A received credit note's IBAN still goes to the vendor."""
        vendor = self._genuine_vendor()
        note = self._draft_bill(vendor, move_type="in_refund")

        self.EdiCommon._import_partner_bank(note, [OTHER_IBAN])

        self.assertEqual(
            len(self._bank_rows_for(vendor, OTHER_IBAN)),
            1,
            "a received credit note's account is the vendor's",
        )
        self.assertFalse(
            self._bank_rows_for(self.company_partner, OTHER_IBAN),
            "and never ours",
        )
