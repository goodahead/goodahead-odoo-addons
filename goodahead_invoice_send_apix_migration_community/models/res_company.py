# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    # The adapter's only write: set by the migration tool's
    # carry-over (sending_stop), cleared by its switch back (sending_resume).
    goodahead_community_sending_stopped = fields.Boolean(
        string="Community APIX sending stopped",
        readonly=True,
        copy=False,
        help="Set by the APIX migration tool: the community APIX modules no longer send "
        "invoices or create e-invoice documents for this company.",
    )
