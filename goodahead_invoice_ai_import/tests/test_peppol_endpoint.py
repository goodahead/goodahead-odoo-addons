# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The Peppol `EndpointID`, and countries without a VAT-based scheme.

The EAS scheme is taken from core's `EAS_MAPPING`, filtered to the entries core
resolves to a partner's VAT, and reconciled against core on every run. Borrowing
another country's scheme (e.g. Belgian `9925` for a Finnish VAT) makes core reject
the endpoint. A vendor from a country with no such scheme must import with no
`EndpointID`, never fail.
"""

from unittest.mock import patch

from lxml import etree

from odoo.tests import TransactionCase, tagged

from odoo.addons.account_edi_ubl_cii.models.account_edi_common import EAS_MAPPING
from odoo.addons.goodahead_invoice_ai_import.lib.ubl_generator import (
    generate_peppol_invoice_xml,
    peppol_endpoint_for_party,
    peppol_endpoint_is_valid,
    sanitize_peppol_endpoint,
    vat_peppol_eas_codes,
)
from odoo.addons.goodahead_invoice_ai_import.models.core_profile import (
    PROFILE_SEARCH_PLAN,
    PROFILE_SEARCH_PLAN_18,
)

from .profiles import skip_method_unless_profile

NS = {
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}


def _finnish_invoice():
    """An extraction of a Finnish invoice between two Finnish companies."""
    return {
        "type": "Invoice",
        "invoice_number": "INV/2026/00001",
        "issue_date": "2026-03-06",
        "due_date": "2026-03-20",
        "currency": "EUR",
        "seller": {
            "name": "Nordic Demo Supplier Oy",
            "vat": "FI23456780",
            "business_id": "2345678-0",
            "street": "Esimerkkikatu 1",
            "city": "HELSINKI",
            "state_code": None,
            "postal_code": "00100",
            "country_code": "FI",
            "routing_number": None,
            "account_number": None,
            "iban": "FI21 1234 5600 0007 85",
            "bic": "OKOYFIHH",
            "remit_to_address": None,
        },
        "buyer": {
            "name": "Nordic Demo Oy",
            "vat": "FI10000004",
            "street": "Esimerkkitie 2",
            "city": "Espoo",
            "state_code": None,
            "postal_code": "02100",
            "country_code": "FI",
        },
        "payment_reference": "2026000011",
        "payment_means_code": "30",
        "note": "Terms & Conditions: https://www.example.com/terms",
        "lines": [
            {
                "product_code": None,
                "name": "Test invoice",
                "description": "This is a test invoice",
                "quantity": 1,
                "unit_code": "C62",
                "price_unit": 1.0,
                "line_total": 1.0,
                "tax_percent": 25.5,
                "tax_amount": 0.26,
                "tax_category": "S",
                "expense_category": "other",
            }
        ],
        "tax_subtotals": [
            {
                "tax_percent": 25.5,
                "taxable_amount": 1.0,
                "tax_amount": 0.26,
                "tax_category": "S",
            }
        ],
        "totals": {
            "line_extension": 1.0,
            "tax_exclusive": 1.0,
            "tax_inclusive": 1.26,
            "payable": 1.26,
        },
    }


@tagged("post_install", "-at_install")
class TestPeppolEndpoint(TransactionCase):
    """The resolver, against core's own table and core's own rules."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.Partner = cls.env["res.partner"]

    # ------------------------------------------------------------------
    # Scheme selection
    # ------------------------------------------------------------------

    def test_a_finnish_vat_number_never_borrows_another_country_s_scheme(self):
        """The emitted scheme is one core's own table lists for FI, or none.

        `EAS_MAPPING['FI']` has no VAT scheme on the search-plan core and `0213`
        on the legacy one, so the expectation is read from core's table.
        """
        eas, endpoint = peppol_endpoint_for_party("FI", "FI23456780")
        fi_schemes = EAS_MAPPING.get("FI", {})
        vat_schemes = [
            code for code, field in fi_schemes.items() if field == "vat"
        ]

        self.assertNotEqual(
            eas, "9925", "9925 is Belgium's scheme; the bug was borrowing it"
        )
        if vat_schemes:
            self.assertIn(
                eas,
                vat_schemes,
                "a scheme we emit must be one core maps to a VAT for FI",
            )
            self.assertEqual(endpoint, "FI23456780")
        else:
            self.assertEqual(
                (eas, endpoint),
                (None, None),
                "core derives no Finnish VAT scheme on this build, so neither "
                "may we",
            )

    @skip_method_unless_profile(PROFILE_SEARCH_PLAN)
    def test_core_would_have_rejected_the_old_pair(self):
        """Control: the 19.0 search-plan core rejects a Finnish VAT under the
        Belgian scheme.

        18.0 cores (both builds) lack the per-scheme character rule and would
        accept the pair silently, so the test is gated.
        """
        error = self.Partner._build_error_peppol_endpoint("9925", "FI23456780")
        self.assertTrue(
            error,
            "core must still reject a Finnish VAT number under the Belgian "
            "scheme; if this ever passes, re-read why this module omits",
        )
        self.assertIn("not valid", error)

    # ------------------------------------------------------------------
    # The shape of the failure: a country nobody anticipated
    # ------------------------------------------------------------------

    def test_an_unanticipated_country_yields_no_endpoint_rather_than_an_error(self):
        """Anything core does not map to a VAT scheme yields `(None, None)`, not a raise."""
        for country, vat in (
            ("CN", "91310000MA1K35XXXX"),   # China — core maps nothing
            ("US", "12-3456789"),           # a US EIN
            ("NL", "NL123456789B01"),       # mapped, but to KvK/OIN, not VAT
            ("XX", "XX1"),                  # not a country at all
            ("FI", None),                   # no VAT on the document
            ("", ""),
        ):
            with self.subTest(country=country):
                self.assertEqual(peppol_endpoint_for_party(country, vat), (None, None))

    def test_a_country_and_a_vat_that_disagree_say_nothing(self):
        """A `DE` VAT on a US-addressed party is contradictory, so no pair is stated."""
        self.assertEqual(peppol_endpoint_for_party("US", "DE123456789"), (None, None))

    # ------------------------------------------------------------------
    # The controls — omission must stay scheme-driven, not a blanket removal
    # ------------------------------------------------------------------

    def test_countries_core_maps_to_their_vat_still_get_an_endpoint(self):
        for country, vat, expected_eas in (
            ("DE", "DE123456789", "9930"),
            ("BE", "BE0477472701", "9925"),
            ("FR", "FR12345678901", "9957"),
            ("SE", "SE123456789701", "9955"),
            ("IT", "IT12345678901", "0211"),
        ):
            with self.subTest(country=country):
                self.assertEqual(
                    peppol_endpoint_for_party(country, vat), (expected_eas, vat)
                )

    def test_the_vat_prefix_alone_is_enough(self):
        """Core's decoder may hand us a party with no country element at all."""
        self.assertEqual(peppol_endpoint_for_party(None, "DE123456789"), ("9930", "DE123456789"))

    def test_a_non_string_country_or_vat_does_not_raise(self):
        """Non-string values from the extraction JSON must not raise.

        A numeric country contributes nothing; any scheme returned must come from
        the VAT's own prefix through core's table.
        """
        for country, vat in (
            (358, "FI23456780"),
            (1, "123456789"),
            (49, 123456789),
            (None, 0),
            (0, 0),
        ):
            with self.subTest(country=country, vat=vat):
                eas, endpoint = peppol_endpoint_for_party(country, vat)
                if eas is None:
                    self.assertIsNone(endpoint)
                    continue
                # A scheme may only come from the VAT's own prefix via core's table.
                prefix = str(vat)[:2].upper()
                self.assertTrue(prefix.isalpha(), "a number is not a country")
                self.assertEqual(
                    EAS_MAPPING.get(prefix, {}).get(eas),
                    "vat",
                    "core must map this scheme to a VAT for %s" % prefix,
                )

    def test_a_number_is_not_a_country_code(self):
        """A numeric `country_code` contributes nothing, on any core."""
        for country in (358, 1, 49, 0, None, True, False):
            with self.subTest(country=country):
                self.assertEqual(
                    peppol_endpoint_for_party(country, "123456789"), (None, None)
                )

    def test_a_boolean_country_does_not_become_turkey(self):
        """`str(True)[:2].upper()` is `"TR"`, which core maps to Turkey's scheme.

        The alphabetic guard keeps a boolean from inventing that scheme.
        """
        self.assertEqual(peppol_endpoint_for_party(True, "123456789"), (None, None))
        self.assertEqual(peppol_endpoint_for_party(True, "DE123456789"), ("9930", "DE123456789"))

    def test_a_numeric_country_is_absent_rather_than_contradictory(self):
        """Any numeric country is treated as absent, so the VAT prefix decides."""
        self.assertEqual(
            peppol_endpoint_for_party(49, "DE123456789"), ("9930", "DE123456789")
        )
        self.assertEqual(
            peppol_endpoint_for_party(0, "DE123456789"), ("9930", "DE123456789")
        )

    def test_a_non_string_value_is_still_usable_not_swallowed(self):
        """Control: non-string values are converted, not swallowed, and still resolve."""
        self.assertEqual(
            peppol_endpoint_for_party("DE", 123456789), ("9930", "123456789")
        )
        self.assertEqual(
            peppol_endpoint_for_party(0, "DE123456789"), ("9930", "DE123456789")
        )

    def test_a_non_string_country_does_not_fail_the_whole_document(self):
        """End to end: a numeric country and VAT still generate a document with both parties."""
        data = _finnish_invoice()
        data["seller"]["country_code"] = 358
        data["seller"]["vat"] = 23456780
        tree = etree.fromstring(generate_peppol_invoice_xml(data))
        self.assertIsNotNone(tree.find(".//cac:AccountingSupplierParty", NS))
        self.assertIsNotNone(tree.find(".//cac:AccountingCustomerParty", NS))
        self.assertEqual(
            tree.find(
                ".//cac:AccountingSupplierParty//cac:PartyLegalEntity"
                "/cbc:RegistrationName",
                NS,
            ).text,
            "Nordic Demo Supplier Oy",
        )

    # ------------------------------------------------------------------
    # Reconciliation with core
    # ------------------------------------------------------------------

    def test_our_schemes_are_cores(self):
        """Every EAS code this module can emit is in core's `peppol_eas` selection."""
        selection = dict(self.Partner._fields["peppol_eas"].selection)
        emitted = {
            eas for country in EAS_MAPPING for eas in vat_peppol_eas_codes(country)
        }
        self.assertTrue(emitted, "the resolver must be able to emit something")
        self.assertFalse(
            emitted - set(selection),
            "EAS codes emitted by this module but unknown to core",
        )

    def test_our_validator_agrees_with_cores(self):
        """`peppol_endpoint_is_valid` never accepts what core rejects, for every
        scheme we emit, and accepts a plain 10-digit number wherever core does.

        It reimplements core's character/length checks and the per-scheme format
        rules core has on its newest build. It may be stricter than an older
        core (that only drops an endpoint), never looser. Probes are sanitised
        per scheme first.
        """
        for country in EAS_MAPPING:
            for eas in vat_peppol_eas_codes(country):
                for raw in ("AB1234567890", "1234567890"):
                    probe = sanitize_peppol_endpoint(raw, eas)
                    core_rejects = bool(
                        self.Partner._build_error_peppol_endpoint(eas, probe)
                    )
                    ours_accepts = peppol_endpoint_is_valid(eas, probe)
                    with self.subTest(country=country, eas=eas, probe=probe):
                        if core_rejects:
                            self.assertFalse(
                                ours_accepts,
                                f"this module would emit an EAS {eas} endpoint "
                                f"({country}) that core rejects; core's rules "
                                "have moved",
                            )
                        if raw == "1234567890":
                            self.assertEqual(
                                ours_accepts,
                                not core_rejects,
                                f"core and this module disagree about a plain "
                                f"10-digit EAS {eas} endpoint ({country})",
                            )

    def test_the_validator_can_still_say_no(self):
        """Control: the validator rejects invalid endpoints."""
        self.assertFalse(peppol_endpoint_is_valid("9925", "FI23456780"))
        self.assertFalse(peppol_endpoint_is_valid("9930", "DE 123 456"))
        self.assertFalse(peppol_endpoint_is_valid("9930", "D" * 51))
        self.assertTrue(peppol_endpoint_is_valid("9930", "DE123456789"))


@tagged("post_install", "-at_install")
class TestFinnishInvoiceImports(TransactionCase):
    """Finnish invoices end to end through the real decoder, mocking only the Claude call.

    The vendor must not exist beforehand: core writes `peppol_eas` /
    `peppol_endpoint` only when it creates a partner, so reusing a partner would
    bypass the endpoint check.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.company.claude_api_key = "test-key"
        cls.AM = cls.env["account.move"]

    def _new_import(self):
        att = self.env["ir.attachment"].create(
            {
                "name": "finnish-invoice.pdf",
                "raw": b"%PDF-1.4 fake",
                "mimetype": "application/pdf",
            }
        )
        return self.env["ai.invoice.import"].create(
            {
                "name": "finnish-invoice.pdf",
                "company_id": self.company.id,
                "pdf_attachment_id": att.id,
            }
        )

    def _run(self, extracted):
        xml_bytes = self.AM._ai_generate_ubl_from_extracted(extracted, self.company)
        rec = self._new_import()
        with patch.object(
            type(self.AM),
            "_ai_extract_and_generate_ubl",
            return_value={
                "extracted": extracted,
                "usage": {"input_tokens": 4292, "output_tokens": 641},
                "model_name": "test-model",
                "xml_bytes": xml_bytes,
            },
        ):
            result = rec._process_import()
        return rec, result

    def test_the_sample_invoice_now_imports(self):
        """A Finnish invoice imports without a "Peppol endpoint is not valid" error."""
        rec, result = self._run(_finnish_invoice())

        self.assertTrue(result.startswith("OK"), result)
        self.assertEqual(rec.state, "done", rec.error_message or "")
        self.assertTrue(rec.move_id, "a vendor bill must be created and linked")
        # No line is added for an unmapped tax; without a 25.5% purchase tax the
        # bill shows the mismatch banner instead.
        self.assertAlmostEqual(rec.move_id.amount_untaxed, 1.0, places=2)
        self.assertTrue(rec.move_id.ai_total_mismatch)
        # The vendor is unknown and no vendor is invented.
        self.assertFalse(rec.move_id.partner_id)

    def test_the_finnish_parties_carry_only_a_scheme_core_derives(self):
        """Any `EndpointID` on either Finnish party uses a scheme core lists for FI.

        The search-plan core has none (no element); the legacy core maps `0213`.
        """
        tree = etree.fromstring(
            self.AM._ai_generate_ubl_from_extracted(_finnish_invoice(), self.company)
        )
        fi_vat_schemes = [
            code
            for code, field in EAS_MAPPING.get("FI", {}).items()
            if field == "vat"
        ]
        for role in ("AccountingSupplierParty", "AccountingCustomerParty"):
            node = tree.find(".//cac:%s//cbc:EndpointID" % role, NS)
            if not fi_vat_schemes:
                self.assertIsNone(node, role)
                continue
            self.assertIsNotNone(
                node,
                "core derives a Finnish VAT scheme on this build, so the "
                "endpoint belongs on the document (%s)" % role,
            )
            self.assertIn(node.get("schemeID"), fi_vat_schemes, role)

    def test_the_finnish_vat_still_reaches_the_bill(self):
        """The VAT still reaches `PartyTaxScheme/CompanyID`, which core's VAT matching reads."""
        tree = etree.fromstring(
            self.AM._ai_generate_ubl_from_extracted(_finnish_invoice(), self.company)
        )
        company_id = tree.find(
            ".//cac:AccountingSupplierParty//cac:PartyTaxScheme/cbc:CompanyID", NS
        )
        self.assertIsNotNone(company_id)
        self.assertEqual(company_id.text, "FI23456780")

    def test_a_vendor_with_an_iban_and_no_aba_still_imports(self):
        """The Finnish vendor's IBAN reaches the UBL `PaymentMeans`."""
        tree = etree.fromstring(
            self.AM._ai_generate_ubl_from_extracted(_finnish_invoice(), self.company)
        )
        account = tree.find(
            ".//cac:PaymentMeans/cac:PayeeFinancialAccount/cbc:ID", NS
        )
        self.assertIsNotNone(account, "the IBAN must still reach the UBL")
        # The vendor prints it in groups of four; core sanitises it downstream.
        self.assertEqual(account.text.replace(" ", ""), "FI2112345600000785")

    def test_a_vendor_with_neither_iban_nor_aba_still_imports(self):
        """A vendor with no payable account (e.g. Chinese, or US paid by check) imports."""
        extracted = _finnish_invoice()
        extracted["invoice_number"] = "CN-2026-0001"
        extracted["currency"] = "USD"
        extracted["seller"].update(
            {
                "name": "ZZ Shenzhen Catering Equipment Co Ltd",
                "vat": None,
                "business_id": "91440300MA5EXXXXXX",
                "city": "Shenzhen",
                "postal_code": "518000",
                "country_code": "CN",
                "iban": None,
                "bic": None,
            }
        )
        extracted["payment_reference"] = None

        rec, result = self._run(extracted)

        self.assertTrue(result.startswith("OK"), result)
        self.assertEqual(rec.state, "done", rec.error_message or "")
        self.assertTrue(rec.move_id)
        # The vendor is unknown, so the bill says so in the chatter.
        self.assertTrue(
            rec.move_id.message_ids.filtered(
                lambda m: "not recognised" in (m.body or "")
            ),
            "an unrecognised vendor must be reported on the bill",
        )

    def test_a_german_vendor_is_still_identified_by_its_endpoint(self):
        """Control: a German vendor's `9930` pair reaches the XML and core matches on it.

        The vendor exists with that pair and an unrelated name, so only core's
        Peppol lookup can find it (the Peppol domain in
        `account.edi.common._import_partner` on Odoo 18.0,
        `_import_retrieve_customer_from_eas_endpoint` on the search-plan core).
        """
        extracted = _finnish_invoice()
        extracted["invoice_number"] = "DE-2026-0001"
        extracted["seller"].update(
            {
                "name": "ZZ Grosskuechen GmbH",
                "vat": "DE123456788",
                "business_id": "HRB 12345",
                "city": "Koeln",
                "postal_code": "50667",
                "country_code": "DE",
            }
        )
        vendor = self.env["res.partner"].create(
            {
                # A name that matches nothing on the document.
                "name": "ZZ A Completely Unrelated German Trading Name",
                "is_company": True,
                "country_id": self.env.ref("base.de").id,
                "peppol_eas": "9930",
                "peppol_endpoint": "DE123456788",
            }
        )

        tree = etree.fromstring(
            self.AM._ai_generate_ubl_from_extracted(extracted, self.company)
        )
        endpoint = tree.find(".//cac:AccountingSupplierParty//cbc:EndpointID", NS)
        self.assertIsNotNone(endpoint, "a German vendor must keep its endpoint")
        self.assertEqual(endpoint.get("schemeID"), "9930")
        self.assertEqual(endpoint.text, "DE123456788")

        rec, result = self._run(extracted)

        self.assertTrue(result.startswith("OK"), result)
        self.assertEqual(
            rec.move_id.partner_id,
            vendor,
            "core did not match the vendor on the Peppol pair we emitted",
        )


@tagged("post_install", "-at_install")
class TestGeneratedXmlIsStillValidUbl(TransactionCase):
    """A sanity check that omitting the element did not break the document."""

    def test_the_document_still_parses_and_keeps_its_parties(self):
        xml = generate_peppol_invoice_xml(_finnish_invoice())
        tree = etree.fromstring(xml)
        self.assertIsNotNone(tree.find(".//cac:AccountingSupplierParty", NS))
        self.assertIsNotNone(tree.find(".//cac:AccountingCustomerParty", NS))
        self.assertEqual(
            tree.find(
                ".//cac:AccountingSupplierParty//cac:PartyLegalEntity"
                "/cbc:RegistrationName",
                NS,
            ).text,
            "Nordic Demo Supplier Oy",
        )
