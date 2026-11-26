# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The community sending adapter.

Runs only where the community modules are installed (this add-on depends on
them). No network: the community backend's ``SendInvoiceZIP`` is patched.
Invented data only.
"""

from datetime import date
from unittest.mock import patch

from lxml import etree

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from ..models.apix_connection import COMMUNITY, READ_KEYS

COMMUNITY_FINVOICE_LOGGER = "odoo.addons.account_edi_finvoice.models.account_edi_format"
CONNECTION = "goodahead.apix.connection"
ATTEMPT = "goodahead.invoice.send.attempt"
FINVOICE = "finvoice_3_0"


def community_answer():
    """A SendInvoiceZIP answer as the community connector parses it."""
    root = etree.Element("Response")
    group = etree.SubElement(etree.SubElement(root, "Content"), "Group")
    for kind, value in (("BatchID", "ZZBATCH-C1"), ("AcceptedDocumentID", "ZZDOC-C1"),
                        ("CostInCredits", "1")):
        etree.SubElement(group, "Value", type=kind).text = value
    return root


@tagged("post_install", "-at_install")
class TestCommunitySendingAdapter(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.company_data["company"]
        cls.company_b = cls.setup_other_company(name="ZZ Community B Oy")["company"]
        cls.Connection = cls.env[CONNECTION].sudo()
        cls.backend = cls._backend(cls.company, "ZZ-COMM-A")
        cls.backend_b = cls._backend(cls.company_b, "ZZ-COMM-B")
        Method = cls.env["transmit.method"]
        cls.method_einvoice = Method.search([("code", "=", "einvoice")], limit=1)
        cls.method_print = Method.search([("code", "=", "printing_service")], limit=1)
        cls.method_mail = cls.env.ref("account_invoice_transmit_method.mail")
        cls.method_post = cls.env.ref("account_invoice_transmit_method.post")
        cls.operator = cls.env["res.partner.operator.einvoice"].create(
            {"name": "ZZ Operator", "identifier": "ZZOPER"}
        )
        Partner = cls.env["res.partner"]
        cls.customer = Partner.create({
            "name": "ZZ Community Customer Oy", "edicode": "003700000019",
            "einvoice_operator_id": cls.operator.id,
        })
        cls.customer.with_company(cls.company).customer_invoice_transmit_method_id = (
            cls.method_einvoice
        )
        cls.letter_customer = Partner.create({"name": "ZZ Letter Customer Oy"})
        cls.letter_customer.with_company(cls.company).customer_invoice_transmit_method_id = (
            cls.method_print
        )
        cls.post_customer = Partner.create({"name": "ZZ Post Customer Oy"})
        cls.post_customer.with_company(cls.company).customer_invoice_transmit_method_id = (
            cls.method_post
        )
        cls.other_company_customer = Partner.create({
            "name": "ZZ Other Company Customer Oy", "company_id": cls.company_b.id,
            "edicode": "003700000027", "einvoice_operator_id": cls.operator.id,
        })

    @classmethod
    def _backend(cls, company, transfer_id):
        return cls.env["apix.backend"].sudo().create({
            "name": company.name,
            "company_id": company.id,
            "username": f"{transfer_id}@example.test",
            "password": "ZZ-web-password",
            "environment": "test",
            "transfer_id": transfer_id,
            "transfer_key": f"secret-{transfer_id}",
        })

    def _invoice(self, partner, company=None, method=None):
        # The community modules check their own posting-time Finvoice against
        # the schema and log what the invented test data lacks; not ours.
        with mute_logger(COMMUNITY_FINVOICE_LOGGER):
            move = self.init_invoice(
                "out_invoice", partner=partner, amounts=[100.0],
                company=company or self.company, post=True,
            )
        if method:
            move.transmit_method_id = method
        return move

    def _bind(self, move, backend=None, batch="ZZBATCH-1", document="ZZDOC-1", sent=None):
        move.date_einvoice_sent = sent or date(2026, 11, 20)
        return self.env["apix.account.invoice"].sudo().create({
            "backend_id": (backend or self.backend).id,
            "odoo_id": move.id,
            "apix_batch_id": batch,
            "apix_accepted_document_id": document,
            "apix_cost_in_credits": 1.5,
        })

    def _status(self, company=None):
        return self.Connection._migration_community_sending_status(company or self.company)

    # -- the registry and the status ------------------------------------------------

    def test_community_is_a_sending_source(self):
        self.assertIn(COMMUNITY, self.Connection._migration_sending_sources())
        self.assertEqual(self.Connection._migration_sending_incomplete(COMMUNITY), [])

    def test_status_sending_until_stopped(self):
        state, reason = self._status()
        self.assertEqual(state, "sending")
        self.assertTrue(reason)
        senders = self.Connection._apix_earlier_senders(self.company)
        self.assertEqual([sender["code"] for sender in senders], [COMMUNITY])
        self.Connection._migration_community_sending_stop(self.company)
        self.assertEqual(self._status()[0], "stopped")
        self.assertEqual(self.Connection._apix_earlier_senders(self.company), [])
        # another company is never touched
        self.assertEqual(self._status(self.company_b)[0], "sending")
        self.Connection._migration_community_sending_resume(self.company)
        self.assertEqual(self._status()[0], "sending")

    def test_status_without_a_backend(self):
        company = self.env["res.company"].create({"name": "ZZ No Backend Oy"})
        self.assertEqual(self._status(company)[0], "none")
        # without a backend the community modules cannot send; their own data
        # of the company can still be carried over
        self.customer.with_company(company).customer_invoice_transmit_method_id = (
            self.method_einvoice
        )
        state, reason = self._status(company)
        self.assertEqual(state, "stopped")
        self.assertIn("no backend", reason)

    # -- read -------------------------------------------------------------------------

    def test_read_returns_plain_values_of_the_company(self):
        sent = self._invoice(self.customer, method=self.method_einvoice)
        self._bind(sent)
        letter = self._invoice(self.letter_customer, method=self.method_print)
        self._bind(letter, batch=False, document=False, sent=date(2026, 11, 21))
        mailed = self._invoice(self.customer, method=self.method_mail)
        other = self._invoice(self.other_company_customer, company=self.company_b,
                              method=self.method_einvoice)
        self._bind(other, backend=self.backend_b)
        values = self.Connection._migration_community_sending_read(self.company)
        self.assertEqual(set(values), set(READ_KEYS))
        recipients = {row["partner_id"]: row for row in values["recipients"]}
        self.assertEqual(recipients[self.customer.id]["edicode"], "003700000019")
        self.assertEqual(recipients[self.customer.id]["operator_identifier"], "ZZOPER")
        self.assertNotIn(self.other_company_customer.id, recipients)
        channels = {row["partner_id"]: row for row in values["customer_channels"]}
        self.assertEqual(channels[self.customer.id]["channel"], "einvoice")
        self.assertEqual(channels[self.customer.id]["company_id"], self.company.id)
        self.assertEqual(channels[self.letter_customer.id]["channel"], "letter")
        self.assertNotIn(self.post_customer.id, channels, "no APIX channel for post")
        methods = {row["move_id"]: row["method"] for row in values["invoice_methods"]}
        self.assertEqual(methods[sent.id], "einvoice")
        self.assertEqual(methods[letter.id], "letter")
        self.assertEqual(methods[mailed.id], "email")
        self.assertNotIn(other.id, methods)
        rows = {row["move_id"]: row for row in values["sent_invoices"]}
        self.assertEqual(set(rows), {sent.id, letter.id})
        self.assertEqual(rows[sent.id]["batch_id"], "ZZBATCH-1")
        self.assertEqual(rows[sent.id]["document_id"], "ZZDOC-1")
        self.assertEqual(rows[sent.id]["credits"], 1.5)
        self.assertEqual(rows[sent.id]["channel"], "einvoice")
        self.assertTrue(str(rows[sent.id]["date"]).startswith("2026-11-20"))
        # a binding without a batch id: batch empty, date kept
        self.assertFalse(rows[letter.id]["batch_id"])
        self.assertEqual(rows[letter.id]["channel"], "letter")
        self.assertTrue(str(rows[letter.id]["date"]).startswith("2026-11-21"))

    def test_read_writes_nothing(self):
        self._bind(self._invoice(self.customer, method=self.method_einvoice))
        self.env.flush_all()
        with patch.object(type(self.env["res.partner"]), "write",
                          side_effect=AssertionError("partner write")), \
                patch.object(type(self.env["account.move"]), "write",
                             side_effect=AssertionError("move write")):
            self.Connection._migration_community_sending_read(self.company)
        self.assertFalse(self.env[ATTEMPT].search([]))

    # -- stop and resume --------------------------------------------------------------------

    def test_stop_is_idempotent_and_cancels_queued_community_sends(self):
        move = self._invoice(self.customer, method=self.method_einvoice)
        move_b = self._invoice(self.other_company_customer, company=self.company_b,
                               method=self.method_einvoice)
        job = move.with_delay().einvoice_send().db_record()
        job_b = move_b.with_delay().einvoice_send().db_record()
        self.Connection._migration_community_sending_stop(self.company)
        self.Connection._migration_community_sending_stop(self.company)
        self.assertEqual(job.state, "cancelled")
        self.assertNotEqual(job_b.state, "cancelled")
        self.assertTrue(self.company.goodahead_community_sending_stopped)
        self.assertFalse(self.company_b.goodahead_community_sending_stopped)

    def test_stopped_company_sends_nothing_through_the_community_modules(self):
        move = self._invoice(self.customer, method=self.method_einvoice)
        self.Connection._migration_community_sending_stop(self.company)
        with self.assertRaises(UserError):
            move.action_einvoice_send()
        with patch.object(type(self.env["apix.backend"]), "SendInvoiceZIP",
                          side_effect=AssertionError("sent")):
            move.einvoice_send()  # a job queued before the stop: skipped
        self.assertFalse(move.apix_bind_ids)

    def test_another_company_still_sends_through_the_community_modules(self):
        self.Connection._migration_community_sending_stop(self.company)
        move_b = self._invoice(self.other_company_customer, company=self.company_b,
                               method=self.method_einvoice)
        with patch.object(type(move_b), "get_apix_payload", return_value=b"ZZ"), \
                patch.object(type(self.env["apix.backend"]), "SendInvoiceZIP",
                             return_value=community_answer()) as send:
            move_b.einvoice_send()
        send.assert_called_once()
        self.assertEqual(move_b.apix_bind_ids.apix_batch_id, "ZZBATCH-C1")

    def test_posting_creates_no_community_einvoice_document_after_stop(self):
        finvoice = self.env["account.edi.format"].search([("code", "=", FINVOICE)])
        for company in (self.company, self.company_b):
            journal = self.env["account.journal"].search(
                [("company_id", "=", company.id), ("type", "=", "sale")]
            )
            journal.edi_format_ids = [(4, finvoice.id)]
        before = self._invoice(self.customer)
        self.assertIn(FINVOICE, before.edi_document_ids.edi_format_id.mapped("code"))
        self.Connection._migration_community_sending_stop(self.company)
        after = self._invoice(self.customer)
        self.assertNotIn(FINVOICE, after.edi_document_ids.edi_format_id.mapped("code"))
        other = self._invoice(self.other_company_customer, company=self.company_b)
        self.assertIn(FINVOICE, other.edi_document_ids.edi_format_id.mapped("code"))
        self.Connection._migration_community_sending_resume(self.company)
        again = self._invoice(self.customer)
        self.assertIn(FINVOICE, again.edi_document_ids.edi_format_id.mapped("code"))

    # -- the carry-over through the bridge -----------------------------------------------

    def test_carry_over_through_the_migration_tool(self):
        move = self._invoice(self.customer, method=self.method_einvoice)
        self._bind(move)
        result = self.Connection._migration_sending_carry_over(COMMUNITY, self.company)
        self.assertEqual(result["sent_invoices"], 1)
        self.assertEqual(move.transmit_method, "einvoice")
        self.assertEqual(
            self.customer.with_company(self.company).invoice_sending_method, "apix"
        )
        self.assertEqual(
            self.letter_customer.with_company(self.company).invoice_sending_method, "apix_letter"
        )
        attempt = self.env[ATTEMPT].search([("move_id", "=", move.id)])
        self.assertEqual(attempt.origin, "migration")
        self.assertEqual(attempt.migration_source, COMMUNITY)
        self.assertEqual(attempt.apix_batch_id, "ZZBATCH-1")
        self.assertTrue(attempt.counts_as_sent)
        self.assertEqual(self._status()[0], "stopped")
        self.assertEqual(self.Connection._apix_earlier_senders(self.company), [])
        # sending it again asks for confirmation
        move.invalidate_recordset()
        self.assertTrue(move._apix_already_sent_info())
