# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The Peppol `EndpointID`, and the countries nobody anticipated.

🔴 THE BUG THESE TESTS EXIST FOR. The module carried a hand-written map that
said Finland was EAS `9925`. `9925` is BELGIAN VAT: core validates it with
`[^beBE0-9]`, so a Finnish vendor VAT such as `FI00000211` was rejected at
partner creation with *"The Peppol endpoint (FI00000211) is not valid. It
should contain only letters and digit."* and the whole import ended `failed`.

Two things had to be true for that to happen, and both are fixed here:

1. The map was ours. It is now core's `EAS_MAPPING`, filtered to the entries
   core itself resolves to a partner's VAT number. `test_our_schemes_are_cores`
   reconciles it against core on every run, so the next core change is a test
   failure rather than a production one.
2. The test beside it asserted `9925` for a Finnish vendor, reading back the
   same wrong table the generator wrote from. A test that shares the belief
   under test cannot fail. The reconciliation test shares nothing with the
   generator.

And the shape of the failure, not just the instance: the US deployment buys from the
United States, from Finland and from China, and the list is open. A vendor from
a country with no honest Peppol scheme must IMPORT, with no `EndpointID`. It
must never fail.
"""

from unittest.mock import patch

from lxml import etree

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_import.models.core_profile import (
    PROFILE_SEARCH_PLAN,
)

from .profiles import skip_method_unless_profile

from odoo.addons.account_edi_ubl_cii.models.account_edi_common import EAS_MAPPING
from odoo.addons.account_edi_ubl_cii.models.res_partner import (
    sanitize_peppol_endpoint,
)
from odoo.addons.goodahead_invoice_import.adapters.ubl_generator import (
    generate_peppol_invoice_xml,
    peppol_endpoint_for_party,
    peppol_endpoint_is_valid,
    vat_peppol_eas_codes,
)

NS = {
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}


def _testitekniikka():
    """🔴 THE SHAPE OF EXTRACTION THAT FAILED.

    Shaped like the `extraction_data` of a Finnish import that failed, with
    invented names, identifiers and addresses. The extraction
    itself succeeded, so everything here has the shape of what Claude actually
    returned from a Finnish invoice. The failure was entirely in the step after
    it.
    """
    return {
        "type": "Invoice",
        "invoice_number": "INV/2026/00001",
        "issue_date": "2026-03-06",
        "due_date": "2026-03-20",
        "currency": "EUR",
        "seller": {
            "name": "Testitekniikka Oy",
            "vat": "FI00000211",
            "business_id": "0000021-1",
            "street": "Testikatu 1 B10",
            "city": "HELSINKI",
            "state_code": None,
            "postal_code": "00100",
            "country_code": "FI",
            "routing_number": None,
            "account_number": None,
            "iban": "FI10 1234 5600 0000 13",
            "bic": "OKOYFIHH",
            "remit_to_address": None,
        },
        "buyer": {
            "name": "ZZ Buyer Oy",
            "vat": "FI00000094",
            "street": "Testitie 2",
            "city": "Nummela",
            "state_code": None,
            "postal_code": "03100",
            "country_code": "FI",
        },
        "payment_reference": "2026000011",
        "payment_means_code": "30",
        "note": "Terms & Conditions: https://www.testitekniikka.test/terms",
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
    # The bug itself
    # ------------------------------------------------------------------

    def test_a_finnish_vat_number_never_borrows_another_country_s_scheme(self):
        """🔴 FAILS WITHOUT THE FIX — it returned `('9925', 'FI00000211')`, the
        BELGIAN scheme, from a hand-written table of this module's own.

        ⚠️ This test used to assert `(None, None)` flat, and that is a reading of
        ONE core's table, not the rule. `EAS_MAPPING['FI']` is
        `{'0216': None}` on the search-plan core and
        `{'0216': None, '0213': 'vat'}` on the legacy one — so on the legacy
        build core itself derives `('0213', <the VAT>)`, and emitting it is
        right there. The rule the fix encodes is narrower and true on both:
        **the scheme we emit is one CORE's own table lists for that country, or
        there is none.** Hard-coding one core's answer turned that rule into a
        bench reading, which is how the five profile tests next door went wrong.
        """
        eas, endpoint = peppol_endpoint_for_party("FI", "FI00000211")
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
            self.assertEqual(endpoint, "FI00000211")
        else:
            self.assertEqual(
                (eas, endpoint),
                (None, None),
                "core derives no Finnish VAT scheme on this build, so neither "
                "may we",
            )

    @skip_method_unless_profile(PROFILE_SEARCH_PLAN)
    def test_core_would_have_rejected_the_old_pair(self):
        """The control that makes the test above mean something — on the build
        where core does the rejecting.

        🔴 GATED, AND THE REASON IS ITSELF THE FINDING. The search-plan core
        rejects `('9925', 'FI00000211')` through
        `PEPPOL_ENDPOINT_INVALID_CHARS_RE_BY_EAS['9925']`, which the LEGACY core
        does not have at all: its `_build_error_peppol_endpoint`
        (`account_edi_ubl_cii/models/res_partner.py:289`) checks only the
        generic character class and the length, both of which `FI00000211`
        passes. *(measured on the legacy bench — the call returns `None`.)*

        So on the legacy core the old bug would not have raised. It would have
        written a Belgian-scheme endpoint onto a Finnish vendor and imported
        cleanly, and core matches partners on exactly that pair. The louder
        failure was the kinder build; deriving the scheme from core's own table
        is what protects the quieter one, and that is asserted above on both.
        """
        error = self.Partner._build_error_peppol_endpoint("9925", "FI00000211")
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
        """🔴 THE RULE, not the instance.

        China is in the list because bills do arrive from there, but
        the point is the opposite of a country list: `peppol_endpoint_for_party`
        answers `(None, None)` for anything core does not map, and the answer is
        a quiet omission rather than a guess or a raise.
        """
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
        """A `DE` VAT number on a party addressed in the US: one of the two is
        wrong and we do not know which, so no pair is stated."""
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
        """🔴 The type of these values is whatever Claude's JSON said it was.

        `country_code` and `vat` arrive from an extraction, not from the ORM. A
        VAT number printed as bare digits, or a country field the model emitted
        as a number, reaches this function as an `int` — and `.strip()` on an
        `int` is an `AttributeError`, which propagates out of
        `generate_peppol_invoice_xml` and FAILS THE WHOLE IMPORT. That is the
        exact outcome this module exists to prevent: a party we cannot address
        on Peppol must import with no `EndpointID`, never crash.

        Before `peppol_endpoint_for_party` existed, both values only ever
        reached `_add_text`, which does its own `str()` and so was immune. The
        consumer is new; the exposure came with it.

        A `358` that is really a dialling code, and a `49` that is really a
        country calling code, are neither of them countries — so the number
        contributes nothing and only the VAT's own prefix can speak. The
        assertion that matters is that no exception leaves any of these calls.

        ⚠️ The first case used to assert `(None, None)` and that was one core's
        answer, not the rule: `358` says nothing, but `"FI00000211"` carries its
        own `FI` prefix, and on the legacy core `EAS_MAPPING['FI']` DOES map a
        VAT scheme — so the honest expectation is "whatever core's table
        derives, and nothing else". A number is still never read as a country:
        that is `test_a_number_is_not_a_country_code` below, which holds on both.
        """
        for country, vat in (
            (358, "FI00000211"),
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
                # A scheme came back. It may only have come from the VAT's OWN
                # two-letter prefix, through core's own table.
                prefix = str(vat)[:2].upper()
                self.assertTrue(prefix.isalpha(), "a number is not a country")
                self.assertEqual(
                    EAS_MAPPING.get(prefix, {}).get(eas),
                    "vat",
                    "core must map this scheme to a VAT for %s" % prefix,
                )

    def test_a_number_is_not_a_country_code(self):
        """The half of the test above that is build-independent: a numeric
        `country_code` contributes NOTHING, whatever core's table says. With a
        VAT that carries no country prefix of its own there is nothing left to
        derive from, so the answer is `(None, None)` on any core."""
        for country in (358, 1, 49, 0, None, True, False):
            with self.subTest(country=country):
                self.assertEqual(
                    peppol_endpoint_for_party(country, "123456789"), (None, None)
                )

    def test_a_boolean_country_does_not_become_turkey(self):
        """🔴 `str()` CAN INVENT LETTERS, AND THIS ONE INVENTS A REAL COUNTRY.

        `str(True)[:2].upper()` is `"TR"`. Core maps `TR` to EAS `9952`,
        Turkey — so without the alphabetic guard a `True` in the country field
        produces `('9952', '123456789')`: a confident, valid-looking,
        completely invented scheme on a vendor nobody checked. Core matches
        partners on exactly that pair.

        This is a REGRESSION THE str() FIX WOULD HAVE INTRODUCED. Before it,
        `True` raised `AttributeError` — loud and wrong in the other direction.
        Turning a loud failure into a silent wrong answer is the one trade this
        module may never make.
        """
        self.assertEqual(peppol_endpoint_for_party(True, "123456789"), (None, None))
        self.assertEqual(peppol_endpoint_for_party(True, "DE123456789"), ("9930", "DE123456789"))

    def test_a_numeric_country_is_absent_rather_than_contradictory(self):
        """One answer for one piece of non-information.

        `0` used to be read as "no country" and `49` as "a country that
        contradicts the VAT prefix" — the same nothing, answered two ways, with
        the second silently suppressing an endpoint that the first allowed.
        Neither is a country code, so neither says anything now and the VAT's
        own prefix decides in both cases.
        """
        self.assertEqual(
            peppol_endpoint_for_party(49, "DE123456789"), ("9930", "DE123456789")
        )
        self.assertEqual(
            peppol_endpoint_for_party(0, "DE123456789"), ("9930", "DE123456789")
        )

    def test_a_non_string_value_is_still_usable_not_swallowed(self):
        """The controls for the test above — `str()`, not a bare `except`.

        If the fix had been to catch the `AttributeError` and return
        `(None, None)`, every assertion in the test above would still pass and
        the resolver would have quietly stopped resolving. These two must
        resolve.

        - A German VAT number arriving as bare digits beside a `"DE"` country
          still gets its `9930` endpoint.
        - A falsy non-string country (`0`) is absent, not contradictory, so the
          VAT's own prefix still decides.
        """
        self.assertEqual(
            peppol_endpoint_for_party("DE", 123456789), ("9930", "123456789")
        )
        self.assertEqual(
            peppol_endpoint_for_party(0, "DE123456789"), ("9930", "DE123456789")
        )

    def test_a_non_string_country_does_not_fail_the_whole_document(self):
        """End to end, because the unit above would pass on a swallowed error.

        The document still generates and still carries both parties. Without
        `str()` this call raises `AttributeError` out of
        `generate_peppol_invoice_xml` and the import is marked failed.
        """
        data = _testitekniikka()
        data["seller"]["country_code"] = 358
        data["seller"]["vat"] = 211
        tree = etree.fromstring(generate_peppol_invoice_xml(data))
        self.assertIsNotNone(tree.find(".//cac:AccountingSupplierParty", NS))
        self.assertIsNotNone(tree.find(".//cac:AccountingCustomerParty", NS))
        self.assertEqual(
            tree.find(
                ".//cac:AccountingSupplierParty//cac:PartyLegalEntity"
                "/cbc:RegistrationName",
                NS,
            ).text,
            "Testitekniikka Oy",
        )

    # ------------------------------------------------------------------
    # Reconciliation with core — the test that replaces a hand-written table
    # ------------------------------------------------------------------

    def test_our_schemes_are_cores(self):
        """Every EAS code this module can emit must be one core offers.

        A code that is not in core's `peppol_eas` selection cannot be written to
        a partner at all, so it would fail the import exactly the way `9925`
        did — just later and for a different reason.
        """
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
        """🔴 THE DRIFT DETECTOR.

        `peppol_endpoint_is_valid` reimplements only the character/length tail of
        core's `_build_error_peppol_endpoint`. That is sound today because the
        four EAS codes carrying extra format rules (`0208`, `0009`, `0007`,
        `EM`) are none of them `'vat'` entries — but "today" is a claim about
        core, so it is asked of core on every run instead of being written in a
        comment.

        The probe is sanitised per scheme first, so the Belgian rule (which
        allows only `b`, `e`, `B`, `E` and digits) is not failed by the probe's
        own letters. If core ever adds a rule to a scheme we emit — a digit
        count, a checksum — core will reject the probe while we accept it, and
        this fails.
        """
        for country in EAS_MAPPING:
            for eas in vat_peppol_eas_codes(country):
                probe = sanitize_peppol_endpoint("AB1234567890", eas)
                with self.subTest(country=country, eas=eas):
                    self.assertEqual(
                        bool(self.Partner._build_error_peppol_endpoint(eas, probe)),
                        not peppol_endpoint_is_valid(eas, probe),
                        f"core and this module disagree about EAS {eas} "
                        f"({country}); core's rules have moved",
                    )

    def test_the_validator_can_still_say_no(self):
        """The control for the two tests above: a validator that always returns
        True would satisfy both halves of the comparison for every passing case
        and prove nothing."""
        self.assertFalse(peppol_endpoint_is_valid("9925", "FI00000211"))
        self.assertFalse(peppol_endpoint_is_valid("9930", "DE 123 456"))
        self.assertFalse(peppol_endpoint_is_valid("9930", "D" * 51))
        self.assertTrue(peppol_endpoint_is_valid("9930", "DE123456789"))


@tagged("post_install", "-at_install")
class TestFinnishInvoiceImports(TransactionCase):
    """The Finnish failure, replayed end to end through the real decoder.

    The unit tests above would pass against a resolver that is right and a
    generator that ignores it, so this class asserts on the vendor bill Odoo
    actually builds — mocking only the paid Claude call, exactly as
    `test_import_end_to_end` does.

    🔴 THE VENDOR MUST NOT EXIST BEFOREHAND. Core writes `peppol_eas` /
    `peppol_endpoint` only into `partner_create_values` — so an import that
    REUSES a partner never touches the constraint, and the bug is invisible.
    That is why the module's existing Finnish end-to-end test passed while
    the import failed: its vendor was pre-created. These use a vendor nothing has
    seen.
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
                "name": "testitekniikka.pdf",
                "raw": b"%PDF-1.4 fake",
                "mimetype": "application/pdf",
            }
        )
        return self.env["goodahead.invoice.import"].create(
            {
                "name": "testitekniikka.pdf",
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

    def test_the_finnish_invoice_now_imports(self):
        """🔴 FAILS WITHOUT THE FIX, with the original error:
        "The Peppol endpoint (FI00000211) is not valid."
        """
        rec, result = self._run(_testitekniikka())

        self.assertTrue(result.startswith("OK"), result)
        self.assertEqual(rec.state, "imported", rec.error_message or "")
        self.assertTrue(rec.move_id, "a vendor bill must be created and linked")
        # 19.0.3.31.0: no line is added for an unmapped tax any more, so the
        # bill is the document's one line. This test database has no 25.5%
        # purchase tax, so the bill shows the red banner instead.
        self.assertAlmostEqual(rec.move_id.amount_untaxed, 1.0, places=2)
        self.assertTrue(rec.move_id.ai_total_mismatch)
        # ⚠️ The vendor-name assertion that used to stand here is gone with
        # 19.0.3.0.0: Testitekniikka Oy matches no partner in a test database, and no
        # vendor is invented any more — neither by this module nor by core's
        # `_import_ubl_create_missing_customer`, which the module now suppresses
        # for its own decodes. What this test is ABOUT is unchanged and is
        # asserted above: the Peppol endpoint no longer fails the import.
        self.assertFalse(rec.move_id.partner_id)

    def test_the_finnish_parties_carry_only_a_scheme_core_derives(self):
        """Why it imports: neither party carries an endpoint this module made up.

        The seller is the one that failed; the buyer is Finnish too
        and would have failed next.

        ⚠️ Restated in 19.0.3.7.0. It asserted the element was ABSENT, which is
        the search-plan core's answer because its `EAS_MAPPING['FI']` is
        `{'0216': None}`. The legacy core adds `'0213': 'vat'` and therefore
        emits an `EndpointID`, correctly — Finland's `0213` really is the VAT
        scheme on that build. What must hold on both is that any scheme present
        is one core's own table lists for FI, never a borrowed one.
        """
        tree = etree.fromstring(
            self.AM._ai_generate_ubl_from_extracted(_testitekniikka(), self.company)
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
        """Omitting the endpoint must not cost the VAT number.

        `PartyTaxScheme/CompanyID` is untouched, and it is what core's own VAT
        matching step reads — so the vendor is still identified, by the field
        that actually identifies it.
        """
        tree = etree.fromstring(
            self.AM._ai_generate_ubl_from_extracted(_testitekniikka(), self.company)
        )
        company_id = tree.find(
            ".//cac:AccountingSupplierParty//cac:PartyTaxScheme/cbc:CompanyID", NS
        )
        self.assertIsNotNone(company_id)
        self.assertEqual(company_id.text, "FI00000211")

    def test_a_vendor_with_an_iban_and_no_aba_still_imports(self):
        """The Finnish vendor pays by IBAN. Keeping the IBAN path beside the US
        ABA work was not redundancy — it is the only payment vocabulary this
        vendor has."""
        tree = etree.fromstring(
            self.AM._ai_generate_ubl_from_extracted(_testitekniikka(), self.company)
        )
        account = tree.find(
            ".//cac:PaymentMeans/cac:PayeeFinancialAccount/cbc:ID", NS
        )
        self.assertIsNotNone(account, "the IBAN must still reach the UBL")
        # The vendor prints it in groups of four; core sanitises it downstream.
        self.assertEqual(account.text.replace(" ", ""), "FI1012345600000013")

    def test_a_vendor_with_neither_iban_nor_aba_still_imports(self):
        """A Chinese vendor will have neither, and neither will a US one that
        expects a check. Nothing may depend on a payable account existing."""
        extracted = _testitekniikka()
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
        self.assertEqual(rec.state, "imported", rec.error_message or "")
        self.assertTrue(rec.move_id)
        # ⚠️ 19.0.3.0.0: the vendor is unknown, so the bill arrives WITHOUT one
        # and says so in the chatter. The point of this test — that nothing may
        # depend on a payable account existing — is the three assertions above.
        self.assertTrue(
            rec.move_id.message_ids.filtered(
                lambda m: "not recognised" in (m.body or "")
            ),
            "an unrecognised vendor must be reported on the bill",
        )

    def test_a_german_vendor_is_still_identified_by_its_endpoint(self):
        """The end-to-end control: the endpoint is omitted where no scheme
        applies, not everywhere. A German vendor's `9930` pair reaches the XML
        and core MATCHES a vendor on it.

        ⚠️ REWRITTEN FOR 19.0.3.0.0, AND IT IS A STRONGER CONTROL THAN IT WAS.
        It used to read the pair back off the partner core had CREATED, which no
        longer happens — and reading back a value you just wrote never proved
        the pair was usable anyway. The vendor is now created here carrying that
        exact pair, so the assertion is that core's own
        `_import_retrieve_customer_from_eas_endpoint` step FOUND it, which is
        what emitting an `EndpointID` is for.
        """
        extracted = _testitekniikka()
        extracted["invoice_number"] = "DE-2026-0001"
        extracted["seller"].update(
            {
                "name": "ZZ Grosskuechen GmbH",
                "vat": "DE100000008",
                "business_id": "HRB 12345",
                "city": "Koeln",
                "postal_code": "50667",
                "country_code": "DE",
            }
        )
        vendor = self.env["res.partner"].create(
            {
                # A name that matches NOTHING on the document, so the only step
                # that can resolve this vendor is the Peppol pair.
                "name": "ZZ A Completely Unrelated German Trading Name",
                "is_company": True,
                "country_id": self.env.ref("base.de").id,
                "peppol_eas": "9930",
                "peppol_endpoint": "DE100000008",
            }
        )

        tree = etree.fromstring(
            self.AM._ai_generate_ubl_from_extracted(extracted, self.company)
        )
        endpoint = tree.find(".//cac:AccountingSupplierParty//cbc:EndpointID", NS)
        self.assertIsNotNone(endpoint, "a German vendor must keep its endpoint")
        self.assertEqual(endpoint.get("schemeID"), "9930")
        self.assertEqual(endpoint.text, "DE100000008")

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
        xml = generate_peppol_invoice_xml(_testitekniikka())
        tree = etree.fromstring(xml)
        self.assertIsNotNone(tree.find(".//cac:AccountingSupplierParty", NS))
        self.assertIsNotNone(tree.find(".//cac:AccountingCustomerParty", NS))
        self.assertEqual(
            tree.find(
                ".//cac:AccountingSupplierParty//cac:PartyLegalEntity"
                "/cbc:RegistrationName",
                NS,
            ).text,
            "Testitekniikka Oy",
        )
