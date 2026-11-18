# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""A credit note recording a done payment refund posts without approval.

Only through `_bill_approval_post_payment_refunds` and only when the data matches; a
user who links a refund and posts, with or without a context key, is still gated.
"""
from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged

from ..models.account_move import PAYMENT_REFUND_KEY
from .common import BillApprovalCommon


@tagged('post_install', '-at_install')
class TestPaymentRefundCreditNotes(BillApprovalCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        assert 'transaction_ids' in cls.AM._fields, \
            "account_payment must be installed for these tests"
        cls.provider = cls.env['payment.provider'].sudo().search(
            [('company_id', '=', cls.company.id)], limit=1)
        assert cls.provider, "this database must carry a payment provider"
        cls.card = cls.env.ref('payment.payment_method_card')

    def setUp(self):
        super().setUp()
        self.invoice = self._create_invoice('out_invoice', price_unit=100.0)
        self.invoice.action_post()
        self.credit_note = self.invoice._reverse_moves([{}])
        self.refund = self._refund_tx(100.0)

    def _refund_tx(self, amount, state='done', confirmed=True):
        tx = self.env['payment.transaction'].sudo().create({
            'provider_id': self.provider.id,
            'payment_method_id': self.card.id,
            'reference': f'BA-REFUND-{amount}-{state}-{confirmed}-{self.credit_note.id}',
            'amount': -amount,
            'currency_id': self.credit_note.currency_id.id,
            'partner_id': self.customer.id,
            'operation': 'refund',
        })
        tx.state = state
        if 'payment_refund_confirmed' in tx._fields:
            tx.payment_refund_confirmed = confirmed  # as the server does, superuser
        return tx

    def test_a_refund_the_provider_did_not_confirm_is_gated(self):
        if 'payment_refund_confirmed' not in self.env['payment.transaction']._fields:
            self.skipTest("no payment module records provider confirmation here")
        typed_in = self._refund_tx(100.0, confirmed=False)
        typed_in.invoice_ids = self.credit_note
        with self.assertRaises(UserError):
            self.credit_note._bill_approval_post_payment_refunds()
        self.assertEqual(self.credit_note.state, 'draft')

    def test_the_server_path_posts_a_matching_credit_note(self):
        self.refund.invoice_ids = self.credit_note
        self.credit_note._bill_approval_post_payment_refunds()
        self.assertEqual(self.credit_note.state, 'posted')
        self.assertFalse(self.credit_note.approval_1_uid)
        self.assertTrue(any(
            'payment refund' in (message.body or '') for message in self.credit_note.message_ids))

    def test_a_book_keeper_who_links_a_refund_is_still_gated(self):
        user = self.user_both
        # Worst case: also grant the optional payment group if it is installed.
        payment_group = self.env.ref(
            'account_voucher_killer.invoice_payment_user', raise_if_not_found=False)
        if payment_group:
            user.groups_id = [Command.link(payment_group.id)]
        self.credit_note.with_user(user).write(
            {'transaction_ids': [Command.link(self.refund.id)]})
        self.assertIn(self.refund, self.credit_note.transaction_ids)
        with self.assertRaises(UserError):
            self.credit_note.with_user(user).action_post()
        self.assertEqual(self.credit_note.state, 'draft')

    def test_the_exemption_cannot_be_reached_with_a_context_key(self):
        self.refund.invoice_ids = self.credit_note
        forged = self.credit_note.with_user(self.user_both).with_context(
            **{PAYMENT_REFUND_KEY: [self.credit_note.id]})
        with self.assertRaises(UserError):
            forged.action_post()
        self.assertEqual(self.credit_note.state, 'draft')

    def test_an_amount_that_does_not_match_is_gated(self):
        other = self._refund_tx(40.0)
        other.invoice_ids = self.credit_note
        with self.assertRaises(UserError):
            self.credit_note._bill_approval_post_payment_refunds()
        self.assertEqual(self.credit_note.state, 'draft')

    def test_a_refund_that_is_not_done_is_gated(self):
        pending = self._refund_tx(100.0, state='pending')
        pending.invoice_ids = self.credit_note
        with self.assertRaises(UserError):
            self.credit_note._bill_approval_post_payment_refunds()

    def test_a_refund_already_recorded_cannot_post_a_second_credit_note(self):
        self.refund.invoice_ids = self.credit_note
        self.credit_note._bill_approval_post_payment_refunds()
        second = self.invoice._reverse_moves([{}])
        self.refund.invoice_ids = [Command.link(second.id)]
        with self.assertRaises(UserError):
            second._bill_approval_post_payment_refunds()
        self.assertEqual(second.state, 'draft')

    def test_a_vendor_credit_note_is_never_exempt(self):
        bill = self._bill('in_invoice')
        self._approve_fully(bill)
        bill.action_post()
        vendor_note = bill._reverse_moves([{}])
        self.refund.invoice_ids = vendor_note
        with self.assertRaises(UserError):
            vendor_note._bill_approval_post_payment_refunds()
