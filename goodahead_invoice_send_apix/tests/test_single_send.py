# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Sending one invoice synchronously (send T27; AC-01 – AC-06, AC-52, AC-56;
contract §1.1 – §1.3, §10.4; sad.md Flow A).

One test per outcome row of contract §10.4, with ``FakeApixSendClient`` in
place of SendInvoiceZIP: no network. The ledger's own-transaction behaviour
(an outcome survives the caller's rollback) is proven on real connections in
``test_ledger``; here the send is shown never to commit the caller's
transaction. Invented data only.
"""

import io
import unittest
import zipfile
from unittest.mock import patch

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo.tests import tagged  # noqa: E402

from ..models.account_move_send import (  # noqa: E402
    CHANNEL_UNSUPPORTED,
    CREDENTIALS_REFUSED,
    NO_ANSWER,
    NO_CONTRACT,
    NO_CREDIT,
    REFUSED,
    SCHEMA_INVALID,
    UNEXPECTED,
    UNREACHABLE,
)
from ..models.account_move import NOT_POSTED, SEND_IN_PROGRESS  # noqa: E402
from .common import BATCH_ID, DOCUMENT_ID, FakeApixSendClient, SendApixCommon  # noqa: E402

ATTEMPT = "goodahead.invoice.send.attempt"


def _zip_names(package):
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


@tagged("post_install", "-at_install")
class TestSingleSend(SendApixCommon):
    def setUp(self):
        super().setUp()
        self.connection = self.env["goodahead.apix.connection"]._apix_for_company(self.company)
        self.connection_b = self.env["goodahead.apix.connection"]._apix_for_company(self.company_b)

    def _send(self, move, **kwargs):
        move = move.with_user(self.accountant).with_context(
            allowed_company_ids=[self.company.id, self.company_b.id]
        )
        return move._apix_send_now("button", **kwargs)

    def _attempts(self, move):
        return self.env[ATTEMPT].search([("move_id", "=", move.id)], order="id")

    def _reset_connection(self):
        self.connection.sudo().write({
            "status": "working", "send_paused_until": False, "status_message": False,
        })

    # -- accepted ----------------------------------------------------------

    def test_accepted(self):
        fake = FakeApixSendClient("accepted").patch(self)
        move = self._make_invoice()
        result = self._send(move)
        self.assertEqual(result.outcome, "accepted", result.message)
        self.assertEqual(result.batch_id, BATCH_ID)
        self.assertEqual(result.document_id, DOCUMENT_ID)
        self.assertEqual(result.credits, 1)
        self.assertEqual(result.warnings, [])
        attempt = self._row(result)
        self.assertEqual(attempt, self._attempts(move))
        self.assertEqual(attempt.state, "accepted")
        self.assertTrue(attempt.counts_as_sent)
        self.assertEqual(attempt.channel, "einvoice")
        self.assertEqual(attempt.origin, "button")
        self.assertEqual(attempt.requested_by_id, self.accountant)
        self.assertEqual(attempt.apix_batch_id, BATCH_ID)
        self.assertEqual(attempt.apix_document_id, DOCUMENT_ID)
        self.assertEqual(attempt.apix_credits, 1)
        self.assertEqual(attempt.schema_result, "valid")
        self.assertTrue(attempt.end_date)
        # one call, with this company's credentials and the package
        self.assertEqual(len(fake.calls), 1)
        call = fake.calls[0]
        self.assertEqual(call["transfer_id"], "ZZ-TRANSFER-003700000011")
        self.assertEqual((call["soft"], call["ver"]), ("Standard", "1.0"))
        files = _zip_names(call["zip"])
        xml_name = next(name for name in files if name.endswith(".xml"))
        self.assertIn(b"<SellerOrganisationName>ZZ Sender Oy<", files[xml_name])
        self.assertIn(f"<MessageIdentifier>{move.id}-{attempt.id}<".encode(), files[xml_name])
        self.assertEqual(files["invoice.pdf"], move._apix_render_pdf()[1])
        # the developer view keeps the message, the package and the masked exchange
        attempt_dev = attempt.sudo()
        self.assertTrue(attempt_dev.message_attachment_id.raw.startswith(b"<?xml"))
        self.assertEqual(attempt_dev.package_attachment_id.raw, call["zip"])
        self.assertNotIn("ZZ-TRANSFER-003700000011", attempt_dev.exchange_log)
        self.assertNotIn("zz-key", attempt_dev.exchange_log)
        # the connection learns the credit balance and stays working
        self.assertEqual(self.connection.credit_balance, 99)
        self.assertEqual(self.connection.status, "working")
        # it is sent now: a second send asks for confirmation
        again = self._send(move)
        self.assertEqual(again.outcome, "not_sent")
        self.assertEqual(again.code, "send.already_sent")

    def test_accepted_with_a_warning(self):
        FakeApixSendClient("warning", text="Buyer VAT code is missing").patch(self)
        result = self._send(self._make_invoice())
        self.assertEqual(result.outcome, "accepted")
        self.assertEqual(result.warnings, ["Buyer VAT code is missing"])
        self.assertEqual(self._row(result).state, "accepted")
        self.assertIn("Buyer VAT code is missing", self._row(result).warning_text)
        self.assertFalse(self._row(result).needs_attention)

    # -- refused --------------------------------------------------------------------

    def test_refused_document(self):
        FakeApixSendClient("refused", text="Receiver address unknown").patch(self)
        move = self._make_invoice()
        result = self._send(move)
        self.assertEqual(result.outcome, "refused")
        self.assertEqual(result.code, REFUSED)
        self.assertIn("Receiver address unknown", result.message)
        attempt = self._row(result)
        self.assertEqual(attempt.state, "refused")
        self.assertTrue(attempt.reason.startswith(f"[{REFUSED}]"))
        self.assertTrue(attempt.needs_attention)
        self.assertFalse(attempt.counts_as_sent)
        # can be sent again once fixed — no block, no confirmation
        FakeApixSendClient("accepted").patch(self)
        self.assertEqual(self._send(move).outcome, "accepted")

    def test_ok_answer_without_an_accepted_document_is_not_sent(self):
        """Review R2: an OK answer that accepts no document is no proof the
        invoice arrived — failed, may already be at APIX, never "sent"."""
        FakeApixSendClient("fixture", fixture="send_ok_no_document.xml").patch(self)
        move = self._make_invoice()
        result = self._send(move)
        self.assertEqual((result.outcome, result.code), ("failed", UNEXPECTED))
        self.assertIn("may already have reached APIX", result.message)
        attempt = self._row(result)
        self.assertEqual(attempt.state, "failed")
        self.assertFalse(attempt.counts_as_sent)
        self.assertTrue(attempt.needs_attention)
        self.assertTrue(attempt.reason.startswith(f"[{UNEXPECTED}]"))
        move.invalidate_recordset()
        self.assertFalse(move.already_sent)

    # -- ERR status endings ------------------------------------------

    def test_err_status_endings(self):
        rows = [
            ("9004", CREDENTIALS_REFUSED, "rejected", True),
            ("9003", NO_CONTRACT, "rejected", True),
            ("9002", NO_CREDIT, "working", True),
            ("9011", NO_CREDIT, "working", True),
            ("9001", REFUSED, "working", False),
            ("9012", REFUSED, "working", False),
            ("4100", REFUSED, "working", False),
            ("9999", UNEXPECTED, "working", False),
        ]
        for status_code, code, status, paused in rows:
            with self.subTest(status_code=status_code):
                self._reset_connection()
                FakeApixSendClient("err", status_code=status_code).patch(self)
                result = self._send(self._make_invoice())
                self.assertEqual(result.code, code)
                self.assertEqual(result.outcome, "refused" if code == REFUSED else "failed")
                self.assertTrue(self._row(result).reason.startswith(f"[{code}]"))
                self.assertIn(status_code, self._row(result).reason)
                self.assertEqual(self._row(result).apix_status_code, status_code)
                self.connection.invalidate_recordset()
                self.assertEqual(self.connection.status, status)
                self.assertEqual(self.connection._apix_send_circuit_open(), paused)

    # -- transport --------------------------------------------------------------

    def test_unreachable(self):
        FakeApixSendClient("unreachable").patch(self)
        result = self._send(self._make_invoice())
        self.assertEqual((result.outcome, result.code), ("failed", UNREACHABLE))
        self.assertNotIn("may already", self._row(result).reason)
        self.assertEqual(self.connection.status, "working")
        self.assertTrue(self.connection._apix_send_circuit_open())

    def test_no_answer_may_have_reached_apix_and_resend_is_free(self):
        fake = FakeApixSendClient("no_answer").patch(self)
        move = self._make_invoice()
        result = self._send(move)
        self.assertEqual((result.outcome, result.code), ("failed", NO_ANSWER))
        self.assertIn("may already have reached APIX", result.message)
        self.assertIn("may already have reached APIX", self._row(result).reason)
        self.assertFalse(self.connection._apix_send_circuit_open())
        again = self._send(move)
        self.assertEqual(again.code, NO_ANSWER)
        self.assertEqual(len(fake.calls), 2)

    def test_http_status_is_unexpected(self):
        FakeApixSendClient("http").patch(self)
        result = self._send(self._make_invoice())
        self.assertEqual((result.outcome, result.code), ("failed", UNEXPECTED))
        self.assertIn("503", self._row(result).reason)

    # -- the schema gate ---------------------------------------------------------------

    def test_schema_invalid_message_sends_nothing(self):
        fake = FakeApixSendClient("accepted").patch(self)
        move = self._make_invoice()
        original = type(move)._apix_finvoice_values

        def broken(record, **kwargs):
            values = original(record, **kwargs)
            values["message"]["identifier"] = "x"  # the schema wants at least 2 characters
            return values

        with patch.object(type(move), "_apix_finvoice_values", broken):
            result = self._send(move)
        self.assertEqual((result.outcome, result.code), ("failed", SCHEMA_INVALID))
        self.assertIn("MessageIdentifier", result.message)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self._row(result).schema_result, "invalid")
        self.assertTrue(self._row(result).sudo().schema_errors)
        self.assertFalse(move._apix_holder(), "the invoice is released")

    def test_missing_value_sends_nothing_and_names_the_field(self):
        fake = FakeApixSendClient("accepted").patch(self)
        move = self._make_invoice()
        original = type(move)._apix_finvoice_values

        def no_seller_name(record, **kwargs):
            values = original(record, **kwargs)
            values["seller"]["name"] = ""
            return values

        with patch.object(type(move), "_apix_finvoice_values", no_seller_name):
            result = self._send(move)
        self.assertEqual(result.code, SCHEMA_INVALID)
        self.assertIn("seller", result.message.lower())
        self.assertEqual(fake.calls, [])

    def test_render_failure_is_unexpected_and_releases_the_invoice(self):
        fake = FakeApixSendClient("accepted").patch(self)
        move = self._make_invoice()
        logger = "odoo.addons.goodahead_invoice_send_apix.models.account_move_send"
        with patch.object(type(move), "_apix_render_pdf", side_effect=RuntimeError("boom")), \
                self.assertLogs(logger, "ERROR"):
            result = self._send(move)
        self.assertEqual((result.outcome, result.code), ("failed", UNEXPECTED))
        self.assertIn("RuntimeError", self._row(result).reason)
        self.assertEqual(fake.calls, [])
        self.assertFalse(move._apix_holder())

    # -- refusals before the claim ---------------------------------------------

    def test_pre_send_refusal_records_nothing(self):
        fake = FakeApixSendClient("accepted").patch(self)
        move = self._make_invoice(post=False)
        result = self._send(move)
        self.assertEqual(result.outcome, "not_sent")
        self.assertEqual(result.code, NOT_POSTED)
        self.assertTrue(result.refusals)
        self.assertFalse(result.attempt)
        self.assertFalse(self._attempts(move))
        self.assertEqual(fake.calls, [])

    def test_email_invoice_needs_an_explicit_apix_channel(self):
        FakeApixSendClient("accepted").patch(self)
        move = self._make_invoice()
        move.transmit_method = "email"
        result = self._send(move)
        self.assertEqual((result.outcome, result.code), ("not_sent", CHANNEL_UNSUPPORTED))
        self.assertFalse(self._attempts(move))

    def test_send_in_progress_names_the_holder(self):
        FakeApixSendClient("accepted").patch(self)
        move = self._make_invoice()
        self.env[ATTEMPT]._ledger_claim(move, "einvoice", "button", self.env.user)
        result = self._send(move)
        self.assertEqual((result.outcome, result.code), ("in_progress", SEND_IN_PROGRESS))
        self.assertEqual(result.holder["user"], self.env.user.name)
        self.assertEqual(len(self._attempts(move)), 1)

    # -- the invoice's own company -----------------------------------------------------

    def test_invoice_of_the_second_company_from_the_first(self):
        fake = FakeApixSendClient("accepted").patch(self)
        move = self._make_invoice(company=self.company_b)
        result = self._send(move)
        self.assertEqual(result.outcome, "accepted", result.message)
        self.assertEqual(fake.calls[0]["transfer_id"], "ZZ-TRANSFER-003700000035")
        self.assertEqual(self._row(result).company_id, self.company_b)
        xml = next(data for name, data in _zip_names(fake.calls[0]["zip"]).items()
                   if name.endswith(".xml"))
        self.assertIn(b"<SellerOrganisationName>ZZ Second Company Oy<", xml)
        self.connection_b.invalidate_recordset()
        self.assertEqual(self.connection_b.credit_balance, 99)
        self.assertFalse(self.connection.credit_balance)

    # -- printed letter ------------------------------------------------------------------

    def test_printed_letter_goes_to_the_print_service(self):
        fake = FakeApixSendClient("accepted").patch(self)
        move = self._make_invoice()
        move.transmit_method = "letter"
        result = self._send(move)
        self.assertEqual(result.outcome, "accepted", result.message)
        self.assertEqual(self._row(result).channel, "letter")
        xml = next(data for name, data in _zip_names(fake.calls[0]["zip"]).items()
                   if name.endswith(".xml"))
        self.assertIn(b"<ToIdentifier>TULOSTUS<", xml)

    # -- the caller's transaction -----------------------------------------------------------------

    def test_never_commits_the_callers_transaction(self):
        FakeApixSendClient("accepted").patch(self)
        move = self._make_invoice()
        with patch.object(type(self.env.cr), "commit", side_effect=AssertionError("commit")):
            result = self._send(move)
        self.assertEqual(result.outcome, "accepted")
