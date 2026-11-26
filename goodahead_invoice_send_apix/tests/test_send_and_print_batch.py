# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Several invoices from Send & Print go to the mass wizard; every email
and download is recorded in the delivery history.

``FakeApixSendClient`` stands in for SendInvoiceZIP: no network. Invented
data only.
"""

import unittest
from unittest.mock import patch

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from lxml import etree

from odoo.addons.account.models.account_move_send import AccountMoveSend as ErpMoveSend
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import FakeApixSendClient, SendApixCommon

ATTEMPT = "goodahead.invoice.send.attempt"
MASS = "goodahead.invoice.send.wizard"
BATCH = "account.move.send.batch.wizard"
SINGLE = "account.move.send.wizard"


@tagged("post_install", "-at_install")
class TestSendAndPrintBatch(SendApixCommon):
    def setUp(self):
        super().setUp()
        self.fake = FakeApixSendClient("accepted").patch(self)

    def _ctx(self, moves):
        return {"active_model": "account.move", "active_ids": moves.ids,
                "allowed_company_ids": [self.company.id]}

    def _batch(self, moves):
        return self.env[BATCH].with_user(self.accountant).with_context(**self._ctx(moves)).create({})

    def _single(self, move, **values):
        return self.env[SINGLE].with_user(self.accountant).with_context(**self._ctx(move)).create(values)

    def _attempts(self, move):
        return self.env[ATTEMPT].search([("move_id", "=", move.id)], order="id")

    def _send_quietly(self, move, **settings):
        """The ERP's own sending outside any dialog; returns what it raised
        (the ledger keeps its row whatever the caller's transaction does)."""
        try:
            self.env["account.move.send"].with_user(self.accountant).with_context(
                allowed_company_ids=[self.company.id]
            )._generate_and_send_invoices(move, **settings)
        except Exception as error:  # noqa: BLE001 - the test inspects it
            return error
        return None

    # -- AC-10: several invoices with APIX go to the mass wizard ----------------------------------

    def test_several_apix_invoices_open_the_mass_wizard(self):
        moves = self._make_invoice() | self._make_invoice() | self._make_invoice(
            transmit_method="letter")
        action = self._batch(moves).action_send_and_print()
        self.assertEqual((action["res_model"], action["target"]), (MASS, "new"))
        wizard = self.env[MASS].with_user(self.accountant).browse(action["res_id"])
        self.assertEqual(wizard.line_ids.move_id, moves)
        self.assertEqual(wizard.state, "review", "the Accountant chooses now or background")
        self.assertFalse(self._attempts(moves[0]) | self._attempts(moves[1]) | self._attempts(moves[2]))
        self.assertFalse(any(moves.mapped("sending_data")), "nothing deferred to the ERP's batch")
        self.assertFalse(self.fake.calls)

    def test_erp_only_invoices_continue_in_the_batch_flow(self):
        apix = self._make_invoice() | self._make_invoice()
        email = self._make_invoice(transmit_method="email")
        action = self._batch(apix | email).action_send_and_print()
        wizard = self.env[MASS].with_user(self.accountant).browse(action["res_id"])
        self.assertEqual(wizard.line_ids.move_id, apix)
        self.assertFalse(any(apix.mapped("sending_data")))
        self.assertTrue(email.sending_data, "the ERP's batch cron sends it")
        self.assertEqual(email.sending_data.get("goodahead_origin"), "send_print")

    def test_batch_without_apix_is_the_erp_s_own(self):
        moves = self._make_invoice(transmit_method="email") | self._make_invoice(
            transmit_method="email")
        action = self._batch(moves).action_send_and_print()
        self.assertEqual(action["tag"], "display_notification")
        self.assertTrue(all(moves.mapped("sending_data")))

    # -- AC-10, AC-55: APIX only when a person sends (review R1) ---------------------------------

    def _apix_attempts(self, move):
        return self._attempts(move).filtered(lambda a: a.channel in ("einvoice", "letter"))

    def _assert_not_sent_through_apix(self, move):
        self.assertEqual(len(self.fake.calls), 0, "no APIX call outside a person's send")
        self.assertFalse(self._apix_attempts(move), "no APIX attempt")

    def test_erp_batch_cron_never_sends_through_apix(self):
        """An e-invoice the ERP's batch cron picks up (deferred from the
        dialog) is not sent through APIX; the invoice says why."""
        move = self._make_invoice()
        self.assertEqual(move.transmit_method, "einvoice")
        move.sending_data = {
            "author_user_id": self.accountant.id,
            "author_partner_id": self.accountant.partner_id.id,
            "goodahead_origin": "send_print",
        }
        self.env["account.move.send"]._generate_and_send_invoices(move, from_cron=True)
        self._assert_not_sent_through_apix(move)
        self.assertIn("Not sent through APIX", "\n".join(move.message_ids.mapped("body")))

    def test_direct_erp_sending_never_sends_through_apix(self):
        """Code outside the dialog that calls the ERP's sending (default
        settings) makes no APIX call; it gets a clear error instead."""
        move = self._make_invoice()
        error = self._send_quietly(move)
        self._assert_not_sent_through_apix(move)
        self.assertIsInstance(error, UserError)
        self.assertIn("Not sent through APIX", str(error))

    def test_direct_erp_sending_with_apix_method_never_sends(self):
        move = self._make_invoice(transmit_method="letter")
        error = self._send_quietly(move, sending_methods=["apix_letter"])
        self._assert_not_sent_through_apix(move)
        self.assertIsInstance(error, UserError)

    def test_the_dialog_still_sends_through_apix(self):
        move = self._make_invoice()
        self._press_send(self._single(move, sending_methods=["apix"]))
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self._apix_attempts(move).mapped("state"), ["accepted"])

    def test_programmatic_wizard_never_sends_through_apix(self):
        """Review F3: code that creates the Send & Print wizard and calls its
        action (no Send button pressed) makes no APIX call."""
        for transmit_method, method in (("einvoice", "apix"), ("letter", "apix_letter")):
            with self.subTest(transmit_method=transmit_method):
                move = self._make_invoice(transmit_method=transmit_method)
                with self.assertRaises(UserError) as caught:
                    self._single(move, sending_methods=[method]).action_send_and_print()
                self.assertIn("Not sent through APIX", str(caught.exception))
                self._assert_not_sent_through_apix(move)

    def test_programmatic_wizard_email_still_goes_out(self):
        """Review F3: the email-only use of the wizard by code keeps working."""
        move = self._make_invoice(transmit_method="email")
        self._single(move, sending_methods=["email"]).action_send_and_print()
        self._after_commit()
        self.assertEqual(self._attempts(move).mapped("state"), ["sent"])

    def test_the_dialogs_send_button_marks_a_persons_send(self):
        arch = self.env[SINGLE].get_views([(False, "form")])["views"]["form"]["arch"]
        buttons = etree.fromstring(arch).xpath("//footer/button[@name='action_send_and_print']")
        self.assertTrue(buttons)
        for button in buttons:
            self.assertIn("goodahead_person_send", button.get("context") or "")

    # -- §2.3: email and download recording ------------------------------------------

    def test_email_from_send_and_print_is_recorded_and_counts(self):
        """AC-26, AC-29: the email send is in the delivery history with
        channel email and counts as sent."""
        move = self._make_invoice(transmit_method="email")
        self._single(move, sending_methods=["email"]).action_send_and_print()
        self._after_commit()
        attempt = self._attempts(move)
        self.assertEqual(len(attempt), 1)
        self.assertEqual(
            (attempt.channel, attempt.state, attempt.origin, attempt.counts_as_sent),
            ("email", "sent", "send_print", True),
        )
        self.assertEqual(attempt.requested_by_id, self.accountant)
        move.invalidate_recordset()
        self.assertTrue(move.already_sent)
        self.assertIn(attempt, move.send_attempt_ids)
        self.assertFalse(self.fake.calls)

    def test_email_sent_outside_the_add_on_is_erp_send(self):
        move = self._make_invoice(transmit_method="email")
        self.assertIsNone(self._send_quietly(move, sending_methods=["email"]))
        self._after_commit()
        attempt = self._attempts(move)
        self.assertEqual((attempt.channel, attempt.origin, attempt.counts_as_sent),
                         ("email", "erp_send", True))

    def test_email_is_recorded_once_the_callers_transaction_commits(self):
        """Review R5, AC-29: until the caller commits, the email may still be
        undone — nothing says "sent by email" yet."""
        move = self._make_invoice(transmit_method="email")
        self._single(move, sending_methods=["email"]).action_send_and_print()
        self.assertFalse(self._attempts(move), "not before the commit")
        move.invalidate_recordset()
        self.assertFalse(move.already_sent)
        self._after_commit()
        self.assertEqual(self._attempts(move).mapped("state"), ["sent"])

    def test_rolled_back_email_leaves_no_sent_record(self):
        """Review R5, AC-29, AC-31: the caller's transaction rolls back after
        the email step — no email went out, nothing says it did."""
        move = self._make_invoice(transmit_method="email")
        self.assertIsNone(self._send_quietly(move, sending_methods=["email"]))
        self._after_rollback()
        self.assertFalse(self._attempts(move))
        move.invalidate_recordset()
        self.assertFalse(move.already_sent)
        self.assertFalse(self.env[ATTEMPT]._ledger_holder(move.id), "the invoice is free")

    def _failing_record(self, failures):
        """``_ledger_record`` failing its first ``failures`` calls."""
        Attempt = type(self.env[ATTEMPT])
        original = Attempt._ledger_record
        calls = []

        def record(self_, *args, **kwargs):
            calls.append(args)
            if len(calls) <= failures:
                raise RuntimeError("ZZ ledger down")
            return original(self_, *args, **kwargs)

        return patch.object(Attempt, "_ledger_record", record)

    def test_history_step_failing_once_is_retried(self):
        """Review F2: a failed after-commit step is logged with the invoice
        and retried once on a fresh cursor."""
        move = self._make_invoice(transmit_method="email")
        with self._failing_record(1), \
                self.assertLogs("odoo.addons.goodahead_invoice_send_apix.models.ledger",
                                "WARNING") as logs:
            self.assertIsNone(self._send_quietly(move, sending_methods=["email"]))
            self._after_commit()
        self.assertIn(str(move.id), "\n".join(logs.output))
        self.assertEqual(self._attempts(move).mapped("state"), ["sent"])

    def test_history_step_failing_twice_leaves_a_note_on_the_invoice(self):
        """Review F2, AC-29: the email went out but its history line could not
        be written: the invoice says so in its chatter."""
        move = self._make_invoice(transmit_method="email")
        with self._failing_record(2), \
                self.assertLogs("odoo.addons.goodahead_invoice_send_apix.models.ledger",
                                "WARNING"):
            self.assertIsNone(self._send_quietly(move, sending_methods=["email"]))
            self._after_commit()
        self.assertFalse(self._attempts(move))
        move.invalidate_recordset()
        bodies = "\n".join(move.message_ids.mapped("body"))
        self.assertIn("Sent by email", bodies)
        self.assertIn("history line could not be written", bodies)

    def test_batch_cron_email_is_recorded_for_the_user_who_started_it(self):
        moves = self._make_invoice(transmit_method="email") | self._make_invoice(
            transmit_method="email")
        self._batch(moves).action_send_and_print()
        self.env["account.move.send"]._generate_and_send_invoices(moves, from_cron=True)
        self._after_commit()
        for move in moves:
            attempt = self._attempts(move)
            self.assertEqual((attempt.channel, attempt.state, attempt.origin),
                             ("email", "sent", "send_print"))
            self.assertEqual(attempt.requested_by_id, self.accountant)

    def test_download_is_recorded_and_never_counts(self):
        move = self._make_invoice(transmit_method="email")
        self._single(move, sending_methods=["manual"]).action_send_and_print()
        self._after_commit()
        attempt = self._attempts(move)
        self.assertEqual((attempt.channel, attempt.state, attempt.counts_as_sent),
                         ("download", "downloaded", False))
        move.invalidate_recordset()
        self.assertFalse(move.already_sent)

    def test_email_refused_while_an_apix_send_runs(self):
        move = self._make_invoice(transmit_method="email")
        holder = self._row(
            self.env[ATTEMPT]._ledger_claim(move, "einvoice", "button", self.accountant))
        error = self._send_quietly(move, sending_methods=["email"])
        self.assertIsInstance(error, UserError)
        self.assertEqual(getattr(error, "code", None), "send.in_progress")
        self._after_rollback()
        self.assertEqual(self._attempts(move), holder, "no email attempt")

    def test_failing_email_is_recorded_failed(self):
        move = self._make_invoice(transmit_method="email")

        def broken(self_, *args, **kwargs):
            raise ValueError("ZZ mail server down")

        with patch.object(ErpMoveSend, "_send_mail", broken):
            error = self._send_quietly(move, sending_methods=["email"])
        self.assertIsInstance(error, ValueError)
        # the caller rolls back after the error; the failure is still recorded
        self._after_rollback()
        attempt = self._attempts(move)
        self.assertEqual((attempt.channel, attempt.state, attempt.counts_as_sent),
                         ("email", "failed", False))
        self.assertTrue(attempt.reason.startswith("[send.email_failed]"), attempt.reason)

    def test_background_email_job_is_recorded_once(self):
        """The job records its own attempt (``goodahead_send_recorded``)."""
        move = self._make_invoice(transmit_method="email")
        self.env["account.move.send"].with_context(goodahead_send_recorded=True)\
            ._generate_and_send_invoices(move, sending_methods=["email"])
        self._after_commit()
        self.assertFalse(self._attempts(move))

    def test_the_erp_sent_flag_is_never_read(self):
        """ADR-0008: only the history says sent after install."""
        move = self._make_invoice(transmit_method="email")
        move.is_move_sent = True
        move.invalidate_recordset()
        self.assertFalse(move.already_sent)

    # -- AC-27: email to e-invoice --------------------------------------------------------------

    def test_switch_from_email_to_einvoice_then_send(self):
        """AC-27, AC-31: the email counts, so the APIX send after switching
        asks first; confirmed, it goes through APIX normally."""
        move = self._make_invoice(transmit_method="email")
        self._single(move, sending_methods=["email"]).action_send_and_print()
        self._after_commit()
        move.write({"transmit_method": "einvoice"})
        move.invalidate_recordset()
        refused = move.with_user(self.accountant)._apix_send_now("button")
        self.assertEqual(refused.code, "send.already_sent")
        accepted = move.with_user(self.accountant)._apix_send_now("button", resend_confirmed=True)
        self.assertEqual(accepted.outcome, "accepted")
        self.assertEqual(self._attempts(move).mapped("channel"), ["email", "einvoice"])
