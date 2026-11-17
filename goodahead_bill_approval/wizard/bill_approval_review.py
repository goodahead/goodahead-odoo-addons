# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Send for Review dialog: optionally choose the step 2 approver, then give step 1.

The approver is saved through the ordinary write (so the usual guard and constraint apply)
and step 1 is given by `action_bill_approval_1`, in one transaction. Several documents get
one choice; the default is the current approver only when all of them share it.
"""

from odoo import api, fields, models
from odoo.exceptions import UserError

from ..models.account_move import APPROVER_FIELD, GROUP_1, GROUP_2


class BillApprovalReview(models.TransientModel):
    _name = 'bill.approval.review'
    _description = 'Give approval step 1 and choose who approves'

    move_ids = fields.Many2many(
        'account.move',
        string='Documents',
        required=True,
        readonly=True,
    )
    approver_id = fields.Many2one(
        'res.users',
        string='Approver (step 2)',
        domain=lambda self: [
            ('all_group_ids', 'in', self.env.ref(GROUP_2).ids),
            ('share', '=', False),
        ],
        help="Who gives approval step 2. Leave empty to let anyone holding \"Bills: may "
             "approve (step 2)\" approve.",
    )

    @api.model
    def default_get(self, fields):
        values = super().default_get(fields)
        if self.env.context.get('active_model') != 'account.move':
            return values
        moves = self.env['account.move'].browse(self.env.context.get('active_ids') or [])
        if 'move_ids' in fields:
            values['move_ids'] = [(6, 0, moves.ids)]
        approvers = moves.mapped(APPROVER_FIELD)
        if 'approver_id' in fields and len(approvers) == 1 \
                and all(move[APPROVER_FIELD] for move in moves):
            values['approver_id'] = approvers.id
        return values

    def action_confirm(self):
        self.ensure_one()
        moves = self.move_ids
        if not moves:
            raise UserError(self.env._("Select at least one document to review."))
        # Refuse a mixed selection rather than silently skipping documents already reviewed.
        reviewed = moves.filtered('approval_1_uid')
        if reviewed:
            raise UserError(self.env._(
                "These documents have already been reviewed, so Send for Review cannot be used "
                "again:\n\n%(moves)s\n\n"
                "Remove them from the selection. To change who approves one, use the "
                "Approver (step 2) field on the document.",
                moves="\n".join("  - %s" % move.display_name for move in reviewed),
            ))
        # Repeat the button's checks: this method is reachable without it.
        moves._bill_approval_check_level(GROUP_1, self.env._("step 1, Review"))
        moves._bill_approval_assert_open()
        changed = moves.filtered(lambda move: move[APPROVER_FIELD] != self.approver_id)
        if changed:
            changed.write({APPROVER_FIELD: self.approver_id.id})
        moves.action_bill_approval_1()
        return {'type': 'ir.actions.act_window_close'}
