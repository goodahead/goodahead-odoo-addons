# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The review dialog: step 1 asks who should give step 2.

Every call goes through `with_user(...)` because `env.su` passes the level checks.
"""

from lxml import etree

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import Form, tagged

from .common import GROUP_2, BillApprovalCommon

REVIEW = 'bill.approval.review'


@tagged('post_install', '-at_install')
class TestReviewDialog(BillApprovalCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other_director = cls.env['res.users'].create({
            'name': 'BA-D second director',
            'login': 'ba_d_director_2',
            'company_id': cls.company.id,
            'company_ids': [Command.set([cls.company.id])],
            'group_ids': [
                Command.link(cls.env.ref('base.group_user').id),
                Command.link(cls.env.ref('account.group_account_invoice').id),
                Command.link(cls.env.ref(GROUP_2).id),
            ],
        })

    def _dialog(self, moves, user=None, **values):
        """The dialog as the button opens it: the action's context, then create."""
        user = user or self.user_reviewer
        action = moves.with_user(user).action_bill_approval_1_dialog()
        self.assertEqual(action['res_model'], REVIEW)
        model = self.env[REVIEW].with_user(user).with_context(**action['context'])
        defaults = model.default_get(['move_ids', 'approver_id'])
        defaults.update(values)
        return model.create(defaults), defaults

    def test_the_button_opens_the_dialog_instead_of_reviewing(self):
        bill = self._bill()
        action = bill.with_user(self.user_reviewer).action_bill_approval_1_dialog()
        self.assertEqual(action['res_model'], REVIEW)
        self.assertFalse(bill.approval_1_uid, "nothing is given until Confirm")

    def test_the_form_button_calls_the_dialog(self):
        tree = etree.fromstring(
            self.env.ref('goodahead_bill_approval.view_move_form_bill_approval').arch)
        node = tree.xpath("//button[contains(@class, 'o_bill_approval_btn_1')]")
        self.assertEqual(node[0].get('name'), 'action_bill_approval_1_dialog')

    def test_the_default_is_the_current_approver(self):
        bill = self._bill()
        bill.with_user(self.user_reviewer).write({
            'bill_approval_approver_id': self.user_director.id})
        _wizard, defaults = self._dialog(bill)
        self.assertEqual(defaults.get('approver_id'), self.user_director.id)

    def test_confirm_with_a_person_gives_step_1_and_saves_the_approver(self):
        bill = self._bill()
        wizard, _defaults = self._dialog(bill, approver_id=self.user_director.id)
        wizard.action_confirm()
        self.assertEqual(bill.approval_1_uid, self.user_reviewer)
        self.assertEqual(bill.bill_approval_approver_id, self.user_director)
        note = bill.message_ids.filtered(
            lambda m: 'Approval step 1 (Review) given by' in (m.body or ''))
        self.assertIn(self.user_director.display_name, note[0].body,
                      "the step 1 note names the chosen approver")
        tasks = bill.activity_ids.filtered(
            lambda a: a.activity_type_id == self.env.ref(
                'goodahead_bill_approval.mail_activity_type_bill_to_approve'))
        self.assertEqual(tasks.user_id, self.user_director,
                         "and the chosen approver gets their task")

    def test_confirm_empty_leaves_the_approver_empty(self):
        bill = self._bill()
        wizard, _defaults = self._dialog(bill)
        self.assertFalse(wizard.approver_id)
        wizard.action_confirm()
        self.assertEqual(bill.approval_1_uid, self.user_reviewer)
        self.assertFalse(bill.bill_approval_approver_id)

    def test_confirm_empty_clears_an_approver_that_was_set(self):
        """The dialog is the choice: emptying the pre-filled value means anyone."""
        bill = self._bill()
        bill.with_user(self.user_reviewer).write({
            'bill_approval_approver_id': self.user_director.id})
        wizard, _defaults = self._dialog(bill, approver_id=False)
        wizard.action_confirm()
        self.assertFalse(bill.bill_approval_approver_id)
        self.assertTrue(bill.approval_1_uid)

    def test_a_non_reviewer_cannot_use_the_dialog(self):
        bill = self._bill()
        for user in (self.user_none, self.user_director):
            with self.assertRaises(AccessError, msg=user.name):
                bill.with_user(user).action_bill_approval_1_dialog()
            with self.assertRaises(AccessError, msg=user.name):
                self.env[REVIEW].with_user(user).create({
                    'move_ids': [Command.set(bill.ids)],
                    'approver_id': self.user_director.id,
                })
        bill.invalidate_recordset()
        self.assertFalse(bill.approval_1_uid)
        self.assertFalse(bill.bill_approval_approver_id)

    def test_the_confirm_checks_the_level_again(self):
        """Confirm re-checks the level, so a dialog confirmed after losing it gives nothing."""
        bill = self._bill()
        wizard, _defaults = self._dialog(bill, approver_id=self.user_director.id)
        self.user_reviewer.bill_approval_may_review = False
        with self.assertRaises(AccessError):
            wizard.action_confirm()
        bill.invalidate_recordset()
        self.assertFalse(bill.approval_1_uid)
        self.assertFalse(bill.bill_approval_approver_id)

    def test_several_documents_get_one_choice(self):
        first = self._bill(ref='BA-D-ONE')
        second = self._bill(move_type='in_refund', ref='BA-D-TWO')
        first.with_user(self.user_reviewer).write({
            'bill_approval_approver_id': self.user_director.id})
        _wizard, defaults = self._dialog(first | second)
        self.assertFalse(
            defaults.get('approver_id'),
            "no default when the selection does not share one approver",
        )
        wizard, _defaults = self._dialog(first | second, approver_id=self.other_director.id)
        wizard.action_confirm()
        for move in (first, second):
            self.assertEqual(move.approval_1_uid, self.user_reviewer)
            self.assertEqual(move.bill_approval_approver_id, self.other_director)

    def test_the_server_route_still_reviews_without_a_dialog(self):
        bill = self._bill()
        bill.with_user(self.user_reviewer).action_bill_approval_1()
        self.assertEqual(bill.approval_1_uid, self.user_reviewer)

    def test_the_dialog_works_through_the_form_like_the_browser(self):
        """Through `odoo.tests.Form`, which, like the browser, drops readonly fields on
        save unless they carry `force_save`."""
        bill = self._bill()
        action = bill.with_user(self.user_reviewer).action_bill_approval_1_dialog()
        with Form(self.env[REVIEW].with_user(self.user_reviewer).with_context(
                **action['context'])) as dialog:
            dialog.approver_id = self.user_director
        wizard = dialog.record
        self.assertEqual(wizard.move_ids, bill, "the documents survived the save")
        wizard.action_confirm()
        self.assertEqual(bill.approval_1_uid, self.user_reviewer)
        self.assertEqual(bill.bill_approval_approver_id, self.user_director)

    def test_a_mixed_selection_is_refused_and_names_the_reviewed_ones(self):
        fresh = self._bill(ref='BA-D-FRESH')
        reviewed = self._bill(ref='BA-D-DONE')
        reviewed.with_user(self.user_reviewer).action_bill_approval_1()
        wizard, _defaults = self._dialog(fresh | reviewed, approver_id=self.user_director.id)
        with self.assertRaises(UserError) as caught:
            wizard.action_confirm()
        self.assertIn(reviewed.display_name, str(caught.exception))
        self.assertNotIn(fresh.display_name, str(caught.exception))
        fresh.invalidate_recordset()
        self.assertFalse(fresh.approval_1_uid, "nothing was half-done")
        self.assertFalse(fresh.bill_approval_approver_id)

    def test_the_step_1_button_reads_send_for_review_everywhere(self):
        """The step 1 button reads "Send for Review" on the form, in the dialog and in
        the list's Action menu."""
        form = etree.fromstring(
            self.env.ref('goodahead_bill_approval.view_move_form_bill_approval').arch)
        self.assertEqual(
            form.xpath("//button[contains(@class, 'o_bill_approval_btn_1')]")[0].get('string'),
            'Send for Review')
        dialog = etree.fromstring(
            self.env.ref('goodahead_bill_approval.view_bill_approval_review_form').arch)
        self.assertEqual(
            dialog.xpath("//button[@name='action_confirm']")[0].get('string'),
            'Send for Review')
        self.assertEqual(
            self.env.ref('goodahead_bill_approval.action_bill_approval_review').name,
            'Send for Review')

    def test_the_list_action_is_bound_and_gated(self):
        action = self.env.ref('goodahead_bill_approval.action_bill_approval_review')
        self.assertEqual(action.binding_model_id.model, 'account.move')
        self.assertEqual(action.binding_view_types, 'list')
        self.assertEqual(
            action.group_ids, self.env.ref('goodahead_bill_approval.group_bill_approval_1'))
