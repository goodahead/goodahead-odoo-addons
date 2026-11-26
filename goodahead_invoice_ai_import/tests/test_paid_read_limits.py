# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Every AI reading is billed: what may start one is limited in one place.

The limits apply wherever a reading is queued — an email to the import
alias, a PDF uploaded on a purchase journal, a retry — because they live
where the job is queued (`_enqueue_import`), not in one entry point. A
refused PDF becomes a failed import record that says why, and no job.

Every limit is a system parameter; none of these tests reaches the network.
"""

import io
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged
from odoo.tools.pdf import PdfFileWriter

from odoo.addons.goodahead_invoice_ai_import.models.ai_invoice_import import AI_READER

PARAM = "goodahead_invoice_ai_import.%s"


def _pdf(pages=1, marker=b""):
    writer = PdfFileWriter()
    for _page in range(pages):
        writer.add_blank_page(width=72, height=72)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue() + b"\n%" + marker + b"\n"


@tagged("post_install", "-at_install")
class TestPaidReadLimits(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.company.claude_api_key = "test-key"
        cls.Import = cls.env["goodahead.invoice.import"]
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", cls.company.id)], limit=1
        )
        cls.alias_values = {"company_id": cls.company.id}

    def _set(self, key, value):
        self.env["ir.config_parameter"].sudo().set_param(PARAM % key, str(value))

    def _upload(self, *pdfs):
        attachments = self.env["ir.attachment"].create(
            [
                {"name": f"up{i}.pdf", "raw": raw, "mimetype": "application/pdf"}
                for i, raw in enumerate(pdfs)
            ]
        )
        self.journal.with_context(default_move_type="in_invoice").create_document_from_attachment(
            attachments.ids
        )
        return self.Import.search(
            [("pdf_attachment_id", "in", attachments.ids)], order="id"
        )

    def _mail(self, sender, *pdfs):
        msg = {
            "subject": "Bills",
            "email_from": sender,
            "attachments": [(f"mail{i}.pdf", raw, {}) for i, raw in enumerate(pdfs)],
        }
        before = self.Import.search([]).ids
        self.Import.message_new(msg, custom_values=self.alias_values)
        return self.Import.search([("id", "not in", before)], order="id")

    def _queued(self, records):
        return records.filtered("queue_job_id")

    def _refused(self, records, code):
        return records.filtered(lambda r: r.state == "failed" and code in (r.error_message or ""))

    # -- size and pages, on the upload path too (finding: limits only on mail) --

    def test_an_uploaded_pdf_over_the_size_limit_is_not_read(self):
        self._set("max_pdf_bytes", 2000)
        big = _pdf(marker=b"x" * 5000)
        records = self._upload(_pdf(), big)
        self.assertEqual(len(records), 2)
        self.assertEqual(len(self._queued(records)), 1)
        refused = records.filtered(lambda r: not r.queue_job_id)
        self.assertEqual(refused.state, "failed")
        self.assertIn("ai_import.attachment_refused", refused.error_message)

    def test_a_pdf_with_too_many_pages_is_not_read(self):
        self._set("max_pdf_pages", 2)
        records = self._upload(_pdf(pages=2, marker=b"a"), _pdf(pages=3, marker=b"b"))
        self.assertEqual(len(self._queued(records)), 1)
        self.assertEqual(len(self._refused(records, "ai_import.too_many_pages")), 1)
        mailed = self._mail("pages@example.test", _pdf(pages=5, marker=b"c"))
        self.assertFalse(self._queued(mailed))
        self.assertTrue(self._refused(mailed, "ai_import.too_many_pages"))

    # -- the daily budget -------------------------------------------------------

    def test_one_sender_cannot_exceed_its_daily_budget(self):
        self._set("daily_reads_per_sender", 2)
        first = self._mail("spam@example.test", _pdf(marker=b"1"), _pdf(marker=b"2"))
        self.assertEqual(len(self._queued(first)), 2)
        second = self._mail("spam@example.test", _pdf(marker=b"3"), _pdf(marker=b"4"))
        self.assertFalse(self._queued(second), "the sender's budget is spent")
        self.assertEqual(len(second), 1, "one failed record for the refused mail")
        self.assertTrue(self._refused(second, "ai_import.budget_exceeded"))
        # Another sender is not affected.
        other = self._mail("vendor@example.test", _pdf(marker=b"5"))
        self.assertEqual(len(self._queued(other)), 1)

    def test_the_company_has_a_daily_budget(self):
        self._set("daily_reads_per_company", 2)
        self._set("daily_reads_per_sender", 0)  # 0 switches a limit off
        self.assertEqual(len(self._queued(self._mail("a@example.test", _pdf(marker=b"a")))), 1)
        self.assertEqual(len(self._queued(self._upload(_pdf(marker=b"b")))), 1)
        late_mail = self._mail("c@example.test", _pdf(marker=b"c"))
        late_upload = self._upload(_pdf(marker=b"d"))
        self.assertFalse(self._queued(late_mail | late_upload))
        self.assertTrue(self._refused(late_mail, "ai_import.budget_exceeded"))
        self.assertTrue(self._refused(late_upload, "ai_import.budget_exceeded"))

    def test_uploads_count_against_the_uploading_user(self):
        self._set("daily_reads_per_sender", 1)
        records = self._upload(_pdf(marker=b"u1"), _pdf(marker=b"u2"))
        self.assertEqual(len(self._queued(records)), 1)
        self.assertEqual(len(self._refused(records, "ai_import.budget_exceeded")), 1)

    # -- the same PDF is not read twice -------------------------------------------

    def test_a_pdf_already_read_recently_is_not_read_again(self):
        raw = _pdf(marker=b"same")
        earlier = self._upload(raw)
        earlier.write({"extraction_data": "{}"})  # it was read
        again = self._mail("resend@example.test", raw)
        self.assertFalse(self._queued(again))
        refused = self._refused(again, "ai_import.already_read")
        self.assertTrue(refused)
        self.assertIn(earlier.display_name, refused.error_message)
        # A different PDF from the same sender is read.
        self.assertEqual(len(self._queued(self._mail("resend@example.test", _pdf(marker=b"x")))), 1)

    def test_the_defaults_are_generous(self):
        Import = self.Import
        self.assertGreaterEqual(Import._ai_daily_reads_per_company(), 500)
        self.assertGreaterEqual(Import._ai_daily_reads_per_sender(), 100)
        self.assertGreaterEqual(Import._ai_max_pdf_pages(), 30)
        self.assertGreaterEqual(Import._ai_recent_read_days(), 7)

    def test_a_reading_job_is_tried_once(self):
        """A worker killed mid-call commits nothing and queue_job runs the job
        again: each run is another billed request. One try only."""
        record = self._upload(_pdf(marker=b"once"))
        self.assertEqual(record.queue_job_id.max_retries, 1)
        mailed = self._mail("once@example.test", _pdf(marker=b"twice"))
        self.assertEqual(mailed.queue_job_id.max_retries, 1)

    def test_a_run_after_an_interrupted_one_makes_no_second_call(self):
        """queue_job requeues a job whose worker died (counting it in
        ``retry``). That run must not bill a second reading."""
        record = self._upload(_pdf(marker=b"dead"))
        record.queue_job_id.sudo().retry = 1  # what the job runner writes on requeue
        with patch.object(
            type(self.env["account.move"]), "_ai_extract_and_generate_ubl"
        ) as paid_call:
            result = record._process_import()
        paid_call.assert_not_called()
        self.assertTrue(result.startswith("FAILED"), result)
        self.assertEqual(record.state, "failed")
        self.assertIn("ai_import.interrupted", record.error_message)
        # A first run reads.
        fresh = self._upload(_pdf(marker=b"alive"))
        with patch.object(
            type(self.env["account.move"]), "_ai_extract_and_generate_ubl",
            side_effect=RuntimeError("stop after the call"),
        ) as paid_call:
            fresh._process_import()
        paid_call.assert_called_once()

    def test_a_refused_record_can_be_retried_once_the_limit_allows(self):
        self._set("daily_reads_per_sender", 1)
        self._upload(_pdf(marker=b"r1"))
        refused = self._upload(_pdf(marker=b"r2"))
        self.assertFalse(refused.queue_job_id)
        self._set("daily_reads_per_sender", 5)
        refused.action_retry()
        self.assertTrue(refused.queue_job_id)
        self.assertEqual(refused.reader, AI_READER)
