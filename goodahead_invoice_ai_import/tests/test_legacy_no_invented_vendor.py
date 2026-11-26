# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The legacy core's vendor creation in `_import_partner` is suppressed too.

The module never invents a vendor. On the search-plan core the creation happens in
`_import_ubl_create_missing_customer`; on the legacy core it happens in
`account.edi.common._import_partner` behind `if not partner and name and vat`.
Suppression also keeps `partner_id` empty, which is what enables the
unrecognised-vendor note and candidate cards.

Controls: without the context key core still creates; a matching vendor still comes
back unchanged; the search-plan build never enters the seam.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_import.adapters.ubl_generator import (
    generate_peppol_invoice_xml,
)
from odoo.addons.goodahead_invoice_import.models.core_profile import (
    PROFILE_LEGACY,
    PROFILE_SEARCH_PLAN,
    PROFILE_SEARCH_PLAN_18,
)

from .profiles import skip_method_unless_profile, skip_unless_profile

# A German VAT that passes `base_vat`'s check; foreign so it cannot accidentally
# match a Finnish company's own number.
UNKNOWN_VAT = "DE123456788"
UNKNOWN_NAME = "ZZ Never Invented GmbH"


class _SuppressionHarness(TransactionCase):
    """A document from an unknown vendor, printing a tax id.

    The tax id matters: core's create guard is `name and vat`, so without one the
    create is never reached and the override would go untested.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.company.claude_api_key = "test-key"
        cls.Partner = cls.env["res.partner"]
        cls.AM = cls.env["account.move"]
        cls.EdiCommon = cls.env["account.edi.common"]
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", cls.company.id)],
            limit=1,
        )

    # -- fixtures ------------------------------------------------------

    def _extracted(self, name=UNKNOWN_NAME, vat=UNKNOWN_VAT, iban=None):
        seller_bank = {"iban": iban} if iban else {}
        return {
            "type": "Invoice",
            "invoice_number": "ZZ-NOINVENT-1",
            "issue_date": "2026-09-30",
            "currency": "EUR",
            "seller": {
                "name": name,
                "vat": vat,
                "country_code": "DE",
                "phone": "+49 30 5550199",
                "email": "ap@zz-never-invented.example",
                "city": "Berlin",
                **seller_bank,
            },
            "buyer": {
                "name": self.company.name,
                "country_code": self.company.country_id.code or "FI",
            },
            "lines": [
                {
                    "product_code": "ZZ-NI-1",
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
                "name": "zz-noinvent.pdf",
                "raw": b"%PDF-1.4 fake",
                "mimetype": "application/pdf",
            }
        )
        record = self.env["goodahead.invoice.import"].create(
            {
                "name": "zz-noinvent.pdf",
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

    def _partner_count(self):
        return self.Partner.with_context(active_test=False).search_count([])

    def _import_partner(self, ours=True, **overrides):
        """Call core's seam the way `_import_fill_invoice` calls it."""
        vals = {
            "name": UNKNOWN_NAME,
            "phone": "+49 30 5550199",
            "email": "ap@zz-never-invented.example",
            "vat": UNKNOWN_VAT,
            "country_code": "DE", "city": "Berlin",
        }
        vals.update(overrides)
        edi = self.EdiCommon.with_context(ai_invoice_import=True) if ours else self.EdiCommon
        return edi._import_partner(self.company, **vals)

    def _counting_import_partner(self):
        """Wrap `account.edi.common._import_partner` and record every entry."""
        edi_cls = type(self.EdiCommon)
        original = edi_cls._import_partner
        calls = []

        def counting(this, company_id, name, phone, email, vat, **kwargs):
            calls.append(name)
            return original(this, company_id, name, phone, email, vat, **kwargs)

        return calls, patch.object(edi_cls, "_import_partner", counting)


@tagged("post_install", "-at_install")
class TestTheLegacyCreateIsSuppressed(_SuppressionHarness):
    """Suppression in `account.edi.common._import_partner`.

    Tests that call the method directly run on both cores, since it exists on both.
    Only the full-import tests are gated, because the search-plan build never
    reaches this seam during an import.
    """

    @skip_method_unless_profile(PROFILE_LEGACY)
    def test_a_full_import_of_an_unknown_vendor_creates_nobody(self):
        before = self._partner_count()

        record, outcome = self._run_import()

        self.assertTrue(outcome.startswith("OK"), outcome)
        self.assertTrue(record.move_id, "the bill itself must still be created")
        self.assertFalse(
            record.move_id.partner_id,
            "an unrecognised vendor leaves the bill empty, on purpose",
        )
        self.assertEqual(
            self._partner_count(),
            before,
            "no res.partner may be created for an unrecognised vendor",
        )

    @skip_method_unless_profile(PROFILE_LEGACY)
    def test_the_unrecognised_vendor_net_appears(self):
        """With `partner_id` left empty, the unrecognised-vendor note appears."""
        record, outcome = self._run_import()
        move = record.move_id

        notes = move.message_ids.filtered(
            lambda m: "not recognised" in (m.body or "")
        )
        self.assertEqual(len(notes), 1, "exactly one note, naming what was read")
        self.assertIn(UNKNOWN_NAME, notes.body)
        # Check the values, not the labels, which may be translated.
        self.assertIn(UNKNOWN_VAT, notes.body)
        self.assertIn("ap@zz-never-invented.example", notes.body)
        self.assertIn("No new contact was created for it on purpose", notes.body)

    def test_a_suppressed_bill_carrying_a_payee_account_still_imports(self):
        """A suppressed bill with a payee IBAN imports and creates no bank account.

        Core's bank step compares two empty recordsets as equal and returns early,
        so the number is dropped; this pins that behaviour.
        """
        before = self._partner_count()
        Bank = self.env["res.partner.bank"].with_context(active_test=False)
        iban = "DE89370400440532013000"

        record, outcome = self._run_import(self._extracted(iban=iban))

        self.assertTrue(outcome.startswith("OK"), outcome)
        self.assertTrue(record.move_id, "the bill itself must still be created")
        self.assertFalse(record.move_id.partner_id)
        self.assertEqual(self._partner_count(), before)
        self.assertFalse(
            Bank.search([("acc_number", "=", iban)]),
            "no bank account may be created for a vendor that does not exist",
        )
        # Control: the generated document really carries the payee account.
        self.assertIn(
            iban.encode(),
            self.AM._ai_generate_ubl_from_extracted(
                self._extracted(iban=iban), self.company
            ),
        )

    def test_the_suppressed_call_returns_cores_shape(self):
        """The early return is a `(recordset, logs)` tuple with an empty `res.partner`.

        `_import_fill_invoice` reads `partner.id` straight afterwards.
        """
        before = self._partner_count()

        result = self._import_partner()

        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        partner, logs = result
        self.assertEqual(partner._name, "res.partner")
        self.assertFalse(partner)
        self.assertEqual(len(partner), 0)
        # The caller's very next statement, reproduced.
        self.assertFalse(partner.id)
        self.assertEqual(self._partner_count(), before)
        self.assertIsInstance(logs, list)

    def test_the_logs_say_it_was_deliberate_and_not_a_near_miss(self):
        """The user-visible logs say the vendor was deliberately left empty."""
        _partner, logs = self._import_partner()

        joined = " ".join(logs)
        self.assertTrue(logs, "the suppressed path must say something")
        self.assertIn(UNKNOWN_NAME, joined)
        self.assertIn("left without a vendor", joined)
        self.assertNotIn("A new partner was created", joined)

    def test_core_would_have_created_one_without_our_context_key(self):
        """Control: without the context key, core creates the partner."""
        before = self._partner_count()

        partner, logs = self._import_partner(ours=False)

        self.assertTrue(partner, "core must still invent one when we do not stop it")
        self.assertEqual(partner.name, UNKNOWN_NAME)
        self.assertEqual(self._partner_count(), before + 1)
        self.assertIn("A new partner was created", " ".join(logs))

    def test_a_vendor_that_matches_still_comes_back(self):
        """Control: an existing vendor is still returned."""
        vendor = self.Partner.create(
            {
                "name": UNKNOWN_NAME,
                "is_company": True,
                "supplier_rank": 3,
                "country_id": self.env.ref("base.de").id,
                "vat": UNKNOWN_VAT,
            }
        )
        before = self._partner_count()

        partner, _logs = self._import_partner()

        self.assertEqual(partner, vendor)
        self.assertEqual(self._partner_count(), before)

    def test_a_matched_vendor_is_returned_unchanged(self):
        """On a match, core's result comes back as is: 18.0 core does not touch
        an existing vendor's address, and neither does this override."""
        vendor = self.Partner.create(
            {"name": UNKNOWN_NAME, "is_company": True, "vat": UNKNOWN_VAT}
        )

        partner, _logs = self._import_partner()

        self.assertEqual(partner, vendor)
        self.assertFalse(partner.city)
        self.assertFalse(partner.country_id)

    def test_a_vendor_reachable_only_by_its_peppol_pair_still_matches(self):
        """A vendor reachable only through its Peppol EAS/endpoint is still found.

        `_ai_core_peppol_domain` copies core's Peppol lookup; this fails if they drift.
        """
        vendor = self.Partner.create(
            {
                "name": "ZZ Peppol Only Oy",
                "is_company": True,
                "peppol_eas": "0007",
                "peppol_endpoint": "1234567890",
            }
        )
        before = self._partner_count()

        partner, _logs = self._import_partner(
            peppol_eas="0007", peppol_endpoint="1234567890"
        )

        self.assertEqual(
            partner,
            vendor,
            "a vendor core would have found through the Peppol domain must "
            "still be found through ours",
        )
        self.assertEqual(self._partner_count(), before)

    def test_a_document_with_no_tax_id_is_left_entirely_to_core(self):
        """Without a VAT core would not create anyway, so core runs untouched."""
        before = self._partner_count()

        partner, logs = self._import_partner(vat=None)

        self.assertFalse(partner)
        self.assertEqual(self._partner_count(), before)
        # 18.0 core logs only when it creates a partner.
        self.assertEqual(logs, [])


@skip_unless_profile(PROFILE_SEARCH_PLAN, PROFILE_SEARCH_PLAN_18)
@tagged("post_install", "-at_install")
class TestTheLegacySuppressionIsInertOnTheSearchPlanBuild(_SuppressionHarness):
    """A real import on the search-plan build never calls `_import_partner`."""

    def test_a_full_import_never_enters_the_legacy_seam(self):
        before = self._partner_count()
        calls, counting = self._counting_import_partner()

        with counting:
            record, outcome = self._run_import()

        self.assertTrue(outcome.startswith("OK"), outcome)
        self.assertEqual(
            calls,
            [],
            "the legacy create-suppression must never be entered on this build",
        )
        # Control: the import really ran and the vendor was suppressed by
        # `_import_ubl_create_missing_customer` instead.
        self.assertTrue(record.move_id)
        self.assertFalse(record.move_id.partner_id)
        self.assertEqual(self._partner_count(), before)
