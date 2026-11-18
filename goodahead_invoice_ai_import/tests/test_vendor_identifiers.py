# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Vendor matching on a phone number and an email/website domain, and the
deliberate refusal to invent a vendor when nothing matches.

On ordinary US invoices core's name step is often the only one that can fire,
which leads to duplicate vendors. Every test pairs "it matched" with a
control showing that something else did not. `extract_invoice` is patched to
raise, so no test can reach the Claude API. Fixture names and numbers are
fictional (`ZZ…`, 555-01xx) so they cannot collide with real partners.
"""

from unittest.mock import patch

from lxml import etree

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_ai_import.lib import claude_client
from odoo.addons.goodahead_invoice_ai_import.lib.ubl_generator import (
    generate_peppol_invoice_xml,
)
from odoo.addons.goodahead_invoice_ai_import.models.core_profile import (
    PROFILE_SEARCH_PLAN,
)

from .posting import post_bill
from .profiles import skip_method_unless_profile

NS = {
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}


def _extraction(**seller_overrides):
    """A minimal, arithmetically valid US extraction."""
    seller = {
        "name": "ZZ Unknown Trading Co",
        "vat": None,
        "business_id": None,
        "street": "1 Nowhere Road",
        "city": "Newark",
        "state_code": "NJ",
        "postal_code": "07102",
        "country_code": "US",
        "phone": None,
        "email": None,
        "website": None,
    }
    seller.update(seller_overrides)
    return {
        "type": "Invoice",
        "invoice_number": "ZZ-VID-1",
        "issue_date": "2026-10-06",
        "currency": "USD",
        "seller": seller,
        "buyer": {"name": "ZZ Buyer", "country_code": "US"},
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


class _NoClaudeMixin:
    def setUp(self):
        super().setUp()

        def _forbidden(*args, **kwargs):
            raise AssertionError(
                "a test reached the Claude extractor — nothing in this file may "
                "call the API, and a path that now does is a real defect"
            )

        guard = patch.object(
            claude_client.ClaudeInvoiceExtractor, "extract_invoice", _forbidden
        )
        guard.start()
        self.addCleanup(guard.stop)


@tagged("post_install", "-at_install")
class TestNormalisers(_NoClaudeMixin, TransactionCase):
    """Pure functions. No database state is involved, so a failure here is
    unambiguous."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Identifier = cls.env["goodahead.partner.identifier"]

    def test_real_printed_phone_formats_all_normalise(self):
        """Common printed phone formats all normalise to ten digits."""
        for printed, expected in (
            ("718.555.0121", "7185550121"),
            ("(973) 555-0130", "9735550130"),
            ("+1 212-555-0119", "2125550119"),
            ("1-833-555-0131", "8335550131"),
        ):
            self.assertEqual(
                self.Identifier._normalise_phone(printed),
                expected,
                "%r did not normalise" % printed,
            )

    def test_a_seven_digit_number_is_not_a_phone_identifier(self):
        """Too short to identify anybody: a local number without its area code
        would collide with every other local number in the country."""
        self.assertIsNone(self.Identifier._normalise_phone("555-0121"))

    def test_two_different_numbers_do_not_collapse(self):
        """Control: two different numbers stay different."""
        a = self.Identifier._normalise_phone("+1 212-555-0119")
        b = self.Identifier._normalise_phone("+1 718-555-0121")
        self.assertTrue(a and b)
        self.assertNotEqual(a, b)

    def test_a_bool_and_a_float_are_not_phone_numbers(self):
        """Bools and floats are rejected: their string forms would yield
        plausible but wrong numbers."""
        self.assertIsNone(self.Identifier._normalise_phone(True))
        self.assertIsNone(self.Identifier._normalise_phone(2125550119.0))

    def test_domains_out_of_emails_urls_and_bare_names(self):
        for printed, expected in (
            ("info@zzlibertyimport.com", "zzlibertyimport.com"),
            ("https://www.zzbluestonemfg.com/", "zzbluestonemfg.com"),
            ("Zzharborfoods.com", "zzharborfoods.com"),
            ("zzfleetcardmanager.com", "zzfleetcardmanager.com"),
            ("AR@zzexamplefleethire.com", "zzexamplefleethire.com"),
            ("http://vendor.example.org:8080/contact?x=1", "vendor.example.org"),
        ):
            self.assertEqual(
                self.Identifier._normalise_domain(printed),
                expected,
                "%r did not normalise" % printed,
            )

    def test_a_free_provider_is_rejected(self):
        """Control: free mail providers never become identifiers."""
        self.assertIsNone(self.Identifier._normalise_domain("zzvendorparts@gmail.com"))
        for public in ("x@yahoo.com", "y@outlook.com", "z@qq.com", "www.163.com"):
            self.assertIsNone(
                self.Identifier._normalise_domain(public),
                "%r must not become an identifier" % public,
            )

    def test_things_that_are_not_domains_are_rejected(self):
        for junk in ("", "n/a", "see above", "212-555-0119", True, None, 17):
            self.assertIsNone(self.Identifier._normalise_domain(junk))


@tagged("post_install", "-at_install")
class TestVendorIdentifierMatching(_NoClaudeMixin, TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Partner = cls.env["res.partner"]
        cls.Identifier = cls.env["goodahead.partner.identifier"]
        cls.AM = cls.env["account.move"]

        # The record holds a fully punctuated number and the invoice prints a bare
        # one; core's phone step compares the two as strings and fails.
        cls.liberty = cls.Partner.create(
            {
                "name": "ZZ Liberty Import",
                "is_company": True,
                "phone": "+1 212-555-0112",
                "email": "info@zzlibertyimport.com",
            }
        )
        # The record holds a phone and no email; the invoice prints a different
        # phone and an email. Nothing may match it.
        cls.fleet_hire = cls.Partner.create(
            {
                "name": "ZZ Example Fleet Hire & Hauling",
                "is_company": True,
                "phone": "+1 877-555-0166",
            }
        )

    # ------------------------------------------------------------------
    # The identifier rows themselves
    # ------------------------------------------------------------------

    def test_creating_a_partner_derives_its_identifiers(self):
        rows = self.liberty.identifier_ids
        self.assertEqual(
            sorted((r.kind, r.value) for r in rows),
            [("domain", "zzlibertyimport.com"), ("phone", "2125550112")],
        )

    def test_changing_a_phone_adds_a_row_and_removes_none(self):
        """A new phone adds a row and keeps the old one, so old bills still match."""
        before = {(r.kind, r.value) for r in self.liberty.identifier_ids}
        self.liberty.write({"phone": "(212) 555-0171"})
        after = {(r.kind, r.value) for r in self.liberty.identifier_ids}

        self.assertTrue(before < after, "the old identifiers must survive")
        self.assertIn(("phone", "2125550171"), after)

    def test_a_write_without_a_contact_field_adds_nothing(self):
        """A write that touches no contact field leaves the identifiers alone."""
        before = len(self.liberty.identifier_ids)
        self.liberty.write({"comment": "ZZ unrelated edit"})
        self.assertEqual(len(self.liberty.identifier_ids), before)

    def test_the_same_identifier_is_not_stored_twice(self):
        """The same domain from two addresses is stored once."""
        self.liberty.write({"email": "accounts@zzlibertyimport.com"})
        domains = self.liberty.identifier_ids.filtered(lambda r: r.kind == "domain")
        self.assertEqual(len(domains), 1, "one domain, two addresses")

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    def test_a_vendor_is_found_by_the_phone_the_invoice_prints(self):
        """A vendor is found by a differently formatted phone; core compares
        phone strings exactly."""
        match = self.AM._ai_match_existing_vendor(
            {"name": "ZZ Liberty Example Equipment Import (NY)Inc.",
             "phone": "212-555-0112"}
        )
        self.assertEqual(match, self.liberty)

    def test_fleet_hire_is_not_matched_on_a_phone_that_is_not_its_own(self):
        """Control: a different phone on the document does not match the vendor."""
        match = self.AM._ai_match_existing_vendor(
            {
                "name": "ZZ Example Fleet Hire",
                "phone": "718-555-0164",
                "email": "AR@zzexamplefleethire.com",
            }
        )
        self.assertFalse(match, "nothing on this document identifies the vendor")

    def test_a_vendor_is_found_by_its_email_domain(self):
        """Another mailbox on the same email domain matches."""
        match = self.AM._ai_match_existing_vendor(
            {"name": "ZZ Some Other Legal Name",
             "email": "purchasing@zzlibertyimport.com"}
        )
        self.assertEqual(match, self.liberty)

    def test_a_vendor_is_found_by_its_website_domain(self):
        """The website domain matches where the email did not."""
        harbor = self.Partner.create(
            {
                "name": "ZZ Harbor Foods",
                "is_company": True,
                "email": "orderseast@zzharborfoods.com",
            }
        )
        match = self.AM._ai_match_existing_vendor(
            {"name": "ZZ Tableware, LLC", "website": "Zzharborfoods.com"}
        )
        self.assertEqual(match, harbor)

    def test_a_shared_domain_matches_nothing(self):
        """An ambiguous domain matches nothing and the bill is left without a
        vendor."""
        for name in ("ZZ Shared One", "ZZ Shared Two"):
            self.Partner.create(
                {
                    "name": name,
                    "is_company": True,
                    "email": "ap@zzsharedholding.com",
                }
            )
        seller = {
            "name": "ZZ A Name Nobody Holds",
            "email": "invoices@zzsharedholding.com",
        }

        self.assertFalse(self.AM._ai_match_existing_vendor(seller))

        invoice = self.AM.create({"move_type": "in_invoice"})
        self.AM._create_partner_from_extraction(invoice, {"seller": seller})
        self.assertFalse(invoice.partner_id, "the bill must end with no vendor")

    def test_a_shared_phone_matches_nothing(self):
        """The same rule for a phone — a switchboard two group companies share."""
        for name in ("ZZ Switchboard A", "ZZ Switchboard B"):
            self.Partner.create(
                {"name": name, "is_company": True, "phone": "+1 201-555-0199"}
            )
        self.assertFalse(
            self.AM._ai_match_existing_vendor(
                {"name": "ZZ Nobody", "phone": "(201) 555-0199"}
            )
        )

    def test_a_child_contact_resolves_to_its_company(self):
        """A bill is filed against the commercial partner, not against the
        person whose direct line is on the letterhead."""
        self.Partner.create(
            {
                "name": "ZZ AP Clerk",
                "parent_id": self.liberty.id,
                "phone": "+1 212-555-0113",
            }
        )
        match = self.AM._ai_match_existing_vendor(
            {"name": "ZZ Anything", "phone": "212.555.0113"}
        )
        self.assertEqual(match, self.liberty)

    # ------------------------------------------------------------------
    # The plan, and the decode that uses it
    # ------------------------------------------------------------------

    # There is no search plan on the legacy core.
    @skip_method_unless_profile(PROFILE_SEARCH_PLAN)
    def test_the_new_steps_are_in_the_plan_in_the_right_place(self):
        """Position is the decision, not the presence. Below core's exact
        identity keys, above core's SUBSTRING name match."""
        plan = self.env[
            "account.edi.xml.ubl_bis3"
        ]._import_ubl_retrieve_customer_search_plan({"company": self.env.company})
        names = [getattr(step, "__name__", "") for step in plan]

        for ours in (
            "_import_retrieve_customer_from_ai_phone",
            "_import_retrieve_customer_from_ai_domain",
        ):
            self.assertIn(ours, names)
            self.assertLess(
                names.index(ours),
                names.index("_import_retrieve_customer_from_name"),
                "%s must beat core's name match" % ours,
            )
            self.assertGreater(
                names.index(ours),
                names.index("_import_retrieve_customer_from_vat"),
                "%s must never beat an exact VAT match" % ours,
            )
        self.assertLess(
            names.index("_import_retrieve_customer_from_ai_business_id"),
            names.index("_import_retrieve_customer_from_ai_phone"),
            "an exact, country-scoped business id outranks a normalised phone",
        )

    def test_the_generated_ubl_carries_the_contact_core_reads(self):
        """The generated UBL carries the seller's phone and email for core to read."""
        xml = generate_peppol_invoice_xml(
            _extraction(phone="718.555.0121", email="info@zzbluestone.com")
        )
        tree = etree.fromstring(xml)
        party = tree.find(".//cac:AccountingSupplierParty//cac:Party", NS)

        self.assertEqual(party.find(".//cbc:Telephone", NS).text, "718.555.0121")
        self.assertEqual(
            party.find(".//cbc:ElectronicMail", NS).text, "info@zzbluestone.com"
        )

    def test_no_contact_element_when_there_is_nothing_to_put_in_it(self):
        """Control: no empty `cac:Contact` when there is nothing to put in it."""
        tree = etree.fromstring(generate_peppol_invoice_xml(_extraction()))
        party = tree.find(".//cac:AccountingSupplierParty//cac:Party", NS)
        self.assertIsNone(party.find("cac:Contact", NS))

    def test_a_bool_never_becomes_a_phone_number(self):
        """A bool never becomes a contact detail."""
        tree = etree.fromstring(
            generate_peppol_invoice_xml(_extraction(phone=True, email=False))
        )
        party = tree.find(".//cac:AccountingSupplierParty//cac:Party", NS)
        self.assertIsNone(party.find("cac:Contact", NS))

    def test_the_decoder_resolves_the_vendor_by_phone(self):
        """Phone matching runs inside a real decode, on either pipeline.

        On Odoo 18.0 it runs at core's name slot; on the search-plan core as a
        plan step. Asserted before `_ai_post_process`, which does its own
        matching.
        """
        extracted = _extraction(
            name="ZZ Liberty Example Equipment Import (NY)Inc.",
            phone="212-555-0112",
        )
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
            self.liberty,
            "the decoder did not resolve the vendor by phone — the plan step "
            "did not run",
        )

    # ------------------------------------------------------------------
    # Who a bill may NEVER be filed against
    # ------------------------------------------------------------------

    def test_our_own_company_is_never_matched_on_its_own_phone(self):
        """Our own company partner is never matched, even on its own phone.

        Control: the same fixture on an ordinary partner is matched.
        """
        company_partner = self.env.company.partner_id
        self.Identifier.create(
            {
                "partner_id": company_partner.id,
                "kind": "phone",
                "value": "2125550199",
                "source": "test",
            }
        )
        seller = {"name": "ZZ Misread Buyer", "phone": "(212) 555-0199"}
        self.assertFalse(
            self.AM._ai_match_existing_vendor(seller),
            "a bill must never be filed against one of our own companies",
        )

        ordinary = self.Partner.create({"name": "ZZ Ordinary Co", "is_company": True})
        self.Identifier.create(
            {
                "partner_id": ordinary.id,
                "kind": "phone",
                "value": "2125550198",
                "source": "test",
            }
        )
        self.assertEqual(
            self.AM._ai_match_existing_vendor(
                {"name": "ZZ Whoever", "phone": "(212) 555-0198"}
            ),
            ordinary,
            "CONTROL: the same shape on an ordinary partner must still match",
        )

    def test_a_partner_we_only_ever_sell_to_is_never_matched(self):
        """A partner we only sell to is never matched as a vendor.

        The rule is `supplier_rank == 0 and customer_rank > 0`; a brand-new
        vendor has both at zero and must still match (the control below).
        """
        customer = self.Partner.create(
            {"name": "ZZ Customer Only", "is_company": True, "customer_rank": 7}
        )
        self.Identifier.create(
            {
                "partner_id": customer.id,
                "kind": "domain",
                "value": "zzcustomeronly.com",
                "source": "test",
            }
        )
        self.assertFalse(
            self.AM._ai_match_existing_vendor(
                {"name": "ZZ Whoever", "email": "ap@zzcustomeronly.com"}
            ),
            "a partner we only ever sell to must not be matched as a vendor",
        )

        brand_new = self.Partner.create(
            {"name": "ZZ Brand New Vendor", "is_company": True}
        )
        self.assertEqual(brand_new.supplier_rank, 0)
        self.assertEqual(brand_new.customer_rank, 0)
        self.Identifier.create(
            {
                "partner_id": brand_new.id,
                "kind": "domain",
                "value": "zzbrandnewvendor.com",
                "source": "test",
            }
        )
        self.assertEqual(
            self.AM._ai_match_existing_vendor(
                {"name": "ZZ Whoever", "email": "ap@zzbrandnewvendor.com"}
            ),
            brand_new,
            "CONTROL: a vendor we have never bought from yet carries both ranks "
            "at zero and MUST still match — otherwise the guard breaks the very "
            "bills this feature exists for",
        )

    def test_our_own_company_is_not_even_offered_as_a_candidate(self):
        """A bill filed against ourselves should not be one click away either.

        CONTROL: a customer IS still offered — an offer a human reads is not a
        decision, and a customer who starts invoicing us is ordinary. So this
        test fails both if our own company appears and if the candidate list
        has simply been emptied.
        """
        company_partner = self.env.company.partner_id
        self.Identifier.create(
            {
                "partner_id": company_partner.id,
                "kind": "domain",
                "value": "zzsharedprobe.com",
                "source": "test",
            }
        )
        customer = self.Partner.create(
            {"name": "ZZ Candidate Customer", "is_company": True, "customer_rank": 3}
        )
        self.Identifier.create(
            {
                "partner_id": customer.id,
                "kind": "domain",
                "value": "zzsharedprobe.com",
                "source": "test",
            }
        )
        offered = {
            candidate["partner"]
            for candidate in self.AM._ai_vendor_candidates(
                {"name": "ZZQXPROBE", "email": "ap@zzsharedprobe.com"}
            )
        }
        self.assertNotIn(company_partner, offered)
        self.assertIn(
            customer, offered, "CONTROL: a customer must still be offered"
        )


@tagged("post_install", "-at_install")
class TestNoVendorIsInvented(_NoClaudeMixin, TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Partner = cls.env["res.partner"]
        cls.AM = cls.env["account.move"]

    def test_an_unknown_vendor_leaves_the_bill_empty_and_says_so(self):
        """An unknown vendor leaves the bill without a partner and posts a note."""
        Partner = self.Partner.with_context(active_test=False)
        before = Partner.search_count([])
        invoice = self.AM.create({"move_type": "in_invoice"})

        self.AM._create_partner_from_extraction(
            invoice,
            {"seller": {"name": "ZZ Nobody Has Ever Heard Of This Co",
                        "phone": "+1 201-555-0133"}},
        )

        self.assertFalse(invoice.partner_id)
        self.assertEqual(
            Partner.search_count([]),
            before,
            "no partner may be created for an unrecognised vendor",
        )
        notes = invoice.message_ids.filtered(
            lambda m: "not recognised" in (m.body or "")
        )
        self.assertEqual(len(notes), 1, "exactly one note, naming what was read")
        self.assertIn("ZZ Nobody Has Ever Heard Of This Co", notes.body)
        self.assertIn("201-555-0133", notes.body)

    def test_a_known_vendor_still_gets_the_bill(self):
        """Control: a known vendor still gets the bill."""
        vendor = self.Partner.create(
            {"name": "ZZ Perfectly Well Known Co", "is_company": True}
        )
        invoice = self.AM.create({"move_type": "in_invoice"})

        self.AM._create_partner_from_extraction(
            invoice, {"seller": {"name": "ZZ Perfectly Well Known Co"}}
        )

        self.assertEqual(invoice.partner_id, vendor)
        self.assertFalse(
            invoice.message_ids.filtered(
                lambda m: "not recognised" in (m.body or "")
            ),
            "a matched vendor must not be reported as unrecognised",
        )

    def test_core_does_not_mint_one_either_when_the_invoice_prints_a_tax_id(self):
        """Core's own vendor-creating step is suppressed too when the document
        prints a tax id."""
        Partner = self.Partner.with_context(active_test=False)
        before = Partner.search_count([])
        extracted = _extraction(
            name="ZZ Fleet Card Bank (fuel)", vat="DE811569869",
            country_code="DE",
        )
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
        self.assertFalse(move.partner_id)
        self.assertEqual(Partner.search_count([]), before)

    def test_a_genuine_peppol_import_is_left_alone(self):
        """Control: a structured UBL import outside our context still creates a
        vendor, since the suppression is scoped to `ai_invoice_import`."""
        Partner = self.Partner.with_context(active_test=False)
        before = Partner.search_count([])
        extracted = _extraction(
            name="ZZ Genuine Peppol GmbH", vat="DE811569869", country_code="DE"
        )
        xml_bytes = self.AM._ai_generate_ubl_from_extracted(
            extracted, self.env.company
        )
        journal = self.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", self.env.company.id)],
            limit=1,
        )
        move = self.AM.create({"move_type": "in_invoice", "journal_id": journal.id})
        file_data = {
            "name": "invoice.xml",
            "raw": xml_bytes,
            "xml_tree": etree.fromstring(xml_bytes),
            "type": "xml",
            "filename": "invoice.xml",
            "mimetype": "application/xml",
            "attachment": self.env["ir.attachment"],
        }

        # NOTE: no `ai_invoice_import` on the context — this is the plain
        # core path, exactly as a document arriving over Peppol would take it.
        # On the move itself: Enterprise's OCR override calls `ensure_one()`.
        decoder = move._get_edi_decoder(file_data, new=True)
        decoder(move, file_data, True)

        self.assertTrue(
            move.partner_id, "core must still create a vendor for a real UBL file"
        )
        self.assertGreater(Partner.search_count([]), before)


@tagged("post_install", "-at_install")
class TestVendorCandidates(_NoClaudeMixin, TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Partner = cls.env["res.partner"]
        cls.AM = cls.env["account.move"]
        # The bill comes from the manufacturer while the ledger holds the rep
        # agency selling for them; no rule links the two, so a person picks it.
        cls.kestrel = cls.Partner.create(
            {
                "name": "ZZKESTREL - Midwest reps",
                "is_company": True,
                "supplier_rank": 86,
            }
        )
        cls.decoy = cls.Partner.create(
            {"name": "ZZKestrel Spare Parts Depot", "is_company": True}
        )

    def test_the_rep_agency_is_offered_first(self):
        candidates = self.AM._ai_vendor_candidates({"name": "ZZKestrel"})
        partners = [c["partner"] for c in candidates]

        self.assertIn(self.kestrel, partners)
        self.assertEqual(
            partners[0],
            self.kestrel,
            "the established vendor must sort above an incidental name match",
        )

    def test_candidates_are_ranked_by_supplier_rank(self):
        """Control: raising the decoy's rank changes the order."""
        self.decoy.supplier_rank = 900
        candidates = self.AM._ai_vendor_candidates({"name": "ZZKestrel"})
        self.assertEqual(candidates[0]["partner"], self.decoy)

    def test_the_bill_count_is_counted_not_proxied(self):
        """`bill_count` counts posted bills, not `supplier_rank`."""
        journal = self.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", self.env.company.id)],
            limit=1,
        )
        bill = self.AM.create(
            {
                "move_type": "in_invoice",
                "partner_id": self.kestrel.id,
                "journal_id": journal.id,
                "invoice_date": "2026-10-01",
                "invoice_line_ids": [
                    (0, 0, {"name": "a line", "quantity": 1, "price_unit": 10.0}),
                ],
            }
        )
        post_bill(bill)
        # A DRAFT bill must not be counted — only posted ones are history.
        self.AM.create(
            {
                "move_type": "in_invoice",
                "partner_id": self.kestrel.id,
                "journal_id": journal.id,
                "invoice_date": "2026-10-02",
                "invoice_line_ids": [
                    (0, 0, {"name": "a line", "quantity": 1, "price_unit": 10.0}),
                ],
            }
        )

        candidates = self.AM._ai_vendor_candidates({"name": "ZZKestrel"})
        found = next(c for c in candidates if c["partner"] == self.kestrel)

        self.assertEqual(found["bill_count"], 1)
        self.assertNotEqual(
            found["bill_count"],
            self.kestrel.supplier_rank,
            "the count is bills, not supplier_rank",
        )

    def test_an_archived_candidate_is_offered_and_marked(self):
        """An archived candidate is offered and marked as archived."""
        self.decoy.active = False
        candidates = self.AM._ai_vendor_candidates({"name": "ZZKestrel Spare"})
        by_partner = {c["partner"]: c for c in candidates}

        self.assertIn(self.decoy, by_partner, "an archived vendor must be offered")
        self.assertTrue(by_partner[self.decoy]["archived"])
        self.assertFalse(
            by_partner[self.kestrel]["archived"],
            "an active vendor must not be marked archived",
        )

    def test_a_child_contact_core_could_have_matched_is_still_offered(self):
        """A child contact whose name contains the printed name is offered via
        its commercial partner.

        The legacy name step is narrowed to an exact match, so the candidate
        list must cover what core's substring step could have matched. The
        commercial partner is offered rather than the contact, so the card shows
        its real bill count and a bill is never filed against a sub-contact.
        """
        parent = self.Partner.create(
            {"name": "ZZ Hudson Holdings Inc", "is_company": True, "supplier_rank": 3}
        )
        child = self.Partner.create(
            {"name": "ZZQXSEVEN Brooklyn Depot", "parent_id": parent.id}
        )
        self.assertTrue(child.parent_id, "CONTROL: it really is a child contact")
        self.assertNotIn(
            "ZZQXSEVEN",
            parent.name,
            "CONTROL 1: the parent can only be reached through the contact",
        )

        seller = {"name": "ZZQXSEVEN"}
        candidates = self.AM._ai_vendor_candidates(seller)
        by_partner = {c["partner"]: c for c in candidates}

        self.assertIn(
            parent,
            by_partner,
            "core's substring step could have matched this contact and "
            "filed the bill against it; the candidate list must offer the "
            "party that bill belongs to",
        )
        self.assertNotIn(
            child, by_partner, "the sub-contact itself is not a billable party"
        )

        # CONTROL 3 — the parent's real history travels with the offer.
        journal = self.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", self.env.company.id)],
            limit=1,
        )
        bill = self.AM.create(
            {
                "move_type": "in_invoice",
                "partner_id": child.id,
                "journal_id": journal.id,
                "invoice_date": "2026-10-01",
                "invoice_line_ids": [
                    (0, 0, {"name": "a line", "quantity": 1, "price_unit": 10.0}),
                ],
            }
        )
        post_bill(bill)
        offered = self.AM._ai_vendor_candidates(seller)
        found = next(c for c in offered if c["partner"] == parent)
        self.assertEqual(
            found["bill_count"],
            1,
            "a contact's card used to read 0 bills however established its "
            "parent — which reads as 'new vendor, safe to skip'",
        )

        # CONTROL 2 — a top-level partner with the same resemblance is still
        # offered, and an established one still sorts first.
        top = self.Partner.create(
            {"name": "ZZQXSEVEN Supply Co", "is_company": True, "supplier_rank": 50}
        )
        partners = [c["partner"] for c in self.AM._ai_vendor_candidates(seller)]
        self.assertIn(top, partners)
        self.assertEqual(
            partners[0], top, "an established top-level vendor still sorts first"
        )

        # CONTROL 4 — offering is not choosing, and the matcher still works.
        self.assertNotEqual(
            self.env["res.partner"]._ai_find_vendor({"name": "ZZQXSEVEN Brooklyn Depot"}),
            child,
            "a child contact is never CHOSEN as the bill's vendor",
        )
        self.assertEqual(
            self.env["res.partner"]._ai_find_vendor({"name": "ZZ Hudson Holdings Inc"}),
            parent,
            "CONTROL: and the matcher has not simply stopped matching",
        )

    def test_a_shared_domain_still_produces_candidates(self):
        """A domain shared by several vendors is not a match but still yields
        candidates."""
        for name in ("ZZ Group Member One", "ZZ Group Member Two"):
            self.Partner.create(
                {"name": name, "is_company": True, "email": "ap@zzgroupshared.com"}
            )
        # Nonsense words, so the loose name step (longest word, `ilike`) cannot
        # find an unrelated partner.
        seller = {
            "name": "ZZQXNINE ZZNOSUCH",
            "email": "billing@zzgroupshared.com",
        }

        self.assertFalse(self.AM._ai_match_existing_vendor(seller))

        candidates = self.AM._ai_vendor_candidates(seller)
        self.assertEqual(len(candidates), 2)
        for candidate in candidates:
            self.assertEqual(candidate["reason"], "same domain")


@tagged("post_install", "-at_install")
class TestTheSeed(_NoClaudeMixin, TransactionCase):
    """The seed creates identifier rows for partners that existed before install."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Partner = cls.env["res.partner"]
        cls.Identifier = cls.env["goodahead.partner.identifier"]

    def test_the_seed_derives_identifiers_for_a_partner_that_has_none(self):
        partner = self.Partner.create(
            {
                "name": "ZZ Seed Probe",
                "is_company": True,
                "phone": "+1 973-555-0158",
                "website": "https://www.zzseedprobe.com/contact",
            }
        )
        # Put it back in the state an upgrade finds: the row exists, the
        # identifiers do not.
        partner.identifier_ids.unlink()
        self.assertFalse(partner.identifier_ids)

        self.Identifier._seed_from_partners()
        partner.invalidate_recordset(["identifier_ids"])

        self.assertEqual(
            sorted((r.kind, r.value) for r in partner.identifier_ids),
            [("domain", "zzseedprobe.com"), ("phone", "9735550158")],
        )

    def test_the_seed_never_stores_a_free_provider_domain(self):
        """Control: the seed never stores a free provider domain."""
        partner = self.Partner.create(
            {
                "name": "ZZ Seed Gmail Probe",
                "is_company": True,
                "phone": "+1 973-555-0130",
                "email": "zzvendorparts@gmail.com",
            }
        )
        partner.identifier_ids.unlink()

        self.Identifier._seed_from_partners()
        partner.invalidate_recordset(["identifier_ids"])

        kinds = partner.identifier_ids.mapped("kind")
        self.assertIn("phone", kinds)
        self.assertNotIn("domain", kinds)

    def test_the_seed_is_idempotent(self):
        """The seed is idempotent, so running it again is harmless."""
        partner = self.Partner.create(
            {"name": "ZZ Seed Twice", "is_company": True, "phone": "+1 833-555-0131"}
        )
        partner.identifier_ids.unlink()

        self.Identifier._seed_from_partners()
        partner.invalidate_recordset(["identifier_ids"])
        first = len(partner.identifier_ids)

        self.Identifier._seed_from_partners()
        partner.invalidate_recordset(["identifier_ids"])

        self.assertEqual(first, 1)
        self.assertEqual(len(partner.identifier_ids), 1)

