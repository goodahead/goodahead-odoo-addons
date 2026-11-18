# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The systray counter: where its numbers come from, and who may see it.

The number on each badge row must equal the list the row opens; the Python domains
and the XML filters are compared clause by clause. These tests are server side only
and do not prove the asset bundle compiles.
"""

from lxml import etree

from odoo.exceptions import AccessError
from odoo.tests import tagged
from odoo.tools.safe_eval import safe_eval

from ..models.account_move import BILL_APPROVAL_QUEUES, resolve_queue_domain
from .common import BillApprovalCommon, GROUP_1

SEARCH_VIEW = 'account.view_account_bill_filter'
JS_PATH = 'goodahead_bill_approval/static/src/js/bill_approval_menu.js'
TEMPLATE_PATH = 'goodahead_bill_approval/static/src/xml/bill_approval_menu.xml'
TEMPLATE_NAME = 'goodahead_bill_approval.BillApprovalMenu'

# Control: a core file always in `web.assets_backend` (paths come with a leading slash).
CORE_CONTROL_PATH = '/web/static/src/webclient/webclient.js'


@tagged('post_install', '-at_install')
class TestTheSystrayCounts(BillApprovalCommon):

    def _counts(self, user):
        return self.env['account.move'].with_user(user)._bill_approval_systray_counts()

    def _rows(self, user):
        return {row['id']: row for row in self._counts(user).get('rows', [])}

    def _served(self, user):
        """What the systray menu receives when `user` loads a page."""
        return self.env['account.move'].with_user(user).bill_approval_systray_data()

    def _filter_domain(self, name, action_xmlid=None, uid=None):
        """A filter's domain from the installed arch of the action's search view.

        `uid` is evaluated into the step 2 filters, which name the reader.
        """
        view = self.env.ref(action_xmlid).search_view_id if action_xmlid else \
            self.env.ref(SEARCH_VIEW)
        arch = self.env['account.move'].get_view(view.id, 'search')['arch']
        nodes = etree.fromstring(arch).xpath("//filter[@name='%s']" % name)
        self.assertTrue(nodes, "no filter named %r in the search view" % name)
        self.assertEqual(len(nodes), 1, "filter %r is not unique" % name)
        return safe_eval(nodes[0].get('domain'), {'uid': uid or self.env.uid})

    @staticmethod
    def _clauses(domain):
        """A domain as an ordered, fully tuplified sequence of clauses.

        Order is kept because the step 2 domains contain `'|'` operators.
        """
        return tuple(
            clause if isinstance(clause, str) else
            tuple(tuple(part) if isinstance(part, list) else part for part in clause)
            for clause in domain
        )

    def _plant(self):
        """One bill in each of the three states, plus two decoys."""
        planted = {}
        planted['to_review'] = self._bill()

        reviewed = self._bill()
        reviewed.with_user(self.user_reviewer).action_bill_approval_1()
        planted['reviewed'] = reviewed

        planted['approved'] = self._approve_fully(self._bill())

        # Decoys: a posted bill with no approvals and a draft customer invoice.
        posted = self._approve_fully(self._bill())
        posted.action_post()
        posted.write({
            'approval_1_uid': False, 'approval_1_date': False,
            'approval_2_uid': False, 'approval_2_date': False,
        })
        planted['posted_unapproved'] = posted
        planted['customer_draft'] = self._create_invoice(move_type='out_invoice')
        return planted

    def test_a_user_holding_neither_level_gets_nothing_at_all(self):
        """A user with no level is served `{}`, which is how the systray item hides
        itself."""
        self.assertEqual(self._counts(self.user_none), {})
        self.assertEqual(
            self._served(self.user_none), {},
            "and the menu must be served nothing, not an empty row list",
        )
        self.assertTrue(
            self._served(self.user_reviewer).get('rows'),
            "the control: rows do arrive for somebody who holds a level, so the "
            "assertion above is not passing because nothing ever arrives",
        )

    def test_each_role_is_shown_only_what_it_can_act_on(self):
        """Each role sees only the queues it can act on."""
        self.assertEqual(
            set(self._rows(self.user_reviewer)),
            {'to_review', 'to_post', 'cn_to_review', 'cn_to_post'},
            "a bookkeeper reviews bills and credit notes and posts approved ones; "
            "approving is not theirs",
        )
        self.assertEqual(
            set(self._rows(self.user_director)), {'to_approve', 'cn_to_approve'},
            "a financial director approves; reviewing and posting are the book "
            "keeper's, and the menu is gated the same way",
        )
        self.assertEqual(
            set(self._rows(self.user_both)),
            {'to_review', 'to_approve', 'to_post',
             'cn_to_review', 'cn_to_approve', 'cn_to_post'},
        )

    def test_the_rows_are_in_the_order_the_bill_travels(self):
        """Rows follow review, approve, post, for bills and then credit notes."""
        self.assertEqual(
            [row['id'] for row in self._counts(self.user_both)['rows']],
            ['to_review', 'to_approve', 'to_post',
             'cn_to_review', 'cn_to_approve', 'cn_to_post'],
        )

    def test_an_external_user_never_reaches_the_count(self):
        """The `_is_internal` guard in `bill_approval_systray_data` keeps external users
        out of the count.

        The portal user is given the review level so that only the guard, not the
        group check, keeps them out.
        """
        external = self.env['res.users'].create({
            'name': 'BA portal with a level',
            'login': 'ba_portal',
            'email': 'ba_portal@example.com',
            'company_id': self.company.id,
            'company_ids': [(6, 0, [self.company.id])],
            'groups_id': [(6, 0, [
                self.env.ref('base.group_portal').id,
                self.env.ref(GROUP_1).id,
            ])],
        })
        self.assertFalse(
            external._is_internal(),
            "the control on the premise: this user really is external",
        )
        self.assertTrue(
            external._has_group(GROUP_1),
            "and the control that makes the guard load-bearing: they DO hold a "
            "level, so only `_is_internal` keeps them out of the count",
        )

        self.assertEqual(self._served(external), {})

    def test_a_level_holder_who_cannot_read_bills_is_served_nothing(self):
        """A level given to an internal user without accounting rights does not raise.

        The menu asks on every page load, so an AccessError here would put an error
        dialog in front of that user on every page.
        """
        no_accounting = self.env['res.users'].create({
            'name': 'BA level without accounting',
            'login': 'ba_no_accounting',
            'email': 'ba_no_accounting@example.com',
            'company_id': self.company.id,
            'company_ids': [(6, 0, [self.company.id])],
            'groups_id': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref(GROUP_1).id,
            ])],
        })
        self.assertTrue(no_accounting._is_internal())
        self.assertTrue(no_accounting._has_group(GROUP_1))
        self.assertFalse(
            self.env['account.move'].with_user(no_accounting).has_access('read'),
            "the control on the premise: this user cannot read journal entries",
        )
        with self.assertRaises(AccessError):
            # The control that makes the guard load-bearing: counting would raise.
            self.env['account.move'].with_user(no_accounting)._bill_approval_systray_counts()

        self.assertEqual(self._served(no_accounting), {})

    def test_every_count_equals_the_list_its_own_row_opens(self):
        """Each count equals the action domain plus filter domain of the list it opens."""
        self._plant()
        rows = self._rows(self.user_both)
        self.assertEqual(len(rows), 6, "the control: there were six rows to check")

        for key, _group, filter_name, action_xmlid, _domain in BILL_APPROVAL_QUEUES:
            action = self.env.ref(action_xmlid)
            as_the_list_shows_it = self.env['account.move'].with_user(
                self.user_both).search_count(
                    safe_eval(action.domain or '[]')
                    + self._filter_domain(filter_name, action_xmlid, self.user_both.id))
            self.assertEqual(
                rows[key]['count'], as_the_list_shows_it,
                "row %r reads %s and the list it opens holds %s" % (
                    key, rows[key]['count'], as_the_list_shows_it),
            )
            self.assertEqual(rows[key]['action_xml_id'], action_xmlid)
            self.assertTrue(rows[key]['label'], "a row with no words is a number")

    def test_the_counts_move_with_the_bills(self):
        """Control: each count moves as a bill goes through the three states."""
        before = self._rows(self.user_both)
        move = self._bill()
        after_create = self._rows(self.user_both)
        self.assertEqual(
            after_create['to_review']['count'], before['to_review']['count'] + 1)

        move.with_user(self.user_reviewer).action_bill_approval_1()
        after_review = self._rows(self.user_both)
        self.assertEqual(
            after_review['to_review']['count'], before['to_review']['count'],
            "it left the review queue",
        )
        self.assertEqual(
            after_review['to_approve']['count'], before['to_approve']['count'] + 1)

        move.with_user(self.user_director).action_bill_approval_2()
        after_approve = self._rows(self.user_both)
        self.assertEqual(
            after_approve['to_approve']['count'], before['to_approve']['count'])
        self.assertEqual(
            after_approve['to_post']['count'], before['to_post']['count'] + 1)

        move.with_user(self.user_reviewer).action_post()
        after_post = self._rows(self.user_both)
        self.assertEqual(
            after_post['to_post']['count'], before['to_post']['count'],
            "and posting is what takes it off the badge, which is the same thing "
            "that clears the activity",
        )

    def test_the_count_is_not_sudo(self):
        """The count is taken as the reading user, so it respects their record rules."""
        self._plant()
        for key, _group, _filter_name, _action, domain in BILL_APPROVAL_QUEUES:
            if key != 'to_review':
                continue
            as_the_user = self.env['account.move'].with_user(
                self.user_reviewer).search_count(resolve_queue_domain(domain, self.user_reviewer.id))
            self.assertEqual(self._rows(self.user_reviewer)[key]['count'], as_the_user)

    def test_every_queue_domain_matches_its_filter_in_the_arch(self):
        """Each Python queue domain matches its XML filter in the installed arch."""
        checked = 0
        for key, _group, filter_name, action_xmlid, domain in BILL_APPROVAL_QUEUES:
            from_arch = self._filter_domain(filter_name, action_xmlid)
            self.assertEqual(
                self._clauses(resolve_queue_domain(domain, self.env.uid)), self._clauses(from_arch),
                "queue %r: Python says %r and the arch says %r" % (
                    key, domain, from_arch),
            )
            checked += 1
        self.assertEqual(checked, 6, "all six queues, or the loop found nothing")

    def test_the_credit_note_filters_do_not_leak_into_the_bill_search_view(self):
        """Credit note filters from the parent search view are removed from the bill view."""
        arch = etree.fromstring(self.env['account.move'].get_view(
            self.env.ref(SEARCH_VIEW).id, 'search')['arch'])
        declared = set(arch.xpath("//filter/@name"))
        self.assertIn('bill_awaiting_review', declared, "the control: the arch was read")
        self.assertIn('bill_my_approvals', declared, "and the shared filter did reach it")
        for name in ('credit_note_awaiting_review', 'credit_note_awaiting_approval',
                     'credit_note_approved_to_post'):
            self.assertNotIn(name, declared)

    def test_the_control_a_domain_that_differs_is_caught(self):
        """Control: a deliberately different domain does not compare equal."""
        from_arch = self._filter_domain('bill_approved_to_post')
        tampered = [('bill_approval_state', '=', 'approved')]
        self.assertNotEqual(
            self._clauses(tampered), self._clauses(from_arch),
            "a domain missing three of its four clauses must not compare equal",
        )
        self.assertEqual(
            len(self._clauses(from_arch)), 4,
            "and the comparison is over real clauses, not an empty set that "
            "would differ from anything",
        )

    def _bundle_paths(self, bundle):
        ir_asset = self.env['ir.asset']
        params = ir_asset._get_asset_params()
        return {entry[0] for entry in ir_asset._get_asset_paths(bundle, params)}

    def test_both_asset_files_are_in_the_backend_bundle(self):
        """The systray JS and template are in `web.assets_backend`."""
        paths = self._bundle_paths('web.assets_backend')

        self.assertIn(
            CORE_CONTROL_PATH, paths,
            "Control failed: %s is not in web.assets_backend, so this test is "
            "not reading the bundle it thinks it is (or the spelling is wrong — "
            "`_get_asset_paths` returns a LEADING SLASH). Nothing below it means "
            "anything until this passes." % CORE_CONTROL_PATH,
        )
        for path in (JS_PATH, TEMPLATE_PATH):
            self.assertIn('/' + path, paths, "%s is not in the bundle" % path)

    def test_this_module_declares_exactly_one_template_and_prefixes_it(self):
        """The module declares one module-prefixed OWL template.

        A duplicate template name silently breaks the whole asset bundle. The
        lookbehind keeps `t-att-name="..."` from matching as `t-name=`.
        """
        import os
        import re

        from odoo.modules.module import get_module_path

        declarations = []
        static = os.path.join(get_module_path('goodahead_bill_approval'), 'static')
        for root, _dirs, files in os.walk(static):
            for name in files:
                if not name.endswith('.xml'):
                    continue
                with open(os.path.join(root, name), encoding='utf-8') as handle:
                    declarations += re.findall(
                        r'(?<![-\w])t-name="([^"]+)"', handle.read())

        self.assertEqual(
            declarations, [TEMPLATE_NAME],
            "this module declares exactly one OWL template and it is "
            "module-prefixed; found %r" % (declarations,),
        )
