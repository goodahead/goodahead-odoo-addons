# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""A US vendor bill, end to end, and the multi-company expense categories.

The module was written for a European business. These tests pin the parts that
had to change for a US one, and they go through the real UBL generator and the
real Odoo decoder rather than asserting on the extraction dict — because every
one of these could be "fixed" in the prompt and still not reach the bill.
"""

from unittest.mock import MagicMock, patch

from lxml import etree

from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_import.adapters.ubl_generator import (
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
        """🔴 Half a US account is not payable, and must not look payable.

        The routing number fails its checksum, so it is dropped — and the
        account number goes with it, because ACH needs both. The bill then
        states no account at all, which is the honest outcome.
        """
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
        """The control for the test above — otherwise it could pass by always
        dropping everything."""
        extracted = _us_extracted()

        self._tree(extracted)

        self.assertNotIn("routing_number_rejected", extracted["seller"])
        self.assertEqual(extracted["seller"]["account_number"], ACCOUNT)

    def test_no_peppol_endpoint_for_a_us_vendor(self):
        """🔴 An EIN must not go out labelled as a German VAT number.

        Core matches partners on the EndpointID/@schemeID pair, so a wrong
        scheme does not merely mislabel — it can match the wrong vendor with
        confidence. Where no scheme honestly applies, the element is omitted.
        """
        tree = self._tree(_us_extracted())

        self.assertIsNone(tree.find(".//cac:AccountingSupplierParty//cbc:EndpointID", NS))

    def test_a_european_vendor_still_gets_its_endpoint(self):
        """The control: omission is scheme-driven, not a blanket removal.

        ⚠️ THIS TEST USED TO ASSERT `9925` FOR A FINNISH VENDOR, AND `9925` IS
        BELGIAN VAT. It passed, because it read back the same hand-written table
        the generator wrote from — so it pinned the bug instead of catching it.
        Germany is used now because `9930` is a country/scheme pair core itself
        states, and `test_peppol_endpoint.py` reconciles the whole table against
        core rather than against a second copy of our own belief.
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
        """⚠️ THIS TEST USED TO READ `assertEqual(..., "USD")` AND IT PASSED ON
        BOTH BENCHES — which is the defect, not the proof.

        `adapters/ubl_generator.py` could not see the company at all: it fell back
        to a compiled-in `DEFAULT_CURRENCY = "USD"`, so the assertion was true
        on a Finnish database for entirely the wrong reason and the bill was
        denominated in dollars. Asserting against `company.currency_id.name`
        makes the two benches answer differently, which is what a portability
        test is for. See the dual-core design.
        """
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
    # 🔴 The routing number must SURVIVE onto the bank account
    # ------------------------------------------------------------------

    def test_the_routing_number_lands_on_the_bank_account(self):
        """Verifying a number and then discarding it is worse than not checking.

        Core's `_import_ubl_invoice_add_partner_bank_values` collects exactly
        `./PaymentMeans/PayeeFinancialAccount/ID` and never reads
        `FinancialInstitutionBranch/ID`, so the routing number does not survive
        the import on its own. Without `_ai_record_routing_number` the vendor
        ends up with a bare US account number — which nobody can pay, because
        ACH needs both halves. That is precisely the state the REJECTION path
        refuses to create.

        Asserting on the XML would not catch this: the element is there and the
        account is still unpayable.

        ⚠️ REWRITTEN FOR 19.0.3.0.0. It used to let
        `_create_partner_from_extraction` MINT the vendor and its bank account.
        No vendor is created any more, so the vendor is created here by hand and
        matched by name — which is also the only shape of this scenario that can
        still occur in production.
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
        """The other path: the vendor already exists, so core's own
        `_import_ubl_retrieve_partner_bank` created the account and
        `_create_partner_from_extraction` never ran."""
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
    # 🔴 The checksums dispatch by SHAPE, not by field name
    # ------------------------------------------------------------------

    def test_a_misread_iban_is_caught_even_in_the_account_number_field(self):
        """The model choosing the wrong field is the reason the guards exist.

        Dispatching mod-97 only on `seller["iban"]` means a misread IBAN that
        Claude puts in `account_number` is recorded as a bank account unchecked.
        ⚠️ `base_iban` is NOT installed on this database, so Odoo does not
        validate it either — there is no second line of defence.
        """
        extracted = _us_extracted(
            seller={
                "iban": None,
                "routing_number": None,
                "account_number": "FI29123456000004521",  # one digit too many
            }
        )

        self._tree(extracted)

        self.assertEqual(
            extracted["seller"]["account_number_rejected"],
            "FI29123456000004521",
        )
        self.assertIsNone(extracted["seller"]["account_number"])

    def test_a_valid_iban_in_the_account_number_field_is_kept(self):
        """The control: shape-gating must not make the guard trigger-happy."""
        extracted = _us_extracted(
            seller={
                "iban": None,
                "routing_number": None,
                "account_number": "FI2912345600004521",  # valid
            }
        )

        self._tree(extracted)

        self.assertNotIn("account_number_rejected", extracted["seller"])

    def test_a_misread_routing_number_in_the_iban_field_is_caught(self):
        """The mirror case, for the same reason."""
        extracted = _us_extracted(
            seller={"iban": "021000012", "routing_number": None, "account_number": None}
        )

        self._tree(extracted)

        self.assertEqual(extracted["seller"]["iban_rejected"], "021000012")

    # ------------------------------------------------------------------
    # 🔴 Credit memo signs — a refund that increases what is owed
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
        """🔴 The `CreditNote` type code 381 ALREADY carries the sign.

        Negative amounts inside it negate a second time, and Odoo builds an
        `in_refund` from the result — a refund that INCREASES what we owe. There
        is no visible symptom at import; it looks like an ordinary credit note
        until somebody reconciles.

        Vendors print credit memos both ways, so the prompt alone cannot settle
        it. The generator normalises.
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
        """The control: normalisation keys off the document's own total, so a
        correctly-stated credit note must pass through unchanged."""
        tree = self._tree(self._credit_note(1))

        self.assertEqual(
            tree.find(".//cac:LegalMonetaryTotal/cbc:PayableAmount", NS).text,
            "1066.25",
        )

    def test_normalisation_does_not_mutate_the_stored_extraction(self):
        """The stored JSON is what a human edits and re-imports. Rewriting it
        underneath them would be its own surprise."""
        extracted = self._credit_note(-1)

        generate_peppol_invoice_xml(extracted)

        self.assertLess(extracted["totals"]["payable"], 0)

    def test_an_invoice_with_a_negative_total_is_left_alone(self):
        """Normalisation is for CreditNotes only — an Invoice carrying negative
        amounts means something else and is not ours to rewrite."""
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
        """🔴 Imports with slipped columns can create bad US states — say a
        second "New Jersey" whose code is "1" — which is what Odoo sends to a
        carrier, and the carrier refuses a booking over it.
        An AI extraction is the same class of input.

        ⚠️ REWRITTEN FOR 19.0.3.0.0, AND IT NOW PROVES SOMETHING STRONGER THAN
        IT DID. It used to pair with `test_a_known_state_is_set_on_the_vendor`,
        which asserted that `NJ` landed on the partner the import MINTED. Since
        no partner is minted, no address is written from an extraction at all,
        so neither a known nor an unknown state code can reach
        `res.country.state` — which is checked here for both.
        """
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
        """🔴 They used to land on whichever company installed the module, so
        the other company got an empty list, a silently generic prompt and a
        warning on every line."""
        Category = self.env["goodahead.expense.category"]
        shipped = self.env.ref(
            "goodahead_invoice_import.expense_category_goods"
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
        """The whole reason the category is shared and the account is not: the
        two companies have different charts of accounts."""
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
        """🔴 The SQL constraint cannot see this: two rows with the same code
        and a NULL company_id do not collide in PostgreSQL, because NULL is not
        equal to NULL. Without the Python check, `_apply_expense_categories`
        would silently pick one of them by id order."""
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
        """🔴 THE CALL SITE, not the field.

        `company_dependent` working is Odoo's business. What this module has to
        get right is calling `with_company(company)` with the BILL's company —
        a queue_job runs in whatever env enqueued it, which on the mail path is
        the alias user's company and need not be the bill's. Without the
        `with_company()` the account resolves against `env.company`, and since
        the two companies have different charts that is not a near miss.

        So this test deliberately runs with `env.company` set to the OTHER
        company and asserts the bill still gets its own company's account.
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

        # 🔴 The env is the OTHER company — the condition the bug needs.
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
        """The lookup must prefer the company's own row.

        🔴 It used to say `order="company_id desc, id asc"`, which does the
        OPPOSITE: PostgreSQL's DESC implies NULLS FIRST, so the shared row sorts
        to the top and wins. `_check_code_is_unambiguous` normally makes the
        pair impossible, so this is reached only through `sudo`/SQL or a future
        relaxation — which is exactly when nobody is watching.

        🔴 AND IT USED TO COPY THE LOOKUP INSTEAD OF CALLING IT (found in
        review). The two searches were written out again here and the test asserted
        its own result, so `_apply_expense_categories` could have been changed
        to anything at all and this would still have passed — a test that cannot
        vary with the thing it names. The lookup is now
        `account.move._ai_expense_category_for` and this calls it.
        """
        Category = self.env["goodahead.expense.category"]
        shared = Category.create({"code": "zz_pref", "name": "ZZ Shared"})
        # Bypass the Python constraint on purpose: the point is what the LOOKUP
        # does if the pair ever exists, not whether it can be created normally.
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
        # CONTROL: with no company-specific row the SHARED one is what the same
        # production call returns — so "it always returns `specific`" cannot
        # pass, and neither can "it always returns nothing".
        specific.unlink()
        self.assertEqual(
            self.env["account.move"]._ai_expense_category_for("zz_pref", self.company),
            shared,
        )

    def test_a_missing_account_names_the_company(self):
        """The multi-company failure must be legible. "No account configured"
        covered both "this code does not exist" and "nobody set an account for
        THIS company", and the second is the one multi-company makes likely."""
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
    """The alias is created by hand, so these guards live in the code.

    Whoever creates `billing@`, and whatever `alias_contact` they choose, one
    message cannot enqueue an unbounded number of paid extractions and a huge
    PDF is not sent at all.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.Import = cls.env["goodahead.invoice.import"]

    def _msg(self, attachments):
        return {
            "email_from": "vendor@example.com",
            "subject": "Invoices",
            "attachments": attachments,
        }

    def test_the_module_ships_no_alias(self):
        """🔴 Each installation creates its inbound alias by hand. A shipped alias
        would sit beside it as a second live inbound address that nobody
        expects, each message billing a Claude call.

        An earlier fork shipped `mail_alias_ai_invoice_import`, and that alias
        may be a deployment's live intake, so it must not be deleted. `migrations/19.0.3.32.0` releases the xmlid
        instead: the alias stays, owned by nobody, like one created by hand.
        After it, no database carries an alias owned by this module.
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
        """🔴 Multi-company must not stop at the email door.

        `company_id` defaults to `env.company`, which on the mail path is the
        alias user's company — the SAME one for every vendor. With two charts of
        accounts that means Beta's bills posted in the US deployment's books, and
        NOTHING complains: the wrong company's accounts resolve perfectly well.

        The answer is one alias per company carrying `alias_defaults`, which
        reaches `message_new` as `custom_values`. This asserts that path works,
        because `docs/SETUP.md` now tells the administrator to rely on it.
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
        """🔴 The failure has no error, so it must have a statement.

        A bill in the wrong company resolves accounts, posts, and complains
        about nothing. So which books it landed in is written on the record as
        a fact, rather than left to be discovered at reconciliation.
        """
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
        """And the configuration that misfiles silently says so on the record,
        not only in a log nobody reads."""
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
        """The fallback is the old behaviour, but it must be discoverable — a
        single-alias setup is the configuration that silently misfiles."""
        with self.assertLogs(
            "odoo.addons.goodahead_invoice_import.models.goodahead_invoice_import",
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
        """Silently dropping an attachment is how a bill goes missing without
        anybody knowing there was one."""
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
