# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo import fields, models

from .uom_uom import check_units_admin


class ResCompany(models.Model):
    _inherit = "res.company"

    einvoice_default_uom_id = fields.Many2one(
        "uom.uom",
        string="Default unit for unmapped incoming codes",
        ondelete="set null",
        help="Unit given to an incoming e-invoice line whose unit code no unit carries; "
        "the bill notes it. Empty: such lines get no unit. Only accounting "
        "administrators change it.",
    )

    def write(self, vals):
        if "einvoice_default_uom_id" not in vals:
            return super().write(vals)
        check_units_admin(self.env)
        # Companies are writable by settings administrators only; an
        # accounting administrator sets this one field through sudo.
        own = {"einvoice_default_uom_id": vals["einvoice_default_uom_id"]}
        rest = {key: value for key, value in vals.items() if key != "einvoice_default_uom_id"}
        result = super(ResCompany, self.sudo()).write(own)
        if rest:
            result = super().write(rest)
        return result
