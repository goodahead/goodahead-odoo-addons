# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Who may give which level, exercised through the actions and not only the groups.

A `groups=` attribute only hides a button; the method can still be called by RPC,
so the permission is asserted where the write happens.
"""

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import GROUP_1, GROUP_2, BillApprovalCommon


@tagged('post_install', '-at_install')
class TestLevels(BillApprovalCommon):

    def test_a_user_with_no_level_cannot_review(self):
        bill = self._bill()
        with self.assertRaises(AccessError):
            bill.with_user(self.user_none).action_bill_approval_1()
        self.assertFalse(bill.approval_1_uid)

    def test_a_user_with_no_level_cannot_approve(self):
        bill = self._bill()
        bill.with_user(self.user_reviewer).action_bill_approval_1()
        with self.assertRaises(AccessError):
            bill.with_user(self.user_none).action_bill_approval_2()
        self.assertFalse(bill.approval_2_uid)

    def test_the_reviewer_cannot_give_step_2(self):
        """Holding step 1 does not imply step 2."""
        bill = self._bill()
        bill.with_user(self.user_reviewer).action_bill_approval_1()
        with self.assertRaises(AccessError):
            bill.with_user(self.user_reviewer).action_bill_approval_2()
        self.assertFalse(bill.approval_2_uid)

    def test_the_director_cannot_give_step_1(self):
        bill = self._bill()
        with self.assertRaises(AccessError):
            bill.with_user(self.user_director).action_bill_approval_1()
        self.assertFalse(bill.approval_1_uid)

    def test_the_control_the_holder_of_each_level_can_give_it(self):
        """Control: the holder of each level can give it."""
        bill = self._bill()
        bill.with_user(self.user_reviewer).action_bill_approval_1()
        self.assertEqual(bill.approval_1_uid, self.user_reviewer)
        self.assertTrue(bill.approval_1_date)
        bill.with_user(self.user_director).action_bill_approval_2()
        self.assertEqual(bill.approval_2_uid, self.user_director)
        self.assertTrue(bill.approval_2_date)
        self.assertEqual(bill.bill_approval_state, 'approved')

    def test_step_2_requires_step_1_first(self):
        bill = self._bill()
        with self.assertRaisesRegex(UserError, 'step 2 follows step 1'):
            bill.with_user(self.user_director).action_bill_approval_2()
        self.assertFalse(bill.approval_2_uid)

    def test_self_approval_is_allowed_and_visible(self):
        """A user holding both levels may clear both steps; this is by design and is
        visible because both approval fields carry the same user."""
        bill = self._bill()
        bill.with_user(self.user_both).action_bill_approval_1()
        bill.with_user(self.user_both).action_bill_approval_2()
        self.assertEqual(bill.bill_approval_state, 'approved')
        self.assertEqual(bill.approval_1_uid, self.user_both)
        self.assertEqual(bill.approval_2_uid, self.user_both)
        self.assertEqual(
            bill.approval_1_uid, bill.approval_2_uid,
            "the two fields carry the same user, which is what makes a "
            "self-approved bill readable without the chatter",
        )
        bill.action_post()
        self.assertEqual(bill.state, 'posted')

    def test_approval_is_refused_on_a_posted_bill(self):
        bill = self._bill()
        self._approve_fully(bill)
        bill.action_post()
        with self.assertRaisesRegex(UserError, 'already posted'):
            bill.with_user(self.user_reviewer).action_bill_approval_1()

    def test_approval_is_refused_on_a_customer_invoice(self):
        invoice = self._create_invoice(move_type='out_invoice')
        with self.assertRaisesRegex(UserError, 'does not go through approval'):
            invoice.with_user(self.user_reviewer).action_bill_approval_1()

    def test_approving_twice_does_not_rewrite_who_and_when(self):
        bill = self._bill()
        bill.with_user(self.user_reviewer).action_bill_approval_1()
        first = bill.approval_1_date
        bill.with_user(self.user_both).action_bill_approval_1()
        self.assertEqual(bill.approval_1_uid, self.user_reviewer)
        self.assertEqual(bill.approval_1_date, first)

    def test_the_mirror_field_follows_the_group(self):
        self.assertTrue(self.user_reviewer.bill_approval_may_review)
        self.assertFalse(self.user_reviewer.bill_approval_may_approve)
        self.assertFalse(self.user_director.bill_approval_may_review)
        self.assertTrue(self.user_director.bill_approval_may_approve)
        self.assertFalse(self.user_none.bill_approval_may_review)
        self.assertFalse(self.user_none.bill_approval_may_approve)

    def test_setting_the_mirror_field_grants_the_group(self):
        self.user_none.bill_approval_may_review = True
        self.assertTrue(self.user_none._has_group(GROUP_1))
        self.user_none.bill_approval_may_approve = True
        self.assertTrue(self.user_none._has_group(GROUP_2))

    def test_clearing_the_mirror_field_removes_the_group(self):
        self.user_both.bill_approval_may_review = False
        self.assertFalse(self.user_both._has_group(GROUP_1))
        self.assertTrue(
            self.user_both._has_group(GROUP_2),
            "the other half must not be cleared with it",
        )

    def test_writing_a_set_of_users_at_once_and_then_one_of_them(self):
        """Writing the mirror field on several users does not affect other members."""
        users = self.user_reviewer + self.user_none
        users.write({'bill_approval_may_review': True})
        self.assertTrue(self.user_none._has_group(GROUP_1))
        self.assertTrue(self.user_reviewer._has_group(GROUP_1))
        self.user_reviewer.bill_approval_may_review = False
        self.assertFalse(self.user_reviewer._has_group(GROUP_1))
        self.assertTrue(
            self.user_none._has_group(GROUP_1),
            "clearing one user must not clear the other",
        )

    def test_the_groups_have_categories_of_their_own(self):
        """Each level has its own category: on 18.0 that is what puts it on the user form
        outside developer mode (see `test_user_form.py`)."""
        group_1 = self.env.ref(GROUP_1)
        group_2 = self.env.ref(GROUP_2)
        self.assertTrue(group_1.category_id)
        self.assertTrue(group_2.category_id)
        self.assertNotEqual(group_1.category_id, group_2.category_id)

    def test_neither_group_implies_the_other(self):
        group_1 = self.env.ref(GROUP_1)
        group_2 = self.env.ref(GROUP_2)
        self.assertNotIn(group_1, group_2.implied_ids)
        self.assertNotIn(group_2, group_1.implied_ids)
