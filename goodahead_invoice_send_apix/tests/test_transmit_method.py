# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The invoice's transmit method, the customer's APIX channels and the
creation default.

Every name, e-invoice address and Business ID here is invented.
"""

import unittest

if not __name__.startswith("odoo.addons."):
    # Plain `python -m unittest discover` (the library tests) has no ORM.
    raise unittest.SkipTest("needs Odoo")

from odoo.exceptions import ValidationError  # noqa: E402
from odoo.tests import new_test_user, tagged  # noqa: E402

from odoo.addons.account.tests.common import AccountTestInvoicingCommon  # noqa: E402

from ..models.account_move import SEND_IN_PROGRESS, SendInProgress  # noqa: E402

ATTEMPT = "goodahead.invoice.send.attempt"


@tagged("post_install", "-at_install")
class TestTransmitMethod(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.company_data["company"]
        Partner = cls.env["res.partner"]
        cls.operator = cls.env["res.partner.operator.einvoice"].create(
            {"name": "ZZ Operator", "identifier": "ZZOPER"}
        )
        # set channel email, but with an e-invoice address
        cls.email_with_address = Partner.create({
            "name": "ZZ Email Customer Oy",
            "edicode": "003700000019",
            "einvoice_operator_id": cls.operator.id,
            "invoice_sending_method": "email",
        })
        # no channel, with an e-invoice address
        cls.with_address = Partner.create({
            "name": "ZZ Address Customer Oy",
            "edicode": "003700000027",
            "einvoice_operator_id": cls.operator.id,
        })
        # no channel, no e-invoice address
        cls.plain = Partner.create({"name": "ZZ Plain Customer Oy", "email": "zz@example.com"})
        cls.accountant = new_test_user(
            cls.env,
            login="zz_transmit_accountant",
            groups="account.group_account_invoice",
            company_id=cls.company.id,
            company_ids=[(6, 0, cls.company.ids)],
        )

    def _invoice(self, partner, move_type="out_invoice", post=False):
        return self.init_invoice(move_type, partner=partner, amounts=[100.0], post=post)

    def _queued(self, move, user=None):
        return self.env[ATTEMPT]._ledger().create({
            "move_id": move.id,
            "channel": "einvoice",
            "state": "queued",
            "origin": "mass_background",
            "requested_by_id": (user or self.accountant).id,
        })

    # -- the customer's channels ----------------------------------------------------

    def test_customer_channels_added(self):
        selection = dict(
            self.env["res.partner"]._fields["invoice_sending_method"]._description_selection(self.env)
        )
        self.assertIn("apix", selection)
        self.assertIn("apix_letter", selection)
        self.assertIn("email", selection)

    def test_e_invoice_address_is_tracked(self):
        for name in ("edicode", "einvoice_operator_id"):
            self.assertTrue(self.env["res.partner"]._fields[name].tracking, name)
        self.assertTrue(self.env["account.move"]._fields["transmit_method"].tracking)

    # -- the creation default -----------------------------------------------------

    def test_set_channel_wins_over_an_e_invoice_address(self):
        self.assertEqual(self._invoice(self.email_with_address).transmit_method, "email")

    def test_no_channel_with_an_address_is_e_invoice(self):
        self.assertEqual(self._invoice(self.with_address).transmit_method, "einvoice")

    def test_no_channel_and_no_address_is_email(self):
        self.assertEqual(self._invoice(self.plain).transmit_method, "email")

    def test_apix_channels_map_to_the_invoice_methods(self):
        for channel, expected in (("apix", "einvoice"), ("apix_letter", "letter"), ("email", "email")):
            with self.subTest(channel=channel):
                self.plain.invoice_sending_method = channel
                self.assertEqual(self._invoice(self.plain).transmit_method, expected)

    def test_other_erp_channel_counts_as_not_set(self):
        self.with_address.invoice_sending_method = "manual"
        self.assertEqual(self._invoice(self.with_address).transmit_method, "einvoice")
        self.plain.invoice_sending_method = "manual"
        self.assertEqual(self._invoice(self.plain).transmit_method, "email")

    def test_contact_falls_back_to_its_commercial_customer(self):
        contact = self.env["res.partner"].create({
            "name": "ZZ Billing Desk", "type": "invoice", "parent_id": self.email_with_address.id,
        })
        self.assertEqual(self._invoice(contact).transmit_method, "email")
        contact_b = self.env["res.partner"].create({
            "name": "ZZ Billing Desk B", "type": "invoice", "parent_id": self.with_address.id,
        })
        self.assertEqual(self._invoice(contact_b).transmit_method, "einvoice")

    def test_contact_channel_wins_over_the_commercial_customer(self):
        contact = self.env["res.partner"].create({
            "name": "ZZ Letter Desk", "type": "invoice", "parent_id": self.email_with_address.id,
            "invoice_sending_method": "apix_letter",
        })
        self.assertEqual(self._invoice(contact).transmit_method, "letter")

    def test_channel_is_read_for_the_invoice_company(self):
        other = self.setup_other_company(name="ZZ Other Company Oy")["company"]
        self.plain.with_company(other).invoice_sending_method = "apix"
        self.assertEqual(self._invoice(self.plain).transmit_method, "email")
        self.assertEqual(
            self.plain.with_company(other)._apix_transmit_default(other), "einvoice"
        )

    def test_credit_note_gets_a_default_too(self):
        self.assertEqual(self._invoice(self.with_address, "out_refund").transmit_method, "einvoice")

    def test_vendor_bill_has_no_transmit_method(self):
        bill = self._invoice(self.with_address, "in_invoice")
        self.assertFalse(bill.transmit_method)
        self.assertFalse(bill._apix_default_transmit_method())
        with self.assertRaises(ValidationError):
            bill.transmit_method = "email"

    def test_explicit_value_on_create_is_kept(self):
        move = self.env["account.move"].create({
            "move_type": "out_invoice",
            "partner_id": self.with_address.id,
            "transmit_method": "letter",
        })
        self.assertEqual(move.transmit_method, "letter")

    def test_default_method_reads_the_rule(self):
        move = self._invoice(self.with_address)
        self.assertEqual(move._apix_default_transmit_method(), "einvoice")
        self.with_address.invoice_sending_method = "email"
        self.assertEqual(move._apix_default_transmit_method(), "email")
        self.assertEqual(move.transmit_method, "einvoice", "set at creation only")

    def test_copy_takes_the_default_again(self):
        move = self._invoice(self.with_address)
        move.transmit_method = "letter"
        self.assertEqual(move.copy().transmit_method, "einvoice")

    def test_sent_before_install_defaults_to_false(self):
        move = self._invoice(self.plain)
        self.assertFalse(move.sent_before_install)
        field = self.env["account.move"]._fields["sent_before_install"]
        self.assertTrue(field.readonly)
        self.assertFalse(field.copy)

    # -- switching on a posted invoice -------------------------------------

    def test_accountant_switches_a_posted_invoice(self):
        move = self._invoice(self.with_address, post=True)
        # the test class disables tracking; the change must be tracked
        tracked_move = move.with_user(self.accountant).with_context(
            tracking_disable=False, mail_notrack=False
        )
        tracked_move.write({"transmit_method": "email"})
        self._flush_tracking()
        self.assertEqual(move.transmit_method, "email")
        tracked_move.write({"transmit_method": "einvoice"})
        self._flush_tracking()
        self.assertEqual(move.transmit_method, "einvoice")
        tracked = move.message_ids.tracking_value_ids.filtered(
            lambda t: t.field_id.name == "transmit_method"
        )
        # one transaction: the ERP folds both changes into the tracking of the first
        self.assertTrue(tracked)

    def _flush_tracking(self):
        self.env.flush_all()
        self.env.cr.flush()

    # -- refused while a send is in progress ---------------------------------------

    def test_switch_refused_while_a_send_is_in_progress(self):
        move = self._invoice(self.with_address, post=True)
        holder_user = new_test_user(self.env, login="zz_transmit_holder",
                                    groups="account.group_account_invoice")
        queued = self._queued(move, user=holder_user)
        with self.assertRaises(SendInProgress) as caught:
            move.with_user(self.accountant).write({"transmit_method": "email"})
        self.assertEqual(caught.exception.code, SEND_IN_PROGRESS)
        self.assertIn(holder_user.name, str(caught.exception))
        self.assertEqual(move.transmit_method, "einvoice")
        # other fields stay writable; once the send has ended the switch is allowed
        move.with_user(self.accountant).write({"ref": "ZZ still writable"})
        self.env[ATTEMPT]._ledger_cancel(queued, self.accountant)
        move.with_user(self.accountant).write({"transmit_method": "email"})
        self.assertEqual(move.transmit_method, "email")

    def test_running_send_refuses_the_switch_too(self):
        move = self._invoice(self.with_address, post=True)
        self.env[ATTEMPT]._ledger_claim(move, "einvoice", "button", self.accountant)
        with self.assertRaises(SendInProgress):
            move.write({"transmit_method": "letter"})

    def test_same_value_is_not_a_switch(self):
        move = self._invoice(self.with_address, post=True)
        self._queued(move)
        move.write({"transmit_method": "einvoice"})  # unchanged: nothing to refuse

    # -- nothing happens until someone sends ----------------------------------------

    def test_creating_and_posting_produces_no_send_record(self):
        Attempt = self.env[ATTEMPT].sudo()
        Job = self.env["queue.job"].sudo()
        attempts, jobs = Attempt.search_count([]), Job.search_count([])
        partners = [self.with_address, self.email_with_address, self.plain]
        moves = self.env["account.move"].create([
            {
                "move_type": "out_invoice" if i % 5 else "out_refund",
                "partner_id": partners[i % 3].id,
                "invoice_line_ids": [(0, 0, {"name": f"ZZ line {i}", "quantity": 1,
                                             "price_unit": 10.0 + i})],
            }
            for i in range(50)
        ])
        moves.action_post()
        self.assertTrue(all(moves.mapped("transmit_method")))
        self.assertEqual(Attempt.search_count([]), attempts)
        self.assertEqual(Job.search_count([]), jobs)
        attachments = self.env["ir.attachment"].sudo().search_count(
            [("res_model", "=", "account.move"), ("res_id", "in", moves.ids)]
        )
        self.assertEqual(attachments, 0)
