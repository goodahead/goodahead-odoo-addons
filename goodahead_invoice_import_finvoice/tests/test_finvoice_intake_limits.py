# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""One Finvoice file or email must not become an unbounded number of records.

Each message of a file gets an import record, a job and a chatter post, and
anybody can mail the alias. Messages per file, Finvoice files per email and
bytes per file are therefore capped (system parameters); over a cap the
file or mail becomes ONE failed record and no job. The files that came with a
multi-message file are attached once, not to every record. Every fixture is
invented.
"""

import os
import unittest
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

try:
    from odoo.tests import TransactionCase, tagged

    from odoo.addons.goodahead_invoice_import.tests.common import ensure_purchase_journal
except ImportError as exc:  # plain unittest without the ERP
    raise unittest.SkipTest("the Finvoice source tests need the ERP") from exc

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
PARAM = "goodahead_invoice_import_finvoice.%s"


def _raw(name):
    with open(os.path.join(DATA_DIR, name), "rb") as handle:
        return handle.read()


@tagged("post_install", "-at_install")
class TestFinvoiceIntakeLimits(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.Import = cls.env["goodahead.invoice.import"]
        cls.journal = ensure_purchase_journal(cls.env, cls.env.company)

    def _set(self, key, value):
        self.env["ir.config_parameter"].sudo().set_param(PARAM % key, str(value))

    def _upload(self, *files):
        attachments = self.env["ir.attachment"].create(
            [{"name": name, "raw": raw, "res_model": "account.journal",
              "res_id": self.journal.id} for name, raw in files]
        )
        before = self.Import.search([]).ids
        self.journal.with_context(
            default_move_type="in_invoice", default_journal_id=self.journal.id
        ).create_document_from_attachment(attachments.ids)
        return self.Import.search([("id", "not in", before)], order="id")

    def _mail(self, files, subject="Laskut"):
        message = MIMEMultipart()
        message["From"] = "laskutus@example.test"
        message["To"] = "bills@example.test"
        message["Subject"] = subject
        message["Message-Id"] = f"<limits-{subject.replace(' ', '-')}@example.test>"
        message.attach(MIMEText("Liitteenä laskut.", "plain", "utf-8"))
        for name, content, subtype in files:
            part = MIMEApplication(content, subtype)
            part.add_header("Content-Disposition", "attachment", filename=name)
            message.attach(part)
        before = self.Import.search([]).ids
        self.env["mail.thread"].message_process(
            "goodahead.invoice.import", message.as_bytes()
        )
        return self.Import.search([("id", "not in", before)], order="id")

    def _one_failed_without_job(self, records, code):
        self.assertEqual(len(records), 1, records.mapped("name"))
        self.assertEqual(records.state, "failed")
        self.assertFalse(records.queue_job_id)
        self.assertIn(code, records.error_message or "")

    def test_a_file_with_too_many_messages_is_one_failed_record(self):
        self._set("max_messages_per_file", 1)
        records = self._upload(("batch.xml", _raw("fi30_two_messages.xml")))
        self._one_failed_without_job(records, "finvoice.too_many_messages")
        mailed = self._mail([("batch.xml", _raw("fi30_two_messages.xml"), "xml")])
        self._one_failed_without_job(mailed, "finvoice.too_many_messages")
        # The control: within the cap the file is split as before.
        self._set("max_messages_per_file", 2)
        records = self._upload(("batch2.xml", _raw("fi30_two_messages.xml")))
        self.assertEqual(len(records), 2)
        self.assertTrue(all(records.mapped("queue_job_id")))

    def test_an_email_with_too_many_finvoice_files_is_one_failed_record(self):
        self._set("max_files_per_email", 1)
        raw = _raw("fi30_simple_invoice.xml")
        mailed = self._mail([("a.xml", raw, "xml"), ("b.xml", raw, "xml")])
        self._one_failed_without_job(mailed, "finvoice.too_many_files")
        self.assertIn("a.xml", mailed.error_message)

    def test_a_file_over_the_size_cap_is_one_failed_record(self):
        raw = _raw("fi30_simple_invoice.xml")
        self._set("max_file_bytes", len(raw) - 1)
        self._one_failed_without_job(
            self._upload(("big.xml", raw)), "finvoice.too_large"
        )
        self._one_failed_without_job(
            self._mail([("big.xml", raw, "xml")]), "finvoice.too_large"
        )

    def test_companion_files_are_attached_once(self):
        records = self._mail(
            [
                ("batch.xml", _raw("fi30_two_messages.xml"), "xml"),
                ("delivery-note.txt", b"Rahtikirja 1", "octet-stream"),
            ]
        )
        self.assertEqual(len(records), 2)
        # (The mail's own message keeps the files it carried; that is the
        # mail, not a copy per record.)
        copies = records.mapped("finvoice_extra_attachment_ids").filtered(
            lambda a: a.name == "delivery-note.txt"
        )
        self.assertEqual(len(copies), 1, "one copy of a companion, not one per record")
        # Every record still knows the file came with it.
        for record in records:
            self.assertIn(copies, record.finvoice_extra_attachment_ids)

    def test_the_defaults_are_generous(self):
        Import = self.Import
        self.assertGreaterEqual(Import._finvoice_max_messages_per_file(), 100)
        self.assertGreaterEqual(Import._finvoice_max_files_per_email(), 10)
        self.assertGreaterEqual(Import._finvoice_max_file_bytes(), 10 * 1024 * 1024)
