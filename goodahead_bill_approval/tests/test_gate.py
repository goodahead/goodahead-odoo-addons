# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The gate: a vendor bill does not reach the ledger without both approvals.

Every refusal has a control that posts, so code that refuses everything cannot pass.
"""

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import BillApprovalCommon


@tagged('post_install', '-at_install')
class TestTheGate(BillApprovalCommon):

    def test_a_fresh_bill_cannot_be_posted(self):
        bill = self._bill()
        self.assertEqual(bill.bill_approval_state, 'to_review')
        with self.assertRaisesRegex(UserError, 'step 1, Review'):
            bill.action_post()
        self.assertEqual(bill.state, 'draft', "nothing may have reached the ledger")

    def test_step_1_alone_is_not_enough(self):
        bill = self._bill()
        bill.with_user(self.user_reviewer).action_bill_approval_1()
        self.assertEqual(bill.bill_approval_state, 'reviewed')
        with self.assertRaisesRegex(UserError, 'step 2, Approve'):
            bill.action_post()
        self.assertEqual(bill.state, 'draft')

    def test_the_control_both_approvals_and_it_posts(self):
        """Control: with both approvals the bill posts."""
        bill = self._bill()
        bill.with_user(self.user_reviewer).action_bill_approval_1()
        bill.with_user(self.user_director).action_bill_approval_2()
        self.assertEqual(bill.bill_approval_state, 'approved')
        bill.action_post()
        self.assertEqual(bill.state, 'posted')

    def test_the_message_names_the_group_that_gives_the_missing_level(self):
        """A refusal that does not name the way out reads as a broken button."""
        bill = self._bill()
        with self.assertRaises(UserError) as caught:
            bill.action_post()
        message = str(caught.exception)
        self.assertIn('Bills: may review (step 1)', message)
        self.assertIn(bill.display_name, message)

    def test_a_vendor_credit_note_is_in_scope(self):
        refund = self._bill(move_type='in_refund')
        with self.assertRaises(UserError):
            refund.action_post()
        self.assertEqual(refund.state, 'draft')
        self._approve_fully(refund)
        refund.action_post()
        self.assertEqual(refund.state, 'posted')

    def test_a_customer_invoice_is_out_of_scope(self):
        """The gate covers vendor bills only, not every move."""
        invoice = self._create_invoice(move_type='out_invoice')
        self.assertFalse(invoice._bill_approval_required())
        invoice.action_post()
        self.assertEqual(invoice.state, 'posted')

    def test_a_journal_entry_is_out_of_scope(self):
        entry = self.AM.create({
            'move_type': 'entry',
            'journal_id': self.misc_journal.id,
            'date': fields.Date.to_date('2026-10-01'),
            'line_ids': [
                (0, 0, {
                    'name': 'debit',
                    'account_id': self.expense_account.id,
                    'debit': 100.0,
                }),
                (0, 0, {
                    'name': 'credit',
                    'account_id': self.income_account.id,
                    'credit': 100.0,
                }),
            ],
        })
        self.assertFalse(entry._bill_approval_required())
        entry.action_post()
        self.assertEqual(entry.state, 'posted')

    def test_a_batch_naming_every_blocked_bill(self):
        """A batch refusal names every blocked bill and not the approved one.

        Each bill gets a distinct `ref` because unnumbered drafts all display as
        "Draft Bill".
        """
        one = self._create_invoice(move_type='in_invoice', ref='BA-BATCH-ONE')
        two = self._create_invoice(move_type='in_invoice', ref='BA-BATCH-TWO')
        self._approve_fully(two)
        three = self._create_invoice(move_type='in_invoice', ref='BA-BATCH-THREE')
        self.assertEqual(
            len({one.display_name, two.display_name, three.display_name}), 3,
            "the three bills must be distinguishable by name for this test to "
            "mean anything",
        )
        with self.assertRaises(UserError) as caught:
            (one + two + three).action_post()
        message = str(caught.exception)
        self.assertIn(one.display_name, message)
        self.assertIn(three.display_name, message)
        self.assertNotIn(two.display_name, message)
        self.assertEqual(set((one + two + three).mapped('state')), {'draft'})

    def test_approvals_survive_an_edit_after_approval(self):
        """Approvals are deliberately not cleared on edit; the chatter tracks them."""
        bill = self._bill()
        self._approve_fully(bill)
        bill.invoice_line_ids[0].price_unit = 999.0
        self.assertEqual(bill.bill_approval_state, 'approved')
        bill.action_post()
        self.assertEqual(bill.state, 'posted')
