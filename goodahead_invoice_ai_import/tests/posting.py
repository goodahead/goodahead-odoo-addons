# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Post a bill in a test the way the real flow does: approved first, where approval exists.

`goodahead_bill_approval`, when installed, refuses `_post` on a vendor bill that has not
been through both approval steps, so the approvals are granted here as a precondition.
This module must not depend on the approval module, so the fields are checked, not assumed.
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
