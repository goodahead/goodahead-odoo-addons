# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Post a bill in a test the way the real flow does: approved first, where approval exists.

🔴 An approval add-on that hooks into posting, where one is installed, may refuse `_post`
on a vendor bill that has not been through both approval steps, with no `env.su` bypass
on purpose. The tests that post here are about something else — the bill COUNT, the
candidate cards — so the approvals are a precondition, not the thing under test. They are
granted with a superuser write of the four approval fields. Without this, the posting
tests error on every database with such an add-on installed.

⚠️ This module must not depend on the approval module
(`test_bill_approval_hook.test_the_import_module_does_not_depend_on_the_approval_module`),
so the fields are checked, not assumed. Without it this is a plain `action_post()`.

Not named `test_*.py`, so the loader never collects it as a test file.
"""

from odoo import fields


def post_bill(bill):
    if "approval_1_uid" in bill._fields:
        now = fields.Datetime.now()
        bill.write({
            "approval_1_uid": bill.env.user.id,
            "approval_1_date": now,
            "approval_2_uid": bill.env.user.id,
            "approval_2_date": now,
        })
    bill.action_post()
    return bill
