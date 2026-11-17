# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The import does not grant approval step 1: an imported bill waits for a human.

Step 1 compares the bill against its document, which is exactly what an extraction can get
wrong, so the import puts the bill into step 1 without completing it. The control is a bill
whose step 1 is granted by hand and must move to `reviewed`, proving the approval machinery
is live.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_ai_import.lib.ubl_generator import (
    generate_peppol_invoice_xml,
)


@tagged("post_install", "-at_install")
class TestTheImportDoesNotGrantStepOne(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.company.claude_api_key = "test-key"
        cls.AM = cls.env["account.move"]

    def setUp(self):
        super().setUp()
        if "bill_approval_state" not in self.AM._fields:
            self.skipTest(
                "goodahead_bill_approval is not installed; this file tests the "
                "integration between the two modules and there is nothing to "
                "integrate with"
            )

    # ------------------------------------------------------------- the harness

    def _extracted(self):
        return {
            "type": "Invoice",
            "invoice_number": "BA-1",
            "issue_date": "2026-10-01",
            "currency": self.company.currency_id.name,
            "seller": {
                "name": "ZZ BA Vendor Inc",
                "business_id": "ZZ-BA-0001",
                "country_code": self.company.country_id.code or "US",
            },
            "buyer": {
                "name": self.company.name,
                "country_code": self.company.country_id.code or "US",
            },
            "lines": [{
                "product_code": "BA-SKU",
                "name": "A thing on a bill",
                "quantity": 1.0,
                "unit_code": "C62",
                "price_unit": 100.0,
                "line_total": 100.0,
                "tax_percent": 0.0,
                "tax_amount": 0.0,
                "tax_category": "Z",
            }],
            "tax_subtotals": [{
                "tax_percent": 0.0,
                "taxable_amount": 100.0,
                "tax_amount": 0.0,
                "tax_category": "Z",
            }],
            "totals": {
                "line_extension": 100.0,
                "tax_exclusive": 100.0,
                "tax_inclusive": 100.0,
                "payable": 100.0,
            },
        }

    def _run_import(self):
        extracted = self._extracted()
        xml_bytes = generate_peppol_invoice_xml(extracted)
        attachment = self.env["ir.attachment"].create({
            "name": "ba.pdf",
            "raw": b"%PDF-1.4 fake",
            "mimetype": "application/pdf",
        })
        rec = self.env["ai.invoice.import"].create({
            "name": "ba.pdf",
            "company_id": self.company.id,
            "pdf_attachment_id": attachment.id,
        })
        ai_result = {
            "extracted": extracted,
            "usage": {"input_tokens": 10, "output_tokens": 20},
            "model_name": "test-model",
            "xml_bytes": xml_bytes,
        }
        with patch.object(
            type(self.AM), "_ai_extract_and_generate_ubl", return_value=ai_result
        ):
            result = rec._process_import()
        self.assertTrue(result.startswith("OK"), result)
        self.assertTrue(rec.move_id, "the import must have produced a bill")
        return rec.move_id

    def _hand_made_bill(self):
        journal = self.env["account.journal"].search([
            ("type", "=", "purchase"), ("company_id", "=", self.company.id),
        ], limit=1)
        partner = self.env["res.partner"].create({
            "name": "ZZ BA Hand Vendor", "is_company": True})
        return self.AM.create({
            "move_type": "in_invoice",
            "journal_id": journal.id,
            "partner_id": partner.id,
            "invoice_date": "2026-10-01",
        })

    # --------------------------------------------------------------- the tests

    def test_the_import_leaves_the_bill_waiting_for_a_human(self):
        imported = self._run_import()

        self.assertEqual(
            imported.bill_approval_state, "to_review",
            "an AI-imported bill must wait for a book keeper. It used to arrive "
            "already reviewed by OdooBot, which removed the one check that compares "
            "the bill with the document it came from.",
        )
        self.assertFalse(
            imported.approval_1_uid,
            "nothing may stamp step 1 on the way in -- not the import, not OdooBot",
        )
        self.assertFalse(imported.approval_1_date)
        self.assertFalse(
            imported.approval_2_uid,
            "step 2 is the financial director's and was never the import's to give",
        )
        self.assertEqual(
            imported.state, "draft",
            "the import must NOT post the bill. `_autopost_bill` is overridden in "
            "the approval module for exactly this, so this assertion is also the one "
            "that fails if that override is lost",
        )

        # Control: granting step 1 by hand must move the state, which shows the field is
        # live and the imported bill staying put is meaningful.
        control = self._hand_made_bill()
        self.assertEqual(control.bill_approval_state, "to_review")
        control._bill_approval_grant_step_1(note="the control for this test")
        self.assertEqual(
            control.bill_approval_state, "reviewed",
            "the approval machinery itself is broken or absent; every other assertion in "
            "this file is therefore meaningless, including the ones that just passed",
        )
        self.assertEqual(
            imported.bill_approval_state, "to_review",
            "the imported bill must not have moved with it",
        )

    def test_the_imported_bill_is_in_the_review_queue_not_the_approval_queue(self):
        imported = self._run_import()
        awaiting_review = self.AM.search([
            ("bill_approval_state", "=", "to_review"),
            ("state", "=", "draft"),
            ("move_type", "in", ("in_invoice", "in_refund")),
        ])
        awaiting_approval = self.AM.search([
            ("bill_approval_state", "=", "reviewed"),
            ("state", "=", "draft"),
            ("move_type", "in", ("in_invoice", "in_refund")),
        ])
        self.assertIn(imported, awaiting_review)
        self.assertNotIn(
            imported, awaiting_approval,
            "an imported bill in the director's queue means nobody read it",
        )

    def test_the_chatter_does_not_claim_anybody_reviewed_it(self):
        """The chatter records the AI import but not that approval step 1 was given."""
        imported = self._run_import()
        bodies = " ".join(imported.message_ids.mapped("body"))
        self.assertIn("Imported via AI invoice extraction", bodies)
        self.assertNotIn("Approval step 1", bodies)

    def test_the_imported_bill_still_cannot_be_posted(self):
        imported = self._run_import()
        from odoo.exceptions import UserError
        with self.assertRaises(UserError):
            imported.action_post()
        self.assertEqual(imported.state, "draft")

    # ------------------------------------------------- the soft dependency

    def test_the_import_module_does_not_depend_on_the_approval_module(self):
        """The AI import must stay installable without the approval module."""
        manifest = self.env["ir.module.module"].search([
            ("name", "=", "goodahead_invoice_ai_import")], limit=1)
        self.assertTrue(manifest)
        self.assertNotIn(
            "goodahead_bill_approval",
            manifest.dependencies_id.mapped("name"),
        )

    def test_the_import_never_calls_the_grant_at_all(self):
        """The grant method is replaced by a recorder and the import must never call it."""
        calls = []

        def recorder(self_move, *args, **kwargs):
            calls.append(self_move.id)

        with patch.object(
            type(self.AM), "_bill_approval_grant_step_1", recorder, create=True
        ):
            imported = self._run_import()

        self.assertEqual(
            calls, [],
            "the import called `_bill_approval_grant_step_1`. It must not: a bill nobody "
            "has read is exactly the bill that needs a reviewer.",
        )
        self.assertFalse(imported.approval_1_uid)
