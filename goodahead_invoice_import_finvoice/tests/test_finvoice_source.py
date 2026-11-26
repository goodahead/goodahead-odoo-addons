# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The Finvoice source: upload and mail intake, the import job (spec
AC-20, AC-21, AC-25; sad.md Flow G; screens.md SCR-10).

Runs with the core and this add-on only — the AI part is not installed, which
is exactly the database AC-21 talks about. Every fixture under ``data/`` is
invented (Business IDs 0000NNN-C, IBANs with bank code 123456,
``@example.test``).
"""

import json
import os
import unittest
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import patch

try:
    from odoo.tests import TransactionCase, tagged

    from odoo.addons.goodahead_invoice_import.tests.common import ensure_purchase_journal
    from odoo.addons.queue_job.job import Job
except ImportError as exc:  # plain unittest without the ERP: library tests only
    raise unittest.SkipTest("the Finvoice source tests need the ERP") from exc

from ..lib.sniff import looks_like_finvoice

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# The smallest well-formed PDF: enough for the ERP to treat it as one.
TINY_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 10 10]>>endobj\n"
    b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n"
    b"0000000052 00000 n \n0000000101 00000 n \n"
    b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n158\n%%EOF\n"
)


def _raw(name):
    with open(os.path.join(DATA_DIR, name), "rb") as handle:
        return handle.read()


@tagged("post_install", "-at_install")
class TestFinvoiceSource(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.Import = cls.env["goodahead.invoice.import"]
        cls.journal = ensure_purchase_journal(cls.env, cls.company)
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
        # The seller of every 3.0 fixture: Business ID 0000003-5, VAT FI00000035.
        cls.vendor = cls.env["res.partner"].create(
            {
                "name": "Example Supplier Oy",
                "is_company": True,
                "vat": "FI00000035",
                "company_registry": "0000003-5",
                "country_id": cls.env.ref("base.fi").id,
            }
        )

    # -- helpers ---------------------------------------------------------------

    def _upload(self, *files):
        attachments = self.env["ir.attachment"].create(
            [
                {"name": name, "raw": raw, "res_model": "account.journal", "res_id": self.journal.id}
                for name, raw in files
            ]
        )
        action = self.journal.with_context(
            default_move_type="in_invoice", default_journal_id=self.journal.id
        ).create_document_from_attachment(attachments.ids)
        return action

    def _records(self, action):
        self.assertEqual(action["res_model"], "goodahead.invoice.import")
        if action.get("res_id"):
            return self.Import.browse(action["res_id"])
        return self.Import.search(action["domain"], order="id")

    def _perform(self, record):
        self.assertTrue(record.queue_job_id, "the import is queued, not run inline")
        Job.load(self.env, record.queue_job_id.uuid).perform()
        record.invalidate_recordset()
        return record

    def _chatter(self, record):
        return "\n".join(str(body) for body in record.message_ids.mapped("body"))

    def _mail(self, parts, subject="Lasku 10045"):
        message = MIMEMultipart()
        message["From"] = "laskutus@example.test"
        message["To"] = "bills@example.test"
        message["Subject"] = subject
        message["Message-Id"] = f"<t9-{subject.replace(' ', '-')}@example.test>"
        message.attach(MIMEText("Liitteenä lasku.", "plain", "utf-8"))
        for part in parts:
            message.attach(part)
        before = self.Import.search([]).ids
        self.env["mail.thread"].message_process(
            "goodahead.invoice.import", message.as_bytes()
        )
        return self.Import.search([("id", "not in", before)], order="id")

    def _file_part(self, name, content, maintype="application", subtype="octet-stream"):
        if maintype == "text":
            part = MIMEText(content.decode("utf-8"), subtype, "utf-8")
        else:
            part = MIMEApplication(content, subtype)
        part.add_header("Content-Disposition", "attachment", filename=name)
        return part

    # -- the sniff ---------------------------------------------------------------

    def test_sniff(self):
        self.assertTrue(looks_like_finvoice(_raw("fi30_simple_invoice.xml")))
        self.assertTrue(looks_like_finvoice(_raw("fi13_invoice.xml")))
        self.assertTrue(
            looks_like_finvoice(_raw("fi13_invoice.xml").replace(b'Version="1.3"', b'Version="1.2"')),
            "refused later, with a reason",
        )
        self.assertTrue(looks_like_finvoice(_raw("fi30_two_messages.xml")))
        self.assertFalse(looks_like_finvoice(TINY_PDF))
        self.assertFalse(looks_like_finvoice(b"<Invoice><FinvoiceSenderInfo/></Invoice>"))
        self.assertFalse(looks_like_finvoice(_raw("not_xml.xml")))

    # -- upload --------------------------------------------

    def test_upload_one_invoice_becomes_a_draft_bill_without_ai(self):
        raw = _raw("fi30_simple_invoice.xml")
        record = self._records(self._upload(("10045.xml", raw)))
        self.assertEqual(len(record), 1)
        self.assertEqual(record.source, "upload")
        self.assertEqual(record.state, "waiting")
        self.assertEqual(record.journal_id, self.journal)
        self.assertEqual(record.finvoice_attachment_id.raw, raw, "the file as uploaded")
        self.assertFalse(record.pdf_attachment_id)

        self._perform(record)
        self.assertEqual(record.state, "imported", record.error_message or "")
        move = record.move_id
        self.assertEqual(move.state, "draft")
        self.assertEqual(move.move_type, "in_invoice")
        self.assertEqual(move.partner_id, self.vendor, "matched by VAT / Business ID")
        self.assertEqual(move.ref, "10045")
        self.assertAlmostEqual(move.amount_total, 451.80, places=2)
        # AC-25: no image, so the Finvoice message is the bill's main document,
        # and no "Generated by Odoo" substitute was made.
        main = move.message_main_attachment_id
        self.assertEqual(main.name, "10045.xml")
        self.assertEqual(main.raw, raw)
        generated = self.env["ir.attachment"].search(
            [("res_model", "=", "account.move"), ("res_id", "=", move.id),
             ("name", "ilike", "Generated by Odoo")]
        )
        self.assertFalse(generated)
        # AC-21: a neutral note, no AI usage.
        chatter = self._chatter(move)
        self.assertIn("Imported from Finvoice (upload)", chatter)
        self.assertNotIn("Claude", chatter)
        self.assertNotIn("tokens", chatter.lower())
        for name in ("extraction_model", "input_tokens", "output_tokens"):
            if name in record._fields:
                self.assertFalse(record[name])
        self.assertEqual(record.seller_business_id, "0000003-5")

    def test_upload_two_invoices_gives_two_records(self):
        action = self._upload(("batch.xml", _raw("fi30_two_messages.xml")))
        records = self._records(action)
        self.assertEqual(len(records), 2)
        self.assertEqual(records.mapped("name"), ["batch-1.xml", "batch-2.xml"])
        for record in records:
            self.assertTrue(record.queue_job_id)
            self.assertNotIn(b"B-2" if record.name == "batch-1.xml" else b"B-1",
                             record.finvoice_attachment_id.raw,
                             "each record keeps its own message only")
        for record in records:
            self._perform(record)
        self.assertEqual(records.mapped("state"), ["imported", "imported"],
                         records.mapped("error_message"))
        self.assertEqual(sorted(records.mapped("move_id.ref")), ["B-1", "B-2"])

    # -- AC-06 / AC-07: the strict bank-account rule (core, T11b) ----------------

    def test_ac07_upload_iban_of_another_partner_is_not_attached_nor_suggested(self):
        owner = self.env["res.partner"].create(
            {"name": "Example Factoring Oy", "is_company": True}
        )
        foreign = self.env["res.partner.bank"].create(
            {"acc_number": "FI1612345600000002", "partner_id": owner.id}
        )
        record = self._records(self._upload(("10045.xml", _raw("fi30_simple_invoice.xml"))))
        self._perform(record)
        self.assertEqual(record.state, "imported", record.error_message or "")
        move = record.move_id
        self.assertEqual(move.partner_id, self.vendor)
        self.assertNotEqual(move.partner_bank_id, foreign)
        self.assertNotIn(
            foreign.sanitized_acc_number,
            self.vendor.with_context(active_test=False).bank_ids.mapped("sanitized_acc_number"),
        )
        self.assertEqual(foreign.partner_id, owner)
        suggestions = self.env["goodahead.vendor.data.suggestion"].search(
            [("move_id", "=", move.id), ("field_name", "=", "bank_account")]
        )
        self.assertFalse(suggestions, "a foreign IBAN is never offered")
        chatter = "\n".join(str(body) for body in move.message_ids.mapped("body"))
        self.assertIn("Example Factoring Oy", chatter)

    def test_ac06_upload_new_iban_is_a_sensitive_suggestion_never_created(self):
        record = self._records(self._upload(("10045.xml", _raw("fi30_simple_invoice.xml"))))
        self._perform(record)
        self.assertEqual(record.state, "imported", record.error_message or "")
        move = record.move_id
        self.assertFalse(
            self.env["res.partner.bank"].with_context(active_test=False).search(
                [("acc_number", "=", "FI1612345600000002")]
            ),
            "the decoder created no bank account",
        )
        self.assertFalse(move.partner_bank_id)
        suggestion = self.env["goodahead.vendor.data.suggestion"].search(
            [("move_id", "=", move.id), ("field_name", "=", "bank_account")]
        )
        self.assertEqual(len(suggestion), 1)
        self.assertTrue(suggestion.is_sensitive, "never pre-selected")

    def test_upload_unknown_finvoice_version_fails_with_the_version_reason(self):
        raw = _raw("fi13_invoice.xml").replace(b'Version="1.3"', b'Version="1.2"')
        record = self._records(self._upload(("old.xml", raw)))
        self.assertEqual(len(record), 1)
        self.assertEqual(record.state, "failed")
        self.assertEqual(record.failure_kind, "data")
        self.assertIn("1.2", record.error_message)
        self.assertIn("finvoice.unsupported_version", record.error_message)
        self.assertFalse(record.queue_job_id, "nothing to run")
        self.assertTrue(record.finvoice_attachment_id, "the file is kept")

    def test_upload_non_invoice_message_fails_in_the_job(self):
        raw = _raw("fi30_simple_invoice.xml").replace(b">INV01<", b">QUO01<")
        record = self._records(self._upload(("quote.xml", raw)))
        self._perform(record)
        self.assertEqual(record.state, "failed")
        self.assertIn("finvoice.not_an_invoice", record.error_message)
        self.assertIn("QUO01", record.error_message)
        self.assertFalse(record.move_id)

    # -- AC-08: a message that does not add up, or lacks a value ------------------

    def _import_raw(self, raw, name="10045.xml"):
        record = self._records(self._upload((name, raw)))
        self._perform(record)
        self.assertEqual(record.state, "imported", record.error_message or "")
        return record.move_id

    def _replace(self, raw, *pairs):
        for old, new in pairs:
            self.assertIn(old, raw)
            raw = raw.replace(old, new)
        return raw

    def test_finvoice_13_with_the_vat_rate_only_in_the_header_is_imported(self):
        """AC-33, AC-34: a 1.3 message, rows net of their discount, no VAT
        rate except what the header totals make (25.5 %)."""
        move = self._import_raw(_raw("fi13_invoice.xml"), "13-1.xml")
        self.assertEqual(move.state, "draft")
        self.assertEqual(move.partner_id, self.vendor)
        self.assertEqual(move.ref, "13-1")
        self.assertEqual(move.invoice_line_ids.mapped("price_subtotal"), [60.0, 15.0])
        self.assertEqual(set(move.invoice_line_ids.mapped("tax_ids.amount")), {25.5})
        self.assertAlmostEqual(move.amount_untaxed, 75.00, places=2)
        self.assertAlmostEqual(move.amount_tax, 19.13, places=2)
        self.assertAlmostEqual(move.amount_total, 94.13, places=2)
        self.assertFalse(move.ai_total_mismatch)

    def test_rows_that_do_not_make_the_stated_total_become_a_draft_with_the_banner(self):
        """AC-29: rows adding up to 350 under a stated
        VAT-exclusive total of 360, no charge to explain it. The draft bill
        carries the rows as they are, and the red banner compares its total
        with the stated one (the document is compared, not the rows with
        themselves)."""
        raw = self._replace(
            _raw("fi30_simple_invoice.xml"),
            (b'<RowVatExcludedAmount AmountCurrencyIdentifier="EUR">240,00',
             b'<RowVatExcludedAmount AmountCurrencyIdentifier="EUR">230,00'),
            (b'<UnitPriceAmount AmountCurrencyIdentifier="EUR">120,00',
             b'<UnitPriceAmount AmountCurrencyIdentifier="EUR">115,00'),
        )
        move = self._import_raw(raw)
        self.assertEqual(move.state, "draft")
        self.assertAlmostEqual(move.amount_untaxed, 350.00, places=2)
        self.assertEqual(len(move.invoice_line_ids), 2, "the rows only, nothing added")
        self.assertTrue(move.ai_total_mismatch)
        self.assertAlmostEqual(move.ai_document_total, 451.80, places=2)

    def test_subline_with_a_real_adjustment_is_a_line_of_the_bill(self):
        """AC-28: a credit for an earlier payment in a SubInvoiceRow, which
        the stated total includes."""
        move = self._import_raw(_raw("fi30_subrow_adjustment.xml"), "10046.xml")
        self.assertTrue(move.invoice_line_ids[2].name.startswith("Ennakkomaksu"))
        self.assertAlmostEqual(move.amount_untaxed, 300.00, places=2)
        self.assertAlmostEqual(move.amount_total, 376.50, places=2)
        self.assertFalse(move.ai_total_mismatch)

    def test_price_per_hundred_not_declared_is_imported_from_the_row_total(self):
        """AC-30: 1200 pieces at 20.00 (a price per 100 pieces, not declared)
        for a row total of 240.00: exactly 100x, which the misreading check
        refuses on a reading. The row total is stated, so the bill is built."""
        raw = self._replace(
            _raw("fi30_simple_invoice.xml"),
            (b'<DeliveredQuantity QuantityUnitCode="kpl">2<',
             b'<DeliveredQuantity QuantityUnitCode="kpl">1200<'),
            (b'<UnitPriceAmount AmountCurrencyIdentifier="EUR">120,00',
             b'<UnitPriceAmount AmountCurrencyIdentifier="EUR">20,00'),
        )
        move = self._import_raw(raw)
        self.assertAlmostEqual(move.amount_untaxed, 360.00, places=2)
        self.assertAlmostEqual(move.amount_total, 451.80, places=2)
        self.assertFalse(move.ai_total_mismatch)

    def test_row_total_survives_the_unit_price_precision(self):
        """AC-31: 7 hours for 120.00 is 17.142857 an hour; at two decimals
        7 x 17.14 is 119.98. The bill keeps the stated 120.00, and the line
        text keeps the quantity and price the sender wrote."""
        raw = self._replace(
            _raw("fi30_simple_invoice.xml"),
            (b'<DeliveredQuantity QuantityUnitCode="h">1,5<',
             b'<DeliveredQuantity QuantityUnitCode="h">7<'),
            (b'<UnitPriceAmount AmountCurrencyIdentifier="EUR">80,00',
             b'<UnitPriceAmount AmountCurrencyIdentifier="EUR">17,14'),
        )
        move = self._import_raw(raw)
        work = move.invoice_line_ids.filtered(lambda l: "Asennustyö" in (l.name or ""))
        self.assertAlmostEqual(work.price_subtotal, 120.00, places=2)
        self.assertIn("7", work.name)
        self.assertIn("17.14", work.name)
        self.assertAlmostEqual(move.amount_untaxed, 360.00, places=2)
        self.assertAlmostEqual(move.amount_total, 451.80, places=2)
        self.assertFalse(move.ai_total_mismatch)

    def _posted_bill(self, ref):
        bill = self.env["account.move"].create(
            {
                "move_type": "in_invoice",
                "partner_id": self.vendor.id,
                "journal_id": self.journal.id,
                "invoice_date": "2026-11-15",
                "ref": ref,
                "invoice_line_ids": [(0, 0, {"name": "Tuoli", "quantity": 1, "price_unit": 100.0})],
            }
        )
        bill.action_post()
        return bill

    def test_credit_note_is_linked_to_the_one_original_bill(self):
        """AC-32: OriginalInvoiceNumber names the vendor's posted bill."""
        original = self._posted_bill("10045")
        move = self._import_raw(_raw("fi30_credit_note_negative.xml"), "10050.xml")
        self.assertEqual(move.move_type, "in_refund")
        self.assertEqual(move.reversed_entry_id, original)

    def test_credit_note_is_not_linked_when_the_original_is_ambiguous(self):
        """AC-32: two posted bills with that number — no link."""
        self._posted_bill("10045")
        self._posted_bill("10045")
        move = self._import_raw(_raw("fi30_credit_note_negative.xml"), "10050.xml")
        self.assertEqual(move.move_type, "in_refund")
        self.assertFalse(move.reversed_entry_id)

    def test_missing_invoice_number_keeps_editable_data_and_reimports(self):
        raw = _raw("fi30_simple_invoice.xml").replace(
            b"<InvoiceNumber>10045</InvoiceNumber>", b"<InvoiceNumber></InvoiceNumber>"
        )
        record = self._records(self._upload(("10045.xml", raw)))
        self._perform(record)
        self.assertEqual(record.state, "failed")
        self.assertEqual(record.failure_kind, "data")
        self.assertIn("finvoice.missing_field", record.error_message)
        self.assertTrue(record.extraction_data, "the rest of the invoice is kept")
        data = json.loads(record.extraction_data)
        self.assertIsNone(data["invoice_number"])
        self.assertEqual(data["seller"]["name"], "Example Supplier Oy")
        self.assertEqual(record.seller_name, "Example Supplier Oy")

        # The Accountant fills in the number and re-imports: no reading again.
        data["invoice_number"] = "10045-B"
        record.extraction_data = json.dumps(data)
        record.action_reimport_from_json()
        self.assertEqual(record.state, "imported", record.error_message or "")
        self.assertEqual(record.move_id.ref, "10045-B")
        self.assertEqual(record.move_id.partner_id, self.vendor)
        self.assertAlmostEqual(record.move_id.amount_total, 451.80, places=2)

    def test_upload_pdf_without_ai_goes_to_the_native_importer(self):
        before = self.Import.search_count([])
        action = self._upload(("scan.pdf", TINY_PDF))
        self.assertEqual(action["res_model"], "account.move")
        self.assertEqual(self.Import.search_count([]), before, "no import record")

    def test_upload_into_a_sale_journal_is_untouched(self):
        sale = self.env["account.journal"].search(
            [("type", "=", "sale"), ("company_id", "=", self.company.id)], limit=1
        )
        if not sale:
            self.skipTest("no sale journal")
        attachment = self.env["ir.attachment"].create(
            {"name": "10045.xml", "raw": _raw("fi30_simple_invoice.xml")}
        )
        before = self.Import.search_count([])
        try:
            sale.with_context(default_move_type="out_invoice").create_document_from_attachment(
                attachment.ids
            )
        except Exception:  # noqa: BLE001 - the native importer's business
            pass
        self.assertEqual(self.Import.search_count([]), before)

    # -- mail ----------------------------------------------

    def test_mail_with_finvoice_becomes_an_email_record(self):
        raw = _raw("fi30_simple_invoice.xml")
        records = self._mail([self._file_part("10045.xml", raw, "text", "xml")])
        self.assertEqual(len(records), 1)
        record = records
        self.assertEqual(record.source, "email")
        self.assertIn("laskutus@example.test", record.source_email)
        self.assertTrue(looks_like_finvoice(record.finvoice_attachment_id.raw))
        self._perform(record)
        self.assertEqual(record.state, "imported", record.error_message or "")
        self.assertEqual(record.move_id.partner_id, self.vendor)
        self.assertIn("Imported from Finvoice (email)", self._chatter(record.move_id))
        self.assertTrue(record.move_id.message_main_attachment_id.name.endswith(".xml"))

    def test_mail_with_finvoice_and_its_image_attaches_both_pdf_first(self):
        raw = _raw("fi30_simple_invoice.xml")
        records = self._mail(
            [
                self._file_part("10045.xml", raw, "application", "xml"),
                self._file_part("10045.pdf", TINY_PDF, "application", "pdf"),
                self._file_part("delivery-note.txt", b"Rahtikirja 1", "text", "plain"),
            ],
            subject="Lasku 10045 kuvalla",
        )
        self.assertEqual(len(records), 1, "the image is not a second invoice")
        record = records
        self.assertEqual(record.pdf_attachment_id.name, "10045.pdf")
        self.assertEqual(record.finvoice_extra_attachment_ids.mapped("name"), ["delivery-note.txt"])
        self._perform(record)
        self.assertEqual(record.state, "imported", record.error_message or "")
        move = record.move_id
        self.assertEqual(move.message_main_attachment_id.name, "10045.pdf")
        names = self.env["ir.attachment"].search(
            [("res_model", "=", "account.move"), ("res_id", "=", move.id)]
        ).mapped("name")
        self.assertIn("10045.xml", names, "the Finvoice message is on the bill too")
        self.assertIn("delivery-note.txt", names)

    def test_mail_without_finvoice_and_without_ai_fails_unsupported(self):
        # Runs in the full gate too: the AI reader, when installed, is taken
        # out of the registry, which is the database AC-21 talks about.
        Model = type(self.Import)
        installed = Model._import_readers

        def readers_without_ai(model):
            return {key: spec for key, spec in installed(model).items() if key != "ai"}

        with patch.object(Model, "_import_readers", readers_without_ai):
            records = self._mail(
                [self._file_part("scan.pdf", TINY_PDF, "application", "pdf")],
                subject="Pelkka PDF",
            )
        self.assertEqual(len(records), 1)
        self.assertEqual(records.source, "email")
        self.assertEqual(records.state, "failed")
        self.assertIn("Format not supported", records.error_message)
        self.assertIn("scan.pdf", records.error_message)
        self.assertFalse(records.queue_job_id)
