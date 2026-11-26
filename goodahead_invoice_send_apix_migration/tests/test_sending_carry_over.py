# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""The sending carry-over bridge (send T23; AC-39, AC-42, AC-43; contract
§11): the earlier-senders guard, carry-over, stop and switch back, with
test-only sending sources. Invented data only; no APIX call."""

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.goodahead_invoice_send_apix.models.apix_connection import EARLIER_SETUP_SENDING

from ..models.migration_sending import SENDING_ON
from .common import FAKE, SendingMigrationCase

ATTEMPT = "goodahead.invoice.send.attempt"


@tagged("post_install", "-at_install")
class TestSendingCarryOver(SendingMigrationCase):
    def _values(self, move):
        return {
            "recipients": [
                {"partner_id": self.customer.id, "edicode": "003700000019",
                 "operator_identifier": "ZZOPER"},
                {"partner_id": self.other.id, "edicode": "003700000027",
                 "operator_identifier": "ZZNOTLISTED"},
            ],
            "customer_channels": [
                {"partner_id": self.customer.id, "company_id": self.company.id,
                 "channel": "letter"},
            ],
            "invoice_methods": [{"move_id": move.id, "method": "letter"}],
            "sent_invoices": [{
                "move_id": move.id, "date": "2026-11-20 09:30:00", "channel": "einvoice",
                "batch_id": "ZZBATCH1", "document_id": "ZZDOC1", "credits": 1.5,
            }],
        }

    def _move(self):
        move = self.init_invoice("out_invoice", partner=self.customer, amounts=[100.0], post=True)
        self.assertEqual(move.transmit_method, "email")
        return move

    # -- the earlier-senders guard -----------------------------------------------

    def test_sending_source_blocks_the_switch(self):
        self.fake_sending_source(status=("sending", "it still sends for this company"))
        senders = self.env["goodahead.apix.connection"]._apix_earlier_senders(self.company)
        self.assertEqual(senders, [{
            "code": FAKE, "label": f"ZZ Earlier Connector ({FAKE})",
            "reason": "it still sends for this company",
        }])
        with self.assertRaises(UserError) as caught:
            self.connection.with_user(self.admin).action_apix_sending_on()
        self.assertEqual(caught.exception.code, EARLIER_SETUP_SENDING)
        self.assertIn(f"ZZ Earlier Connector ({FAKE})", str(caught.exception))
        self.assertFalse(self.connection.sending_enabled)

    def test_stopped_or_unused_source_lets_the_switch_on(self):
        for state in ("stopped", "none"):
            with self.subTest(state=state):
                self.fake_sending_source(status=(state, "ZZ reason"))
                self.assertEqual(
                    self.env["goodahead.apix.connection"]._apix_earlier_senders(self.company), []
                )
        self.connection.with_user(self.admin).action_apix_sending_on()
        self.assertTrue(self.connection.sending_enabled)

    def test_incomplete_source_counts_as_sending(self):
        self.fake_sending_source(status=("none", ""), methods=("sending_status", "sending_read"))
        senders = self.env["goodahead.apix.connection"]._apix_earlier_senders(self.company)
        self.assertEqual([s["code"] for s in senders], [FAKE])
        self.assertIn("incomplete", senders[0]["reason"])

    def test_detected_connector_without_sending_adapter_blocks_the_switch(self):
        """Review R14, AC-39: a receiving connector the migration tool
        detects for the company, with no sending adapter, may still send
        there: switching sending on is refused and names it."""
        self.fake_sending_source(status=("none", ""), extra_codes=("zzrecv",))
        self._patch_connection("_migration_zzrecv_detect", create=True, return_value={
            "environment": "test", "transfer_id": "ZZ-OLD-TRANSFER", "transfer_key": "zz-old",
            "business_id": "0000001-9",
        })
        senders = self.env["goodahead.apix.connection"]._apix_earlier_senders(self.company)
        self.assertEqual([s["code"] for s in senders], ["zzrecv"])
        with self.assertRaises(UserError) as caught:
            self.connection.with_user(self.admin).action_apix_sending_on()
        self.assertEqual(caught.exception.code, EARLIER_SETUP_SENDING)
        message = str(caught.exception)
        self.assertIn("ZZ Earlier Connector (zzrecv)", message)
        self.assertIn("no sending adapter", message)
        # review F4: the real ways out, not the migration tool
        self.assertIn("remove its APIX credentials for", message)
        self.assertIn("archived", message)
        self.assertIn("uninstall it", message)
        self.assertIn("install its sending adapter", message)
        self.assertNotIn("Stop its sending there", message)
        self.assertFalse(self.connection.sending_enabled)

    def test_connector_without_credentials_for_the_company_does_not_block(self):
        self.fake_sending_source(status=("none", ""), extra_codes=("zzrecv",))
        self._patch_connection("_migration_zzrecv_detect", create=True, return_value=None)
        self.assertEqual(
            self.env["goodahead.apix.connection"]._apix_earlier_senders(self.company), []
        )

    def test_receiving_only_source_is_not_a_sender(self):
        self.fake_sending_source(status=("none", ""), extra_codes=("zzrecv",))
        self.assertEqual(
            self.env["goodahead.apix.connection"]._apix_earlier_senders(self.company), []
        )

    # -- carry-over ---------------------------------------------------------

    def test_carry_over_writes_all_four_keys_then_stops(self):
        move = self._move()
        mocks = self.fake_sending_source(values=self._values(move))
        result = self.as_admin()._migration_sending_carry_over(FAKE, self.company)
        self.assertEqual(self.customer.edicode, "003700000019")
        self.assertEqual(self.customer.einvoice_operator_id, self.operator)
        self.assertFalse(self.other.edicode, "operator not on the list: nothing guessed")
        self.assertEqual(result["not_carried"], [self.other.id])
        self.assertEqual(
            self.customer.with_company(self.company).invoice_sending_method, "apix_letter"
        )
        self.assertEqual(move.transmit_method, "letter")
        attempt = self.env[ATTEMPT].sudo().search([("move_id", "=", move.id)])
        self.assertEqual(len(attempt), 1)
        self.assertEqual(attempt.origin, "migration")
        self.assertEqual(attempt.migration_source, FAKE)
        self.assertEqual(attempt.state, "sent")
        self.assertTrue(attempt.counts_as_sent)
        self.assertEqual(attempt.apix_batch_id, "ZZBATCH1")
        self.assertEqual(attempt.apix_document_id, "ZZDOC1")
        self.assertEqual(str(attempt.end_date), "2026-11-20 09:30:00")
        mocks["sending_stop"].assert_called_once_with(self.company)
        move.invalidate_recordset()
        self.assertTrue(move.already_sent, "resending it shows the already-sent warning")
        self.assertEqual(result["sent_invoices"], 1)

    def test_unknown_key_refuses_the_carry_over(self):
        move = self._move()
        values = dict(self._values(move), passwords=["zz"])
        mocks = self.fake_sending_source(values=values)
        with self.assertRaises(UserError):
            self.as_admin()._migration_sending_carry_over(FAKE, self.company)
        mocks["sending_stop"].assert_not_called()
        self.assertFalse(self.customer.edicode)

    def test_repeat_creates_no_duplicates(self):
        move = self._move()
        self.fake_sending_source(values=self._values(move))
        self.as_admin()._migration_sending_carry_over(FAKE, self.company)
        self.as_admin()._migration_sending_carry_over(FAKE, self.company)
        self.assertEqual(self.env[ATTEMPT].sudo().search_count([("move_id", "=", move.id)]), 1)

    def test_other_company_rows_are_skipped(self):
        other_company = self.setup_other_company(name="ZZ Other Oy")["company"]
        move = self._move()
        values = self._values(move)
        values["customer_channels"] = [{
            "partner_id": self.customer.id, "company_id": other_company.id, "channel": "letter",
        }]
        self.fake_sending_source(values=values)
        self.as_admin()._migration_sending_carry_over(FAKE, self.company)
        self.assertFalse(self.customer.with_company(other_company).invoice_sending_method)

    def test_accountant_cannot_carry_over(self):
        self.fake_sending_source(values={})
        with self.assertRaises(UserError):
            self.env["goodahead.apix.connection"].with_user(self.accountant)\
                ._migration_sending_carry_over(FAKE, self.company)

    def test_unknown_source_carries_nothing(self):
        self.fake_sending_source(values={})
        with self.assertRaises(UserError):
            self.as_admin()._migration_sending_carry_over("zzmissing", self.company)

    def test_counts_for_the_tool_screen(self):
        move = self._move()
        self.fake_sending_source(values=self._values(move))
        counts = self.as_admin()._migration_sending_counts(FAKE, self.company)
        self.assertEqual(counts, {
            "recipients": 2, "customer_channels": 1, "invoice_methods": 1, "sent_invoices": 1,
        })

    # -- switch back (Flow N) ----------------------------------------------------------------

    def test_switch_back_refused_while_sending_on(self):
        mocks = self.fake_sending_source(status=("stopped", ""))
        self.connection.with_user(self.admin).action_apix_sending_on()
        with self.assertRaises(UserError) as caught:
            self.as_admin()._migration_sending_switch_back(FAKE, self.company)
        self.assertEqual(caught.exception.code, SENDING_ON)
        mocks["sending_resume"].assert_not_called()

    def test_switch_back_allowed_while_off(self):
        mocks = self.fake_sending_source(status=("stopped", ""))
        self.as_admin()._migration_sending_switch_back(FAKE, self.company)
        mocks["sending_resume"].assert_called_once_with(self.company)
