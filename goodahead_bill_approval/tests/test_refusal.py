# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Refusal: the bill goes back to step 1 with a required reason.

A refusal clears both approvals and posts the reason in the chatter under the
refuser's name; there is no Refused state and nothing is cancelled.
"""

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import BillApprovalCommon


@tagged('post_install', '-at_install')
class TestRefusal(BillApprovalCommon):

    def _wizard_for(self, bill, user=None, reason='placeholder'):
        """Create a refusal wizard; `reason` is required, so one is always passed."""
        model = self.env['bill.approval.refusal']
        if user:
            model = model.with_user(user)
        return model.with_context(
            active_model='account.move', active_id=bill.id).create({
                'move_id': bill.id,
                'reason': reason,
            })

    def test_a_wizard_with_no_reason_cannot_even_be_created(self):
        """The NOT NULL column blocks a reasonless refusal from any route."""
        bill = self._bill()
        with self.assertRaises(Exception):
            self.env['bill.approval.refusal'].create({'move_id': bill.id})

    def test_a_refusal_with_no_reason_is_refused(self):
        bill = self._bill()
        bill.with_user(self.user_reviewer).action_bill_approval_1()
        wizard = self._wizard_for(bill)
        # `required=True` does not stop a whitespace-only reason; the server check does.
        wizard.reason = '   '
        with self.assertRaisesRegex(UserError, 'needs a reason'):
            wizard.action_refuse()
        self.assertEqual(
            bill.bill_approval_state, 'reviewed',
            "a refused refusal must change nothing",
        )

    def test_the_control_a_refusal_with_a_reason_goes_through(self):
        bill = self._bill()
        bill.with_user(self.user_reviewer).action_bill_approval_1()
        bill.with_user(self.user_director).action_bill_approval_2()
        self.assertEqual(bill.bill_approval_state, 'approved')

        wizard = self._wizard_for(bill, user=self.user_director)
        wizard.reason = 'The freight line is billed twice.'
        wizard.action_refuse()

        self.assertFalse(bill.approval_1_uid)
        self.assertFalse(bill.approval_1_date)
        self.assertFalse(bill.approval_2_uid)
        self.assertFalse(bill.approval_2_date)
        self.assertEqual(
            bill.bill_approval_state, 'to_review',
            "the bill is back in the STEP 1 queue — the bookkeeper's queue, "
            "which is what 'returns to the bookkeeper' means",
        )
        self.assertEqual(bill.state, 'draft', "nothing is cancelled")

    def test_the_reason_and_the_refuser_are_in_the_chatter(self):
        bill = self._bill()
        bill.with_user(self.user_reviewer).action_bill_approval_1()
        wizard = self._wizard_for(bill, user=self.user_director)
        wizard.reason = 'The PO number on page 2 is not ours.'
        wizard.action_refuse()

        bodies = bill.message_ids.mapped('body')
        hit = [b for b in bodies if 'page 2 is not ours' in b]
        self.assertTrue(hit, "the reason must be in the chatter: %s" % bodies)
        self.assertIn(
            self.user_director.display_name, hit[0],
            "a reversal of a control is chattered NAMING THE USER — the field "
            "says a control was cleared, it does not say by whom",
        )

    def test_a_refused_bill_still_cannot_be_posted(self):
        bill = self._bill()
        self._approve_fully(bill)
        wizard = self._wizard_for(bill)
        wizard.reason = 'Wrong vendor.'
        wizard.action_refuse()
        with self.assertRaises(UserError):
            bill.action_post()
        self.assertEqual(bill.state, 'draft')

    def test_a_user_with_no_level_cannot_refuse(self):
        bill = self._bill()
        bill.with_user(self.user_reviewer).action_bill_approval_1()
        with self.assertRaises(AccessError):
            bill.with_user(self.user_none).action_bill_approval_refuse()

    def test_either_level_may_refuse(self):
        """Both the step 1 and the step 2 user may refuse."""
        for user in (self.user_reviewer, self.user_director):
            bill = self._bill()
            bill.with_user(self.user_reviewer).action_bill_approval_1()
            action = bill.with_user(user).action_bill_approval_refuse()
            self.assertEqual(action['res_model'], 'bill.approval.refusal')
            self.assertEqual(action['context']['default_move_id'], bill.id)

    def test_the_button_refuses_a_posted_bill(self):
        bill = self._bill()
        self._approve_fully(bill)
        bill.action_post()
        with self.assertRaisesRegex(UserError, 'already posted'):
            bill.with_user(self.user_director).action_bill_approval_refuse()

    def test_the_wizard_picks_the_bill_up_from_the_context(self):
        """The wizard reads the bill from `active_id` as well as `default_move_id`."""
        bill = self._bill()
        values = self.env['bill.approval.refusal'].with_context(
            active_model='account.move', active_id=bill.id,
        ).default_get(['move_id', 'reason'])
        self.assertEqual(values.get('move_id'), bill.id)
