# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The import does NOT grant approval step 1. An imported bill waits for a human.

🔴 THIS FILE USED TO ASSERT THE OPPOSITE, AND THAT IS WHY IT IS WORTH READING.
The original design said the import would trigger step 1 via a hook, and was built as
"the import CLEARS step 1". So an imported bill arrived already
reviewed — by OdooBot — and went to the finance director with no human having compared it
against the PDF.

⚠️ That contradicted the reason an approval add-on that hooks into posting exists at all: an
AI or OCR import makes the gap larger, not smaller, because a bill can now exist without
anyone having read a line of it. Step 1 checks
the vendor, the dates, the lines and the accounts against the document — which is exactly what an
extraction can get wrong, and the only step that would catch it.

The decision, once an import was seen landing stamped *"Reviewed by OdooBot"*: **"trigger
step 1" means put the bill INTO step 1, not complete it.**

🔴 THE CONTROL HAD TO CHANGE SHAPE WITH THE BEHAVIOUR, AND THIS IS THE INTERESTING PART.
The old control was a hand-made bill, and the test asserted the two read DIFFERENTLY. That worked
while the import moved the field. Now the import leaves it alone — so a hand-made bill and an
imported one both read `to_review`, and "they are the same" would pass just as well against a
database where the approval machinery is broken, absent, or stuck. So the control is inverted
too: a bill whose step 1 IS granted, which must read `reviewed`. That proves the field can still
move, which is what makes the imported bill sitting at `to_review` mean anything at all.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_import.adapters.ubl_generator import (
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
                "no approval add-on that hooks into posting is installed; this file tests the "
                "integration between the two modules and there is nothing to "
                "integrate with"
            )

    # ------------------------------------------------------------- the harness

    def _extracted(self):
        return {
            "type": "Invoice",
            "invoice_number": "F33-1",
            "issue_date": "2026-10-01",
            "currency": self.company.currency_id.name,
            "seller": {
                "name": "ZZ F33 Vendor Inc",
                "business_id": "ZZ-F33-0001",
                "country_code": self.company.country_id.code or "US",
            },
            "buyer": {
                "name": self.company.name,
                "country_code": self.company.country_id.code or "US",
            },
            "lines": [{
                "product_code": "F33-SKU",
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
            "name": "f33.pdf",
            "raw": b"%PDF-1.4 fake",
            "mimetype": "application/pdf",
        })
        rec = self.env["goodahead.invoice.import"].create({
            "name": "f33.pdf",
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
            "name": "ZZ F33 Hand Vendor", "is_company": True})
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
            "🔴 the import must NOT post the bill. `_autopost_bill` is overridden in "
            "the approval module for exactly this, so this assertion is also the one "
            "that fails if that override is lost",
        )

        # 🔴 THE CONTROL, AND IT IS NOT THE OBVIOUS ONE. A hand-made bill is also
        # `to_review`, so comparing the two proves nothing now -- "they match" would pass
        # against a database where the approval machinery never works at all. Granting step 1
        # by hand and watching the state MOVE is what shows the field is live, and therefore
        # that the imported bill staying put is a decision rather than a dead feature.
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
        """The chatter still records that an AI imported it. What it must not say is
        that approval step 1 was given -- the screen has to be able to show that no
        human judgement has been applied yet."""
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
        """⚠️ A `depends` entry here would force the approval module onto every project
        that wants the AI import, which is the opposite of the standing rule that generic
        things stay independent. Still true now that there is no hook at all."""
        manifest = self.env["ir.module.module"].search([
            ("name", "=", "goodahead_invoice_ai_import")], limit=1)
        self.assertTrue(manifest)
        # No dependency on any bill-approval add-on, whatever it is called.
        self.assertFalse([
            name for name in manifest.dependencies_id.mapped("name")
            if "bill_approval" in name
        ])

    def test_the_import_never_calls_the_grant_at_all(self):
        """🔴 The strongest form of this, and the one that survives a refactor.

        The assertions above would still pass if the import called the grant and something
        else happened to undo it. This one proves the call is not made: the method is
        replaced by a recorder, and the import must leave it untouched.
        """
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
