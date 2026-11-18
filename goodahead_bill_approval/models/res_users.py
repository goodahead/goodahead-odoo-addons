# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""Approval levels, and the step 2 substitute.

Each level is a `res.groups` record alone in its own category, so the user form shows it as
core's generated access-rights field under Accounting (see security/res_groups.xml). The
Booleans below mirror the groups for code and tests and drive the substitute's visibility;
they are not on the user form.
"""

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from .account_move import GROUP_1, GROUP_2

SUBSTITUTE_FIELD = 'bill_approval_substitute_id'


class ResUsers(models.Model):
    _inherit = 'res.users'

    # Who may give step 2 on the documents this user was chosen to approve (one hop only).
    # Set by the user on My Preferences, hence the SELF_*_FIELDS below, or by an administrator.
    bill_approval_substitute_id = fields.Many2one(
        'res.users',
        string='Approval substitute (step 2)',
        domain=lambda self: [
            ('groups_id', 'in', self.env.ref(GROUP_2).ids),
            ('share', '=', False),
        ],
        help="Who may give approval step 2 for you on the documents you were chosen to "
             "approve. The document then shows that it was approved on your behalf. Only "
             "your own substitute counts: their substitute does not.",
    )

    @property
    def SELF_READABLE_FIELDS(self):
        # My Preferences shows the substitute only to approvers, based on this field.
        return super().SELF_READABLE_FIELDS + [SUBSTITUTE_FIELD, 'bill_approval_may_approve']

    @property
    def SELF_WRITEABLE_FIELDS(self):
        return super().SELF_WRITEABLE_FIELDS + [SUBSTITUTE_FIELD]

    @api.constrains(SUBSTITUTE_FIELD)
    def _check_bill_approval_substitute(self):
        """The substitute must be another active internal user holding step 2.

        `sudo()` on the read: a user cannot necessarily read another user's groups.
        """
        for user in self:
            substitute = user.bill_approval_substitute_id.sudo()
            if not substitute:
                continue
            if substitute == user:
                raise ValidationError(self.env._(
                    "%(user)s cannot be their own approval substitute.",
                    user=user.display_name,
                ))
            if substitute.active and substitute._is_internal() and substitute._has_group(GROUP_2):
                continue
            raise ValidationError(self.env._(
                "%(substitute)s cannot be the approval substitute of %(user)s: a substitute "
                "must be an active internal user holding \"Bills: may approve (step 2)\".",
                substitute=substitute.display_name,
                user=user.display_name,
            ))

    def _bill_approval_holds_a_level(self):
        """True when this user holds either approval level."""
        self.ensure_one()
        return self._has_group(GROUP_1) or self._has_group(GROUP_2)

    bill_approval_may_review = fields.Boolean(
        string='Bills: may review (step 1)',
        compute='_compute_bill_approval_levels',
        inverse='_inverse_bill_approval_may_review',
        help="Let this user give approval step 1 on a vendor bill or customer credit note: the partner, the dates, "
             "the lines and the accounts are right. Held by bookkeepers. Grants nothing "
             "else — it does not let this user post the bill.",
    )
    bill_approval_may_approve = fields.Boolean(
        string='Bills: may approve (step 2)',
        compute='_compute_bill_approval_levels',
        inverse='_inverse_bill_approval_may_approve',
        help="Let this user give approval step 2 on a vendor bill or customer credit note: we agree to it. "
             "Held by the financial director. A user who holds both levels may clear both "
             "steps on the same bill — that is a deliberate decision, not an oversight.",
    )

    @api.depends('groups_id')
    def _compute_bill_approval_levels(self):
        for user in self:
            user.bill_approval_may_review = user._has_group(GROUP_1)
            user.bill_approval_may_approve = user._has_group(GROUP_2)

    def _inverse_bill_approval_may_review(self):
        self._bill_approval_write_group(GROUP_1, 'bill_approval_may_review')

    def _inverse_bill_approval_may_approve(self):
        self._bill_approval_write_group(GROUP_2, 'bill_approval_may_approve')

    def _bill_approval_write_group(self, group_xmlid, field_name):
        group = self.env.ref(group_xmlid)
        # `sudo()` only for writing back to `res.groups.users`: reaching this inverse already
        # required write access on `res.users`, which is the real gate (core does the same).
        # Written one way at a time, so a mixed selection cannot clear the other half.
        add = self.filtered(field_name)
        remove = self - add
        if add:
            group.sudo().write({'users': [(4, user.id) for user in add]})
        if remove:
            group.sudo().write({'users': [(3, user.id) for user in remove]})
