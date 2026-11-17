# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Retry-storm protection for the AI invoice import.

A commit inside the queue_job body would split the transaction queue_job manages
and cause unbounded retries, re-billing the paid Claude call each time. The job
therefore runs as one transaction and an extraction failure returns "FAILED"
instead of raising. The paid call is mocked throughout.
"""

import json
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_ai_import.lib.claude_client import (
    ClaudeExtractionError,
)

EXTRACT = "_ai_extract_and_generate_ubl"


@tagged("post_install", "-at_install")
class TestAiInvoiceImportRetryGuard(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.attachment = cls.env["ir.attachment"].create(
            {
                "name": "inv.pdf",
                "raw": b"%PDF-1.4 fake",
                "mimetype": "application/pdf",
            }
        )

    def _new_import(self, **vals):
        base = {
            "name": "inv.pdf",
            "company_id": self.company.id,
            "pdf_attachment_id": self.attachment.id,
        }
        base.update(vals)
        return self.env["ai.invoice.import"].create(base)

    def _patch_extract(self, **kw):
        """Patch the paid Claude extraction on account.move's registry class."""
        return patch.object(type(self.env["account.move"]), EXTRACT, **kw)

    def test_extraction_failure_is_terminal(self):
        """An extraction error returns FAILED without raising, after one Claude call."""
        self.company.claude_api_key = "test-key"
        rec = self._new_import()
        with self._patch_extract(
            side_effect=ClaudeExtractionError("Missing required fields: issue_date")
        ) as mocked:
            # A raise here would let queue_job retry (and re-bill); it must return.
            result = rec._process_import()
        mocked.assert_called_once()
        self.assertTrue(result.startswith("FAILED"))
        self.assertEqual(rec.state, "failed")
        self.assertIn("issue_date", rec.error_message)

    def test_extraction_broad_catch_is_terminal(self):
        """An unexpected (untyped) extraction error is terminal too."""
        self.company.claude_api_key = "test-key"
        rec = self._new_import()
        with self._patch_extract(
            side_effect=RuntimeError("boom (not a ClaudeExtractionError)")
        ) as mocked:
            result = rec._process_import()
        mocked.assert_called_once()
        self.assertTrue(result.startswith("FAILED"))
        self.assertEqual(rec.state, "failed")

    def test_post_extraction_failure_links_bill_and_is_terminal(self):
        """A failure after the bill is created marks the import failed and links
        the bill, so a later retry cannot duplicate it."""
        self.company.claude_api_key = "test-key"
        rec = self._new_import()
        ai_result = {
            "extracted": {"seller": {"name": "ACME"}},
            "usage": {"input_tokens": 1, "output_tokens": 2},
            "model_name": "test-model",
            "xml_bytes": b"<Invoice/>",
        }
        AccountMove = type(self.env["account.move"])
        # Decode succeeds (bill built), post-processing then fails.
        with self._patch_extract(return_value=ai_result) as extract, patch.object(
            AccountMove, "_ai_decode_ubl_into_move", return_value=None
        ), patch.object(
            AccountMove, "_ai_post_process", side_effect=RuntimeError("post fail")
        ):
            result = rec._process_import()
        extract.assert_called_once()
        self.assertTrue(result.startswith("FAILED"))
        self.assertEqual(rec.state, "failed")
        self.assertIn("post fail", rec.error_message)
        self.assertTrue(rec.move_id, "created bill must be linked, not orphaned")

    def test_post_extraction_db_error_is_contained_by_savepoint(self):
        """A database error in post-processing is contained by a savepoint, so the
        failure can still be recorded and the job returns FAILED."""
        self.company.claude_api_key = "test-key"
        rec = self._new_import()
        ai_result = {
            "extracted": {"seller": {"name": "ACME"}},
            "usage": {"input_tokens": 1, "output_tokens": 2},
            "model_name": "test-model",
            "xml_bytes": b"<Invoice/>",
        }

        def _db_boom(*args, **kwargs):
            # Invalid SQL aborts the transaction, like a real DB error would.
            self.env.cr.execute("SELECT 1 FROM table_that_does_not_exist")

        AccountMove = type(self.env["account.move"])
        with self._patch_extract(return_value=ai_result), patch.object(
            AccountMove, "_ai_decode_ubl_into_move", return_value=None
        ), patch.object(AccountMove, "_ai_post_process", side_effect=_db_boom):
            result = rec._process_import()  # must not raise
        self.assertTrue(result.startswith("FAILED"))
        self.assertEqual(rec.state, "failed")
        self.assertTrue(rec.move_id)

    def test_action_retry_blocked_when_bill_linked(self):
        """Retry is refused once a bill is linked."""
        move = self.env["account.move"].create({"move_type": "in_invoice"})
        rec = self._new_import(state="failed", move_id=move.id)
        with patch.object(type(rec), "with_delay") as delay:
            with self.assertRaises(UserError):
                rec.action_retry()
        delay.assert_not_called()

    def test_extraction_validation_failure_stores_json(self):
        """A validation failure still stores the parsed JSON for re-import."""
        self.company.claude_api_key = "test-key"
        rec = self._new_import()
        partial = {"seller": {"name": "ACME"}, "invoice_number": "X", "lines": []}
        err = ClaudeExtractionError(
            "Missing required fields in extraction: issue_date", data=partial
        )
        with self._patch_extract(side_effect=err):
            result = rec._process_import()
        self.assertTrue(result.startswith("FAILED"))
        self.assertEqual(rec.state, "failed")
        self.assertTrue(rec.extraction_data, "parsed JSON must be stored on failure")
        self.assertEqual(json.loads(rec.extraction_data)["invoice_number"], "X")

    def test_reimport_from_json_skips_claude(self):
        """Re-import builds the bill from the stored JSON without calling Claude."""
        self.company.claude_api_key = "test-key"
        rec = self._new_import(
            state="failed",
            extraction_model="test-model",
            extraction_data=json.dumps(
                {"seller": {"name": "ACME"}, "issue_date": "2026-01-01"}
            ),
        )
        AccountMove = type(self.env["account.move"])
        with self._patch_extract() as extract, patch.object(
            AccountMove, "_ai_generate_ubl_from_extracted", return_value=b"<Invoice/>"
        ), patch.object(
            AccountMove, "_ai_decode_ubl_into_move", return_value=None
        ), patch.object(AccountMove, "_ai_post_process", return_value=None):
            rec.action_reimport_from_json()
        extract.assert_not_called()
        self.assertEqual(rec.state, "done")
        self.assertTrue(rec.move_id)

    def test_reimport_blocked_when_bill_linked(self):
        """Re-import is refused once a bill exists."""
        move = self.env["account.move"].create({"move_type": "in_invoice"})
        rec = self._new_import(
            state="failed", move_id=move.id, extraction_data=json.dumps({"a": 1})
        )
        with self.assertRaises(UserError):
            rec.action_reimport_from_json()

    def test_reimport_blocked_when_not_failed(self):
        """Re-import is refused server-side for a record that is not failed."""
        rec = self._new_import(
            state="processing", extraction_data=json.dumps({"a": 1})
        )
        with self.assertRaises(UserError):
            rec.action_reimport_from_json()

    def test_done_record_is_noop(self):
        """A finished record is never reprocessed (no Claude call)."""
        rec = self._new_import(state="done")
        with self._patch_extract() as mocked:
            result = rec._process_import()
        mocked.assert_not_called()
        self.assertTrue(result.startswith("OK"))

    def test_action_retry_rejected_while_processing(self):
        """Retry is refused unless the import is failed."""
        rec = self._new_import(state="processing")
        with patch.object(type(rec), "with_delay") as delay:
            with self.assertRaises(UserError):
                rec.action_retry()
        delay.assert_not_called()
        self.assertEqual(rec.state, "processing")

    def test_action_retry_reenqueues_failed(self):
        """Retry on a failed import resets it to draft and enqueues one job."""
        rec = self._new_import(state="failed", error_message="boom")
        with patch.object(type(rec), "with_delay", return_value=MagicMock()) as delay:
            rec.action_retry()
        delay.assert_called_once()
        self.assertEqual(rec.state, "draft")
        self.assertFalse(rec.error_message)
