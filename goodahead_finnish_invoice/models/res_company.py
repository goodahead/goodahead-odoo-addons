# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    # Free text rather than numbers: a Finnish invoice prints these verbatim
    # ("7,50 %", "8 pv") and the wording varies between companies.
    fi_invoice_penalty_interest = fields.Char(
        "Late Payment Interest",
        help="Printed on the customer invoice as Viivästyskorko, e.g. 7,50 %. "
        "Left off the invoice when empty.",
    )
    fi_invoice_complaint_period = fields.Char(
        "Complaint Period",
        help="Printed on the customer invoice as Huomautusaika, e.g. 8 pv. "
        "Left off the invoice when empty.",
    )
