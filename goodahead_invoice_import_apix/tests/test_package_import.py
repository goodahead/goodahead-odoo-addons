# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The package-import job.

AC-01, AC-02, AC-03, AC-06, AC-07, AC-08b, AC-23, AC-24, AC-25. No network:
`ApixClient.list_packages` and `ApixClient.download` are patched; the package
is a ZIP built here from the Finvoice add-on's invented fixtures (Business IDs
0000NNN-C, IBANs with bank code 123456, @example.test).
"""

import io
import os
import zipfile
from unittest.mock import patch

from psycopg2.errors import LockNotAvailable

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_apix.lib.apix_client import (
    ApixClient,
    ApixEmptyAnswer,
    ApixUnreachable,
)
from odoo.addons.goodahead_invoice_import_apix.models.goodahead_invoice_import import (
    unpack_package,
)
from odoo.addons.queue_job.exception import RetryableJobError
from odoo.addons.queue_job.job import Job

FINVOICE_DATA = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "goodahead_invoice_import_finvoice",
    "tests",
    "data",
)
TRANSFER = "cccccccc-0000-4000-8000-000000000011"

TINY_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 10 10]>>endobj\n"
    b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
    b"0000000052 00000 n \n0000000101 00000 n \n"
    b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n158\n%%EOF\n"
)


def finvoice_fixture(name):
    with open(os.path.join(FINVOICE_DATA, name), "rb") as handle:
        return handle.read()


def make_zip(*files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files:
            archive.writestr(name, content)
    return buffer.getvalue()


def listing_group(storage_id, document_name="invoice.xml", image_name=None):
    group = {
        "StorageID": storage_id,
        "StorageKey": f"key-{storage_id}",
        "StorageStatus": "UNRECEIVED",
        "DocumentID": f"INV-{storage_id}",
        "SenderName": "Example Supplier Oy",
        "DocumentName": document_name,
        "DocumentDate": "20261115",
    }
    if image_name:
        group["ImageName"] = image_name
    return group


@tagged("post_install", "-at_install")
class TestApixPackageImport(TransactionCase):
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
        # The seller of the 3.0 fixtures: Business ID 0000003-5, VAT FI00000035.
        cls.vendor = cls.env["res.partner"].create(
            {
                "name": "Example Supplier Oy",
                "is_company": True,
                "vat": "FI00000035",
                "company_registry": "0000003-5",
                "street": "Esimerkkikatu 1",
                "city": "Helsinki",
                "zip": "00100",
                "country_id": cls.env.ref("base.fi").id,
            }
        )

    def setUp(self):
        super().setUp()
        if not self.journal:
            self.skipTest("no purchase journal on this database")
        self.packages = {}
        self.download_calls = []

        def fake_download(client, storage_id, storage_key, mark_received):
            self.download_calls.append((storage_id, mark_received))
            answer = self.packages[storage_id]
            if isinstance(answer, list):
                answer = answer.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        patcher = patch.object(ApixClient, "download", autospec=True, side_effect=fake_download)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Every test must leave APIX untouched: never a download that marks.
        self.addCleanup(self._assert_never_marked)

    def _assert_never_marked(self):
        marked = [call for call in self.download_calls if call[1] is not False]
        self.assertFalse(marked, "the import job never downloads with mark_received=True")

    # -- helpers -------------------------------------------------------------

    def _record(self, storage_id, body, document_name="invoice.xml", image_name=None):
        """A waiting APIX record, as the run leaves it, whose download
        answers ``body`` (bytes, an exception, or a list of those in turn)."""
        self.packages[storage_id] = body
        group = listing_group(storage_id, document_name, image_name)
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

    def _package_job(self, record):
        return self.env["queue.job"].sudo().search(
            [("identity_key", "=", f"apix-import:{record.id}")], order="id desc", limit=1
        )

    def _mark_jobs(self, record):
        return self.env["queue.job"].sudo().search(
            [("identity_key", "=", f"apix-mark:{record.id}")]
        )

    def _attempt(self, job_record):
        """One try of a stored job, as the job runner does it: a retryable
        error postpones the job and stores the retry count."""
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

    def _import(self, record):
        """Enqueue and run the package job once."""
        record._apix_enqueue_package_import()
        result = self._attempt(self._package_job(record))
        record.invalidate_recordset()
        return result

    def _bill_attachments(self, move):
        return self.env["ir.attachment"].search(
            [("res_model", "=", "account.move"), ("res_id", "=", move.id)]
        )

    def _chatter(self, record):
        return "\n".join(str(body) for body in record.message_ids.mapped("body"))

    # -- AC-01 -----------------------------------------------------------------

    def test_ac01_scheduler_run_imports_a_draft_bill_and_enqueues_mark(self):
        body = make_zip(("invoice.xml", finvoice_fixture("fi30_simple_invoice.xml")))
        self.packages["S-AC01"] = body
        with patch.object(
            ApixClient, "list_packages", autospec=True, return_value=[listing_group("S-AC01")]
        ):
            self.assertEqual(self.Connection._apix_run(self.company.id), "success")
        record = self.Import.search([("apix_storage_id", "=", "S-AC01")])
        self.assertEqual(record.state, "waiting")
        job = self._package_job(record)
        self.assertTrue(job, "the run enqueued the package job")
        self.assertEqual(job.channel, "root.apix")
        self.assertEqual(job.max_retries, 3)

        result = self._attempt(job)
        record.invalidate_recordset()
        self.assertEqual(record.state, "imported", record.error_message or result)
        self.assertEqual(self.download_calls, [("S-AC01", False)])

        move = record.move_id
        self.assertEqual(move.state, "draft")
        self.assertEqual(move.move_type, "in_invoice")
        self.assertEqual(move.partner_id, self.vendor, "matched by VAT number")
        self.assertEqual(move.ref, "10045")
        self.assertEqual(str(move.invoice_date), "2026-11-15")
        self.assertEqual(str(move.invoice_date_due), "2026-11-29")
        self.assertEqual(move.payment_reference, "10001235")
        lines = move.invoice_line_ids.filtered(lambda line: line.display_type == "product")
        self.assertEqual(len(lines), 2)
        self.assertAlmostEqual(move.amount_untaxed, 360.00, places=2)
        self.assertAlmostEqual(move.amount_tax, 91.80, places=2)
        self.assertAlmostEqual(move.amount_total, 451.80, places=2)
        self.assertEqual(lines.tax_ids.mapped("amount"), [25.5])
        self.assertIn("Imported from APIX", self._chatter(move))

        # The package files are kept on the record.
        self.assertEqual(record.finvoice_attachment_id.name, "invoice.xml")
        # Mark-received enqueued in the same transaction, once.
        mark = self._mark_jobs(record)
        self.assertEqual(len(mark), 1)
        self.assertEqual(mark.channel, "root.apix")
        self.assertEqual(mark.method_name, "_apix_mark_received")
        self.assertEqual(mark.max_retries, 5)

    # -- AC-02 -----------------------------------------------------------------

    def test_ac02_business_id_in_the_vat_field(self):
        vendor = self.env["res.partner"].create(
            {
                "name": "Example Trading Oy",
                "is_company": True,
                "company_registry": "0000001-9",
                "country_id": self.env.ref("base.fi").id,
            }
        )
        self.assertFalse(vendor.vat, "known by its Business ID only")
        body = make_zip(("invoice.xml", finvoice_fixture("fi30_business_id_in_vat_field.xml")))
        record = self._record("S-AC02", body)
        self._import(record)
        self.assertEqual(record.state, "imported", record.error_message or "")
        self.assertEqual(record.move_id.partner_id, vendor)
        self.assertEqual(record.move_id.ref, "T-2026-0091")
        self.assertTrue(self._mark_jobs(record))

    # -- AC-24 -----------------------------------------------------------------

    def test_ac24_credit_notes_come_out_positive_whatever_the_sign(self):
        for storage_id, fixture, total in (
            ("S-AC24N", "fi30_credit_note_negative.xml", 125.50),
            ("S-AC24P", "fi30_credit_note_positive.xml", 50.20),
        ):
            with self.subTest(fixture=fixture):
                record = self._record(storage_id, make_zip(("invoice.xml", finvoice_fixture(fixture))))
                self._import(record)
                self.assertEqual(record.state, "imported", record.error_message or "")
                move = record.move_id
                self.assertEqual(move.move_type, "in_refund", "a credit note, never a bill to pay")
                self.assertEqual(move.partner_id, self.vendor)
                self.assertAlmostEqual(move.amount_total, total, places=2)
                lines = move.invoice_line_ids.filtered(lambda line: line.display_type == "product")
                self.assertTrue(lines)
                self.assertTrue(all(line.price_subtotal > 0 for line in lines))
                self.assertTrue(self._mark_jobs(record))

    # -- AC-25 -----------------------------------------------------------------

    def test_ac25_without_image_the_finvoice_is_the_main_document(self):
        raw = finvoice_fixture("fi30_simple_invoice.xml")
        body = make_zip(("invoice.xml", raw), ("terms.txt", b"Toimitusehdot"))
        record = self._record("S-AC25A", body)
        self._import(record)
        self.assertEqual(record.state, "imported", record.error_message or "")
        self.assertFalse(record.pdf_attachment_id)
        self.assertEqual(record.finvoice_extra_attachment_ids.mapped("name"), ["terms.txt"])
        move = record.move_id
        self.assertEqual(move.message_main_attachment_id.name, "invoice.xml")
        self.assertEqual(move.message_main_attachment_id.raw, raw)
        names = self._bill_attachments(move).mapped("name")
        self.assertIn("terms.txt", names)
        self.assertFalse([name for name in names if "Generated by Odoo" in name])

    def test_ac25_with_image_the_pdf_is_main_and_the_finvoice_attached(self):
        body = make_zip(
            ("invoice.xml", finvoice_fixture("fi30_simple_invoice.xml")),
            ("invoice-10045.pdf", TINY_PDF),
            ("liite.pdf", TINY_PDF + b"%liite"),
        )
        record = self._record("S-AC25B", body, image_name="invoice-10045.pdf")
        self._import(record)
        self.assertEqual(record.state, "imported", record.error_message or "")
        self.assertEqual(record.pdf_attachment_id.name, "invoice-10045.pdf", "named by ImageName")
        move = record.move_id
        self.assertEqual(move.message_main_attachment_id.name, "invoice-10045.pdf")
        names = self._bill_attachments(move).mapped("name")
        self.assertIn("invoice.xml", names)
        self.assertIn("liite.pdf", names)

    def test_plain_finvoice_body_is_accepted(self):
        record = self._record("S-XML", finvoice_fixture("fi30_simple_invoice.xml"))
        self._import(record)
        self.assertEqual(record.state, "imported", record.error_message or "")
        self.assertEqual(record.finvoice_attachment_id.name, "invoice.xml")

    def test_several_messages_fail_as_data_with_a_reason(self):
        """One package is one record and one bill: a package with
        several Finvoice messages is not half imported but waits for an
        Accountant, with the files kept and nothing marked."""
        body = make_zip(("batch.xml", finvoice_fixture("fi30_two_messages.xml")))
        record = self._record("S-TWO", body, document_name="batch.xml")
        result = self._import(record)
        self.assertEqual(result, "FAILED: apix.several_messages")
        self.assertEqual((record.state, record.failure_kind), ("failed", "data"))
        self.assertFalse(record.move_id)
        self.assertIn("holds 2 invoices", record.error_message)
        self.assertIn("close this record", record.error_message)
        self.assertEqual(record.finvoice_attachment_id.name, "batch.xml", "the files are kept")
        self.assertFalse(self._mark_jobs(record))
        # Closed by the Accountant once booked by hand: then APIX hears of it.
        record.action_close_without_bill("Two invoices booked by hand")
        self.assertTrue(self._mark_jobs(record))

    # -- AC-06 / AC-07 ---------------------------------------------------------

    def test_ac06_differences_become_suggestions_vendor_unchanged(self):
        self.vendor.write({"street": "Vanhakatu 9", "city": "Turku", "zip": "20100"})
        self.env["res.partner.bank"].create(
            {"acc_number": "FI3912345600000008", "partner_id": self.vendor.id}
        )
        record = self._record(
            "S-AC06", make_zip(("invoice.xml", finvoice_fixture("fi30_simple_invoice.xml")))
        )
        self._import(record)
        self.assertEqual(record.state, "imported", record.error_message or "")
        self.assertEqual(record.move_id.partner_id, self.vendor)
        self.vendor.invalidate_recordset()
        self.assertEqual(self.vendor.street, "Vanhakatu 9", "the vendor's data is unchanged")
        self.assertEqual(self.vendor.city, "Turku")
        self.assertEqual(self.vendor.bank_ids.mapped("acc_number"), ["FI3912345600000008"])
        self.assertNotEqual(record.move_id.partner_bank_id.acc_number, "FI1612345600000002")
        suggestions = self.env["goodahead.vendor.data.suggestion"].search(
            [("move_id", "=", record.move_id.id)]
        )
        by_field = {s.field_name: s for s in suggestions}
        self.assertIn("street", by_field)
        self.assertIn("bank_account", by_field)
        self.assertTrue(by_field["bank_account"].is_sensitive, "never pre-selected")
        self.assertFalse(by_field["street"].is_sensitive)
        for suggestion in suggestions:
            self.assertEqual(suggestion.state, "pending")

    def test_ac07_iban_of_another_partner_is_not_attached_nor_suggested(self):
        owner = self.env["res.partner"].create({"name": "Example Factoring Oy", "is_company": True})
        foreign = self.env["res.partner.bank"].create(
            {"acc_number": "FI1612345600000002", "partner_id": owner.id}
        )
        record = self._record(
            "S-AC07", make_zip(("invoice.xml", finvoice_fixture("fi30_simple_invoice.xml")))
        )
        self._import(record)
        self.assertEqual(record.state, "imported", record.error_message or "")
        move = record.move_id
        self.assertEqual(move.partner_id, self.vendor)
        self.assertNotEqual(move.partner_bank_id, foreign)
        self.assertNotIn(foreign.sanitized_acc_number, self.vendor.bank_ids.mapped("sanitized_acc_number"))
        self.assertEqual(foreign.partner_id, owner)
        suggestions = self.env["goodahead.vendor.data.suggestion"].search(
            [("move_id", "=", move.id), ("field_name", "=", "bank_account")]
        )
        self.assertFalse(suggestions, "a foreign IBAN is never offered")
        self.assertIn("Example Factoring Oy", self._chatter(move))

    # -- AC-03 -----------------------------------------------------------------

    def test_ac03_bad_data_fails_and_does_not_mark(self):
        unknown_version = finvoice_fixture("fi13_invoice.xml").replace(b'Version="1.3"', b'Version="1.2"')
        record = self._record("S-AC03", make_zip(("invoice.xml", unknown_version)))
        result = self._import(record)
        self.assertEqual(result, "FAILED: finvoice.unsupported_version")
        self.assertEqual(record.state, "failed")
        self.assertEqual(record.failure_kind, "data")
        self.assertIn("finvoice.unsupported_version", record.error_message)
        self.assertFalse(record.move_id)
        self.assertTrue(record.finvoice_attachment_id, "the package files are kept")
        self.assertFalse(self._mark_jobs(record), "never marked without a bill")
        self.assertEqual(self._package_job(record).state, "done", "no automatic retry")

    def test_ac03_zip_without_finvoice_fails_and_keeps_files(self):
        record = self._record("S-NOFV", make_zip(("scan.pdf", TINY_PDF), ("notes.txt", b"x")))
        result = self._import(record)
        self.assertEqual(result, "FAILED: apix.no_finvoice")
        self.assertEqual(record.state, "failed")
        self.assertEqual(record.failure_kind, "data")
        self.assertIn("scan.pdf", record.error_message)
        kept = record.pdf_attachment_id | record.finvoice_extra_attachment_ids
        self.assertEqual(sorted(kept.mapped("name")), ["notes.txt", "scan.pdf"])
        self.assertFalse(self._mark_jobs(record))

        # A retry downloads again and replaces the kept files, not doubles them.
        self.packages["S-NOFV"] = make_zip(("scan.pdf", TINY_PDF), ("notes.txt", b"x"))
        record.action_retry()
        self._attempt(self._package_job(record))
        record.invalidate_recordset()
        attachments = self.env["ir.attachment"].search(
            [("res_model", "=", record._name), ("res_id", "=", record.id)]
        )
        self.assertEqual(sorted(attachments.mapped("name")), ["notes.txt", "scan.pdf"])

    def test_ac03_not_a_package_fails_as_data(self):
        record = self._record("S-JUNK", b"this is not a package")
        result = self._import(record)
        self.assertEqual(result, "FAILED: apix.package_unreadable")
        self.assertEqual(record.failure_kind, "data")
        self.assertFalse(self._mark_jobs(record))

    def test_duplicate_outcome_does_not_mark(self):
        raw = finvoice_fixture("fi30_simple_invoice.xml")
        first = self._record("S-DUP1", make_zip(("invoice.xml", raw)))
        self._import(first)
        self.assertEqual(first.state, "imported", first.error_message or "")
        second = self._record("S-DUP2", make_zip(("invoice.xml", raw)))
        self._import(second)
        self.assertEqual(second.state, "duplicate")
        self.assertEqual(second.duplicate_of_move_id, first.move_id)
        self.assertFalse(second.move_id)
        self.assertFalse(self._mark_jobs(second), "a duplicate is not marked received")
        self.assertTrue(self._mark_jobs(first))

    # -- AC-08b ----------------------------------------------------------------

    def test_ac08b_unreachable_is_retried_then_interrupted_then_next_run(self):
        good = make_zip(("invoice.xml", finvoice_fixture("fi30_simple_invoice.xml")))
        record = self._record(
            "S-AC08",
            [
                ApixUnreachable("Download: APIX could not be reached (ConnectionError)"),
                ApixEmptyAnswer("Download: APIX returned nothing (wrong storage id or key)"),
                ApixUnreachable("Download: APIX did not answer within 60 s"),
                good,
            ],
        )
        record._apix_enqueue_package_import()
        job = self._package_job(record)

        self.assertIsInstance(self._attempt(job), RetryableJobError)
        job.invalidate_recordset()
        record.invalidate_recordset()
        self.assertEqual((job.state, job.retry), ("pending", 1))
        self.assertEqual(record.state, "waiting", "nothing recorded while the queue retries")

        self.assertIsInstance(self._attempt(job), RetryableJobError)
        job.invalidate_recordset()
        self.assertEqual((job.state, job.retry), ("pending", 2))

        # Third and last try: recorded as interrupted, job done.
        result = self._attempt(job)
        job.invalidate_recordset()
        record.invalidate_recordset()
        self.assertEqual(result, "FAILED: apix.unreachable")
        self.assertEqual(job.state, "done")
        self.assertEqual(record.state, "failed")
        self.assertEqual(record.failure_kind, "interrupted")
        self.assertFalse(record.move_id)
        self.assertFalse(self._mark_jobs(record))

        # The next run enqueues it again, without an Accountant.
        with patch.object(
            ApixClient, "list_packages", autospec=True, return_value=[listing_group("S-AC08")]
        ):
            self.Connection._apix_run(self.company.id)
        again = self._package_job(record)
        self.assertNotEqual(again, job)
        self._attempt(again)
        record.invalidate_recordset()
        self.assertEqual(record.state, "imported", record.error_message or "")
        self.assertEqual(
            self.env["account.move"].search_count([("ref", "=", "10045"), ("partner_id", "=", self.vendor.id)]),
            1,
        )
        self.assertEqual(len(self.download_calls), 4)

    def test_truncated_zip_is_technical(self):
        good = make_zip(("invoice.xml", finvoice_fixture("fi30_simple_invoice.xml")))
        record = self._record("S-CUT", [good[: len(good) // 2], good])
        record._apix_enqueue_package_import()
        job = self._package_job(record)
        self.assertIsInstance(self._attempt(job), RetryableJobError)
        self._attempt(job)
        record.invalidate_recordset()
        self.assertEqual(record.state, "imported", record.error_message or "")

    # -- AC-23 -----------------------------------------------------------------

    def test_ac23_job_run_twice_gives_one_bill_and_one_download(self):
        body = make_zip(("invoice.xml", finvoice_fixture("fi30_simple_invoice.xml")))
        record = self._record("S-AC23", body)
        self._import(record)
        self.assertEqual(record.state, "imported", record.error_message or "")
        move = record.move_id

        # The same job delivered again, and a second job for the record.
        self.assertEqual(record._apix_import_package(), "OK: already processed")
        record._apix_enqueue_package_import()
        self.assertEqual(self._attempt(self._package_job(record)), "OK: already processed")
        record.invalidate_recordset()
        self.assertEqual(record.move_id, move)
        self.assertEqual(len(self.download_calls), 1)
        self.assertEqual(
            self.env["account.move"].search_count([("ref", "=", "10045"), ("partner_id", "=", self.vendor.id)]),
            1,
        )
        self.assertEqual(len(self._mark_jobs(record)), 1)

    def test_ac23_settled_record_is_not_downloaded(self):
        record = self._record("S-SETTLED", make_zip(("invoice.xml", b"<x/>")))
        record.action_close_without_bill("Handled by hand")
        self.assertEqual(record._apix_import_package(), "OK: already processed")
        self.assertFalse(self.download_calls)

    def test_retry_of_a_data_failure_reuses_the_kept_files(self):
        bad = finvoice_fixture("fi30_simple_invoice.xml").replace(b">INV01<", b">QUO01<")
        record = self._record("S-RETRY", make_zip(("invoice.xml", bad)))
        self._import(record)
        self.assertEqual(record.state, "failed")
        # The Accountant fixes the kept file and retries: no new download.
        record.finvoice_attachment_id.raw = finvoice_fixture("fi30_simple_invoice.xml")
        record.action_retry()
        job = self._package_job(record)
        self.assertEqual(job.method_name, "_apix_import_package", "an APIX retry runs the package job")
        self._attempt(job)
        record.invalidate_recordset()
        self.assertEqual(record.state, "imported", record.error_message or "")
        self.assertEqual(len(self.download_calls), 1)

    # -- AC-23: lock contention ------------------------------------------------

    def _locked(self, table, mode):
        """Make the NOWAIT row lock on ``table`` fail as if another
        transaction held the row (PostgreSQL's lock_not_available)."""
        cursor_class = type(self.env.cr)
        execute = cursor_class.execute

        def locked_execute(cr, query, *args, **kwargs):
            text = str(query)
            if f"FROM {table} WHERE" in text and f"FOR {mode} NOWAIT" in text:
                raise LockNotAvailable("could not obtain lock on row")
            return execute(cr, query, *args, **kwargs)

        return patch.object(cursor_class, "execute", locked_execute)

    def test_ac23_locked_record_is_retried_without_using_a_try(self):
        body = make_zip(("invoice.xml", finvoice_fixture("fi30_simple_invoice.xml")))
        record = self._record("S-LOCK", body)
        record._apix_enqueue_package_import()
        job = self._package_job(record)
        with self._locked("goodahead_invoice_import", "UPDATE"):
            error = self._attempt(job)
        self.assertIsInstance(error, RetryableJobError)
        job.invalidate_recordset()
        self.assertEqual((job.state, job.retry), ("pending", 0), "no try used up")
        self.assertFalse(self.download_calls, "nothing done while the row is locked")
        # The lock is gone: the same job imports, once.
        self._attempt(job)
        record.invalidate_recordset()
        self.assertEqual(record.state, "imported", record.error_message or "")
        self.assertEqual(len(self.download_calls), 1)

    # -- T15: switch-back race -------------------------------------------------

    def test_package_job_waits_while_the_connection_is_locked_by_a_switch(self):
        body = make_zip(("invoice.xml", finvoice_fixture("fi30_simple_invoice.xml")))
        record = self._record("S-CONNLOCK", body)
        record._apix_enqueue_package_import()
        job = self._package_job(record)
        with self._locked("goodahead_apix_connection", "SHARE"):
            error = self._attempt(job)
        self.assertIsInstance(error, RetryableJobError)
        job.invalidate_recordset()
        self.assertEqual((job.state, job.retry), ("pending", 0))
        self.assertFalse(self.download_calls)

    def test_package_job_rechecks_receiving_under_the_lock(self):
        """A switch back committed by another transaction after this one
        read the connection: the job reads the state again under its lock."""
        record = self._record(
            "S-SWITCHED", make_zip(("invoice.xml", finvoice_fixture("fi30_simple_invoice.xml")))
        )
        self.assertEqual(self.connection.receiving_state, "live")  # cached
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE goodahead_apix_connection SET receiving_state = 'off' WHERE id = %s",
            [self.connection.id],
        )
        self.assertEqual(record._apix_import_package(), "SKIPPED: receiving off")
        self.assertFalse(record.move_id)
        self.assertFalse(self.download_calls)

    # -- T15: no import on a stale status -----------------------

    def test_received_package_goes_to_recognition_not_to_import(self):
        raw = finvoice_fixture("fi30_simple_invoice.xml")
        record = self._record("S-RCVD", make_zip(("invoice.xml", raw)))
        record.apix_status = "RECEIVED"  # what the last run listed
        result = self._import(record)
        self.assertEqual(result, "SKIPPED: received at APIX, recognising")
        self.assertEqual(record.state, "possibly_lost")
        self.assertFalse(record.move_id)
        self.assertFalse(self.download_calls, "the package job did not download it")
        recognise = self.env["queue.job"].sudo().search(
            [("identity_key", "=", f"apix-recognise:{record.id}")]
        )
        self.assertEqual(len(recognise), 1)

        # An Accountant decides to import it after all: the package job does.
        recognise.button_cancelled()
        record.action_import_possibly_lost()
        self.assertTrue(record.apix_import_requested)
        self._attempt(self._package_job(record))
        record.invalidate_recordset()
        self.assertEqual(record.state, "imported", record.error_message or "")

    # -- AC-11c: a reopened record waits for an explicit Retry -------------------

    def test_reopened_record_is_left_for_an_explicit_retry(self):
        raw = finvoice_fixture("fi30_simple_invoice.xml")
        record = self._record("S-REOPEN", make_zip(("invoice.xml", raw)))
        record.action_close_without_bill("Thought to be a test message")
        # The close was confirmed at APIX: it now lists the package as received.
        record.write({"apix_mark_confirmed": True, "apix_status": "RECEIVED"})
        record.action_reopen()
        self.assertTrue(record.apix_reopened)
        received = dict(listing_group("S-REOPEN"), StorageStatus="RECEIVED")
        with patch.object(ApixClient, "list_packages", autospec=True, return_value=[received]):
            self.assertEqual(self.Connection._apix_run(self.company.id), "success")
        record.invalidate_recordset()
        self.assertEqual(record.state, "waiting", "never turned possibly lost by a run")
        jobs = self.env["queue.job"].sudo().search(
            [("identity_key", "in", [f"apix-recognise:{record.id}", f"apix-import:{record.id}"])]
        )
        self.assertFalse(jobs, "nothing automatic")

        record.action_retry()
        self._attempt(self._package_job(record))
        record.invalidate_recordset()
        self.assertEqual(record.state, "imported", record.error_message or "")

    # -- T15: Accountant actions only while live ---------------------------------

    def test_accountant_actions_refused_unless_live(self):
        bad = finvoice_fixture("fi30_simple_invoice.xml").replace(b">INV01<", b">QUO01<")
        record = self._record("S-NOTLIVE", make_zip(("invoice.xml", bad)))
        self._import(record)
        self.assertEqual(record.state, "failed")
        jobs_before = self.env["queue.job"].sudo().search_count([])
        self.connection._apix_set_receiving_state("shadow")
        for action in (record.action_retry, record.action_reimport_from_json):
            with self.subTest(action=action.__name__), self.assertRaisesRegex(UserError, "not live"):
                action()
        record.write({"state": "duplicate"})
        with self.assertRaisesRegex(UserError, "not live"):
            record.action_override_duplicate()
        record.write({"state": "failed"})
        self.assertEqual(self.env["queue.job"].sudo().search_count([]), jobs_before)

        self.connection._apix_set_receiving_state("live")
        record.action_retry()
        self.assertEqual(self._package_job(record).state, "pending")

    # -- events.md: the last try always leaves a reason ------------------------

    def test_last_try_records_any_error_as_interrupted(self):
        record = self._record(
            "S-BOOM", make_zip(("invoice.xml", finvoice_fixture("fi30_simple_invoice.xml")))
        )
        with patch.object(
            type(self.Import),
            "_apix_import_stored_package",
            autospec=True,
            side_effect=ValueError("unexpected"),
        ):
            result = record._apix_import_package()  # outside a job: the last try
        record.invalidate_recordset()
        self.assertEqual(result, "FAILED: apix.unexpected")
        self.assertEqual((record.state, record.failure_kind), ("failed", "interrupted"))
        self.assertIn("unexpected error (ValueError)", record.error_message)
        self.assertFalse(record.move_id)

    def test_earlier_tries_leave_errors_to_the_queue(self):
        record = self._record(
            "S-BOOM2", make_zip(("invoice.xml", finvoice_fixture("fi30_simple_invoice.xml")))
        )
        record._apix_enqueue_package_import()
        job = self._package_job(record)
        with (
            patch.object(
                type(self.Import),
                "_apix_import_stored_package",
                autospec=True,
                side_effect=ValueError("unexpected"),
            ),
            self.assertRaises(ValueError),
        ):
            Job.load(self.env, job.uuid).perform()

    def test_settled_hook_hears_of_the_billed_package(self):
        """`_apix_after_package_settled`: once, with the record, in the
        transaction that saved the bill, after the mark was enqueued."""
        record = self._record(
            "S-HOOK", make_zip(("invoice.xml", finvoice_fixture("fi30_simple_invoice.xml")))
        )
        seen = []

        def settled(records):
            seen.append((records.ids, records.move_id.ids, len(self._mark_jobs(records))))

        with patch.object(
            type(self.Import), "_apix_after_package_settled", autospec=True, side_effect=settled
        ):
            self._import(record)
        self.assertEqual(record.state, "imported", record.error_message)
        self.assertEqual(seen, [([record.id], record.move_id.ids, 1)])

    # -- the package reader ----------------------------------------------------

    def test_unpack_finds_files_by_listing_names_or_content(self):
        raw = finvoice_fixture("fi30_simple_invoice.xml")
        package = unpack_package(
            make_zip(("a.pdf", TINY_PDF), ("doc.xml", raw), ("img.pdf", TINY_PDF)),
            document_name="doc.xml",
            image_name="img.pdf",
        )
        self.assertEqual(package.finvoice_file.name, "doc.xml")
        self.assertEqual(package.image_file.name, "img.pdf")
        self.assertEqual([f.name for f in package.other_files()], ["a.pdf"])

        package = unpack_package(make_zip(("x/inv.xml", raw), ("x/scan.pdf", TINY_PDF)))
        self.assertEqual(package.finvoice_file.name, "inv.xml")
        self.assertEqual(package.image_file.name, "scan.pdf")
