# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The sending step of the migration tool screen: every state, driven through the form with
test-only sources. Invented data only; no APIX call."""

from odoo.exceptions import UserError
from odoo.tests import Form, tagged

from .common import FAKE, SendingMigrationCase

MIGRATION = "goodahead.apix.migration"
ATTEMPT = "goodahead.invoice.send.attempt"
UNKNOWN = "zzunknown"


@tagged("post_install", "-at_install")
class TestSendingStepScreen(SendingMigrationCase):
    def setUp(self):
        super().setUp()
        # The receiving carry-over is done: the connection holds credentials.
        self.connection.write({
            "environment": "test",
            "transfer_id": "ZZ-TRANSFER-SENDSTEP",
            "transfer_key": "zz-key-sendstep",
        })
        # The receiving screen's detection and source checks, answered for the
        # test-only sources.
        self.detected = self._patch_connection("_migration_detect_all", return_value=({}, {}))
        self._patch_connection("_migration_problem", return_value=False)
        self.move = self.init_invoice(
            "out_invoice", partner=self.customer, amounts=[100.0], post=True
        )

    def _values(self, recipients=None):
        return {
            "recipients": recipients if recipients is not None else [
                {"partner_id": self.customer.id, "edicode": "003700000019",
                 "operator_identifier": "ZZOPER"},
            ],
            "customer_channels": [
                {"partner_id": self.customer.id, "company_id": self.company.id,
                 "channel": "einvoice"},
            ],
            "invoice_methods": [{"move_id": self.move.id, "method": "einvoice"}],
            "sent_invoices": [{
                "move_id": self.move.id, "date": "2026-11-20 09:30:00", "channel": "einvoice",
                "batch_id": "ZZBATCH1", "document_id": "ZZDOC1", "credits": 1.0,
            }],
        }

    def _form(self):
        Wizard = self.env[MIGRATION].with_user(self.admin).with_context(default_step="sending")
        return Form(Wizard)

    def _wizard(self):
        return self._form().save()

    # -- default — sending found -------------------------------------------------

    def test_sending_found_shows_status_and_counts(self):
        self.fake_sending_source(values=self._values())
        form = self._form()
        self.assertEqual(form.step, "sending")
        self.assertEqual(form.sending_source_code, FAKE)
        self.assertEqual(form.sending_view, "found")
        self.assertEqual(form.sending_status, "sending")
        self.assertIn("it still sends", form.sending_status_reason)
        self.assertEqual(
            (form.sending_count_recipients, form.sending_count_channels,
             form.sending_count_methods, form.sending_count_sent),
            (1, 1, 1, 1),
        )

    # -- success, repeated ---------------------------------------------------------

    def test_carry_over_success_and_repeat(self):
        mocks = self.fake_sending_source(values=self._values())
        wizard = self._wizard()
        action = wizard.action_sending_carry_over()
        self.assertEqual(action["res_id"], wizard.id)
        self.assertEqual(wizard.step, "sending")
        self.assertEqual(wizard.sending_result, "done")
        self.assertIn("earlier sending stopped for", wizard.sending_result_message)
        self.assertIn(self.company.name, wizard.sending_result_message)
        self.assertIn("switch sending on", wizard.sending_result_message)
        mocks["sending_stop"].assert_called_once()
        self.assertEqual(self.customer.edicode, "003700000019")
        sent = self.env[ATTEMPT].search([("move_id", "=", self.move.id)])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent.apix_batch_id, "ZZBATCH1")
        # carrying over again changes nothing already carried
        wizard.action_sending_carry_over()
        self.assertEqual(self.env[ATTEMPT].search_count([("move_id", "=", self.move.id)]), 1)
        self.assertEqual(wizard.sending_result, "done")

    def test_carry_over_partial(self):
        self.fake_sending_source(values=self._values(recipients=[
            {"partner_id": self.customer.id, "edicode": "003700000019",
             "operator_identifier": "ZZOPER"},
            {"partner_id": self.other.id, "edicode": "003700000027",
             "operator_identifier": "ZZNOTLISTED"},
        ]))
        wizard = self._wizard()
        wizard.action_sending_carry_over()
        self.assertEqual(wizard.sending_result, "partial")
        self.assertIn("1 customers not carried", wizard.sending_result_message)
        self.assertIn("ZZ Unlisted Customer Oy", wizard.sending_result_message)
        self.assertFalse(self.other.edicode)

    # -- empty states -------------------------------------------------------------

    def test_no_adapter_for_the_connector(self):
        self._patch_connection("_migration_sources", return_value=[UNKNOWN])
        self._patch_connection(
            "_migration_source_label", side_effect=lambda code: f"ZZ Proprietary ({code})"
        )
        self.detected.return_value = ({UNKNOWN: {"transfer_id": "ZZ-OLD"}}, {})
        form = self._form()
        self.assertFalse(form.sending_source_code)
        self.assertEqual(form.sending_view, "no_adapter")
        self.assertIn("No migration adapter exists for ZZ Proprietary", form.sending_banner)
        self.assertIn("nothing carried over", form.sending_banner)
        wizard = form.save()
        with self.assertRaises(UserError):
            wizard.action_sending_carry_over()

    def test_nothing_to_carry(self):
        self.fake_sending_source(status=("none", ""), values=self._values())
        form = self._form()
        self.assertEqual(form.sending_view, "nothing")
        self.assertIn("No earlier sending found", form.sending_banner)

    def test_no_source_at_all_is_nothing_to_carry(self):
        self._patch_connection("_migration_sources", return_value=[])
        form = self._form()
        self.assertEqual(form.sending_view, "nothing")

    # -- error, validation -----------------------------------------------------------------

    def test_incomplete_source_is_an_error_and_writes_nothing(self):
        mocks = self.fake_sending_source(
            values=self._values(), methods=("sending_status", "sending_read")
        )
        form = self._form()
        self.assertEqual(form.sending_view, "error")
        self.assertIn("incomplete", form.sending_banner)
        wizard = form.save()
        with self.assertRaises(UserError):
            wizard.action_sending_carry_over()
        mocks["sending_read"].assert_not_called()
        self.assertFalse(self.customer.edicode)

    def test_connection_must_be_carried_over_first(self):
        mocks = self.fake_sending_source(values=self._values())
        self.connection.write({"transfer_id": False, "transfer_key": False})
        form = self._form()
        self.assertEqual(form.sending_view, "needs_connection")
        self.assertIn("Carry over the connection first", form.sending_banner)
        wizard = form.save()
        with self.assertRaises(UserError):
            wizard.action_sending_carry_over()
        mocks["sending_stop"].assert_not_called()
        self.assertFalse(self.customer.edicode)

    # -- switch back -------------------------------------------------------------------------

    def test_switch_back(self):
        mocks = self.fake_sending_source(status=("stopped", ""), values=self._values())
        wizard = self._wizard()
        self.assertTrue(wizard.sending_can_switch_back)
        wizard.action_sending_switch_back()
        mocks["sending_resume"].assert_called_once()
        self.assertEqual(wizard.sending_result, "switched_back")
        self.assertIn("sends again for", wizard.sending_result_message)

    def test_switch_back_refused_while_sending_is_on(self):
        mocks = self.fake_sending_source(status=("stopped", ""), values=self._values())
        self.connection.write({"status": "working", "sending_enabled": True})
        wizard = self._wizard()
        wizard.action_sending_switch_back()
        mocks["sending_resume"].assert_not_called()
        self.assertEqual(wizard.sending_result, "refused")
        self.assertIn("Switch sending off", wizard.sending_result_message)
        action = wizard.action_open_sending_settings()
        self.assertEqual(action["res_model"], "goodahead.apix.connection")
        self.assertEqual(action["res_id"], self.connection.id)

    # -- reaching the step -------------------------------------------------------------------

    def test_open_the_sending_step_from_the_receiving_steps(self):
        self.fake_sending_source(values=self._values())
        wizard = self.env[MIGRATION].with_user(self.admin).create({})
        self.assertNotEqual(wizard.step, "sending")
        action = wizard.action_open_sending()
        self.assertEqual(wizard.step, "sending")
        self.assertEqual(action["res_id"], wizard.id)
