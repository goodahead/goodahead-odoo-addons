# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The "bill to post" activity raised for the book keeper after step 2.

Assertions read `mail.activity` directly rather than through `activity_search`.
Every test that expects no task also checks what must still hold (the bill in the
bills-to-post queue, or a chatter note), so a silent failure cannot pass.
"""

from datetime import timedelta

from odoo import Command, fields
from odoo.tests import tagged

from ..models.account_move import ACTIVITY_TYPE_XMLID, QUEUE_TO_POST_DOMAIN
from .common import BillApprovalCommon, GROUP_1


@tagged('post_install', '-at_install')
class TestTheTask(BillApprovalCommon):

    @property
    def activity_type(self):
        return self.env.ref(ACTIVITY_TYPE_XMLID)

    def _tasks(self, move, automated=None):
        """Our tasks on this bill, read straight out of `mail.activity`."""
        domain = [
            ('res_model', '=', 'account.move'),
            ('res_id', '=', move.id),
            ('activity_type_id', '=', self.activity_type.id),
        ]
        if automated is not None:
            domain.append(('automated', '=', automated))
        return self.env['mail.activity'].sudo().search(domain)

    def _approved(self, move=None, reviewer=None, director=None):
        """A bill approved through both actions as real users (the task is raised
        by `action_bill_approval_2`, which `_approve_fully` bypasses)."""
        move = move if move is not None else self._bill()
        move.with_user(reviewer or self.user_reviewer).action_bill_approval_1()
        move.with_user(director or self.user_director).action_bill_approval_2()
        return move

    def _second_reviewer(self):
        """A second book keeper: neither the assignee nor the creator of the task."""
        return self.env['res.users'].create({
            'name': 'BA second book keeper',
            'login': 'ba_reviewer_2',
            'company_id': self.company.id,
            'company_ids': [Command.set([self.company.id])],
            'group_ids': [
                Command.link(self.env.ref('base.group_user').id),
                Command.link(self.env.ref('account.group_account_invoice').id),
                Command.link(self.env.ref('account.group_account_user').id),
                Command.link(self.env.ref(GROUP_1).id),
            ],
        })

    def test_the_activity_type_is_bound_to_account_move_and_due_today(self):
        """The type is bound to `account.move` and due today.

        A wrong `res_model` makes `activity_schedule` silently fall back to core's
        "To Do", which the closing logic would then delete.
        """
        activity_type = self.activity_type
        self.assertEqual(activity_type.res_model, 'account.move')
        self.assertEqual(activity_type.delay_count, 0)
        self.assertEqual(activity_type.delay_unit, 'days')
        self.assertFalse(
            activity_type.default_user_id,
            "a default assignee here would silently override "
            "`_bill_approval_post_task_assignee`, including its deliberate "
            "assign-nobody outcome",
        )

    def test_step_2_raises_one_task_on_the_step_1_approver(self):
        move = self._bill()
        move.with_user(self.user_reviewer).action_bill_approval_1()
        self.assertFalse(
            self._tasks(move),
            "the control: step 1 alone raises nothing — the task is the book "
            "keeper's NEXT action, and before step 2 there is none",
        )

        move.with_user(self.user_director).action_bill_approval_2()

        tasks = self._tasks(move)
        self.assertEqual(len(tasks), 1, "exactly one task, on one person")
        self.assertEqual(
            tasks.user_id, self.user_reviewer,
            "assigned to whoever gave approval step 1",
        )
        self.assertEqual(tasks.activity_type_id, self.activity_type)
        self.assertEqual(tasks.res_id, move.id)
        self.assertTrue(tasks.summary, "a task with no name is a row, not a task")
        self.assertTrue(tasks.automated, "ours, so ours to close")

    def test_the_task_lands_in_the_bell_the_activity_filters_and_my_activities(self):
        """The task shows in the activity bell, the Today filter and "My Activities"
        via `activity_state` and `activity_user_id` on the bill."""
        move = self._approved()
        today = fields.Date.context_today(move)

        tasks = self._tasks(move)
        self.assertEqual(
            tasks.date_deadline, today,
            "due TODAY: a deadline even one day out means a bill approved this "
            "morning is invisible until tomorrow",
        )
        self.assertIn(tasks, move.activity_ids)
        self.assertEqual(
            move.activity_state, 'today',
            "'today' is the value the Today filter matches; 'planned' would hide "
            "it and 'overdue' would be wrong on the day it was raised",
        )
        self.assertEqual(
            move.activity_user_id, self.user_reviewer,
            "\"My Activities\" matches on this field, so it is what puts the bill "
            "on the book keeper's own list rather than merely in the database",
        )

    def test_the_task_is_not_raised_twice_on_one_bill(self):
        """Raising the task again returns the existing one; one task per bill."""
        move = self._approved()
        self.assertEqual(len(self._tasks(move)), 1)

        again = move._bill_approval_raise_post_task()

        self.assertEqual(len(self._tasks(move)), 1, "still one")
        self.assertEqual(
            again, self._tasks(move),
            "and the existing one is handed back, not a fresh empty recordset — "
            "a caller that gets nothing cannot tell dedup from failure",
        )

    def test_one_person_holding_both_levels_is_not_told_to_post_it_themselves(self):
        """A user who gave both approvals gets no task, but the bill stays in the
        bills-to-post queue."""
        move = self._approved(reviewer=self.user_both, director=self.user_both)

        self.assertFalse(
            self._tasks(move),
            "no task when one person gave both approvals",
        )
        self.assertEqual(move.bill_approval_state, 'approved')
        self.assertIn(
            move,
            self.env['account.move'].search(
                QUEUE_TO_POST_DOMAIN + [('id', '=', move.id)]),
            "the control: the bill must still be in the book keeper's queue. "
            "Without this clause the test above passes just as well for a feature "
            "that lost the bill entirely",
        )

    def test_an_archived_step_1_approver_gets_no_task_and_the_bill_says_so(self):
        """An archived step 1 approver gets no task; the bill stays queued and a
        chatter note says where it waits."""
        move = self._bill()
        move.with_user(self.user_reviewer).action_bill_approval_1()
        self.user_reviewer.sudo().write({'active': False})

        move.with_user(self.user_director).action_bill_approval_2()

        self.assertFalse(self._tasks(move), "an archived user is given no task")
        self.assertIn(
            move,
            self.env['account.move'].search(
                QUEUE_TO_POST_DOMAIN + [('id', '=', move.id)]),
            "the control: the bill is still the book keeper's to post",
        )
        bodies = ' '.join(move.message_ids.mapped('body'))
        self.assertIn(
            'Bills to Post', bodies,
            "and it must not be SILENT. The one case where the feature cannot "
            "do its job must not be the one case nobody is told about, so the "
            "bill itself names where it is waiting",
        )

    def test_an_import_that_raised_step_1_as_odoobot_gets_no_task(self):
        """No task is assigned to OdooBot when an automated import gave step 1."""
        move = self._bill()
        move._bill_approval_grant_step_1(note='an automated import')
        self.assertEqual(
            move.approval_1_uid, self.env.ref('base.user_root'),
            "the control on the premise: the hook really does record OdooBot "
            "when it runs as OdooBot, which is what makes this case reachable",
        )

        move.with_user(self.user_director).action_bill_approval_2()

        self.assertFalse(self._tasks(move))
        self.assertIn(
            move,
            self.env['account.move'].search(
                QUEUE_TO_POST_DOMAIN + [('id', '=', move.id)]),
        )

    def test_no_step_1_approver_at_all_degrades_without_raising(self):
        """An approved bill with no step 1 approver raises no task and no error.

        The state is only reachable below the ORM (a deleted user's `ON DELETE SET
        NULL`); an ORM write would recompute the stored state, so SQL is used.
        """
        move = self._bill()
        move.write({
            'approval_1_uid': self.user_reviewer.id,
            'approval_1_date': fields.Datetime.now(),
            'approval_2_uid': self.user_director.id,
            'approval_2_date': fields.Datetime.now(),
        })

        # Via the ORM the stored state is recomputed, so that route is benign.
        benign = self._bill()
        benign.write({
            'approval_1_uid': self.user_reviewer.id,
            'approval_2_uid': self.user_director.id,
        })
        benign.write({'approval_1_uid': False})
        self.assertEqual(
            benign.bill_approval_state, 'to_review',
            "an ORM write recomputes the stored state, so that route is benign",
        )

        # Flush first: `invalidate_recordset()` flushes pending writes, which would
        # otherwise land after the UPDATE and restore the approver.
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE account_move SET approval_1_uid = NULL WHERE id = %s", (move.id,))
        move.invalidate_recordset()
        self.assertFalse(move.approval_1_uid)
        self.assertEqual(
            move.bill_approval_state, 'approved',
            "the control on the premise: the stored state still says approved "
            "with nobody named, which is exactly why the field has to be "
            "re-asked rather than assumed",
        )

        raised = move._bill_approval_raise_post_task()

        self.assertFalse(raised, "no task, and no traceback")
        self.assertFalse(self._tasks(move))
        self.assertIn('Bills to Post', ' '.join(move.message_ids.mapped('body')))

    def test_posting_the_bill_closes_the_task(self):
        move = self._approved()
        self.assertTrue(self._tasks(move), "the control: there was a task to close")

        move.with_user(self.user_reviewer).action_post()

        self.assertEqual(move.state, 'posted')
        self.assertFalse(
            self._tasks(move),
            "posting IS the task, so it clears — a to-do that outlives its "
            "reason is how a queue stops being read",
        )
        self.assertIn(
            'posted', ' '.join(move.message_ids.mapped('body')).lower(),
            "and `activity_feedback` leaves core's own completed-activity trail, "
            "rather than the row simply vanishing",
        )

    def test_a_third_book_keeper_can_post_a_bill_whose_task_is_someone_elses(self):
        """A user who is neither assignee nor creator of the task can still post.

        `mail.activity` grants write through write rights on the document, which is
        why closing the task needs no `sudo()`.
        """
        move = self._approved()
        other = self._second_reviewer()
        self.assertEqual(self._tasks(move).user_id, self.user_reviewer)
        self.assertNotEqual(other, self.user_reviewer)
        self.assertNotEqual(
            self._tasks(move).create_uid, other,
            "the control on the premise: this user is neither the assignee nor "
            "the creator, which is the only situation in which the record rule "
            "would bite",
        )

        move.with_user(other).action_post()

        self.assertEqual(move.state, 'posted')
        self.assertFalse(self._tasks(move))

    def test_refusing_the_bill_deletes_the_task(self):
        """Refusing the bill deletes the task (rather than marking it done)."""
        move = self._approved()
        self.assertTrue(self._tasks(move), "the control: there was a task")

        wizard = self.env['bill.approval.refusal'].with_user(self.user_director).create({
            'move_id': move.id,
            'reason': 'The second line is the wrong account.',
        })
        wizard.action_refuse()

        self.assertEqual(move.bill_approval_state, 'to_review')
        self.assertFalse(
            self._tasks(move),
            "a to-do telling somebody to post a bill the gate will refuse is "
            "worse than no to-do at all",
        )

    def test_a_task_typed_by_hand_is_not_swept_away_by_posting(self):
        """Posting closes only the automated task, not one scheduled by hand."""
        move = self._approved()
        by_hand = self.env['mail.activity'].create({
            'activity_type_id': self.activity_type.id,
            'summary': 'Check the freight line with the vendor first',
            'res_model_id': self.env['ir.model']._get('account.move').id,
            'res_id': move.id,
            'user_id': self.user_reviewer.id,
            'automated': False,
        })
        self.assertEqual(len(self._tasks(move)), 2)

        move.with_user(self.user_reviewer).action_post()

        self.assertFalse(self._tasks(move, automated=True), "the control: ours went")
        self.assertTrue(by_hand.exists(), "and theirs stayed")

    def test_a_bill_already_set_to_auto_post_is_given_no_task(self):
        """A bill scheduled to post itself gets no task on step 2."""
        future = fields.Date.context_today(self.env['account.move']) + timedelta(days=30)
        move = self._bill()
        move.write({'invoice_date': future, 'date': future, 'auto_post': 'at_date'})

        self._approved(move=move)

        self.assertEqual(move.state, 'draft')
        self.assertFalse(self._tasks(move))
        self.assertNotIn(
            move,
            self.env['account.move'].search(
                QUEUE_TO_POST_DOMAIN + [('id', '=', move.id)]),
            "the control: the queue agrees — one predicate, two readers",
        )

    def test_a_bill_deferred_by_a_soft_post_loses_its_task(self):
        """A bill deferred by `_post(soft=True)` loses its task.

        `_post` clears tasks by the bills' final condition, not by what `super()`
        returned, since a deferred move is not in the return value.
        """
        move = self._approved()
        self.assertTrue(self._tasks(move), "the control: there was a task")

        future = fields.Date.context_today(move) + timedelta(days=30)
        move.write({'invoice_date': future, 'date': future})
        move._post()

        self.assertEqual(move.state, 'draft', "core deferred it rather than posting it")
        self.assertNotEqual(move.auto_post, 'no')
        self.assertFalse(
            self._tasks(move),
            "the bill posts itself now, so nobody is told to post it",
        )
        self.assertNotIn(
            move,
            self.env['account.move'].search(
                QUEUE_TO_POST_DOMAIN + [('id', '=', move.id)]),
            "and the queue agrees, which is the property the shared predicate buys",
        )

    def test_the_predicate_and_the_queue_domain_agree(self):
        """`_bill_approval_is_waiting_to_post()` and `QUEUE_TO_POST_DOMAIN` select
        the same bills over a planted set covering every shape."""
        future = fields.Date.context_today(self.env['account.move']) + timedelta(days=30)

        waiting = self._approved()
        reviewed = self._bill()
        reviewed.with_user(self.user_reviewer).action_bill_approval_1()
        untouched = self._bill()

        posted = self._approve_fully(self._bill())
        posted.action_post()

        deferred = self._approved()
        deferred.write({'invoice_date': future, 'date': future})
        deferred._post()

        customer = self._create_invoice(move_type='out_invoice')
        self._approve_fully(customer)

        planted = waiting | reviewed | untouched | posted | deferred | customer

        by_sql = self.env['account.move'].search(
            QUEUE_TO_POST_DOMAIN + [('id', 'in', planted.ids)])
        by_python = planted.filtered(
            lambda move: move._bill_approval_is_waiting_to_post())

        self.assertEqual(
            by_sql, by_python,
            "the SQL domain and the Python predicate must select the same bills. "
            "SQL gave %r, Python gave %r" % (by_sql.ids, by_python.ids),
        )
        self.assertEqual(by_sql, waiting, "and the right one")
        self.assertTrue(
            by_sql and (planted - by_sql),
            "the control: the planted set must contain rows on both sides of "
            "the rule, or two empty answers would agree perfectly",
        )


@tagged('post_install', '-at_install')
class TestTheTaskSurvivesAMissingType(BillApprovalCommon):
    """A missing activity type skips the task instead of falling back to "To Do",
    and the approval still lands."""

    def test_an_absent_activity_type_skips_the_task_and_keeps_the_approval(self):
        self.env.ref('goodahead_bill_approval.mail_activity_type_bill_to_post').unlink()
        self.assertFalse(
            self.env.ref(ACTIVITY_TYPE_XMLID, raise_if_not_found=False),
            "the control on the premise: the type really is gone",
        )

        move = self._bill()
        move.with_user(self.user_reviewer).action_bill_approval_1()
        move.with_user(self.user_director).action_bill_approval_2()

        self.assertEqual(
            move.bill_approval_state, 'approved',
            "the approval is what the user pressed the button for; a missing data "
            "record must not take it down",
        )
        self.assertEqual(
            self.env['mail.activity'].sudo().search_count([
                ('res_model', '=', 'account.move'), ('res_id', '=', move.id),
            ]), 0,
            "and NO activity of ANY type was created — not a fallback To Do, "
            "which is the failure the `raise_if_not_found=False` guard exists for",
        )
        self.assertIn(
            move,
            self.env['account.move'].search(
                QUEUE_TO_POST_DOMAIN + [('id', '=', move.id)]),
            "the queue is the backstop, and it needs no stored row to be right",
        )

    def test_posting_without_the_type_does_not_raise(self):
        """Posting still works when the activity type is missing."""
        move = self._approve_fully(self._bill())
        self.env.ref('goodahead_bill_approval.mail_activity_type_bill_to_post').unlink()

        move.action_post()

        self.assertEqual(move.state, 'posted')
