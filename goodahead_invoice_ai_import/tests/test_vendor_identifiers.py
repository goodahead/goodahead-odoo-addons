# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Vendor matching on a phone number and an email/website domain, and the
deliberate refusal to invent a vendor when nothing matches (19.0.3.0.0).

🔴 WHAT THIS IS BUILT FROM. A set of sample vendor PDFs was put through the
import: every one extracted correctly and most minted a duplicate vendor,
because on an ordinary US invoice core's EXACT name step is the only one that
can fire. Measured on a migrated test database's vendors: most have a phone,
about half an email, and next to none a bank account or a VAT.

🔴 EVERY TEST HERE CARRIES A CONTROL — something that would FAIL if the thing
under test were stubbed out or made unconditional. This project's recurring
defect is a check that reads a proxy and fails in the direction that looks like
success, so "it matched" is never asserted without "and this one did not".

🔴 NOTHING HERE MAY REACH THE CLAUDE API. `setUp` replaces
`ClaudeInvoiceExtractor.extract_invoice` with a function that raises, for every
test in every class below, so a refactor that moved a real call into this path
could not pass quietly. ⚠️ The older `lib.claude_client.requests` patch named in
some notes no longer exists and must not be reintroduced: this module has owned
no HTTP transport since 19.0.1.2.0 — `goodahead_claude` does — and a patch that
silently stops matching is how you find out you have been billing yourself in a
test suite.

Fake `ZZ…` names and numbers throughout, so a DB-touching assertion can never
collide with a real vendor in a migrated test database. `ZZNorthwind` stands in
for `Northwind` for exactly that reason: a vendor named like `NORTHWIND - Smith
reps` may well be in the database the suite runs on, and an `ilike` test against
it would pass or fail depending on which database it ran in.
"""

import logging

from unittest.mock import patch

from lxml import etree

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_import.models.core_profile import (
    PROFILE_SEARCH_PLAN,
)

from .posting import post_bill
from .profiles import skip_method_unless_profile

from odoo.addons.goodahead_invoice_ai_import.lib import claude_client
from odoo.addons.goodahead_invoice_import.adapters.ubl_generator import (
    generate_peppol_invoice_xml,
)

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
        "invoice_number": "ZZ-F27-1",
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
        """The four phone shapes US vendor documents print."""
        for printed, expected in (
            ("212.555.0111", "2125550111"),
            ("(212) 555-0130", "2125550130"),
            ("+1 212-555-0199", "2125550199"),
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
        self.assertIsNone(self.Identifier._normalise_phone("543-2111"))

    def test_two_different_numbers_do_not_collapse(self):
        """🔴 THE CONTROL. A normaliser that returned a constant, or truncated
        to too few digits, would make every test above pass and would match
        every vendor to every other vendor."""
        a = self.Identifier._normalise_phone("+1 212-555-0199")
        b = self.Identifier._normalise_phone("+1 212-555-0111")
        self.assertTrue(a and b)
        self.assertNotEqual(a, b)

    def test_a_bool_and_a_float_are_not_phone_numbers(self):
        """`str(True)` is `"True"` — the lesson that put `TR`/Turkey into a
        Peppol scheme in 19.0.2.2.0. And `str(2125550199.0)` ends in `.0`, so
        the last-ten window slides by one and produces a plausible wrong
        number."""
        self.assertIsNone(self.Identifier._normalise_phone(True))
        self.assertIsNone(self.Identifier._normalise_phone(2125550199.0))

    def test_domains_out_of_emails_urls_and_bare_names(self):
        for printed, expected in (
            ("info@wideworldimport.test", "wideworldimport.test"),
            ("https://www.bluebirdmanufacturing.test/", "bluebirdmanufacturing.test"),
            ("Contoso-us.test", "contoso-us.test"),
            ("tailwindfleetmanager.test", "tailwindfleetmanager.test"),
            ("AR@tailspinleasing.test", "tailspinleasing.test"),
            ("http://vendor.example.org:8080/contact?x=1", "vendor.example.org"),
        ):
            self.assertEqual(
                self.Identifier._normalise_domain(printed),
                expected,
                "%r did not normalise" % printed,
            )

    def test_a_free_provider_is_rejected(self):
        """🔴 THE CONTROL, AND IT IS NOT HYPOTHETICAL: `zzexamplevendor@gmail.com` is
        the shape of address a real vendor carries. Without this one gmail
        address makes dozens of unrelated vendors each other's match."""
        self.assertIsNone(self.Identifier._normalise_domain("zzexamplevendor@gmail.com"))
        for public in ("x@yahoo.com", "y@outlook.com", "z@qq.com", "www.163.com"):
            self.assertIsNone(
                self.Identifier._normalise_domain(public),
                "%r must not become an identifier" % public,
            )

    def test_things_that_are_not_domains_are_rejected(self):
        for junk in ("", "n/a", "see above", "212-555-0199", True, None, 17):
            self.assertIsNone(self.Identifier._normalise_domain(junk))


@tagged("post_install", "-at_install")
class TestVendorIdentifierMatching(_NoClaudeMixin, TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Partner = cls.env["res.partner"]
        cls.Identifier = cls.env["goodahead.partner.identifier"]
        cls.AM = cls.env["account.move"]

        # Wide World Import's shape: the record holds a fully punctuated number and the
        # invoice prints a bare one. Core's phone step compares the two as
        # strings and fails. A busy vendor, high `supplier_rank`.
        #
        # 🔴 THE NUMBER IS FICTIONAL (555-01xx) AND THAT IS NOT FUSSINESS. A
        # fixture carrying a number some vendor in the test database already
        # holds makes TWO commercial partners on that number, and the uniqueness
        # rule correctly declines to choose — the test then fails for a reason
        # that has nothing to do with what it is about. The fixture is what has
        # to stay out of the way.
        cls.wideworld = cls.Partner.create(
            {
                "name": "ZZ Wide World Import",
                "is_company": True,
                "phone": "+1 212-555-0112",
                "email": "info@zzwideworldimport.test",
            }
        )
        # Tailspin's shape: the record holds a phone and NO email; the invoice
        # prints a DIFFERENT phone and an email. Nothing may match it — that is
        # the whole reason F27's part 2 exists.
        cls.tailspin = cls.Partner.create(
            {
                "name": "ZZ Tailspin Truck Leasing & Rental",
                "is_company": True,
                "phone": "+1 877-555-0166",
            }
        )

    # ------------------------------------------------------------------
    # The identifier rows themselves
    # ------------------------------------------------------------------

    def test_creating_a_partner_derives_its_identifiers(self):
        rows = self.wideworld.identifier_ids
        self.assertEqual(
            sorted((r.kind, r.value) for r in rows),
            [("domain", "zzwideworldimport.test"), ("phone", "2125550112")],
        )

    def test_changing_a_phone_adds_a_row_and_removes_none(self):
        """🔴 ADD, NEVER REPLACE. A vendor accumulates the ways it is known; the
        point of the table is that the OLD number still matches an old bill."""
        before = {(r.kind, r.value) for r in self.wideworld.identifier_ids}
        self.wideworld.write({"phone": "(212) 555-0171"})
        after = {(r.kind, r.value) for r in self.wideworld.identifier_ids}

        self.assertTrue(before < after, "the old identifiers must survive")
        self.assertIn(("phone", "2125550171"), after)

    def test_a_write_without_a_contact_field_adds_nothing(self):
        """The hot-path guard. `res.partner.write` is called tree-wide against
        over a hundred thousand partners, so a write that touches none of phone/email/website
        must not go near this table."""
        before = len(self.wideworld.identifier_ids)
        self.wideworld.write({"comment": "ZZ unrelated edit"})
        self.assertEqual(len(self.wideworld.identifier_ids), before)

    def test_the_same_identifier_is_not_stored_twice(self):
        """The uniqueness constraint is `models.Constraint`, not
        `_sql_constraints` — which 19.0 reads, logs about and ignores
        (`odoo/orm/model_classes.py:162-164`)."""
        self.wideworld.write({"email": "accounts@zzwideworldimport.test"})
        domains = self.wideworld.identifier_ids.filtered(lambda r: r.kind == "domain")
        self.assertEqual(len(domains), 1, "one domain, two addresses")

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    def test_a_vendor_is_found_by_the_phone_the_invoice_prints(self):
        """🔴 The measured case. Core's own phone step is
        `('phone', '=', phone)` (`account/models/partner.py:982-992`), so
        `212-555-0199` does not find `+1 212-555-0199`."""
        match = self.AM._ai_match_existing_vendor(
            {"name": "ZZ Wide World Restaurant Manufacturing Import (NY)Inc.",
             "phone": "212-555-0112"}
        )
        self.assertEqual(match, self.wideworld)

    def test_tailspin_is_not_matched_on_a_phone_that_is_not_its_own(self):
        """🔴 THE CONTROL, and it is the Tailspin document's shape. The record holds
        `+1 877-555-0136` and no email; the invoice prints a different number
        and `AR@tailspinleasing.test`. A matcher that fired on "a phone was
        present" rather than on the phone would pass the test above and fail
        here, filing the bill against the wrong vendor."""
        match = self.AM._ai_match_existing_vendor(
            {
                "name": "ZZ Tailspin Truck Leasing",
                "phone": "718-555-0164",
                "email": "AR@zztailspinleasing.test",
            }
        )
        self.assertFalse(match, "nothing on this document identifies Tailspin")

    def test_a_vendor_is_found_by_its_email_domain(self):
        """`purchasing@…` and `info@…` are the same company — that is the
        rule."""
        match = self.AM._ai_match_existing_vendor(
            {"name": "ZZ Some Other Legal Name",
             "email": "purchasing@zzwideworldimport.test"}
        )
        self.assertEqual(match, self.wideworld)

    def test_a_vendor_is_found_by_its_website_domain(self):
        """The website caught Woodgrove and Contoso where the email did not."""
        contoso = self.Partner.create(
            {
                "name": "ZZ Contoso",
                "is_company": True,
                "email": "orders@zzcontoso-us.test",
            }
        )
        match = self.AM._ai_match_existing_vendor(
            {"name": "ZZ Fabrikam, LLC", "website": "Zzcontoso-us.test"}
        )
        self.assertEqual(match, contoso)

    def test_a_shared_domain_matches_nothing(self):
        """🔴 AN AMBIGUOUS MATCH IS WORSE THAN NONE. It files the bill against
        one of several possible vendors, with confidence, and nothing
        downstream disagrees."""
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
                "parent_id": self.wideworld.id,
                "phone": "+1 212-555-0113",
            }
        )
        match = self.AM._ai_match_existing_vendor(
            {"name": "ZZ Anything", "phone": "212.555.0113"}
        )
        self.assertEqual(match, self.wideworld)

    # ------------------------------------------------------------------
    # The plan, and the decode that uses it
    # ------------------------------------------------------------------

    # Same reason as `test_partner_matching.test_business_id_step_is_in_the_
    # plan_before_name`: there is no search plan on the legacy build to be in.
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
        """🔴 Core has mapped `.//{*}Telephone` and `.//{*}ElectronicMail` all
        along (`account_edi_ubl_cii/models/account_edi_ubl.py:2654,2659`) and
        had never had anything to read, because the schema had no such field."""
        xml = generate_peppol_invoice_xml(
            _extraction(phone="212.555.0111", email="info@zzbluebird.test")
        )
        tree = etree.fromstring(xml)
        party = tree.find(".//cac:AccountingSupplierParty//cac:Party", NS)

        self.assertEqual(party.find(".//cbc:Telephone", NS).text, "212.555.0111")
        self.assertEqual(
            party.find(".//cbc:ElectronicMail", NS).text, "info@zzbluebird.test"
        )

    def test_no_contact_element_when_there_is_nothing_to_put_in_it(self):
        """🔴 THE CONTROL for the test above: an empty `cac:Contact` on every
        document would make it pass whatever the values were."""
        tree = etree.fromstring(generate_peppol_invoice_xml(_extraction()))
        party = tree.find(".//cac:AccountingSupplierParty//cac:Party", NS)
        self.assertIsNone(party.find("cac:Contact", NS))

    def test_a_bool_never_becomes_a_phone_number(self):
        """`str(True)[:2].upper()` once became the country code `TR` and
        therefore a Peppol scheme meaning TURKEY. A bool is not a contact
        detail either."""
        tree = etree.fromstring(
            generate_peppol_invoice_xml(_extraction(phone=True, email=False))
        )
        party = tree.find(".//cac:AccountingSupplierParty//cac:Party", NS)
        self.assertIsNone(party.find("cac:Contact", NS))

    def test_the_decoder_resolves_the_vendor_by_phone(self):
        """🔴 The fact, not the proxy: the step RUNS inside a real decode.

        Asserted straight after `_ai_decode_ubl_into_move` and BEFORE
        `_ai_post_process`, because post-processing does its own matching and
        would let this pass with the plan hook removed.
        """
        extracted = _extraction(
            name="ZZ Wide World Restaurant Manufacturing Import (NY)Inc.",
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
            self.wideworld,
            "the decoder did not resolve the vendor by phone — the plan step "
            "did not run",
        )

    # ------------------------------------------------------------------
    # Who a bill may NEVER be filed against
    # ------------------------------------------------------------------

    def test_our_own_company_is_never_matched_on_its_own_phone(self):
        """🔴 MEASURED, NOT IMAGINED. Both company partners in this database
        carry a phone, an email and a website — `Alpha LLC` on
        `(212) 555-0101` / `orders@alpha.example.test`, `Beta Inc.` on
        `(212) 555-0102` / `sales@beta.example.test`. The identifiers are seeded
        from the WHOLE contact book, so both are matchable keys.

        Every invoice prints the BUYER as well as the seller, and the only
        thing separating them is prompt text this module's own manifest says is
        unverified. One misread files a vendor bill against the importing
        company itself.

        CONTROL: the identical fixture on an ordinary partner IS matched, so a
        version of this guard that simply stopped matching anything fails here.
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
        """A customer's phone resolving uniquely is the common case, not the
        rare one: among the keys that resolve to exactly one partner, **non-vendors
        outnumber vendors by two orders of magnitude** for phone and **by about
        one** for domain. So uniqueness alone decides confidently and wrongly.

        ⚠️ The rule is `supplier_rank == 0 AND customer_rank > 0` — a record we
        have sold to and never bought from. A genuinely NEW vendor has both at
        zero and must still match, which is the control below.
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
        """🔴 The deliberate behaviour change. A bill nobody filed is visible; a
        bill filed against the wrong vendor is not."""
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
        """🔴 THE CONTROL. A `_create_partner_from_extraction` that simply did
        nothing at all would pass the test above."""
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
        """🔴 THE HALF MEASURE THIS AVOIDS.

        Core has a vendor-creating step of its own,
        `_import_ubl_create_missing_customer`
        (`account_edi_ubl_cii/models/account_edi_ubl.py:2752`), guarded on a VAT
        being present — which is why a US sample set sees it on only the odd
        document. Suppressing OUR creation without suppressing core's would leave
        exactly the Woodgrove case, an invoice that prints an EIN.
        """
        Partner = self.Partner.with_context(active_test=False)
        before = Partner.search_count([])
        extracted = _extraction(
            name="ZZ Woodgrove Bank (tailwind fleet)", vat="DE100000008",
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
        """🔴 THE CONTROL for the suppression above, and the reason it is scoped
        to a context key. `account.edi.ubl` is shared: a real structured
        e-invoice with a registered VAT is not a model's reading of a PDF, and
        core's behaviour there is not this feature's to change. Without the
        `ai_invoice_import` scope this returns early and the test fails.
        """
        Partner = self.Partner.with_context(active_test=False)
        before = Partner.search_count([])
        extracted = _extraction(
            name="ZZ Genuine Peppol GmbH", vat="DE100000008", country_code="DE"
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
            "mimetype": "application/xml",
            "attachment": self.env["ir.attachment"],
            "origin_attachment": self.env["ir.attachment"],
            "origin_import_file_type": "xml",
        }
        file_data["import_file_type"] = move._get_import_file_type(file_data)

        # NOTE: no `ai_invoice_import` on the context — this is the plain
        # core path, exactly as a document arriving over Peppol would take it.
        decoder = self.AM._get_edi_decoder(file_data, new=True)
        decoder["decoder"](move, file_data, True)

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
        # 🔴 The case that justifies the whole feature: the bill comes from the
        # MANUFACTURER and the ledger holds the REP AGENCY that sells for them.
        # Different company, different phone, different domain — no rule derives
        # one from the other, so a person says so once and the system remembers.
        # `ZZNorthwind` rather than `Northwind`, because a vendor named like
        # `NORTHWIND - Smith reps` may be in the test database and an `ilike`
        # would find it.
        cls.northwind = cls.Partner.create(
            {
                "name": "ZZNORTHWIND - Smith reps",
                "is_company": True,
                "supplier_rank": 63,
            }
        )
        cls.decoy = cls.Partner.create(
            {"name": "ZZNorthwind Spare Parts Depot", "is_company": True}
        )

    def test_the_rep_agency_is_offered_first(self):
        candidates = self.AM._ai_vendor_candidates({"name": "ZZNorthwind"})
        partners = [c["partner"] for c in candidates]

        self.assertIn(self.northwind, partners)
        self.assertEqual(
            partners[0],
            self.northwind,
            "the established vendor must sort above an incidental name match",
        )

    def test_candidates_are_ranked_by_supplier_rank(self):
        """🔴 THE CONTROL for the ordering: raise the decoy above the agency and
        the order must change. A list returned in id order would pass the test
        above by accident and fail here."""
        self.decoy.supplier_rank = 900
        candidates = self.AM._ai_vendor_candidates({"name": "ZZNorthwind"})
        self.assertEqual(candidates[0]["partner"], self.decoy)

    def test_the_bill_count_is_counted_not_proxied(self):
        """🔴 `supplier_rank` IS NOT BILLS. It is incremented by other things
        and never decremented, and the question asked is whether the
        candidates already have earlier bills. The agency carries rank 63 and
        has one posted bill here; if the number read 63 it would be the proxy.
        """
        journal = self.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", self.env.company.id)],
            limit=1,
        )
        bill = self.AM.create(
            {
                "move_type": "in_invoice",
                "partner_id": self.northwind.id,
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
                "partner_id": self.northwind.id,
                "journal_id": journal.id,
                "invoice_date": "2026-10-02",
                "invoice_line_ids": [
                    (0, 0, {"name": "a line", "quantity": 1, "price_unit": 10.0}),
                ],
            }
        )

        candidates = self.AM._ai_vendor_candidates({"name": "ZZNorthwind"})
        found = next(c for c in candidates if c["partner"] == self.northwind)

        self.assertEqual(found["bill_count"], 1)
        self.assertNotEqual(
            found["bill_count"],
            self.northwind.supplier_rank,
            "the count is bills, not supplier_rank",
        )

    def test_an_archived_candidate_is_offered_and_marked(self):
        """Hiding it sends somebody off to create the duplicate by hand.

        ⚠️ Both fixtures come back, because the longest word of the printed
        name is `ZZNorthwind` and both carry it — which is the loose name match
        doing its job. What this test is about is the archived one being in the
        list at all, and carrying the mark.
        """
        self.decoy.active = False
        candidates = self.AM._ai_vendor_candidates({"name": "ZZNorthwind Spare"})
        by_partner = {c["partner"]: c for c in candidates}

        self.assertIn(self.decoy, by_partner, "an archived vendor must be offered")
        self.assertTrue(by_partner[self.decoy]["archived"])
        self.assertFalse(
            by_partner[self.northwind]["archived"],
            "an active vendor must not be marked archived",
        )

    def test_a_child_contact_core_could_have_matched_is_still_offered(self):
        """🔴 A REVIEW FINDING — A SUPERSET CLAIM THAT DID NOT HOLD.

        `_ai_retrieve_partner_with_exact_name` narrows the legacy core's name
        step from `ilike` to `=ilike`, and the argument for shipping that
        unguarded is this (`res_partner.py:429-437`, repeated at
        the changelog of that version):

          > 🔴 NOTHING IS LOST, AND THAT IS CHECKABLE. Every partner core's
          > substring step could have matched is already in the set
          > `account.move._ai_vendor_candidates` offers under *"similar name"* —
          > it searches the same `('name', 'ilike', name)`.

        It did not search the same domain. The candidate search carried
        `("parent_id", "=", False)` and core's legacy step
        (`account/models/partner.py:956` at `ca1f0aea`) carries no such filter.
        So for a CHILD CONTACT whose name merely contains the printed name both
        halves failed at once: the narrowed step stopped matching it AND the
        candidate list excluded it. The resemblance was not moved from a silent
        decision to a visible candidate — it was dropped, and the quantifier in
        the claim says *every*.

        🔴 WHAT IS OFFERED IS THE CONTACT'S COMMERCIAL PARTNER, NOT THE
        CONTACT. The first version of this fix offered the contact itself, and
        three things were wrong with it: its card always read "0 bills" because
        `_ai_posted_bill_counts` groups on `commercial_partner_id`; it arrived
        at `supplier_rank = 0` because the rank lives on the parent, so against
        another rank-0 candidate the `id asc` tiebreak let an old contact
        displace a phone-matched vendor; and `action_use_this` would have filed
        the bill against a sub-contact and written the company's phone, email
        and address onto it. Every candidate stays a party a bill may be filed
        against — the vocabulary `_partners_for` already uses for the other two
        keys — and the offer is still new, because the parent's own name need
        not contain the printed name at all.

        CONTROL 1: the parent's name deliberately does NOT contain the printed
        name, so it can only be in the list through the contact. A fix that
        merely searched harder on top-level names could not pass this.
        CONTROL 2: the top-level partner carrying the same name is still
        offered, and still first.
        CONTROL 3: the card carries the parent's REAL bill count, which is what
        the 0-bills defect would have hidden.
        CONTROL 4: the matcher still refuses to CHOOSE the contact.
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
            "🔴 core's substring step could have matched this contact and "
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
        """What is NOT a match is still a suggestion — F27: a domain that
        resolves to more than one vendor "becomes a candidate, never a
        decision"."""
        for name in ("ZZ Group Member One", "ZZ Group Member Two"):
            self.Partner.create(
                {"name": name, "is_company": True, "email": "ap@zzgroupshared.com"}
            )
        # ⚠️ EVERY WORD HERE IS NONSENSE, AND THAT IS LOAD-BEARING. The loose
        # name step takes the longest word of the printed name and runs `ilike`
        # on it across the whole contact book — over a hundred thousand partners
        # on a large test database. An earlier version of this fixture said "ZZ No Such Name",
        # whose longest word is "Such", and `ilike "Such"` found a REAL partner,
        # so the exact count below failed for a reason that had nothing to do
        # with what the test is about.
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
    """🔴 WITHOUT THE SEED NOTHING MATCHES ON DAY ONE. The vendors with
    `supplier_rank > 0` carry no identifier rows on the morning of the upgrade,
    so every new step would decline and the feature would ship measurably dead —
    with new code in place, a green suite and nothing reporting a fault."""

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
                "phone": "+1 212-555-0158",
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
            [("domain", "zzseedprobe.com"), ("phone", "2125550158")],
        )

    def test_the_seed_never_stores_a_free_provider_domain(self):
        """🔴 THE CONTROL. A seed that stored whatever the email column held
        would pass the test above and would make every gmail vendor a match for
        every other one. The phone is asserted beside it, so a seed that simply
        skipped this partner could not pass either."""
        partner = self.Partner.create(
            {
                "name": "ZZ Seed Gmail Probe",
                "is_company": True,
                "phone": "+1 212-555-0130",
                "email": "zzexamplevendor2@gmail.com",
            }
        )
        partner.identifier_ids.unlink()

        self.Identifier._seed_from_partners()
        partner.invalidate_recordset(["identifier_ids"])

        kinds = partner.identifier_ids.mapped("kind")
        self.assertIn("phone", kinds)
        self.assertNotIn("domain", kinds)

    def test_the_seed_is_idempotent(self):
        """It runs from both `post_init_hook` and the 19.0.3.0.0 migration,
        because neither alone covers both an install and an upgrade."""
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


