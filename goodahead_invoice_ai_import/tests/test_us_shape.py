# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""A US vendor bill, end to end, and the multi-company expense categories.

These go through the real UBL generator and Odoo decoder rather than asserting
on the extraction dict, since a prompt-level fix can still fail to reach the
bill.
"""

from unittest.mock import MagicMock, patch

from lxml import etree

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_ai_import.lib.ubl_generator import (
    generate_peppol_invoice_xml,
)

NS = {
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}

ROUTING = "021000021"          # real ABA routing number, valid checksum
BAD_ROUTING = "021000012"      # same digits, two transposed
ACCOUNT = "1234567890"


def _us_extracted(**overrides):
    seller = {
        "name": "ZZ US Supply Co",
        "vat": "12-3456789",
        "business_id": None,
        "street": "100 Industrial Way",
        "city": "Newark",
        "state_code": "NJ",
        "postal_code": "07102",
        "country_code": "US",
        "routing_number": ROUTING,
        "account_number": ACCOUNT,
    }
    seller.update(overrides.pop("seller", {}))
    data = {
        "type": "Invoice",
        "invoice_number": "ZZ-US-1001",
        "issue_date": "2026-03-04",
        "currency": "USD",
        "seller": seller,
        "buyer": {"name": "ZZ Buyer", "country_code": "US", "state_code": "NJ"},
        "lines": [
            {
                "product_code": "ZZ-US-SKU-1",
                "name": "Commercial mixer, 20qt",
                "quantity": 2.0,
                "unit_code": "C62",
                "price_unit": 500.0,
                "line_total": 1000.0,
                "tax_percent": 6.625,       # New Jersey sales tax
                "tax_amount": 66.25,
                "tax_category": "S",
            }
        ],
        "tax_subtotals": [
            {
                "tax_percent": 6.625,
                "taxable_amount": 1000.0,
                "tax_amount": 66.25,
                "tax_category": "S",
            }
        ],
        "totals": {
            "line_extension": 1000.0,
            "tax_exclusive": 1000.0,
            "tax_inclusive": 1066.25,
            "payable": 1066.25,
        },
    }
    data.update(overrides)
    return data


@tagged("post_install", "-at_install")
class TestUsShape(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.AM = cls.env["account.move"]

    # ------------------------------------------------------------------
    # The generated UBL
    # ------------------------------------------------------------------

    def _tree(self, extracted):
        return etree.fromstring(
            self.AM._ai_generate_ubl_from_extracted(extracted, self.company)
        )

    def test_us_bank_details_reach_the_ubl(self):
        """Routing + account number occupy the slots an IBAN + BIC used to."""
        tree = self._tree(_us_extracted())

        account = tree.find(
            ".//cac:PaymentMeans/cac:PayeeFinancialAccount/cbc:ID", NS
        )
        institution = tree.find(
            ".//cac:PaymentMeans/cac:PayeeFinancialAccount"
            "/cac:FinancialInstitutionBranch/cbc:ID",
            NS,
        )
        self.assertIsNotNone(account)
        self.assertEqual(account.text, ACCOUNT)
        self.assertIsNotNone(institution)
        self.assertEqual(institution.text, ROUTING)

    def test_a_misread_routing_number_takes_the_account_with_it(self):
        """A routing number failing its checksum drops the account number too,
        since ACH needs both."""
        extracted = _us_extracted(seller={"routing_number": BAD_ROUTING})

        tree = self._tree(extracted)

        self.assertIsNone(
            tree.find(".//cac:PayeeFinancialAccount/cbc:ID", NS),
            "a bill must not state an account number whose routing number was "
            "rejected",
        )
        self.assertEqual(extracted["seller"]["routing_number_rejected"], BAD_ROUTING)
        self.assertEqual(extracted["seller"]["account_number_rejected"], ACCOUNT)

    def test_a_valid_routing_number_is_left_alone(self):
        """A valid routing number and its account are kept."""
        extracted = _us_extracted()

        self._tree(extracted)

        self.assertNotIn("routing_number_rejected", extracted["seller"])
        self.assertEqual(extracted["seller"]["account_number"], ACCOUNT)

    def test_no_peppol_endpoint_for_a_us_vendor(self):
        """A US EIN gets no EndpointID: core matches partners on the
        EndpointID/@schemeID pair, so a wrong scheme can match the wrong
        vendor."""
        tree = self._tree(_us_extracted())

        self.assertIsNone(tree.find(".//cac:AccountingSupplierParty//cbc:EndpointID", NS))

    def test_a_european_vendor_still_gets_its_endpoint(self):
        """A European VAT number still gets its endpoint (DE uses `9930`, a
        pair core itself defines). `test_peppol_endpoint.py` checks the whole
        table against core.
        """
        extracted = _us_extracted(
            seller={"vat": "DE123456789", "country_code": "DE", "state_code": None}
        )

        tree = self._tree(extracted)

        endpoint = tree.find(".//cac:AccountingSupplierParty//cbc:EndpointID", NS)
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint.get("schemeID"), "9930")
        self.assertEqual(endpoint.text, "DE123456789")

    def test_the_state_reaches_the_address(self):
        tree = self._tree(_us_extracted())
        subentity = tree.find(
            ".//cac:AccountingSupplierParty//cac:PostalAddress/cbc:CountrySubentity",
            NS,
        )
        self.assertIsNotNone(subentity)
        self.assertEqual(subentity.text, "NJ")

    def test_currency_falls_back_to_the_companys_own(self):
        """A missing currency falls back to the company's currency, not a
        hard-coded default."""
        extracted = _us_extracted()
        extracted.pop("currency")

        tree = self._tree(extracted)

        self.assertEqual(
            tree.find(".//cbc:DocumentCurrencyCode", NS).text,
            self.company.currency_id.name,
        )

    def test_generator_tolerates_a_vendor_with_no_bank_details_at_all(self):
        """Common on a US invoice that expects a check — it must still import."""
        extracted = _us_extracted(
            seller={"routing_number": None, "account_number": None}
        )
        extracted["payment_reference"] = None

        tree = self._tree(extracted)

        self.assertIsNone(tree.find(".//cac:PayeeFinancialAccount", NS))
        # and the document is still well-formed with its totals intact
        self.assertEqual(
            tree.find(".//cac:LegalMonetaryTotal/cbc:PayableAmount", NS).text,
            "1066.25",
        )

    # ------------------------------------------------------------------
    # The routing number must survive onto the bank account
    # ------------------------------------------------------------------

    def test_the_routing_number_lands_on_the_bank_account(self):
        """The routing number is stored as the bank account's clearing number.

        Core's UBL import never reads `FinancialInstitutionBranch/ID`, so
        without `_ai_record_routing_number` the vendor gets a bare account
        number that cannot be paid by ACH.
        """
        vendor = self.env["res.partner"].create(
            {"name": "ZZ Routing Probe", "is_company": True}
        )
        invoice = self.AM.create({"move_type": "in_invoice"})
        extracted = _us_extracted(seller={"name": "ZZ Routing Probe", "vat": None})
        self.AM._create_partner_from_extraction(invoice, extracted)
        self.assertEqual(invoice.partner_id, vendor)

        bank = self.env["res.partner.bank"].create(
            {"acc_number": ACCOUNT, "partner_id": vendor.id}
        )
        invoice.partner_bank_id = bank
        self.AM._ai_record_routing_number(invoice, extracted)

        self.assertEqual(
            bank.clearing_number,
            ROUTING,
            "the routing number was verified and then thrown away; the account "
            "cannot be paid",
        )

    def test_the_routing_number_is_stamped_on_an_account_core_created(self):
        """The routing number is also stamped on an account core created for an
        existing vendor."""
        vendor = self.env["res.partner"].create(
            {"name": "ZZ Existing Routing Vendor", "is_company": True}
        )
        bank = self.env["res.partner.bank"].create(
            {"acc_number": ACCOUNT, "partner_id": vendor.id}
        )
        self.assertFalse(bank.clearing_number)

        invoice = self.AM.create(
            {"move_type": "in_invoice", "partner_id": vendor.id}
        )
        invoice.partner_bank_id = bank

        self.AM._ai_record_routing_number(invoice, _us_extracted())

        self.assertEqual(bank.clearing_number, ROUTING)

    def test_an_existing_clearing_number_is_never_overwritten(self):
        """A human may have corrected it; an extraction must not win."""
        vendor = self.env["res.partner"].create(
            {"name": "ZZ Corrected Routing Vendor", "is_company": True}
        )
        bank = self.env["res.partner.bank"].create(
            {
                "acc_number": ACCOUNT,
                "partner_id": vendor.id,
                "clearing_number": "111000025",
            }
        )
        invoice = self.AM.create(
            {"move_type": "in_invoice", "partner_id": vendor.id}
        )
        invoice.partner_bank_id = bank

        self.AM._ai_record_routing_number(invoice, _us_extracted())

        self.assertEqual(bank.clearing_number, "111000025")

    # ------------------------------------------------------------------
    # The checksums dispatch by shape, not by field name
    # ------------------------------------------------------------------

    def test_a_misread_iban_is_caught_even_in_the_account_number_field(self):
        """An IBAN-shaped value in `account_number` is checked with mod-97 too;
        without `base_iban` Odoo would not validate it."""
        extracted = _us_extracted(
            seller={
                "iban": None,
                "routing_number": None,
                "account_number": "FI16400010000000017",  # one digit too many
            }
        )

        self._tree(extracted)

        self.assertEqual(
            extracted["seller"]["account_number_rejected"],
            "FI16400010000000017",
        )
        self.assertIsNone(extracted["seller"]["account_number"])

    def test_a_valid_iban_in_the_account_number_field_is_kept(self):
        """A valid IBAN in `account_number` is kept."""
        extracted = _us_extracted(
            seller={
                "iban": None,
                "routing_number": None,
                "account_number": "FI1640001000000017",  # valid
            }
        )

        self._tree(extracted)

        self.assertNotIn("account_number_rejected", extracted["seller"])

    def test_a_misread_routing_number_in_the_iban_field_is_caught(self):
        """A routing-number-shaped value in `iban` is checked as one."""
        extracted = _us_extracted(
            seller={"iban": "021000012", "routing_number": None, "account_number": None}
        )

        self._tree(extracted)

        self.assertEqual(extracted["seller"]["iban_rejected"], "021000012")

    # ------------------------------------------------------------------
    # Credit memo signs
    # ------------------------------------------------------------------

    def _credit_note(self, sign):
        data = _us_extracted()
        data["type"] = "CreditNote"
        for line in data["lines"]:
            for key in ("quantity", "line_total", "tax_amount"):
                line[key] = sign * abs(line[key])
        for sub in data["tax_subtotals"]:
            for key in ("taxable_amount", "tax_amount"):
                sub[key] = sign * abs(sub[key])
        for key in data["totals"]:
            data["totals"][key] = sign * abs(data["totals"][key])
        return data

    def test_a_credit_note_extracted_as_negatives_is_normalised(self):
        """Negative credit-note amounts are made positive.

        Type code 381 already carries the sign; negatives inside it would
        produce a refund that increases what is owed. Vendors print credit
        memos both ways, so the generator normalises.
        """
        tree = self._tree(self._credit_note(-1))

        self.assertEqual(
            tree.find(".//cac:LegalMonetaryTotal/cbc:PayableAmount", NS).text,
            "1066.25",
        )
        self.assertEqual(
            float(tree.find(".//cac:CreditNoteLine/cbc:LineExtensionAmount", NS).text),
            1000.00,
        )
        self.assertEqual(tree.find(".//cbc:CreditNoteTypeCode", NS).text, "381")

    def test_a_credit_note_extracted_as_positives_is_untouched(self):
        """A credit note with positive amounts passes through unchanged."""
        tree = self._tree(self._credit_note(1))

        self.assertEqual(
            tree.find(".//cac:LegalMonetaryTotal/cbc:PayableAmount", NS).text,
            "1066.25",
        )

    def test_normalisation_does_not_mutate_the_stored_extraction(self):
        """Normalisation does not rewrite the stored extraction JSON."""
        extracted = self._credit_note(-1)

        generate_peppol_invoice_xml(extracted)

        self.assertLess(extracted["totals"]["payable"], 0)

    def test_an_invoice_with_a_negative_total_is_left_alone(self):
        """Normalisation applies to credit notes only."""
        data = _us_extracted()
        data["totals"]["payable"] = -1066.25

        tree = self._tree(data)

        self.assertEqual(
            tree.find(".//cac:LegalMonetaryTotal/cbc:PayableAmount", NS).text,
            "-1066.25",
        )

    # ------------------------------------------------------------------
    # The vendor's state is looked up, never invented
    # ------------------------------------------------------------------

    def _import_seller(self, state_code):
        invoice = self.AM.create({"move_type": "in_invoice"})
        extracted = _us_extracted(
            seller={
                "name": f"ZZ State Probe {state_code or 'none'}",
                "state_code": state_code,
                "vat": None,
            }
        )
        self.AM._create_partner_from_extraction(invoice, extracted)
        return invoice

    def test_no_state_is_created_from_an_extraction(self):
        """No vendor and no `res.country.state` is created from an extraction,
        for a known or an unknown state code (invented states break carrier
        bookings)."""
        State = self.env["res.country.state"]
        before = State.search_count([])

        for state_code in ("NJ", "ZZ"):
            invoice = self._import_seller(state_code)
            self.assertFalse(
                invoice.partner_id,
                "an unrecognised vendor must not be created (%s)" % state_code,
            )

        self.assertEqual(
            State.search_count([]),
            before,
            "no state may be created from an extraction",
        )

    # ------------------------------------------------------------------
    # Multi-company expense categories
    # ------------------------------------------------------------------

    def test_shipped_categories_are_available_to_every_company(self):
        """Shipped categories have no company, so every company sees them."""
        Category = self.env["goodahead.expense.category"]
        shipped = self.env.ref(
            "goodahead_invoice_ai_import.expense_category_goods"
        )
        self.assertFalse(
            shipped.company_id,
            "a shipped category must be shared, not bound to one company",
        )

        for company in self.env["res.company"].search([]):
            self.assertIn(
                shipped,
                Category._for_company(company),
                f"{company.display_name} cannot see the shipped categories",
            )

    def test_the_account_is_resolved_per_company(self):
        """The category is shared but its account is company-dependent."""
        companies = self.env["res.company"].search([], limit=2)
        if len(companies) < 2:
            self.skipTest("needs two companies")
        first, second = companies

        category = self.env["goodahead.expense.category"].create(
            {"code": "zz_percompany", "name": "ZZ Per Company"}
        )
        account_a = self.env["account.account"].search(
            [("company_ids", "in", first.id)], limit=1
        )
        account_b = self.env["account.account"].search(
            [("company_ids", "in", second.id)], limit=1
        )
        if not account_a or not account_b or account_a == account_b:
            self.skipTest("needs a distinct account in each company")

        category.with_company(first).account_id = account_a
        category.with_company(second).account_id = account_b

        self.assertEqual(category.with_company(first).account_id, account_a)
        self.assertEqual(
            category.with_company(second).account_id,
            account_b,
            "company_dependent did not keep the two charts apart",
        )

    def test_a_duplicate_code_is_refused_even_when_shared(self):
        """Duplicate shared codes are refused by the Python check; a SQL unique
        constraint does not treat two NULL company_ids as equal."""
        Category = self.env["goodahead.expense.category"]
        Category.create({"code": "zz_dupe", "name": "ZZ Dupe"})

        with self.assertRaises(ValidationError):
            Category.create({"code": "zz_dupe", "name": "ZZ Dupe Again"})

    def test_a_company_category_may_not_shadow_a_shared_code(self):
        Category = self.env["goodahead.expense.category"]
        Category.create({"code": "zz_shadow", "name": "ZZ Shared"})

        with self.assertRaises(ValidationError):
            Category.create(
                {
                    "code": "zz_shadow",
                    "name": "ZZ Company",
                    "company_id": self.company.id,
                }
            )

    def test_the_account_is_resolved_in_the_bills_company_not_the_envs(self):
        """The account is resolved with the bill's company, even when the env
        (e.g. a queue job enqueued from the mail path) is another company.
        """
        companies = self.env["res.company"].search([], limit=2)
        if len(companies) < 2:
            self.skipTest("needs two companies")
        bill_company, other_company = companies

        accounts = {}
        for company in (bill_company, other_company):
            account = self.env["account.account"].search(
                [("company_ids", "in", company.id)], limit=1
            )
            if not account:
                self.skipTest(f"no account available in {company.display_name}")
            accounts[company] = account
        if accounts[bill_company] == accounts[other_company]:
            self.skipTest("the two companies share an account; nothing to prove")

        category = self.env["goodahead.expense.category"].create(
            {"code": "zz_callsite", "name": "ZZ Call Site"}
        )
        category.with_company(bill_company).account_id = accounts[bill_company]
        category.with_company(other_company).account_id = accounts[other_company]

        journal = self.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", bill_company.id)],
            limit=1,
        )
        if not journal:
            self.skipTest("no purchase journal in the bill's company")

        move = (
            self.env["account.move"]
            .with_company(bill_company)
            .create({"move_type": "in_invoice", "journal_id": journal.id})
        )
        self.env["account.move.line"].with_company(bill_company).create(
            {"move_id": move.id, "name": "a line", "quantity": 1, "price_unit": 10}
        )
        extracted = _us_extracted()
        extracted["lines"][0]["expense_category"] = "zz_callsite"

        # The env is deliberately the other company.
        AM_in_other_company = self.env["account.move"].with_company(other_company)
        self.assertEqual(AM_in_other_company.env.company, other_company)
        AM_in_other_company._apply_expense_categories(
            move, extracted, bill_company
        )

        line = move.invoice_line_ids.filtered(
            lambda line: line.display_type not in ("line_section", "line_note")
        )[0]
        self.assertEqual(
            line.account_id,
            accounts[bill_company],
            "the account was resolved in the env's company, not the bill's",
        )

    def test_a_company_specific_category_beats_a_shared_one(self):
        """`_ai_expense_category_for` prefers the company's own row over a
        shared one with the same code.

        Ordering by `company_id desc` would do the opposite, since PostgreSQL
        sorts NULLs first on DESC. The pair is normally prevented by a
        constraint, so it is created through SQL here.
        """
        Category = self.env["goodahead.expense.category"]
        shared = Category.create({"code": "zz_pref", "name": "ZZ Shared"})
        # Bypass the Python constraint to test the lookup itself.
        specific = Category.create(
            {"code": "zz_pref_tmp", "name": "ZZ Specific", "company_id": self.company.id}
        )
        self.env.cr.execute(
            "UPDATE goodahead_expense_category SET code = %s WHERE id = %s",
            ("zz_pref", specific.id),
        )
        Category.invalidate_model(["code"])

        found = self.env["account.move"]._ai_expense_category_for(
            "zz_pref", self.company
        )

        self.assertEqual(found, specific, "the shared row won over the company's own")
        self.assertNotEqual(found, shared)
        # Control: without the company row, the shared one is returned.
        specific.unlink()
        self.assertEqual(
            self.env["account.move"]._ai_expense_category_for("zz_pref", self.company),
            shared,
        )

    def test_a_missing_account_names_the_company(self):
        """The missing-account warning names the company lacking the
        account."""
        invoice = self.AM.create({"move_type": "in_invoice"})
        move = invoice  # readability

        extracted = _us_extracted()
        extracted["lines"][0]["expense_category"] = "zz_no_account"
        self.env["goodahead.expense.category"].create(
            {"code": "zz_no_account", "name": "ZZ No Account"}
        )
        # One product-less line to carry the category.
        self.env["account.move.line"].create(
            {
                "move_id": move.id,
                "name": "a line",
                "quantity": 1,
                "price_unit": 10,
            }
        )

        self.AM._apply_expense_categories(move, extracted, self.company)

        body = "".join(m.body or "" for m in move.message_ids)
        self.assertIn("zz_no_account", body)
        self.assertIn(
            self.company.display_name,
            body,
            "the warning must name the company whose account is missing",
        )


@tagged("post_install", "-at_install")
class TestInboundLimits(TransactionCase):
    """Inbound mail guards: a per-message PDF cap, a size limit, and the
    company chosen by the alias. The alias itself is created by hand.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.Import = cls.env["ai.invoice.import"]

    def _msg(self, attachments):
        return {
            "email_from": "vendor@example.com",
            "subject": "Invoices",
            "attachments": attachments,
        }

    def test_the_module_ships_no_alias(self):
        """The module owns no `mail.alias`; aliases are created by hand.

        Databases that had an alias from an earlier version keep it, with the
        xmlid released by a migration.
        """
        self.assertFalse(
            self.env["ir.model.data"].search_count(
                [
                    ("module", "=", "goodahead_invoice_ai_import"),
                    ("model", "=", "mail.alias"),
                ]
            ),
            "this module must not create a mail.alias",
        )

    def test_one_message_cannot_enqueue_unbounded_extractions(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "goodahead_invoice_ai_import.max_pdfs_per_message", "2"
        )
        attachments = [(f"bill{i}.pdf", b"%PDF-1.4 x") for i in range(5)]

        with patch.object(
            type(self.Import), "with_delay", return_value=MagicMock()
        ):
            self.Import.message_new(self._msg(attachments))

        created = self.Import.search([("name", "like", "bill%")])
        self.assertEqual(
            len(created), 2, "the per-message cap did not bound the enqueue"
        )

    def test_an_oversized_pdf_is_not_sent_for_extraction(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "goodahead_invoice_ai_import.max_pdf_bytes", "100"
        )
        attachments = [
            ("small.pdf", b"%PDF-1.4 small"),
            ("huge.pdf", b"%PDF-1.4 " + b"x" * 500),
        ]

        with patch.object(
            type(self.Import), "with_delay", return_value=MagicMock()
        ):
            self.Import.message_new(self._msg(attachments))

        self.assertTrue(self.Import.search([("name", "=", "small.pdf")]))
        self.assertFalse(
            self.Import.search([("name", "=", "huge.pdf")]),
            "an oversized PDF must not become a billed extraction",
        )

    def test_the_alias_decides_the_company(self):
        """The alias's `alias_defaults` (as `custom_values`) set the company.

        Otherwise `env.company` on the mail path is the alias user's company
        for every bill, and a wrong company raises no error.
        """
        companies = self.env["res.company"].search([], limit=2)
        if len(companies) < 2:
            self.skipTest("needs two companies")
        other = companies[1] if companies[0] == self.env.company else companies[0]

        with patch.object(
            type(self.Import), "with_delay", return_value=MagicMock()
        ):
            record = self.Import.message_new(
                self._msg([("aliased.pdf", b"%PDF-1.4 x")]),
                custom_values={"company_id": other.id},
            )

        self.assertEqual(
            record.company_id,
            other,
            "the alias's company was ignored; the bill would land in the wrong "
            "chart of accounts",
        )

    def test_the_landing_company_is_stated_on_the_record(self):
        """The import record states which company it landed in."""
        with patch.object(
            type(self.Import), "with_delay", return_value=MagicMock()
        ):
            record = self.Import.message_new(
                self._msg([("filed.pdf", b"%PDF-1.4 x")]),
                custom_values={"company_id": self.env.company.id},
            )

        body = "".join(m.body or "" for m in record.message_ids)
        self.assertIn(self.env.company.display_name, body)
        self.assertIn("as set by the alias", body)

    def test_a_fallback_company_is_marked_as_a_fallback(self):
        """A fallback company is marked as such on the record."""
        with patch.object(
            type(self.Import), "with_delay", return_value=MagicMock()
        ):
            record = self.Import.message_new(
                self._msg([("fallback.pdf", b"%PDF-1.4 x")])
            )

        body = "".join(m.body or "" for m in record.message_ids)
        self.assertIn("fallback", body)
        self.assertIn("names no company", body)

    def test_without_an_alias_company_it_falls_back_and_says_so(self):
        """Without an alias company, `env.company` is used and a warning is
        logged."""
        with self.assertLogs(
            "odoo.addons.goodahead_invoice_ai_import.models.ai_invoice_import",
            level="WARNING",
        ) as logs:
            with patch.object(
                type(self.Import), "with_delay", return_value=MagicMock()
            ):
                record = self.Import.message_new(
                    self._msg([("nocompany.pdf", b"%PDF-1.4 x")])
                )

        self.assertEqual(record.company_id, self.env.company)
        self.assertTrue(
            any("names no company" in line for line in logs.output),
            "a company-less alias must be logged, not silent",
        )

    def test_what_was_refused_is_said_on_the_record(self):
        """Refused attachments are named on the record."""
        self.env["ir.config_parameter"].sudo().set_param(
            "goodahead_invoice_ai_import.max_pdf_bytes", "100"
        )
        attachments = [
            ("small.pdf", b"%PDF-1.4 small"),
            ("huge.pdf", b"%PDF-1.4 " + b"x" * 500),
        ]

        with patch.object(
            type(self.Import), "with_delay", return_value=MagicMock()
        ):
            record = self.Import.message_new(self._msg(attachments))

        self.assertIn(
            "huge.pdf", "".join(m.body or "" for m in record.message_ids)
        )
