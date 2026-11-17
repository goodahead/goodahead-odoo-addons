# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The queues are counted exactly, not sampled.

Each queue is counted with the installed action and filter domains and compared
with a ground truth computed from the approval fields, with decoys planted for each
kind of wrong domain. Every `search_default_*` key is checked against the search
view, because the web client silently drops unknown keys.
"""

from datetime import timedelta

from lxml import etree

from odoo import fields
from odoo.tests import tagged
from odoo.tools.safe_eval import safe_eval

from .common import BillApprovalCommon

SEARCH_VIEW = 'account.view_account_bill_filter'
QUEUE_1 = 'goodahead_bill_approval.action_bills_awaiting_review'
QUEUE_2 = 'goodahead_bill_approval.action_bills_awaiting_approval'
QUEUE_3 = 'goodahead_bill_approval.action_bills_to_post'


@tagged('post_install', '-at_install')
class TestTheQueues(BillApprovalCommon):

    def _search_arch(self):
        return self.env['account.move'].get_view(
            self.env.ref(SEARCH_VIEW).id, 'search')['arch']

    def _filter_domain(self, name):
        """The filter's own domain, out of the installed arch."""
        tree = etree.fromstring(self._search_arch())
        nodes = tree.xpath("//filter[@name='%s']" % name)
        self.assertTrue(nodes, "no filter named %r in the search view" % name)
        self.assertEqual(len(nodes), 1, "filter %r is not unique" % name)
        return safe_eval(nodes[0].get('domain'), {'uid': self.env.uid})

    def _action_domain(self, xmlid):
        action = self.env.ref(xmlid)
        return safe_eval(action.domain or '[]')

    def _search_default_keys(self, xmlid):
        context = safe_eval(self.env.ref(xmlid).context or '{}')
        return [k for k in context if k.startswith('search_default_')]

    def _queue_count(self, action_xmlid, filter_name):
        """What the user sees: the action's domain AND the filter's domain."""
        return self.env['account.move'].search_count(
            self._action_domain(action_xmlid) + self._filter_domain(filter_name))

    def _plant_decoys(self):
        planted = {}
        planted['waiting'] = self._bill()
        planted['waiting_refund'] = self._bill(move_type='in_refund')

        reviewed = self._bill()
        reviewed.with_user(self.user_reviewer).action_bill_approval_1()
        planted['reviewed'] = reviewed

        approved = self._bill()
        self._approve_fully(approved)
        planted['approved_draft'] = approved

        # A posted bill with no approvals, as every bill that predates the module looks.
        posted = self._bill()
        self._approve_fully(posted)
        posted.action_post()
        posted.write({
            'approval_1_uid': False, 'approval_1_date': False,
            'approval_2_uid': False, 'approval_2_date': False,
        })
        self.assertEqual(posted.state, 'posted')
        self.assertEqual(posted.bill_approval_state, 'to_review')
        planted['posted_unapproved'] = posted

        planted['customer_draft'] = self._create_invoice(move_type='out_invoice')
        self.assertEqual(planted['customer_draft'].bill_approval_state, 'to_review')

        # `_post(soft=True)` defers a future-dated move (sets `auto_post`) instead of
        # posting it, leaving an approved draft that the third queue must exclude.
        future = fields.Date.context_today(self.env['account.move']) + timedelta(days=30)
        deferred = self._bill()
        deferred.write({'invoice_date': future, 'date': future})
        self._approve_fully(deferred)
        deferred._post()
        self.assertEqual(
            deferred.state, 'draft',
            "a future-dated move is not posted by `_post(soft=True)` - if this fails, core "
            "changed and the fourth clause needs re-deriving rather than re-pointing",
        )
        self.assertNotEqual(deferred.auto_post, 'no')
        self.assertEqual(deferred.bill_approval_state, 'approved')
        planted['approved_auto_post'] = deferred

        return planted

    def test_queue_1_counts_exactly_what_is_awaiting_review(self):
        planted = self._plant_decoys()

        # Ground truth from the approval fields, not the stored state.
        truth = self.env['account.move'].search([
            ('move_type', 'in', ('in_invoice', 'in_refund')),
            ('state', '=', 'draft'),
            ('approval_1_uid', '=', False),
        ])
        seen = self.env['account.move'].search(
            self._action_domain(QUEUE_1) + self._filter_domain('bill_awaiting_review'))

        self.assertEqual(
            len(seen), len(truth),
            "the number the queue shows must be the number actually awaiting "
            "step 1 — seen %s, actually %s" % (len(seen), len(truth)),
        )
        self.assertEqual(seen, truth, "and the same records, not just as many")

        self.assertIn(planted['waiting'], seen)
        self.assertIn(planted['waiting_refund'], seen)
        self.assertNotIn(planted['reviewed'], seen)
        self.assertNotIn(planted['approved_draft'], seen)
        self.assertNotIn(
            planted['posted_unapproved'], seen,
            "a domain missing `state = 'draft'` swallows every historical bill",
        )
        self.assertNotIn(
            planted['customer_draft'], seen,
            "a domain missing the move type swallows every customer invoice",
        )

    def test_queue_2_counts_exactly_what_is_awaiting_approval(self):
        planted = self._plant_decoys()

        truth = self.env['account.move'].search([
            ('move_type', 'in', ('in_invoice', 'in_refund')),
            ('state', '=', 'draft'),
            ('approval_1_uid', '!=', False),
            ('approval_2_uid', '=', False),
        ])
        seen = self.env['account.move'].search(
            self._action_domain(QUEUE_2) + self._filter_domain('bill_awaiting_approval'))

        self.assertEqual(len(seen), len(truth))
        self.assertEqual(seen, truth)
        self.assertIn(planted['reviewed'], seen)
        self.assertNotIn(planted['waiting'], seen)
        self.assertNotIn(planted['approved_draft'], seen)

    def test_queue_3_counts_exactly_what_is_approved_and_still_draft(self):
        """The bills-to-post queue counts approved drafts that are not auto-posting."""
        planted = self._plant_decoys()

        truth = self.env['account.move'].search([
            ('move_type', 'in', ('in_invoice', 'in_refund')),
            ('state', '=', 'draft'),
            ('auto_post', '=', 'no'),
            ('approval_1_uid', '!=', False),
            ('approval_2_uid', '!=', False),
        ])
        seen = self.env['account.move'].search(
            self._action_domain(QUEUE_3) + self._filter_domain('bill_approved_to_post'))

        self.assertEqual(
            len(seen), len(truth),
            "the number the book keeper's queue shows must be the number actually "
            "approved and still draft - seen %s, actually %s" % (len(seen), len(truth)),
        )
        self.assertEqual(seen, truth, "and the same records, not just as many")

        self.assertIn(planted['approved_draft'], seen)
        self.assertNotIn(planted['waiting'], seen)
        self.assertNotIn(planted['reviewed'], seen)
        self.assertNotIn(
            planted['posted_unapproved'], seen,
            "a domain missing `state = 'draft'` swallows every historical bill",
        )
        self.assertNotIn(
            planted['customer_draft'], seen,
            "a domain missing the move type swallows every customer invoice",
        )
        self.assertNotIn(
            planted['approved_auto_post'], seen,
            "a domain missing the `auto_post` clause tells the book keeper to post a "
            "bill that is already scheduled to post itself, for ever - the task would "
            "never clear, because posting is what clears it and nobody has to post it",
        )

    def test_the_three_queues_do_not_overlap(self):
        self._plant_decoys()
        one = self.env['account.move'].search(
            self._action_domain(QUEUE_1) + self._filter_domain('bill_awaiting_review'))
        two = self.env['account.move'].search(
            self._action_domain(QUEUE_2) + self._filter_domain('bill_awaiting_approval'))
        three = self.env['account.move'].search(
            self._action_domain(QUEUE_3) + self._filter_domain('bill_approved_to_post'))
        self.assertFalse(one & two, "a bill is in one queue or the other")
        self.assertFalse(one & three)
        self.assertFalse(two & three)
        # Control: all three queues are non-empty.
        self.assertTrue(one and two and three, "every queue must have planted rows")

    def test_a_grouped_count_matches_the_queue(self):
        """A grouped count by the stored state matches the queue count."""
        self._plant_decoys()
        domain = self._action_domain(QUEUE_1) + [('state', '=', 'draft')]
        groups = self.env['account.move']._read_group(
            domain, groupby=['bill_approval_state'], aggregates=['__count'])
        counted = dict(groups)
        self.assertEqual(
            counted.get('to_review', 0),
            self._queue_count(QUEUE_1, 'bill_awaiting_review'),
        )

    def test_every_search_default_names_a_filter_that_exists(self):
        """Every `search_default_*` key names an existing filter; unknown keys are
        silently dropped by the client."""
        arch = etree.fromstring(self._search_arch())
        declared = set(arch.xpath("//filter/@name")) | set(arch.xpath("//field/@name"))
        self.assertTrue(declared, "the search view arch came back empty")

        checked = 0
        for xmlid in (QUEUE_1, QUEUE_2, QUEUE_3):
            keys = self._search_default_keys(xmlid)
            self.assertTrue(keys, "%s declares no search default" % xmlid)
            for key in keys:
                name = key[len('search_default_'):]
                self.assertIn(
                    name, declared,
                    "%s sets %r, and no filter or field of that name exists in "
                    "%s — the client would drop it in silence" % (xmlid, key, SEARCH_VIEW),
                )
                checked += 1
        self.assertEqual(checked, 3, "each of the three queues sets exactly one default")

    def test_the_control_a_made_up_key_is_not_declared(self):
        """Control: a misspelled filter name is not found in the arch."""
        arch = etree.fromstring(self._search_arch())
        declared = set(arch.xpath("//filter/@name")) | set(arch.xpath("//field/@name"))
        self.assertNotIn('bill_awaiting_reivew', declared)
        self.assertIn('bill_awaiting_review', declared)
        self.assertNotIn('bill_approved_to_pos', declared)
        self.assertIn('bill_approved_to_post', declared)

    def test_each_action_points_at_the_right_search_view(self):
        """Each queue action uses the bill search view where the filters are declared."""
        for xmlid in (QUEUE_1, QUEUE_2, QUEUE_3):
            self.assertEqual(
                self.env.ref(xmlid).search_view_id,
                self.env.ref(SEARCH_VIEW),
                "%s must use the bill search view, which is where the filters "
                "are declared" % xmlid,
            )

    def test_the_menu_items_are_gated_by_the_matching_level(self):
        review = self.env.ref('goodahead_bill_approval.menu_bills_awaiting_review')
        approve = self.env.ref('goodahead_bill_approval.menu_bills_awaiting_approval')
        to_post = self.env.ref('goodahead_bill_approval.menu_bills_to_post')
        self.assertEqual(
            review.group_ids,
            self.env.ref('goodahead_bill_approval.group_bill_approval_1'))
        self.assertEqual(
            approve.group_ids,
            self.env.ref('goodahead_bill_approval.group_bill_approval_2'))
        self.assertEqual(
            to_post.group_ids,
            self.env.ref('goodahead_bill_approval.group_bill_approval_1'),
            "posting is the book keeper's job, so *Bills to Post* is gated on the REVIEW "
            "level. Gating it on both would hand every financial director a worklist nobody "
            "asked them to work; a director who also keeps books ticks the review box, "
            "which the module keeps as a separate visible checkbox for exactly that reason",
        )

    def test_the_state_column_is_stored_and_indexed(self):
        """The state is stored (for grouping) and indexed (for queue filters)."""
        field = self.env['account.move']._fields['bill_approval_state']
        self.assertTrue(field.store)
        self.assertTrue(field.index)

    def test_the_install_did_not_recompute_the_table(self):
        """The column has a PostgreSQL default, proving `_auto_init` created it
        without a full-table recompute."""
        self.env.cr.execute("""
            SELECT column_default
              FROM information_schema.columns
             WHERE table_name = 'account_move'
               AND column_name = 'bill_approval_state'
        """)
        row = self.env.cr.fetchone()
        self.assertTrue(row, "the column must exist")
        self.assertTrue(
            row[0] and 'to_review' in row[0],
            "the postgres-level default is the proof the `_auto_init` path ran; "
            "got %r" % (row[0],),
        )


@tagged('post_install', '-at_install')
class TestTheDomainsAreNotRetyped(BillApprovalCommon):
    """Checks the shape of the filter and action domains read from the installed records."""

    def test_the_filter_domain_read_from_the_arch_has_all_three_clauses(self):
        arch = self.env['account.move'].get_view(
            self.env.ref(SEARCH_VIEW).id, 'search')['arch']
        tree = etree.fromstring(arch)
        for name, state in (
            ('bill_awaiting_review', 'to_review'),
            ('bill_awaiting_approval', 'reviewed'),
        ):
            node = tree.xpath("//filter[@name='%s']" % name)
            self.assertTrue(node, "filter %r is missing from the arch" % name)
            domain = safe_eval(node[0].get('domain'), {'uid': self.env.uid})
            fields_used = {clause[0] for clause in domain if isinstance(clause, (list, tuple))}
            # The per-reader clauses are covered in `test_chosen_approver.py`.
            per_reader = {
                'bill_approval_approver_id',
                'bill_approval_approver_id.bill_approval_substitute_id',
            }
            self.assertEqual(
                fields_used - per_reader, {'bill_approval_state', 'state', 'move_type'},
                "each queue filter must be COMPLETE on its own — the approval "
                "state, the draft state and the move type. A filter that is only "
                "correct inside one action shows a plausible subset everywhere "
                "else. Filter %r reads %r" % (name, domain),
            )
            self.assertIn(
                ('bill_approval_state', '=', state), [tuple(c) for c in domain],
            )

    def test_the_third_filter_domain_has_all_four_clauses(self):
        """The bills-to-post filter also carries the `auto_post` clause."""
        tree = etree.fromstring(self.env['account.move'].get_view(
            self.env.ref(SEARCH_VIEW).id, 'search')['arch'])
        node = tree.xpath("//filter[@name='bill_approved_to_post']")
        self.assertTrue(node, "the third queue's filter is missing from the arch")
        domain = safe_eval(node[0].get('domain'))
        fields_used = {clause[0] for clause in domain if isinstance(clause, (list, tuple))}
        self.assertEqual(
            fields_used,
            {'bill_approval_state', 'auto_post', 'state', 'move_type'},
            "approved, not auto-posting, draft, and a vendor bill. Reads %r" % (domain,),
        )
        self.assertIn(
            ('bill_approval_state', '=', 'approved'), [tuple(c) for c in domain])
        self.assertIn(('auto_post', '=', 'no'), [tuple(c) for c in domain])

    def test_the_action_domain_scopes_the_move_type(self):
        for xmlid in (QUEUE_1, QUEUE_2, QUEUE_3):
            domain = safe_eval(self.env.ref(xmlid).domain)
            self.assertEqual(
                [tuple(c) for c in domain],
                [('move_type', 'in', ['in_invoice', 'in_refund'])],
            )

