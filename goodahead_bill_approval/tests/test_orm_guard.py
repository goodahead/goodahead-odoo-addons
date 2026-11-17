# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The approval fields cannot be written directly through the ORM.

`readonly=True` is only a client modifier, so without the guard any user who can
edit a bill could forge an approval. Each refusal is paired with a control: ordinary
edits, the actions, the refusal wizard, the import hook and self-approval still work.
"""

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .common import BillApprovalCommon


@tagged('post_install', '-at_install')
class TestTheOrmGuard(BillApprovalCommon):

    def test_a_user_in_neither_group_cannot_write_either_approval_field(self):
        bill = self._bill()
        for field_name, value in (
            ('approval_1_uid', self.user_none.id),
            ('approval_2_uid', self.user_none.id),
            ('approval_1_date', fields.Datetime.now()),
            ('approval_2_date', fields.Datetime.now()),
        ):
            with self.assertRaises(AccessError, msg=field_name):
                bill.with_user(self.user_none).write({field_name: value})
        bill.invalidate_recordset()
        self.assertFalse(bill.approval_1_uid)
        self.assertFalse(bill.approval_2_uid)
        self.assertEqual(bill.bill_approval_state, 'to_review')

    def test_the_control_the_same_user_can_still_edit_the_same_bill(self):
        """Control: the same user can still edit an ordinary field on the bill."""
        bill = self._bill()
        bill.with_user(self.user_none).write({'ref': 'BA-ORDINARY-EDIT'})
        self.assertEqual(bill.ref, 'BA-ORDINARY-EDIT')

    def test_holding_one_level_does_not_open_the_field_either(self):
        """Level holders must use the action too; a direct write would skip its checks."""
        bill = self._bill()
        with self.assertRaises(AccessError):
            bill.with_user(self.user_reviewer).write({
                'approval_1_uid': self.user_reviewer.id})
        with self.assertRaises(AccessError):
            bill.with_user(self.user_director).write({
                'approval_2_uid': self.user_director.id})
        bill.invalidate_recordset()
        self.assertEqual(bill.bill_approval_state, 'to_review')

    def test_a_user_cannot_forge_someone_elses_approval(self):
        """A user cannot write a step 2 approval without holding the level."""
        bill = self._bill()
        with self.assertRaises(AccessError):
            bill.with_user(self.user_reviewer).write({
                'approval_1_uid': self.user_reviewer.id,
                'approval_1_date': fields.Datetime.now(),
                'approval_2_uid': self.user_director.id,
                'approval_2_date': fields.Datetime.now(),
            })
        bill.invalidate_recordset()
        self.assertFalse(bill.approval_2_uid)

    def test_create_carrying_an_approval_field_is_refused(self):
        bill_vals = {
            'move_type': 'in_invoice',
            'journal_id': self.purchase_journal.id,
            'partner_id': self.vendor.id,
            'invoice_date': fields.Date.to_date('2026-10-01'),
            'approval_1_uid': self.user_none.id,
        }
        with self.assertRaises(AccessError):
            self.AM.with_user(self.user_none).create(bill_vals)

    def test_the_control_create_without_an_approval_field_still_works(self):
        """The guard must not make an ordinary bill uncreatable."""
        bill = self.AM.with_user(self.user_none).create({
            'move_type': 'in_invoice',
            'journal_id': self.purchase_journal.id,
            'partner_id': self.vendor.id,
            'invoice_date': fields.Date.to_date('2026-10-01'),
        })
        self.assertTrue(bill)
        self.assertEqual(bill.bill_approval_state, 'to_review')

    def test_the_two_actions_still_work_through_the_guard(self):
        bill = self._bill()
        bill.with_user(self.user_reviewer).action_bill_approval_1()
        self.assertEqual(bill.approval_1_uid, self.user_reviewer)
        bill.with_user(self.user_director).action_bill_approval_2()
        self.assertEqual(bill.approval_2_uid, self.user_director)
        self.assertEqual(bill.bill_approval_state, 'approved')

    def test_the_action_records_the_real_approver_not_odoobot(self):
        """The approver is captured before the `sudo()` write, so it is not OdooBot."""
        bill = self._bill()
        bill.with_user(self.user_reviewer).action_bill_approval_1()
        self.assertEqual(bill.approval_1_uid, self.user_reviewer)
        self.assertNotEqual(bill.approval_1_uid, self.env.ref('base.user_root'))

    def test_the_action_posts_a_note_authored_by_the_approver(self):
        """The approval note is authored by the approver, not by OdooBot."""
        bill = self._bill()
        bill.with_user(self.user_reviewer).action_bill_approval_1()
        notes = bill.message_ids.filtered(
            lambda m: 'Approval step 1 (Review) given by' in (m.body or ''))
        self.assertTrue(notes, "the approval must be chattered")
        self.assertEqual(notes[0].author_id, self.user_reviewer.partner_id)
        self.assertIn(self.user_reviewer.display_name, notes[0].body)

    def test_a_user_holding_both_levels_can_still_clear_both_steps(self):
        """The guard does not introduce separation of duties as a side effect."""
        bill = self._bill()
        bill.with_user(self.user_both).action_bill_approval_1()
        bill.with_user(self.user_both).action_bill_approval_2()
        self.assertEqual(bill.bill_approval_state, 'approved')
        self.assertEqual(bill.approval_1_uid, self.user_both)
        self.assertEqual(bill.approval_2_uid, self.user_both)
        bill.action_post()
        self.assertEqual(bill.state, 'posted')

    def test_the_refusal_wizard_still_works_through_the_guard(self):
        """The refusal wizard can still clear all four approval fields."""
        bill = self._bill()
        bill.with_user(self.user_reviewer).action_bill_approval_1()
        bill.with_user(self.user_director).action_bill_approval_2()
        wizard = self.env['bill.approval.refusal'].with_user(
            self.user_director).create({
                'move_id': bill.id,
                'reason': 'The freight line is billed twice.',
            })
        wizard.action_refuse()
        bill.invalidate_recordset()
        self.assertFalse(bill.approval_1_uid)
        self.assertFalse(bill.approval_2_uid)
        self.assertEqual(bill.bill_approval_state, 'to_review')

    def test_the_import_hook_still_works_through_the_guard(self):
        """The import hook grants step 1 even for a user with no level, since the
        import runs as whoever enqueued it."""
        bill = self._bill()
        granted = bill.with_user(self.user_none)._bill_approval_grant_step_1(
            note='a test harness')
        self.assertTrue(granted)
        bill.invalidate_recordset()
        self.assertEqual(bill.approval_1_uid, self.user_none)
        self.assertEqual(bill.bill_approval_state, 'reviewed')
        self.assertEqual(bill.state, 'draft', "the hook must never post")

    def test_superuser_may_still_write_the_fields(self):
        """Superuser may write the fields; unlike a context key, `env.su` cannot be
        set over RPC."""
        bill = self._bill()
        bill.write({'approval_1_uid': self.env.user.id})
        self.assertEqual(bill.approval_1_uid, self.env.user)

    def test_a_bill_with_one_approval_is_not_posted_by_anything(self):
        """No posting route, auto-post included, posts a bill with one approval."""
        self.vendor.autopost_bills = 'always'
        self.company.autopost_bills = True
        bill = self._bill()
        bill.with_user(self.user_reviewer).action_bill_approval_1()
        self.assertEqual(bill.bill_approval_state, 'reviewed')

        bill._autopost_bill()
        self.assertEqual(bill.state, 'draft')

        bill._bill_approval_try_autopost()
        self.assertEqual(bill.state, 'draft')

        with self.assertRaises(UserError):
            bill.action_post()
        self.assertEqual(bill.state, 'draft')
