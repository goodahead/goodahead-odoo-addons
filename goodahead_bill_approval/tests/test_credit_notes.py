# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Customer credit notes are gated, customer invoices are not, and a posted customer
invoice can still be cancelled by a full reversal.

`_reverse_moves(cancel=True)` creates and posts the credit note in one call, so that
exact credit note is let through; the same credit note posted any other way, including
via context keys any RPC can send, is still refused.
"""

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tools.safe_eval import safe_eval

from ..models.account_move import CANCEL_REVERSAL_KEY
from .common import BillApprovalCommon


@tagged('post_install', '-at_install')
class TestCustomerCreditNotes(BillApprovalCommon):

    def _posted_invoice(self):
        invoice = self._create_invoice(move_type='out_invoice', ref='BA-C-INVOICE')
        invoice.action_post()
        self.assertEqual(invoice.state, 'posted', "the control: the invoice posted")
        return invoice

    def _reversal_wizard(self, moves):
        return self.env['account.move.reversal'].with_context(
            active_model='account.move', active_ids=moves.ids,
        ).create({'journal_id': moves[0].journal_id.id})

    def test_a_customer_credit_note_is_gated(self):
        refund = self._create_invoice(move_type='out_refund')
        self.assertTrue(refund._bill_approval_required())
        with self.assertRaisesRegex(UserError, 'step 1, Review'):
            refund.action_post()
        self.assertEqual(refund.state, 'draft')

    def test_the_control_an_approved_credit_note_posts(self):
        refund = self._create_invoice(move_type='out_refund')
        refund.with_user(self.user_reviewer).action_bill_approval_1()
        refund.with_user(self.user_director).action_bill_approval_2()
        self.assertEqual(refund.bill_approval_state, 'approved')
        refund.action_post()
        self.assertEqual(refund.state, 'posted')

    def test_a_customer_invoice_is_still_not_gated(self):
        invoice = self._create_invoice(move_type='out_invoice')
        self.assertFalse(invoice._bill_approval_required())
        invoice.action_post()
        self.assertEqual(invoice.state, 'posted')

    def test_a_credit_note_lands_in_the_credit_note_queue_not_the_bill_one(self):
        refund = self._create_invoice(move_type='out_refund')
        cn_queue = self.env.ref('goodahead_bill_approval.action_credit_notes_awaiting_review')
        bill_queue = self.env.ref('goodahead_bill_approval.action_bills_awaiting_review')
        self.assertIn(
            refund, self.AM.search([('bill_approval_state', '=', 'to_review'),
                                    ('state', '=', 'draft')] + safe_eval(cn_queue.domain)))
        self.assertNotIn(
            refund, self.AM.search([('bill_approval_state', '=', 'to_review'),
                                    ('state', '=', 'draft')] + safe_eval(bill_queue.domain)))

    def test_the_credit_note_post_task_is_worded_for_a_credit_note(self):
        refund = self._create_invoice(move_type='out_refund')
        refund.with_user(self.user_reviewer).action_bill_approval_1()
        refund.with_user(self.user_director).action_bill_approval_2()
        task = refund.activity_ids.filtered(
            lambda a: a.activity_type_id == self.env.ref(
                'goodahead_bill_approval.mail_activity_type_bill_to_post'))
        self.assertTrue(task, "the bookkeeper is told to post it, as for a bill")
        self.assertIn('credit note', task.summary)
        self.assertIn('Credit Notes to Post', task.note)

    def test_reverse_and_create_invoice_still_cancels_a_posted_invoice(self):
        """A `cancel=True` reversal of a customer invoice still posts."""
        invoice = self._posted_invoice()
        self._reversal_wizard(invoice).modify_moves()
        refund = invoice.reversal_move_ids
        self.assertEqual(len(refund), 1)
        self.assertEqual(refund.move_type, 'out_refund')
        self.assertEqual(refund.state, 'posted', "the cancelling credit note posted")
        self.assertFalse(refund.approval_1_uid, "and nobody approved it, truthfully")
        self.assertIn(invoice.payment_state, ('reversed', 'paid'),
                      "and it really cancelled the invoice")
        self.assertIn(
            'cancels', ' '.join(refund.message_ids.mapped('body')),
            "the credit note says in words why it carries no approval",
        )

    def test_reverse_and_create_invoice_on_a_vendor_bill_stays_gated(self):
        """The cancel-reversal exemption does not apply to vendor bills."""
        bill = self._approve_fully(self._bill())
        bill.action_post()
        self.assertEqual(bill.state, 'posted', "the control: the bill posted")
        with self.assertRaisesRegex(UserError, 'step 1, Review'):
            self._reversal_wizard(bill).modify_moves()
        # The wizard's draft copy may remain in a test (no request rollback); nothing is posted.
        self.env.invalidate_all()
        self.assertFalse(
            bill.reversal_move_ids.filtered(lambda m: m.state == 'posted'),
            "no vendor credit note was posted",
        )
        self.assertEqual(bill.state, 'posted')
        self.assertNotEqual(bill.payment_state, 'reversed', "the bill was not cancelled")

    def test_a_direct_cancel_reversal_of_a_vendor_bill_is_refused_too(self):
        """`_reverse_moves(cancel=True)` called from code on a vendor bill is refused."""
        bill = self._approve_fully(self._bill())
        bill.action_post()
        with self.assertRaises(UserError):
            bill._reverse_moves(cancel=True)
        self.assertEqual(bill.state, 'posted')

    def test_a_partial_credit_note_from_reverse_is_a_draft_and_is_gated(self):
        """A `cancel=False` reversal makes a draft credit note that needs both steps."""
        invoice = self._posted_invoice()
        self._reversal_wizard(invoice).refund_moves()
        refund = invoice.reversal_move_ids
        self.assertEqual(refund.state, 'draft')
        with self.assertRaises(UserError):
            refund.action_post()
        self.assertEqual(refund.state, 'draft')

    def test_the_exemption_cannot_be_reached_with_a_context_key(self):
        """Neither core's `move_reverse_cancel` key nor this module's key as a plain
        list grants the exemption."""
        invoice = self._posted_invoice()
        self._reversal_wizard(invoice).refund_moves()
        refund = invoice.reversal_move_ids
        with self.assertRaises(UserError):
            refund.with_context(move_reverse_cancel=True).action_post()
        with self.assertRaises(UserError):
            refund.with_context(**{CANCEL_REVERSAL_KEY: [invoice.id]}).action_post()
        self.assertEqual(refund.state, 'draft')

    def test_the_cancelling_credit_note_does_not_carry_the_exemption_onwards(self):
        """The returned credit note carries no exemption token, so re-posting it is gated."""
        invoice = self._posted_invoice()
        refund = invoice._reverse_moves(cancel=True)
        self.assertEqual(refund.state, 'posted')
        self.assertNotIn(CANCEL_REVERSAL_KEY, refund.env.context)
        refund.button_draft()
        self.assertEqual(refund.state, 'draft')
        with self.assertRaisesRegex(UserError, 'step 1, Review'):
            refund.action_post()
        self.assertEqual(refund.state, 'draft')

    def test_a_partial_cancel_reversal_is_refused_and_says_why(self):
        """A `cancel=True` reversal with changed lines (a partial credit note) is gated."""
        invoice = self._posted_invoice()
        with self.assertRaisesRegex(UserError, 'only in part') as caught:
            invoice._reverse_moves([{
                'invoice_line_ids': [Command.create({
                    'name': 'BA-C partial',
                    'quantity': 1.0,
                    'price_unit': 10.0,
                    'account_id': self.income_account.id,
                    'tax_ids': [Command.set([])],
                })],
            }], cancel=True)
        self.assertIn('both approvals', str(caught.exception))
        self.env.invalidate_all()
        self.assertFalse(
            invoice.reversal_move_ids.filtered(lambda m: m.state == 'posted'),
            "no partial credit note reached the ledger",
        )
        self.assertNotEqual(invoice.payment_state, 'reversed')

    def test_the_mirror_check_reads_amounts_and_accounts(self):
        """A full-reversal check compares amounts and accounts, not only the total."""
        invoice = self._posted_invoice()
        untouched = invoice._reverse_moves()
        self.assertTrue(untouched._bill_approval_is_full_reversal(),
                        "the control: an untouched reversal is a full mirror")

        moved = invoice._reverse_moves()
        moved.invoice_line_ids.account_id = self.expense_account
        self.assertEqual(moved.amount_total, invoice.amount_total,
                         "the premise: the total is unchanged")
        self.assertFalse(moved._bill_approval_is_full_reversal(),
                         "the same total on another account is not a mirror")

        smaller = invoice._reverse_moves()
        smaller.invoice_line_ids.price_unit = 10.0
        self.assertFalse(smaller._bill_approval_is_full_reversal())
