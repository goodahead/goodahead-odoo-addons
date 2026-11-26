# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The mark-received job.

AC-03, AC-11c, AC-17 (shadow never marks), AC-19b (records left unconfirmed
are found for the reconciliation and the switch-back gate). No network:
`ApixClient.download` is patched. Invented data only.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_apix.lib.apix_client import (
    ApixClient,
    ApixRejected,
    ApixUnreachable,
)
from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.job import Job

from .test_package_import import TRANSFER, finvoice_fixture, listing_group, make_zip


@tagged("post_install", "-at_install")
class TestApixMarkReceived(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.Import = cls.env["goodahead.invoice.import"]
        cls.Connection = cls.env["goodahead.apix.connection"]
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", cls.company.id)], limit=1
        )
        if not cls.journal:
            return
        cls.env.ref("base.EUR").active = True
        if not cls.env["account.tax"].search(
            [
                ("type_tax_use", "=", "purchase"),
                ("amount_type", "=", "percent"),
                ("amount", "=", 25.5),
                ("company_id", "=", cls.company.id),
            ],
            limit=1,
        ):
            cls.env["account.tax"].create(
                {
                    "name": "Example 25.5% Purchase",
                    "type_tax_use": "purchase",
                    "amount_type": "percent",
                    "amount": 25.5,
                    "company_id": cls.company.id,
                }
            )
        cls.connection = cls.Connection.search([("company_id", "=", cls.company.id)], limit=1)
        if not cls.connection:
            cls.connection = cls.Connection.create(
                {
                    "company_id": cls.company.id,
                    "environment": "test",
                    "transfer_id": TRANSFER,
                    "transfer_key": f"secret-{TRANSFER}",
                    "business_id": "0000006-0",
                }
            )
        cls.connection.write(
            {
                "transfer_id": TRANSFER,
                "transfer_key": f"secret-{TRANSFER}",
                "status": "working",
            }
        )
        cls.connection._apix_set_receiving_state("live")
        cls.vendor = cls.env["res.partner"].create(
            {
                "name": "Example Supplier Oy",
                "is_company": True,
                "vat": "FI00000035",
                "company_registry": "0000003-5",
                "country_id": cls.env.ref("base.fi").id,
            }
        )

    def setUp(self):
        super().setUp()
        if not self.journal:
            self.skipTest("no purchase journal on this database")
        self.packages = {}
        self.marks = {}
        self.download_calls = []

        def fake_download(client, storage_id, storage_key, mark_received):
            self.download_calls.append((storage_id, mark_received))
            if mark_received:
                answer = self.marks.get(storage_id, b"PK-marked")
            else:
                answer = self.packages[storage_id]
            if isinstance(answer, list):
                answer = answer.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        patcher = patch.object(ApixClient, "download", autospec=True, side_effect=fake_download)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- helpers -------------------------------------------------------------

    def _record(self, storage_id, body=b"<x/>"):
        self.packages[storage_id] = body
        group = listing_group(storage_id)
        return self.Import.sudo().create(
            dict(
                self.Import._apix_listing_vals(group),
                name=f"Example Supplier Oy — {storage_id}",
                company_id=self.company.id,
                source="apix",
                state="waiting",
                apix_storage_id=storage_id,
            )
        )

    def _jobs(self, record, prefix):
        return self.env["queue.job"].sudo().search(
            [("identity_key", "=", f"{prefix}{record.id}")], order="id desc"
        )

    def _attempt(self, job_record):
        """One try of a stored job, as the job runner does it."""
        job = Job.load(self.env, job_record.uuid)
        job.set_started()
        job.store()
        try:
            result = job.perform()
        except RetryableJobError as error:
            job.postpone(result=str(error), seconds=error.seconds)
            job.set_pending(reset_retry=False)
            job.store()
            return error
        job.set_done(result=result)
        job.store()
        return result

    def _marked(self):
        return [call for call in self.download_calls if call[1] is True]

    def _closed(self, storage_id):
        record = self._record(storage_id)
        record.action_close_without_bill("Test message, no bill needed")
        return record

    # -- mark after import -----------------------------------------------------

    def test_import_then_mark_received(self):
        body = make_zip(("invoice.xml", finvoice_fixture("fi30_simple_invoice.xml")))
        record = self._record("S-M01", body)
        record._apix_enqueue_package_import()
        self._attempt(self._jobs(record, "apix-import:")[:1])
        record.invalidate_recordset()
        self.assertEqual(record.state, "imported", record.error_message or "")
        self.assertFalse(record.apix_mark_confirmed, "not before the job runs")
        self.assertIn(record, self.Import._apix_unconfirmed_marks(self.company))

        mark = self._jobs(record, "apix-mark:")
        self.assertEqual(len(mark), 1)
        self.assertEqual(self._attempt(mark), "OK: marked received")
        record.invalidate_recordset()
        self.assertTrue(record.apix_mark_confirmed)
        self.assertEqual(record.apix_status, "RECEIVED")
        self.assertEqual(self._marked(), [("S-M01", True)], "one download that marks")
        self.assertEqual(self.download_calls[0], ("S-M01", False), "the import never marks")
        self.assertNotIn(record, self.Import._apix_unconfirmed_marks(self.company))

    def test_idempotent(self):
        record = self._closed("S-M02")
        self._attempt(self._jobs(record, "apix-mark:"))
        self.assertEqual(len(self._marked()), 1)

        # The same job delivered again, and a second job for the record.
        self.assertEqual(record._apix_mark_received(), "OK: already confirmed")
        record._apix_enqueue_mark_received()
        self.assertEqual(self._attempt(self._jobs(record, "apix-mark:")[:1]), "OK: already confirmed")
        # Settling again enqueues nothing for a confirmed record.
        count = len(self._jobs(record, "apix-mark:"))
        record._on_settled()
        self.assertEqual(len(self._jobs(record, "apix-mark:")), count)
        self.assertEqual(len(self._marked()), 1, "APIX is asked once")

    # -- AC-11c ----------------------------------------------------------------

    def test_close_without_bill_marks(self):
        record = self._closed("S-M03")
        self.assertEqual(record.state, "closed")
        mark = self._jobs(record, "apix-mark:")
        self.assertEqual(len(mark), 1, "the close enqueued the mark in its transaction")
        self.assertEqual(mark.channel, "root.apix")
        self.assertEqual(mark.max_retries, 5)
        self._attempt(mark)
        record.invalidate_recordset()
        self.assertTrue(record.apix_mark_confirmed)
        self.assertEqual(self._marked(), [("S-M03", True)])

    def test_reopen_keeps_the_mark_and_clears_nothing(self):
        record = self._closed("S-M04")
        self._attempt(self._jobs(record, "apix-mark:"))
        record.action_reopen()
        self.assertEqual(record.state, "waiting")
        self.assertTrue(record.apix_mark_confirmed, "no un-mark at APIX, nothing cleared")
        # Closing again does not ask APIX a second time.
        record.action_close_without_bill("Handled by hand")
        self.assertEqual(len(self._marked()), 1)

    def test_reopened_before_the_job_ran_is_not_marked(self):
        record = self._closed("S-M05")
        record.action_reopen()
        self.assertEqual(self._attempt(self._jobs(record, "apix-mark:")), "SKIPPED: not settled")
        record.invalidate_recordset()
        self.assertFalse(record.apix_mark_confirmed)
        self.assertFalse(self._marked(), "AC-03: never marked without a bill or a close")

    # -- AC-17: shadow ---------------------------------------------------------

    def test_shadow_never_marks(self):
        self.connection._apix_set_receiving_state("shadow")
        record = self._closed("S-M06")
        self.assertEqual(self._attempt(self._jobs(record, "apix-mark:")), "SKIPPED: shadow")
        record.invalidate_recordset()
        self.assertFalse(record.apix_mark_confirmed)
        self.assertFalse(self.download_calls, "no APIX call at all in shadow")
        self.assertIn("shadow mode", "\n".join(str(b) for b in record.message_ids.mapped("body")))
        self.assertIn(record, self.Import._apix_unconfirmed_marks(self.company))

    def test_no_storage_key_is_skipped(self):
        record = self._closed("S-M07")
        record.sudo().apix_storage_key = False
        self.assertEqual(record._apix_mark_received(), "SKIPPED: no storage id or key")
        self.assertFalse(self.download_calls)

    # -- retries ---------------------------------------------------------------

    def test_retry_then_success(self):
        self.marks["S-M08"] = [
            ApixUnreachable("Download: APIX could not be reached (ConnectionError)"),
            ApixRejected("Download: rejected (StatusCode 4001)"),
            b"PK-marked",
        ]
        record = self._closed("S-M08")
        job = self._jobs(record, "apix-mark:")
        self.assertIsInstance(self._attempt(job), RetryableJobError)
        job.invalidate_recordset()
        self.assertEqual((job.state, job.retry), ("pending", 1))
        self.assertIsInstance(self._attempt(job), RetryableJobError, "rejected also retries")
        self.assertEqual(self._attempt(job), "OK: marked received")
        record.invalidate_recordset()
        self.assertTrue(record.apix_mark_confirmed)
        self.assertEqual(len(self._marked()), 3)

    def test_retries_exhausted_leave_the_record_unconfirmed(self):
        self.marks["S-M09"] = [ApixUnreachable("Download: APIX did not answer within 60 s")] * 5
        record = self._closed("S-M09")
        job = self._jobs(record, "apix-mark:")
        for attempt in range(1, 5):
            self.assertIsInstance(self._attempt(job), RetryableJobError)
            job.invalidate_recordset()
            self.assertEqual((job.state, job.retry), ("pending", attempt))
        self.assertEqual(self._attempt(job), "FAILED: apix.unreachable")
        job.invalidate_recordset()
        record.invalidate_recordset()
        self.assertEqual(job.state, "done", "given up quietly, no failed job")
        self.assertFalse(record.apix_mark_confirmed)
        self.assertEqual(record.state, "closed", "the close stands")
        self.assertEqual(len(self._marked()), 5)
        self.assertEqual(self.Import._apix_unconfirmed_marks(self.company), record)

    def test_retry_pattern_is_exponential(self):
        function = self.env.ref("goodahead_invoice_import_apix.job_function_apix_mark_received")
        pattern = function._parse_retry_pattern()
        delays = [pattern[key] for key in sorted(pattern)]
        self.assertEqual(len(delays), 4, "a delay before each of the 4 retries")
        self.assertTrue(all(delay == delays[0] * 4**i for i, delay in enumerate(delays)))

    # -- the storage key is never logged --------------------------------------

    def test_storage_key_never_logged(self):
        self.marks["S-M10"] = [ApixUnreachable("Download: APIX could not be reached"), b"PK"]
        record = self._closed("S-M10")
        key = record.sudo().apix_storage_key
        self.assertTrue(key)
        job = self._jobs(record, "apix-mark:")
        with self.assertLogs("odoo", level="DEBUG") as logs:
            self._attempt(job)
            self._attempt(job)
            record._apix_mark_received()
        record.invalidate_recordset()
        self.assertTrue(record.apix_mark_confirmed)
        self.assertFalse([line for line in logs.output if key in line])
        job.invalidate_recordset()
        self.assertNotIn(key, str(job.result or "") + str(job.exc_info or ""))
        chatter = "\n".join(str(b) for b in record.message_ids.mapped("body"))
        self.assertNotIn(key, chatter)
