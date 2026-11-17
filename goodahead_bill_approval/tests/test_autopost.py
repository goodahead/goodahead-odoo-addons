# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Auto-posting: core's `_autopost_bill()` calls `action_post()` directly, so the gate
must hold there too. Each refusal is paired with an approved bill that does auto-post.
"""

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import tagged

from .common import BillApprovalCommon


@tagged('post_install', '-at_install')
class TestAutopost(BillApprovalCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company.autopost_bills = True
        cls.vendor.autopost_bills = 'always'
        # Control vendor that does not ask for auto-posting.
        cls.vendor_manual = cls.env['res.partner'].create({
            'name': 'ZZ BA Vendor (no auto-post)',
            'is_company': True,
        })
        cls.vendor_manual.autopost_bills = 'never'

    def test_an_unapproved_bill_is_not_auto_posted(self):
        bill = self._bill()
        bill._autopost_bill()
        self.assertEqual(bill.state, 'draft')
        self.assertEqual(bill.bill_approval_state, 'to_review')

    def test_a_reviewed_but_unapproved_bill_is_not_auto_posted(self):
        bill = self._bill()
        bill.with_user(self.user_reviewer).action_bill_approval_1()
        bill._autopost_bill()
        self.assertEqual(bill.state, 'draft')
        self.assertEqual(bill.bill_approval_state, 'reviewed')

    def test_the_control_an_approved_bill_does_auto_post(self):
        """Control: an approved bill still auto-posts."""
        bill = self._bill()
        self._approve_fully(bill)
        bill._autopost_bill()
        self.assertEqual(
            bill.state, 'posted',
            "core's auto-posting must still work once the bill is approved; if "
            "this fails, auto-posting was broken rather than gated",
        )

    def test_the_second_control_a_receipt_still_auto_posts(self):
        """Receipts are out of scope and still auto-post."""
        receipt = self._create_invoice(move_type='in_receipt')
        self.assertFalse(receipt._bill_approval_required())
        receipt._autopost_bill()
        self.assertEqual(receipt.state, 'posted')

    # ------------------------------------- the second approval re-offers it

    def test_the_second_approval_auto_posts_when_the_vendor_asks_for_it(self):
        """The second approval re-offers the bill to auto-posting, since core's own
        auto-post moment has already passed."""
        bill = self._bill()
        bill.with_user(self.user_reviewer).action_bill_approval_1()
        self.assertEqual(bill.state, 'draft', "held at step 1, as it should be")

        bill.with_user(self.user_director).action_bill_approval_2()
        self.assertEqual(
            bill.state, 'posted',
            "the vendor is set to auto-post and the bill is now approved, so the "
            "second approval must re-offer it to core's own auto-posting",
        )

    def test_the_control_a_vendor_not_set_to_auto_post_stays_in_draft(self):
        """Control: a vendor not set to auto-post keeps the approved bill in draft."""
        bill = self._bill(partner=self.vendor_manual)
        bill.with_user(self.user_reviewer).action_bill_approval_1()
        bill.with_user(self.user_director).action_bill_approval_2()
        self.assertEqual(bill.bill_approval_state, 'approved')
        self.assertEqual(
            bill.state, 'draft',
            "this vendor did not ask for auto-posting, so someone still presses "
            "Confirm — core's own conditions decide, not this module",
        )
        bill.action_post()
        self.assertEqual(bill.state, 'posted')

    def test_the_first_approval_alone_never_auto_posts(self):
        """Step 1 alone never triggers auto-posting."""
        bill = self._bill()
        bill.with_user(self.user_reviewer).action_bill_approval_1()
        self.assertEqual(bill.bill_approval_state, 'reviewed')
        self.assertEqual(bill.state, 'draft')

    def _approve_with_failing_post(self, bill, failure):
        """Give step 2 while `_post` (gate included) is patched to `failure`."""
        with patch.object(type(self.AM), '_post', failure):
            bill.with_user(self.user_director).action_bill_approval_2()
        bill.invalidate_recordset()

    def _assert_approval_survived(self, bill):
        self.assertEqual(
            bill.approval_2_uid, self.user_director,
            "the approval must survive a failed auto-post",
        )
        self.assertEqual(bill.bill_approval_state, 'approved')
        self.assertEqual(bill.state, 'draft')
        said = bill.message_ids.filtered(
            lambda m: 'posting it did not succeed' in (m.body or ''))
        self.assertTrue(
            said, "a bill that was meant to auto-post and did not must say so")

    def test_a_usererror_failure_to_auto_post_keeps_the_approval(self):
        """A `UserError` during auto-posting does not roll back the approval."""
        bill = self._bill()
        bill.with_user(self.user_reviewer).action_bill_approval_1()

        def refuse(records, soft=True):
            raise UserError("BA simulated posting refusal")

        self._approve_with_failing_post(bill, refuse)
        self._assert_approval_survived(bill)

    def test_a_non_usererror_failure_also_keeps_the_approval(self):
        """A database error during auto-posting does not roll back the approval.

        A real integrity error aborts the cursor, so this also proves the savepoint
        restores a usable transaction for the following `message_post`.
        """
        bill = self._bill()
        bill.with_user(self.user_reviewer).action_bill_approval_1()

        self.env.cr.execute(
            "SELECT module, name, model FROM ir_model_data LIMIT 1")
        module, name, model = self.env.cr.fetchone()

        def integrity_error(records, soft=True):
            records.env.cr.execute(
                "INSERT INTO ir_model_data (module, name, model) "
                "VALUES (%s, %s, %s)", (module, name, model))

        self._approve_with_failing_post(bill, integrity_error)
        self._assert_approval_survived(bill)

    def test_the_company_flag_is_left_alone(self):
        """The module does not touch the company's `autopost_bills` flag or its default,
        which also governs documents outside its scope."""
        defaults = self.env['res.company'].default_get(['autopost_bills'])
        self.assertTrue(
            defaults.get('autopost_bills'),
            "core's default must survive this module being installed",
        )
        field = self.env['res.company']._fields['autopost_bills']
        self.assertNotIn(
            'goodahead_bill_approval', getattr(field, '_modules', ()) or (),
            "this module must not declare or redeclare the company flag",
        )
