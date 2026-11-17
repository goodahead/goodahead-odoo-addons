# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Refusal wizard: clears both approvals and records a required reason.

There is no "refused" state: the document returns to the review queue. The reason is
required both on the field and in `action_refuse`, for calls that bypass the form.
"""

from odoo import api, fields, models
from odoo.exceptions import UserError


class BillApprovalRefusal(models.TransientModel):
    _name = 'bill.approval.refusal'
    _description = 'Refuse a document and return it for review'

    move_id = fields.Many2one(
        'account.move',
        string='Document',
        required=True,
        readonly=True,
        ondelete='cascade',
    )
    reason = fields.Text(
        string='Reason',
        required=True,
        help="What is wrong with this document. It goes in its chatter under your name, "
             "and it is the only thing the person who has to fix it will have to work from.",
    )

    @api.model
    def default_get(self, fields):
        # Parameter named `fields` as in core 19.0, so keyword callers keep working.
        values = super().default_get(fields)
        # Opened with `default_move_id` from the button, or `active_id` from a server action.
        if not values.get('move_id') and self.env.context.get('active_model') == 'account.move':
            values['move_id'] = self.env.context.get('active_id')
        return values

    def action_refuse(self):
        self.ensure_one()
        reason = (self.reason or '').strip()
        if not reason:
            raise UserError(self.env._(
                "A refusal needs a reason. The document goes back to the review queue and "
                "the reason is the only thing the person who has to fix it will have to "
                "work from, so \"no\" on its own is not enough."
            ))
        move = self.move_id
        # Repeat the button's checks: this method is reachable without it.
        move._bill_approval_assert_open()
        move._bill_approval_check_refuser()
        move._bill_approval_clear(reason, self.env.user)
        return {'type': 'ir.actions.act_window_close'}
