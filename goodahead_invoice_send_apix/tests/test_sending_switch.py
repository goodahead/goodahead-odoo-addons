# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The per-company sending switch, the background threshold and the sending
pause on the APIX connection.

Every company name and Business ID here is invented. No APIX call is made.
"""

import unittest
from datetime import timedelta
from unittest.mock import patch

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo import fields  # noqa: E402
from odoo.exceptions import AccessError, UserError, ValidationError  # noqa: E402
from odoo.tests import TransactionCase, new_test_user, tagged  # noqa: E402

from ..models.apix_connection import (  # noqa: E402
    CONNECTION_NOT_WORKING,
    EARLIER_SETUP_SENDING,
    NOT_ALLOWED,
    PAUSE,
    THRESHOLD_INVALID,
)

CONNECTION = "goodahead.apix.connection"


@tagged("post_install", "-at_install")
class TestSendingSwitch(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company_b = cls.env["res.company"].create({"name": "ZZ Switch Second Oy"})
        both = [(6, 0, (cls.company | cls.company_b).ids)]
        cls.admin = new_test_user(
            cls.env,
            login="zz_switch_admin",
            groups="base.group_user,account.group_account_manager",
            company_id=cls.company.id,
            company_ids=both,
        )
        cls.accountant = new_test_user(
            cls.env,
            login="zz_switch_accountant",
            groups="account.group_account_invoice",
            company_id=cls.company.id,
            company_ids=both,
        )
        Connection = cls.env[CONNECTION].sudo()
        Connection.search([]).unlink()
        cls.conn_a = Connection.create({
            "company_id": cls.company.id, "business_id": "0000001-9", "status": "working",
        })
        cls.conn_b = Connection.create({
            "company_id": cls.company_b.id, "business_id": "0000002-7", "status": "working",
        })

    def _on(self, connection, user=None):
        return connection.with_user(user or self.admin).action_apix_sending_on()

    # -- the fields ---------------------------------------------------

    def test_fields_and_defaults(self):
        Connection = self.env[CONNECTION]
        self.assertFalse(self.conn_a.sending_enabled)
        self.assertEqual(self.conn_a.background_threshold, 20)
        self.assertFalse(self.conn_a.send_paused_until)
        enabled = Connection._fields["sending_enabled"]
        self.assertFalse(enabled.copy)
        self.assertTrue(enabled.tracking)
        self.assertEqual(Connection._fields["background_threshold"].groups,
                         "account.group_account_manager")
        for name in ("credit_balance", "credit_balance_date"):
            field = Connection._fields[name]
            self.assertTrue(field.readonly, name)
            self.assertFalse(field.copy, name)
            self.assertEqual(field.groups, "account.group_account_manager", name)
        self.assertFalse(Connection._fields["send_paused_until"].copy)

    # -- switch on ---------------------------------------------------------------

    def test_administrator_switches_one_company_on(self):
        self._on(self.conn_a)
        self.assertTrue(self.conn_a.sending_enabled)
        self.assertFalse(self.conn_b.sending_enabled, "no other company's sending changes")

    def test_switch_off_again(self):
        self._on(self.conn_a)
        self.conn_a.with_user(self.admin).action_apix_sending_off()
        self.assertFalse(self.conn_a.sending_enabled)

    def test_switch_is_tracked(self):
        # a record created in the same transaction is not tracked: end the
        # creation's pre-commit first
        self.env.cr.flush()
        connection = self.conn_a.with_context(tracking_disable=False, mail_notrack=False)
        self._on(connection)
        self.env.flush_all()
        self.env.cr.flush()  # tracking is written at pre-commit
        connection.invalidate_recordset(["message_ids"])
        tracked = connection.message_ids.tracking_value_ids.filtered(
            lambda t: t.field_id.name == "sending_enabled"
        )
        self.assertTrue(tracked)

    # -- refusals (Flow L, AC-38, AC-39) -------------------------------------------------

    def test_not_working_connection_is_refused(self):
        for status in ("unchecked", "rejected"):
            with self.subTest(status=status):
                self.conn_a.sudo().write({"status": status})
                with self.assertRaises(UserError) as caught:
                    self._on(self.conn_a)
                self.assertEqual(caught.exception.code, CONNECTION_NOT_WORKING)
                self.assertFalse(self.conn_a.sending_enabled)

    def test_accountant_cannot_switch(self):
        with self.assertRaises(AccessError) as caught:
            self._on(self.conn_a, self.accountant)
        self.assertEqual(caught.exception.code, NOT_ALLOWED)
        self._on(self.conn_a)
        with self.assertRaises(AccessError) as caught:
            self.conn_a.with_user(self.accountant).action_apix_sending_off()
        self.assertEqual(caught.exception.code, NOT_ALLOWED)
        self.assertTrue(self.conn_a.sending_enabled)

    def test_direct_write_runs_the_same_guards(self):
        self.conn_a.sudo().write({"status": "rejected"})
        with self.assertRaises(UserError) as caught:
            self.conn_a.with_user(self.admin).write({"sending_enabled": True})
        self.assertEqual(caught.exception.code, CONNECTION_NOT_WORKING)

    def test_earlier_sender_is_refused_with_its_name(self):
        senders = [{
            "code": "zz_fake", "label": "ZZ Fake APIX Connector",
            "reason": "still sends for this company",
        }]
        company_a = self.company

        def earlier_senders(model, company):
            return senders if company == company_a else []

        with patch.object(type(self.env[CONNECTION]), "_apix_earlier_senders", earlier_senders):
            with self.assertRaises(UserError) as caught:
                self._on(self.conn_a)
            self.assertEqual(caught.exception.code, EARLIER_SETUP_SENDING)
            self.assertIn("ZZ Fake APIX Connector", str(caught.exception))
            self.assertIn("migration tool", str(caught.exception))
            self.assertFalse(self.conn_a.sending_enabled)
            self._on(self.conn_b)  # the fake source does not send for company B
            self.assertTrue(self.conn_b.sending_enabled)

    def test_no_earlier_senders_in_the_sending_add_on(self):
        self.assertEqual(self.env[CONNECTION]._apix_earlier_senders(self.company), [])

    # -- what an Accountant sees -------------------------------------------

    def test_accountant_reads_the_switch_but_not_the_threshold(self):
        self._on(self.conn_a)
        connection = self.conn_a.with_user(self.accountant)
        self.assertTrue(connection.sending_enabled, "the doors read it as the Accountant")
        for name in ("background_threshold", "credit_balance", "credit_balance_date"):
            with self.subTest(field=name), self.assertRaises(AccessError):
                connection.read([name])

    def test_form_for_administrator_and_accountant(self):
        view = self.env.ref("goodahead_apix.goodahead_apix_connection_view_form")
        for user, admin in ((self.admin, True), (self.accountant, False)):
            with self.subTest(user=user.login):
                arch = self.env[CONNECTION].with_user(user).get_views(
                    [(view.id, "form")]
                )["views"]["form"]["arch"]
                self.assertIn('name="sending_enabled"', arch)
                self.assertEqual('name="background_threshold"' in arch, admin)
                self.assertEqual('name="credit_balance"' in arch, admin)
                self.assertEqual('name="action_apix_sending_on"' in arch, admin)
                self.assertEqual('name="action_apix_sending_off"' in arch, admin)

    # -- the background threshold -----------------------------------------------

    def test_threshold_below_one_is_refused(self):
        for value in (0, -5):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError) as caught:
                    self.conn_a.with_user(self.admin).write({"background_threshold": value})
                self.assertEqual(caught.exception.code, THRESHOLD_INVALID)
        self.conn_a.with_user(self.admin).write({"background_threshold": 1})
        self.assertEqual(self.conn_a.background_threshold, 1)

    def test_accountant_cannot_set_the_threshold(self):
        with self.assertRaises(AccessError):
            self.conn_a.with_user(self.accountant).write({"background_threshold": 50})

    # -- the sending pause --------------------------------------

    def test_circuit_closed_by_default(self):
        self.assertFalse(self.conn_a._apix_send_circuit_open())

    def test_unreachable_opens_the_circuit_without_rejecting(self):
        before = fields.Datetime.now()
        self.conn_a._apix_open_send_circuit("send.unreachable", "ZZ APIX could not be reached")
        self.assertTrue(self.conn_a._apix_send_circuit_open())
        self.assertEqual(self.conn_a.status, "working", "a blip keeps the credentials trusted")
        self.assertEqual(self.conn_a.status_message, "ZZ APIX could not be reached")
        self.assertGreaterEqual(self.conn_a.send_paused_until, before + PAUSE - timedelta(seconds=1))
        self.assertLessEqual(self.conn_a.send_paused_until,
                             fields.Datetime.now() + PAUSE + timedelta(seconds=1))

    def test_refused_credentials_and_no_contract_reject_the_connection(self):
        for code in ("send.credentials_refused", "send.no_contract"):
            with self.subTest(code=code):
                self.conn_a.sudo().write({"status": "working", "send_paused_until": False})
                self.conn_a._apix_open_send_circuit(code, "ZZ no sending contract")
                self.assertEqual(self.conn_a.status, "rejected")
                self.assertTrue(self.conn_a._apix_send_circuit_open())

    def test_past_pause_is_closed(self):
        self.conn_a.sudo().send_paused_until = fields.Datetime.now() - timedelta(minutes=1)
        self.assertFalse(self.conn_a._apix_send_circuit_open())

    def test_close_circuit_early(self):
        self.conn_a._apix_open_send_circuit("send.no_credit", "ZZ no credit")
        self.conn_a._apix_close_send_circuit()
        self.assertFalse(self.conn_a._apix_send_circuit_open())

    def test_passing_connection_check_closes_the_pause(self):
        self.conn_a._apix_open_send_circuit("send.unreachable", "ZZ unreachable")
        self.conn_a.sudo().write({"transfer_id": "ZZ-TRANSFER", "transfer_key": "zz-key"})
        answer = {"groups": [{"y-tunnus": "0000001-9", "Name": "ZZ Example Oy"}]}
        with patch("odoo.addons.goodahead_apix.lib.apix_client.ApixClient.company_info",
                   return_value=answer):
            self.conn_a.sudo().action_check_connection()
        self.assertEqual(self.conn_a.status, "working")
        self.assertFalse(self.conn_a._apix_send_circuit_open())

    def test_credit_balance_is_recorded(self):
        self.conn_a._apix_record_credit_balance(248.5)
        connection = self.conn_a.with_user(self.admin)
        self.assertEqual(connection.credit_balance, 248.5)
        self.assertTrue(connection.credit_balance_date)

    def test_open_pause_is_connection_trouble(self):
        Connection = self.env[CONNECTION].sudo()
        domain = Connection._attention_connection_domain()
        self.assertFalse(Connection.search(domain))
        self.conn_b._apix_open_send_circuit("send.no_credit", "ZZ no credit left")
        self.assertEqual(Connection.search(Connection._attention_connection_domain()), self.conn_b)
        section = next(
            s for s in self.env["goodahead.einvoice.attention"].with_user(self.admin)
            ._attention_data()["sections"] if s["key"] == "connection_trouble"
        )
        self.assertEqual([item["id"] for item in section["items"]], [self.conn_b.id])
