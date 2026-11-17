# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The bill form and list: modifiers evaluated per record, not just found in the arch.

`get_view` returns one arch for every record, so "the arch contains X" cannot catch
a too-permissive `invisible=`. `_element_invisible_for` evaluates the expression
against a record's values, and each test drives both the visible and hidden case.
"""

import re

from lxml import etree

from odoo.tests import tagged
from odoo.tools.safe_eval import safe_eval

from .common import BillApprovalCommon


@tagged('post_install', '-at_install')
class TestTheScreen(BillApprovalCommon):

    def _arch(self, view_xmlid='account.view_move_form', view_type='form', user=None):
        """The arch as seen by `user` (default: a user holding both levels).

        `get_view` strips nodes whose `groups=` the reader lacks, and OdooBot holds
        neither approval group.
        """
        model = self.env['account.move'].with_user(user or self.user_both)
        return model.get_view(self.env.ref(view_xmlid).id, view_type)['arch']

    def _element_invisible_for(self, css_class, record, arch=None):
        """Evaluate the arch element's `invisible=` against `record`'s values.

        Returns True when the client would HIDE it. Raises when the element is
        absent, so a deleted block cannot read as "hidden".
        """
        tree = etree.fromstring(arch or self._arch())
        nodes = tree.xpath(
            "//*[contains(concat(' ', normalize-space(@class), ' '), ' %s ')]"
            % css_class
        )
        self.assertTrue(nodes, "no element carrying class %r in the arch" % css_class)
        self.assertEqual(len(nodes), 1, "class %r is not unique in the arch" % css_class)
        expression = nodes[0].get('invisible')
        if expression in (None, ''):
            return False
        if expression in ('1', 'True', 'true'):
            return True
        # Read only the fields the expression uses.
        AM = self.env['account.move']
        names = {
            name for name in AM._fields
            if re.search(r'\b%s\b' % re.escape(name), expression)
        }
        values = {name: record[name] for name in names}
        # The client sees a many2one as its id.
        for name, value in list(values.items()):
            if hasattr(value, '_name'):
                values[name] = value.id
        return bool(safe_eval(expression, values))

    def test_the_helper_can_fail(self):
        """Control: the helper returns both True and False for the same element."""
        waiting = self._bill()
        approved = self._approve_fully(self._bill())
        self.assertFalse(self._element_invisible_for('o_bill_approval_alert_1', waiting))
        self.assertTrue(self._element_invisible_for('o_bill_approval_alert_1', approved))

    def test_a_missing_element_raises_rather_than_reading_as_hidden(self):
        with self.assertRaises(AssertionError):
            self._element_invisible_for('o_bill_approval_no_such_class', self._bill())

    def test_the_review_alert_is_on_screen_only_while_review_is_outstanding(self):
        waiting = self._bill()
        self.assertFalse(
            self._element_invisible_for('o_bill_approval_alert_1', waiting),
            "a bill nobody has reviewed must say so above the sheet",
        )

        reviewed = self._bill()
        reviewed.with_user(self.user_reviewer).action_bill_approval_1()
        self.assertTrue(
            self._element_invisible_for('o_bill_approval_alert_1', reviewed))

        approved = self._approve_fully(self._bill())
        self.assertTrue(
            self._element_invisible_for('o_bill_approval_alert_1', approved))

        posted = self._approve_fully(self._bill())
        posted.action_post()
        self.assertTrue(
            self._element_invisible_for('o_bill_approval_alert_1', posted),
            "a posted bill must not carry an approval alert",
        )

        customer = self._create_invoice(move_type='out_invoice')
        self.assertTrue(
            self._element_invisible_for('o_bill_approval_alert_1', customer),
            "the modifier must name the move type. A customer invoice carries "
            "`bill_approval_state = 'to_review'` too, because the field is "
            "deliberately not scoped — so an alert that only tests the approval "
            "state appears on every draft invoice in the database",
        )

    def test_the_approval_alert_is_on_screen_only_between_the_two_steps(self):
        reviewed = self._bill()
        reviewed.with_user(self.user_reviewer).action_bill_approval_1()
        self.assertFalse(
            self._element_invisible_for('o_bill_approval_alert_2', reviewed))

        for record in (
            self._bill(),
            self._approve_fully(self._bill()),
            self._create_invoice(move_type='out_invoice'),
        ):
            self.assertTrue(
                self._element_invisible_for('o_bill_approval_alert_2', record))

    def test_the_review_button_shows_only_on_a_draft_bill_awaiting_review(self):
        waiting = self._bill()
        self.assertFalse(
            self._element_invisible_for('o_bill_approval_btn_1', waiting))

        reviewed = self._bill()
        reviewed.with_user(self.user_reviewer).action_bill_approval_1()
        self.assertTrue(
            self._element_invisible_for('o_bill_approval_btn_1', reviewed),
            "a reviewed bill must not offer Review again",
        )

        posted = self._approve_fully(self._bill())
        posted.action_post()
        self.assertTrue(
            self._element_invisible_for('o_bill_approval_btn_1', posted))

        self.assertTrue(
            self._element_invisible_for(
                'o_bill_approval_btn_1', self._create_invoice(move_type='out_invoice')),
            "Review must not be offered on a customer invoice",
        )

    def test_the_approve_button_shows_only_after_review_and_before_approval(self):
        reviewed = self._bill()
        reviewed.with_user(self.user_reviewer).action_bill_approval_1()
        self.assertFalse(
            self._element_invisible_for('o_bill_approval_btn_2', reviewed))

        self.assertTrue(
            self._element_invisible_for('o_bill_approval_btn_2', self._bill()),
            "Approve must not be offered before Review. The two levels are "
            "sequential, which is what makes 'returns to the bookkeeper' mean "
            "something",
        )
        self.assertTrue(
            self._element_invisible_for(
                'o_bill_approval_btn_2', self._approve_fully(self._bill())))

    def test_the_refuse_button_shows_only_when_there_is_something_to_clear(self):
        self.assertTrue(
            self._element_invisible_for('o_bill_approval_btn_refuse', self._bill()),
            "nothing has been approved, so there is nothing to refuse",
        )

        reviewed = self._bill()
        reviewed.with_user(self.user_reviewer).action_bill_approval_1()
        self.assertFalse(
            self._element_invisible_for('o_bill_approval_btn_refuse', reviewed))
        self.assertFalse(
            self._element_invisible_for(
                'o_bill_approval_btn_refuse', self._approve_fully(self._bill())))

        posted = self._approve_fully(self._bill())
        posted.action_post()
        self.assertTrue(
            self._element_invisible_for('o_bill_approval_btn_refuse', posted))

    def test_a_user_with_no_level_gets_no_buttons_at_all(self):
        """The approval buttons are stripped from the arch for a user with no level."""
        privileged = self._arch()
        stripped = self._arch(user=self.user_none)
        for css_class in ('o_bill_approval_btn_1', 'o_bill_approval_btn_2',
                          'o_bill_approval_btn_refuse'):
            self.assertIn(css_class, privileged)
            self.assertNotIn(
                css_class, stripped,
                "%s must not reach a user holding no approval level" % css_class,
            )
        # The alerts deliberately have no `groups=`.
        self.assertIn('o_bill_approval_alert_1', stripped)

    def test_each_button_names_its_own_level_in_the_source_arch(self):
        """Each button names its group in the view record's source arch.

        Read from `ir.ui.view.arch` because `get_view` removes `groups=` attributes.
        """
        tree = etree.fromstring(
            self.env.ref('goodahead_bill_approval.view_move_form_bill_approval').arch)
        for css_class, expected in (
            ('o_bill_approval_btn_1',
             'goodahead_bill_approval.group_bill_approval_1'),
            ('o_bill_approval_btn_2',
             'goodahead_bill_approval.group_bill_approval_2'),
        ):
            node = tree.xpath(
                "//button[contains(concat(' ', normalize-space(@class), ' '), ' %s ')]"
                % css_class)
            self.assertTrue(node, "no button carrying %r" % css_class)
            self.assertEqual(node[0].get('groups'), expected)

    def test_who_and_when_appear_only_once_they_have_an_answer(self):
        waiting = self._bill()
        self.assertTrue(
            self._element_invisible_for('o_bill_approval_given_1', waiting),
            "an unapproved bill gains no clutter",
        )
        self.assertTrue(
            self._element_invisible_for('o_bill_approval_given_2', waiting))

        reviewed = self._bill()
        reviewed.with_user(self.user_reviewer).action_bill_approval_1()
        self.assertFalse(
            self._element_invisible_for('o_bill_approval_given_1', reviewed))
        self.assertTrue(
            self._element_invisible_for('o_bill_approval_given_2', reviewed))

        approved = self._approve_fully(self._bill())
        self.assertFalse(
            self._element_invisible_for('o_bill_approval_given_1', approved))
        self.assertFalse(
            self._element_invisible_for('o_bill_approval_given_2', approved))

    def test_the_four_approval_fields_are_on_the_form(self):
        arch = self._arch()
        for name in ('approval_1_uid', 'approval_1_date',
                     'approval_2_uid', 'approval_2_date'):
            self.assertIn(
                name, arch,
                "%s must be on the bill form: self-approval is allowed, and the "
                "only thing that makes it visible is both fields being on "
                "screen" % name,
            )

    def test_the_state_is_a_column_on_the_bill_list_and_not_on_the_invoice_list(self):
        arch = etree.fromstring(self._arch('account.view_in_invoice_bill_tree', 'list'))
        nodes = arch.xpath("//field[@name='bill_approval_state']")
        self.assertEqual(len(nodes), 1, "the state must be one column on the bill list")
        self.assertEqual(
            nodes[0].get('column_invisible'),
            "context.get('default_move_type') not in ('in_invoice', 'in_refund', 'out_refund')",
            "the column must name the move type. `view_invoice_tree` is shared "
            "with the customer-invoice lists, so a column with no "
            "`column_invisible` appears there too — and it would read "
            "'Waiting for Review' on every draft invoice in the database",
        )
