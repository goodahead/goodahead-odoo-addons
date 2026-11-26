# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The core's source dispatch with the Finvoice reader.

Mail attachments and import jobs reach a reader through the core, in the
readers' priority order and on the record's stored ``reader`` — never through
whichever add-on happened to load last. Runs with and without the AI add-on;
the AI half of an assertion is made only when it is installed. Invented data
only (``@example.test``, ``ZZ`` vendors, Business IDs 900000N-N).
"""

import json
import os
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.goodahead_invoice_import.lib.import_source import (
    DEFAULT_POLICY,
    STRICT_POLICY,
)
from odoo.addons.goodahead_invoice_import.tests.common import ensure_purchase_journal

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

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
class TestSourceDispatch(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.company = cls.env.company
        cls.Import = cls.env["goodahead.invoice.import"]
        cls.ai = "ai" in cls.Import._import_readers()
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
                    "name": "ZZ T9b 25.5% Purchase",
                    "type_tax_use": "purchase",
                    "amount_type": "percent",
                    "amount": 25.5,
                    "company_id": cls.company.id,
                }
            )

    # -- the registry ------------------------------------------------------------

    def test_readers_are_asked_in_priority_order(self):
        order = self.Import._import_sources_in_order()
        # Other installed sources (e.g. APIX) may come first; what matters is
        # that the order follows the priorities and Finvoice precedes the AI.
        priorities = [self.Import._import_source_priority(key) for key in order]
        self.assertEqual(priorities, sorted(priorities))
        self.assertIn("finvoice", order)
        self.assertEqual(self.Import._import_source_priority("finvoice"), 10)
        self.assertIsNone(self.Import._import_source_priority("zz-nothing"))
        if self.ai:
            self.assertLess(order.index("finvoice"), order.index("ai"))
            self.assertLess(
                self.Import._import_source_priority("finvoice"),
                self.Import._import_source_priority("ai"),
            )
        readers = self.Import._import_readers()
        self.assertIs(readers["finvoice"].policy, STRICT_POLICY)
        self.assertTrue(readers["finvoice"].structured)
        if self.ai:
            self.assertIs(readers["ai"].policy, DEFAULT_POLICY)
            self.assertFalse(readers["ai"].structured)

    # -- mail: the core routes, whatever the load order -------------------------

    def _mail_files(self):
        invoice = _raw("fi30_simple_invoice.xml")
        return [
            ("a.xml", invoice, "application/xml"),
            ("b.xml", invoice.replace(b">10045<", b">10046<"), "application/xml"),
            ("a.pdf", TINY_PDF, "application/pdf"),
            ("scan.pdf", TINY_PDF, "application/pdf"),
            ("notes.txt", b"ZZ notes", "text/plain"),
        ]

    def _names(self, files):
        return [name for name, _c, _m in files]

    def _assert_routes(self, routes, unaccepted):
        self.assertEqual(list(routes)[0], "finvoice")
        self.assertEqual(self._names(routes["finvoice"]["files"]), ["a.xml", "b.xml"])
        self.assertEqual(
            self._names(routes["finvoice"]["companions"]),
            ["a.pdf"],
            "the image goes with its Finvoice file, not to the AI",
        )
        if self.ai:
            self.assertEqual(self._names(routes["ai"]["files"]), ["scan.pdf"])
            self.assertEqual(self._names(unaccepted), ["notes.txt"])
        else:
            self.assertNotIn("ai", routes)
            self.assertEqual(self._names(unaccepted), ["scan.pdf", "notes.txt"])

    def test_mail_routing_goes_by_priority_not_by_load_order(self):
        files = self._mail_files()
        self._assert_routes(*self.Import._mail_route_files(files))

        # The registry handed back in the reverse order — what a different
        # load order of the add-ons would give — routes exactly the same.
        readers = self.Import._import_readers()
        reversed_readers = dict(reversed(list(readers.items())))
        with patch.object(
            type(self.Import), "_import_readers", lambda self: dict(reversed_readers)
        ):
            self.assertEqual(list(self.Import._import_readers()), list(reversed_readers))
            self._assert_routes(*self.Import._mail_route_files(files))

    def test_a_single_finvoice_file_takes_every_other_file(self):
        files = [
            ("10045.xml", _raw("fi30_simple_invoice.xml"), "application/xml"),
            ("scan.pdf", TINY_PDF, "application/pdf"),
        ]
        routes, unaccepted = self.Import._mail_route_files(files)
        self.assertEqual(list(routes), ["finvoice"])
        self.assertEqual(self._names(routes["finvoice"]["companions"]), ["scan.pdf"])
        self.assertFalse(unaccepted)

    def test_a_pdf_alone_goes_to_the_ai_or_nowhere(self):
        routes, unaccepted = self.Import._mail_route_files(
            [("scan.pdf", TINY_PDF, "application/pdf")]
        )
        if self.ai:
            self.assertEqual(list(routes), ["ai"])
            self.assertFalse(unaccepted)
        else:
            self.assertFalse(routes)
            self.assertEqual(self._names(unaccepted), ["scan.pdf"])

    def test_mail_with_finvoice_and_an_unpaired_pdf(self):
        """Two Finvoice invoices, the image of one, an unrelated PDF and a
        note: two Finvoice records; the unrelated PDF read by the AI when it
        is installed; what nobody reads is named on the first record."""
        msg = {
            "email_from": "laskutus@example.test",
            "subject": "ZZ T9b mixed",
            "attachments": [(name, content) for name, content, _m in self._mail_files()],
        }
        before = self.Import.search([]).ids
        with patch.object(type(self.Import), "_enqueue_import", return_value=None):
            first = self.Import.message_new(msg, {"company_id": self.company.id})
        records = self.Import.search([("id", "not in", before)], order="id")
        finvoice_records = records.filtered(lambda r: r.reader == "finvoice")
        self.assertEqual(finvoice_records.mapped("name"), ["a.xml", "b.xml"])
        self.assertEqual(first, finvoice_records[:1], "the mail is on the first reader's record")
        self.assertEqual(finvoice_records[0].pdf_attachment_id.name, "a.pdf")
        self.assertFalse(finvoice_records[1].pdf_attachment_id)
        chatter = " ".join(first.message_ids.mapped("body"))
        self.assertIn("as set by the alias", chatter)
        self.assertIn("notes.txt", chatter)
        ai_records = records.filtered(lambda r: r.reader == "ai")
        if self.ai:
            self.assertEqual(ai_records.mapped("name"), ["scan.pdf"])
            self.assertEqual(ai_records.pdf_attachment_id.name, "scan.pdf")
        else:
            self.assertFalse(ai_records)
            self.assertIn("scan.pdf", chatter)
        self.assertEqual(len(records), 3 if self.ai else 2)

    def test_mail_nobody_reads_fails_unsupported_and_keeps_the_files(self):
        msg = {
            "email_from": "laskutus@example.test",
            "subject": "ZZ T9b nothing",
            "attachments": [("notes.txt", b"ZZ notes")],
        }
        record = self.Import.message_new(msg)
        self.assertEqual(record.state, "failed")
        self.assertIn("Format not supported", record.error_message)
        self.assertIn("notes.txt", record.error_message)
        self.assertIn("Finvoice", record.error_message, "names what can be read")
        self.assertFalse(record.reader)
        self.assertTrue(
            self.env["ir.attachment"].search(
                [("res_model", "=", record._name), ("res_id", "=", record.id),
                 ("name", "=", "notes.txt")]
            ),
            "the files are kept",
        )

    def test_inline_parts_are_not_attachments(self):
        files = self.Import._mail_intake_files(
            {"attachments": [("logo.png", b"\x89PNG", {"cid": "logo@zz"}), ("x.pdf", TINY_PDF)]}
        )
        self.assertEqual(self._names(files), ["x.pdf"])

    # -- the job: dispatched on the stored reader --------------------------------

    def _finvoice_record(self):
        return self.Import._finvoice_receive(
            "upload", self.company, "10045.xml", _raw("fi30_simple_invoice.xml"),
            values={"journal_id": self.journal.id},
        )

    def test_the_job_goes_to_the_records_reader(self):
        Model = type(self.Import)
        with patch.object(Model, "_enqueue_import", return_value=None):
            fin = self._finvoice_record()
        self.assertEqual(fin.reader, "finvoice")
        with patch.object(Model, "_finvoice_import", autospec=True, return_value="ZZ finvoice") as read:
            self.assertEqual(fin._process_import(), "ZZ finvoice")
        read.assert_called_once()

        if not self.ai:
            return
        pdf = self.env["ir.attachment"].create(
            {"name": "zz.pdf", "raw": TINY_PDF, "mimetype": "application/pdf"}
        )
        ai_record = self.Import.create(
            {"name": "zz.pdf", "company_id": self.company.id, "pdf_attachment_id": pdf.id,
             "reader": "ai"}
        )
        with patch.object(Model, "_ai_read_and_finalize", autospec=True, return_value="ZZ ai") as read_ai, \
                patch.object(Model, "_finvoice_import", autospec=True) as read_fin:
            self.assertEqual(ai_record._process_import(), "ZZ ai")
        read_ai.assert_called_once()
        read_fin.assert_not_called()

    def test_a_record_without_a_reader_is_claimed_in_priority_order(self):
        """A T9 record from before readers were stored: its Finvoice message
        claims it for Finvoice, even though it also has a PDF the AI would
        claim; the claim is written on the record."""
        Model = type(self.Import)
        with patch.object(Model, "_enqueue_import", return_value=None):
            record = self._finvoice_record()
        pdf = self.env["ir.attachment"].create(
            {"name": "10045.pdf", "raw": TINY_PDF, "mimetype": "application/pdf"}
        )
        record.write({"reader": False, "pdf_attachment_id": pdf.id})
        self.assertEqual(record._import_reader(), "finvoice")
        with patch.object(Model, "_finvoice_import", autospec=True, return_value="ZZ finvoice"):
            record._process_import()
        self.assertEqual(record.reader, "finvoice")

        orphan = self.Import.create({"name": "zz-orphan.xml", "company_id": self.company.id})
        self.assertEqual(orphan._import_reader(), "ai" if self.ai else False)

    # -- re-import: the reader's policy and files --------------------------------

    def test_reimport_of_a_finvoice_record_uses_the_strict_policy(self):
        """A weak (name) match is proposed, not set, on a re-import from the
        stored data — the policy is the reader's, not the AI's default."""
        by_name = self.env["res.partner"].create(
            {"name": "ZZ Name Only Oy", "is_company": True,
             "country_id": self.env.ref("base.fi").id}
        )
        with patch.object(type(self.Import), "_enqueue_import", return_value=None):
            record = self._finvoice_record()
        self.assertIs(record._import_reader_policy(), STRICT_POLICY)
        data = {
            "type": "Invoice",
            "invoice_number": "ZZ-T9B-1",
            "issue_date": "2026-11-02",
            "due_date": "2026-11-30",
            "currency": "EUR",
            "seller": {
                "name": "ZZ Name Only Oy",
                "vat": "FI90000077",
                "business_id": "9000007-7",
                "country_code": "FI",
            },
            "buyer": {"name": self.company.name, "country_code": "FI"},
            "lines": [
                {"name": "ZZ service", "quantity": 1.0, "price_unit": 100.0,
                 "line_total": 100.0, "tax_percent": 25.5, "tax_amount": 25.5,
                 "tax_category": "S"}
            ],
            "tax_subtotals": [
                {"tax_percent": 25.5, "taxable_amount": 100.0, "tax_amount": 25.5,
                 "tax_category": "S"}
            ],
            "totals": {"line_extension": 100.0, "tax_exclusive": 100.0,
                       "tax_inclusive": 125.5, "payable": 125.5},
            "source_meta": {"format": "finvoice"},
        }
        record.write(
            {"state": "failed", "error_message": "ZZ fixed by hand",
             "extraction_data": json.dumps(data)}
        )
        record.action_reimport_from_json()
        self.assertEqual(record.state, "imported", record.error_message or "")
        move = record.move_id
        self.assertFalse(move.partner_id, "a name match never sets the vendor of a Finvoice bill")
        self.assertIn(by_name, move.ai_vendor_candidate_ids.partner_id, "it is proposed")
        # The Finvoice message is on the re-imported bill too, as its main
        # document (no image came with it), and no AI usage is recorded.
        self.assertEqual(move.message_main_attachment_id.name, "10045.xml")
        for name in ("extraction_model", "input_tokens", "output_tokens"):
            if name in record._fields:
                self.assertFalse(record[name], name)
