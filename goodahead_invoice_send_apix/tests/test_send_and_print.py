# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""APIX in the ERP's Send & Print, one invoice.

``FakeApixSendClient`` stands in for SendInvoiceZIP: no network. Invented
data only.
"""

import io
import unittest
import zipfile
from unittest.mock import patch

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import BATCH_ID, FakeApixSendClient, SendApixCommon

ATTEMPT = "goodahead.invoice.send.attempt"
WIZARD = "account.move.send.wizard"


@tagged("post_install", "-at_install")
class TestSendAndPrint(SendApixCommon):
    def setUp(self):
        super().setUp()
        self.connection = self.env["goodahead.apix.connection"]._apix_for_company(self.company)

    def _wizard(self, move, **values):
        return self.env[WIZARD].with_user(self.accountant).with_context(
            active_model="account.move", active_ids=move.ids,
            allowed_company_ids=[move.company_id.id],
        ).create(values)

    def _checked(self, wizard):
        return {key for key, vals in wizard.sending_method_checkboxes.items() if vals["checked"]}

    def _attempts(self, move):
        return self.env[ATTEMPT].search([("move_id", "=", move.id)], order="id")

    def _send(self, wizard):
        """``(action, error)`` of the dialog's Send; the ledger's outcome
        stays whatever the dialog raises."""
        try:
            return self._press_send(wizard), None
        except UserError as error:
            return None, error

    # -- preselection ------------------------------------------------------

    def test_einvoice_preselected_and_other_channels_alongside(self):
        wizard = self._wizard(self._make_invoice())
        self.assertEqual(self._checked(wizard), {"apix"})
        self.assertIn("apix_letter", wizard.sending_method_checkboxes)
        self.assertIn("email", wizard.sending_method_checkboxes)

    def test_letter_preselected(self):
        wizard = self._wizard(self._make_invoice(transmit_method="letter"))
        self.assertEqual(self._checked(wizard), {"apix_letter"})

    def test_email_method_preselects_email_with_apix_available(self):
        move = self._make_invoice(transmit_method="email")
        wizard = self._wizard(move)
        self.assertEqual(self._checked(wizard), {"email"})
        self.assertIn("apix", wizard.sending_method_checkboxes)

    def test_sending_off_offers_no_apix_method(self):
        """AC-28 on SCR-04."""
        self.connection.write({"sending_enabled": False})
        wizard = self._wizard(self._make_invoice())
        self.assertNotIn("apix", wizard.sending_method_checkboxes)
        self.assertNotIn("apix_letter", wizard.sending_method_checkboxes)
        self.assertEqual(wizard.alerts["apix_sending_off"]["level"], "info")

    # -- alerts -----------------------------------------------------------------------------

    def test_not_ready_keeps_apix_listed_but_unusable(self):
        """AC-09, AC-57: not ticked, a warning names what is missing; ticked,
        it blocks; email stays usable."""
        customer = self._make_customer("ZZ No Address Oy", business_id="0000004-3",
                                       invoice_sending_method="apix")
        move = self._make_invoice(customer)
        wizard = self._wizard(move)
        self.assertIn("apix", wizard.sending_method_checkboxes)
        self.assertNotIn("apix", self._checked(wizard))
        alert = wizard.alerts["apix_not_ready"]
        self.assertEqual(alert["level"], "warning")
        self.assertIn("e-invoice address", alert["message"])
        self.assertEqual(alert["action"]["res_model"], "goodahead.invoice.send.fix")
        ticked = self._wizard(move, sending_methods=["apix"])
        self.assertEqual(ticked.alerts["apix_not_ready"]["level"], "danger")
        fake = FakeApixSendClient("accepted").patch(self)
        _action, error = self._send(ticked)
        self.assertIsNotNone(error)
        self.assertFalse(fake.calls)
        self.assertFalse(self._attempts(move))

    def test_in_progress_blocks(self):
        move = self._make_invoice()
        self.env[ATTEMPT]._ledger_claim(move, "einvoice", "button", self.accountant)
        wizard = self._wizard(move)
        alert = wizard.alerts["apix_in_progress"]
        self.assertEqual(alert["level"], "danger")
        self.assertIn("already in progress", alert["message"])

    def test_already_sent_blocks_until_the_box_is_ticked(self):
        """AC-31: a blocking alert with when and how; ticked, it sends again
        as a new history entry."""
        fake = FakeApixSendClient("accepted").patch(self)
        move = self._make_invoice()
        move.with_user(self.accountant)._apix_send_now("button")
        wizard = self._wizard(move)
        alert = wizard.alerts["apix_already_sent"]
        self.assertEqual(alert["level"], "danger")
        self.assertIn("already sent", alert["message"])
        _action, error = self._send(wizard)
        self.assertIsNotNone(error)
        self.assertEqual(len(fake.calls), 1, "nothing sent while the box is not ticked")
        wizard.apix_resend_confirmed = True
        self.assertEqual(wizard.alerts["apix_already_sent"]["level"], "warning")
        _action, error = self._send(wizard)
        self.assertIsNone(error, error)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(len(self._attempts(move)), 2)

    def test_copy_warning_never_blocks(self):
        """AC-40."""
        fake = FakeApixSendClient("accepted").patch(self)
        with patch.object(type(self.env["goodahead.apix.connection"]), "_apix_copy_warning",
                          lambda connection: "ZZ copy warning"):
            wizard = self._wizard(self._make_invoice())
            alert = wizard.alerts["apix_copy_warning"]
            self.assertEqual(alert["level"], "warning")
            self.assertEqual(alert["message"], "ZZ copy warning")
            _action, error = self._send(wizard)
        self.assertIsNone(error, error)
        self.assertEqual(len(fake.calls), 1)

    # -- sending ------------------------------------------------------------------------

    def test_apix_send_gives_the_button_s_outcome_with_the_rendered_pdf(self):
        fake = FakeApixSendClient("accepted").patch(self)
        move = self._make_invoice()
        wizard = self._wizard(move)
        with patch.object(type(self.env["account.move"]), "_apix_render_pdf",
                          side_effect=AssertionError("the dialog's PDF is used")):
            action, error = self._send(wizard)
        self.assertIsNone(error, error)
        self.assertEqual(action["tag"], "display_notification")
        self.assertEqual(action["params"]["type"], "success")
        self.assertIn(BATCH_ID, action["params"]["message"])
        attempt = self._attempts(move)
        self.assertEqual(attempt.state, "accepted")
        self.assertEqual(attempt.origin, "send_print")
        self.assertEqual(attempt.channel, "einvoice")
        with zipfile.ZipFile(io.BytesIO(fake.calls[0]["zip"])) as archive:
            pdf = archive.read("invoice.pdf")
        move.invalidate_recordset()
        self.assertEqual(pdf, move.invoice_pdf_report_id.raw)
        self.assertEqual(move.transmit_method, "einvoice")

    def test_apix_refusal_shows_in_the_dialog_and_is_recorded(self):
        FakeApixSendClient("refused", text="ZZ invalid address").patch(self)
        move = self._make_invoice()
        _action, error = self._send(self._wizard(move))
        self.assertIsNotNone(error)
        self.assertIn("ZZ invalid address", str(error))
        self.assertEqual(self._attempts(move).state, "refused")

    def test_email_chosen_for_an_einvoice_method(self):
        """AC-57: email instead of the preselected APIX; transmit method
        unchanged, nothing sent through APIX."""
        fake = FakeApixSendClient("accepted").patch(self)
        move = self._make_invoice()
        messages_before = len(move.message_ids)
        _action, error = self._send(self._wizard(move, sending_methods=["email"]))
        self.assertIsNone(error, error)
        self.assertFalse(fake.calls)
        move.invalidate_recordset()
        self.assertEqual(move.transmit_method, "einvoice")
        self.assertGreater(len(move.message_ids), messages_before)

    def test_email_chosen_for_a_not_ready_einvoice(self):
        """AC-57: not ready for e-invoice, email still goes out."""
        customer = self._make_customer("ZZ No Address Oy", business_id="0000004-3",
                                       invoice_sending_method="apix")
        move = self._make_invoice(customer)
        _action, error = self._send(self._wizard(move, sending_methods=["email"]))
        self.assertIsNone(error, error)
        move.invalidate_recordset()
        self.assertEqual(move.transmit_method, "einvoice")

    def test_apix_for_an_email_method_ready_invoice(self):
        """AC-57: transmit method email, ready for e-invoice: APIX can be
        chosen and sends normally; the method stays email."""
        fake = FakeApixSendClient("accepted").patch(self)
        move = self._make_invoice(transmit_method="email")
        _action, error = self._send(self._wizard(move, sending_methods=["apix"]))
        self.assertIsNone(error, error)
        self.assertEqual(len(fake.calls), 1)
        attempt = self._attempts(move)
        self.assertEqual((attempt.state, attempt.channel), ("accepted", "einvoice"))
        move.invalidate_recordset()
        self.assertEqual(move.transmit_method, "email")
