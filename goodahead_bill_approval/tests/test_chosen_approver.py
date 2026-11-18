# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The chosen approver, the substitute and "on behalf of".

A document may name one step 2 approver (empty means anyone with step 2); that user
may name a substitute (one hop only), whose approval is recorded "on behalf of". Either
level may choose the approver while step 2 is not given. Refusals are paired with
calls that must still work, all through `with_user(...)`.
"""

from lxml import etree

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import tagged
from odoo.tools.safe_eval import safe_eval

from ..models.account_move import APPROVE_ACTIVITY_TYPE_XMLID
from .common import GROUP_2, BillApprovalCommon


@tagged('post_install', '-at_install')
class TestChosenApprover(BillApprovalCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        base_groups = [
            Command.link(cls.env.ref('base.group_user').id),
            Command.link(cls.env.ref('account.group_account_invoice').id),
            Command.link(cls.env.ref('account.group_account_user').id),
            Command.link(cls.env.ref(GROUP_2).id),
        ]

        def approver(name, login):
            return cls.env['res.users'].create({
                'name': name,
                'login': login,
                'email': '%s@example.com' % login,
                'company_id': cls.company.id,
                'company_ids': [Command.set([cls.company.id])],
                'groups_id': base_groups,
            })

        cls.other_director = approver('BA-C second director', 'ba_c_director_2')
        cls.substitute = approver('BA-C substitute', 'ba_c_substitute')
        cls.substitute_of_substitute = approver('BA-C substitute of substitute', 'ba_c_sub_sub')
        cls.user_director.bill_approval_substitute_id = cls.substitute
        cls.substitute.bill_approval_substitute_id = cls.substitute_of_substitute

    def _reviewed(self, approver=None, move_type='in_invoice'):
        move = self._bill(move_type=move_type)
        if approver:
            move.with_user(self.user_reviewer).write({'bill_approval_approver_id': approver.id})
        move.with_user(self.user_reviewer).action_bill_approval_1()
        return move

    def _approve_tasks(self, move):
        """The approve tasks on this move, read straight out of `mail.activity`."""
        return self.env['mail.activity'].sudo().search([
            ('res_model', '=', 'account.move'),
            ('res_id', '=', move.id),
            ('activity_type_id', '=', self.env.ref(APPROVE_ACTIVITY_TYPE_XMLID).id),
        ])

    def _refuse(self, move, user=None):
        self.env['bill.approval.refusal'].with_user(user or self.user_director).create({
            'move_id': move.id,
            'reason': 'Wrong amount.',
        }).action_refuse()
        move.invalidate_recordset()

    def test_the_chosen_approver_may_give_step_2(self):
        move = self._reviewed(approver=self.user_director)
        move.with_user(self.user_director).action_bill_approval_2()
        self.assertEqual(move.approval_2_uid, self.user_director)
        self.assertFalse(
            move.approval_2_on_behalf_uid,
            "an approval the chosen approver gave in person is not 'on behalf of' anybody",
        )
        self.assertEqual(move.bill_approval_state, 'approved')

    def test_another_step_2_holder_is_refused_and_told_who_can(self):
        move = self._reviewed(approver=self.user_director)
        with self.assertRaises(AccessError) as caught:
            move.with_user(self.other_director).action_bill_approval_2()
        message = str(caught.exception)
        self.assertIn(self.user_director.display_name, message,
                      "the refusal must name who can approve")
        self.assertIn(self.substitute.display_name, message,
                      "and their substitute, the other way out")
        move.invalidate_recordset()
        self.assertFalse(move.approval_2_uid)
        self.assertTrue(
            self.other_director._has_group(GROUP_2),
            "the control on the premise: the refused user DOES hold the level, so only "
            "the chosen approver can be what refused them",
        )

    def test_the_control_an_empty_approver_lets_anyone_holding_step_2_approve(self):
        """With no chosen approver, anyone holding step 2 may approve."""
        move = self._reviewed()
        move.with_user(self.other_director).action_bill_approval_2()
        self.assertEqual(move.approval_2_uid, self.other_director)

    def test_the_substitute_may_approve_and_on_behalf_is_recorded(self):
        move = self._reviewed(approver=self.user_director)
        move.with_user(self.substitute).action_bill_approval_2()
        self.assertEqual(move.approval_2_uid, self.substitute)
        self.assertEqual(move.approval_2_on_behalf_uid, self.user_director)
        notes = move.message_ids.filtered(
            lambda m: 'Approval step 2 (Approve) given by' in (m.body or ''))
        self.assertTrue(notes, "step 2 must be chattered")
        self.assertIn('on behalf of', notes[0].body)
        self.assertIn(self.user_director.display_name, notes[0].body)
        self.assertEqual(notes[0].author_id, self.substitute.partner_id)

    def test_the_substitutes_own_substitute_is_refused(self):
        """Substitution is one hop: the substitute's substitute may not approve."""
        self.assertEqual(
            self.substitute.bill_approval_substitute_id, self.substitute_of_substitute,
            "the control on the premise: the chain really is two hops long",
        )
        move = self._reviewed(approver=self.user_director)
        with self.assertRaises(AccessError):
            move.with_user(self.substitute_of_substitute).action_bill_approval_2()
        move.invalidate_recordset()
        self.assertFalse(move.approval_2_uid)

    def test_a_refusal_clears_on_behalf_and_keeps_the_chosen_approver(self):
        move = self._reviewed(approver=self.user_director)
        move.with_user(self.substitute).action_bill_approval_2()
        self.assertTrue(move.approval_2_on_behalf_uid)
        self._refuse(move)
        self.assertFalse(move.approval_2_uid)
        self.assertFalse(move.approval_2_on_behalf_uid)
        self.assertEqual(
            move.bill_approval_approver_id, self.user_director,
            "the person chosen to approve is still the person to approve the fixed document",
        )

    def test_the_post_refusal_names_the_chosen_approver(self):
        move = self._reviewed(approver=self.user_director)
        with self.assertRaises(UserError) as caught:
            move.action_post()
        self.assertIn(self.user_director.display_name, str(caught.exception))

    def test_a_user_with_no_level_cannot_choose_the_approver(self):
        move = self._bill()
        with self.assertRaises(AccessError):
            move.with_user(self.user_none).write({
                'bill_approval_approver_id': self.user_director.id})
        move.invalidate_recordset()
        self.assertFalse(move.bill_approval_approver_id)

    def test_the_control_either_level_may_choose_the_approver(self):
        for user in (self.user_reviewer, self.user_director):
            move = self._bill()
            move.with_user(user).write({'bill_approval_approver_id': self.other_director.id})
            self.assertEqual(move.bill_approval_approver_id, self.other_director)

    def test_a_user_with_no_level_cannot_choose_it_on_create_either(self):
        with self.assertRaises(AccessError):
            self.AM.with_user(self.user_none).create({
                'move_type': 'in_invoice',
                'journal_id': self.purchase_journal.id,
                'partner_id': self.vendor.id,
                'bill_approval_approver_id': self.user_director.id,
            })

    def test_the_approver_cannot_change_once_step_2_is_given(self):
        move = self._reviewed(approver=self.user_director)
        move.with_user(self.user_director).action_bill_approval_2()
        with self.assertRaises(UserError):
            move.with_user(self.user_reviewer).write({
                'bill_approval_approver_id': self.other_director.id})
        # A refusal makes it editable again.
        self._refuse(move)
        move.with_user(self.user_reviewer).write({
            'bill_approval_approver_id': self.other_director.id})
        self.assertEqual(move.bill_approval_approver_id, self.other_director)

    def test_the_chosen_approver_must_hold_step_2(self):
        move = self._bill()
        with self.assertRaises(ValidationError):
            move.with_user(self.user_reviewer).write({
                'bill_approval_approver_id': self.user_reviewer.id})

    def test_on_behalf_cannot_be_written_directly(self):
        """`approval_2_on_behalf_uid` is guarded like the other approval fields."""
        move = self._reviewed()
        with self.assertRaises(AccessError):
            move.with_user(self.user_director).write({
                'approval_2_on_behalf_uid': self.user_director.id})

    def test_a_user_can_set_their_own_substitute_from_preferences(self):
        """A non-admin user can set their own substitute via `SELF_WRITEABLE_FIELDS`."""
        self.assertFalse(self.other_director.has_group('base.group_erp_manager'))
        self.other_director.with_user(self.other_director).write({
            'bill_approval_substitute_id': self.user_director.id})
        self.assertEqual(self.other_director.bill_approval_substitute_id, self.user_director)

    def test_the_control_a_user_cannot_set_somebody_elses_substitute(self):
        with self.assertRaises(AccessError):
            self.user_director.with_user(self.other_director).write({
                'bill_approval_substitute_id': self.other_director.id})

    def test_a_substitute_must_hold_step_2_and_be_somebody_else(self):
        with self.assertRaises(ValidationError):
            self.other_director.bill_approval_substitute_id = self.other_director
        with self.assertRaises(ValidationError):
            self.other_director.bill_approval_substitute_id = self.user_reviewer

    def test_the_substitute_is_on_my_preferences_for_an_approver_only(self):
        """The substitute field on My Preferences is shown only to step 2 holders.

        Each user reads their own record, which also checks `SELF_READABLE_FIELDS`.
        """
        view = self.env.ref('base.view_users_form_simple_modif')
        arch = etree.fromstring(self.env['res.users'].with_user(self.user_reviewer).get_view(
            view.id, 'form')['arch'])
        node = arch.xpath("//field[@name='bill_approval_substitute_id']")
        self.assertEqual(len(node), 1, "the substitute must be on My Preferences")
        expression = node[0].get('invisible')
        self.assertTrue(expression, "and it must be hidden for somebody")
        for user, hidden in ((self.other_director, False), (self.user_reviewer, True)):
            own = user.with_user(user).read(['bill_approval_may_approve'])[0]
            self.assertEqual(
                bool(safe_eval(expression, own)), hidden,
                "%s: somebody who does not approve has nothing to delegate" % user.name,
            )

    def test_the_chosen_approver_gets_one_task_once_the_document_waits_for_them(self):
        move = self._bill()
        move.with_user(self.user_reviewer).write({
            'bill_approval_approver_id': self.user_director.id})
        self.assertFalse(
            self._approve_tasks(move),
            "nobody has reviewed it yet, so the approver cannot act and gets no task",
        )
        move.with_user(self.user_reviewer).action_bill_approval_1()
        tasks = self._approve_tasks(move)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks.user_id, self.user_director)

    def test_changing_the_approver_moves_the_task(self):
        move = self._reviewed(approver=self.user_director)
        move.with_user(self.user_reviewer).write({
            'bill_approval_approver_id': self.other_director.id})
        tasks = self._approve_tasks(move)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks.user_id, self.other_director)

    def test_step_2_closes_the_task_and_a_refusal_deletes_it(self):
        move = self._reviewed(approver=self.user_director)
        self.assertTrue(self._approve_tasks(move), "the control: there was a task")
        move.with_user(self.substitute).action_bill_approval_2()
        self.assertFalse(self._approve_tasks(move), "step 2 is what the task asked for")

        other = self._reviewed(approver=self.user_director)
        self.assertTrue(self._approve_tasks(other))
        self._refuse(other)
        self.assertFalse(self._approve_tasks(other))

    def test_no_chosen_approver_means_no_approve_task(self):
        move = self._reviewed()
        self.assertFalse(self._approve_tasks(move))

    def test_my_approvals_finds_the_approvers_and_the_substitutes_documents(self):
        mine = self._reviewed(approver=self.user_director)
        not_mine = self._reviewed(approver=self.other_director)
        unassigned = self._reviewed()
        arch = etree.fromstring(self.env['account.move'].get_view(
            self.env.ref('account.view_account_bill_filter').id, 'search')['arch'])
        node = arch.xpath("//filter[@name='bill_my_approvals']")
        self.assertTrue(node, "the filter must reach the bill search view")
        planted = mine | not_mine | unassigned
        for user in (self.user_director, self.substitute):
            domain = safe_eval(node[0].get('domain'), {'uid': user.id})
            found = self.env['account.move'].with_user(user).search(
                domain + [('id', 'in', planted.ids)])
            self.assertEqual(found, mine, "for %s" % user.name)

    def test_the_step_2_queue_and_count_hold_only_what_the_reader_can_approve(self):
        """The step 2 queues and counts hold only what the reader can approve.

        That is: no chosen approver, the reader as approver, or the reader as the
        approver's substitute. Each count equals the list its row opens.
        """
        planted = {}
        for move_type, prefix in (('in_invoice', ''), ('out_refund', 'cn_')):
            planted[prefix + 'mine'] = self._reviewed(self.user_director, move_type)
            planted[prefix + 'other'] = self._reviewed(self.other_director, move_type)
            planted[prefix + 'open'] = self._reviewed(None, move_type)
        everything = self.env['account.move'].browse(
            [move.id for move in planted.values()])

        cases = (
            (self.user_director, {'mine', 'open'}),
            (self.substitute, {'mine', 'open'}),
            (self.other_director, {'other', 'open'}),
            (self.substitute_of_substitute, {'open'}),
        )
        queues = (
            ('to_approve', 'bill_awaiting_approval',
             'goodahead_bill_approval.action_bills_awaiting_approval', ''),
            ('cn_to_approve', 'credit_note_awaiting_approval',
             'goodahead_bill_approval.action_credit_notes_awaiting_approval', 'cn_'),
        )
        for user, expected in cases:
            rows = {row['id']: row for row in self.env['account.move'].with_user(
                user)._bill_approval_systray_counts()['rows']}
            for key, filter_name, action_xmlid, prefix in queues:
                action = self.env.ref(action_xmlid)
                arch = etree.fromstring(self.env['account.move'].get_view(
                    action.search_view_id.id, 'search')['arch'])
                node = arch.xpath("//filter[@name='%s']" % filter_name)
                list_domain = safe_eval(action.domain) + safe_eval(
                    node[0].get('domain'), {'uid': user.id})
                listed = self.env['account.move'].with_user(user).search(list_domain)
                self.assertEqual(
                    listed & everything,
                    self.env['account.move'].browse(
                        [planted[prefix + name].id for name in expected]),
                    "%s, %s: the list must hold exactly what they can approve" % (
                        user.name, key),
                )
                self.assertEqual(
                    rows[key]['count'], len(listed),
                    "%s, %s: the badge must equal the list it opens" % (user.name, key),
                )

    def test_the_form_carries_the_approver_and_on_behalf(self):
        arch = self.env['account.move'].with_user(self.user_both).get_view(
            self.env.ref('account.view_move_form').id, 'form')['arch']
        for name in ('bill_approval_approver_id', 'approval_2_on_behalf_uid',
                     'o_bill_approval_on_behalf_label'):
            self.assertIn(name, arch)
