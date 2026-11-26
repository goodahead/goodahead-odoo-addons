# Copyright 2026 Goodahead Oy
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).

"""The buyer reference of an invoice or bill (finvoice content ADR-0006).

A plain text the buyer asks to see on its invoices (BT-10), sent in the
e-invoice and read from incoming ones. The sending add-on fills it from the
customer's standing value; this add-on only holds it. Nothing fills it on
install or upgrade.
"""

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

#: Finvoice BuyerReferenceIdentifier length.
BUYER_REFERENCE_MAX = 70


def check_buyer_reference(value):
    """Refuse a buyer reference the e-invoice cannot hold. No ORM
    ``size``: the ORM would cut a longer value silently."""
    if value and len(value) > BUYER_REFERENCE_MAX:
        raise ValidationError(_(
            "The buyer reference is too long: at most %(max)s characters fit in the e-invoice.",
            max=BUYER_REFERENCE_MAX,
        ))


class AccountMove(models.Model):
    _inherit = "account.move"

    einvoice_buyer_reference = fields.Char(
        string="Buyer Reference",
        copy=True,
        tracking=True,
        help="The reference the buyer asks to see on its invoices, such as an orderer's "
        "name or a cost centre; sent in the e-invoice (at most 70 characters).",
    )

    @api.constrains("einvoice_buyer_reference")
    def _check_einvoice_buyer_reference(self):
        for move in self:
            check_buyer_reference(move.einvoice_buyer_reference)
